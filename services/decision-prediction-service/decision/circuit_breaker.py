"""
Circuit breaker with optional Redis-shared state across Render instances.

Local memory is always used as a fast path; when UPSTASH_REDIS_* is set,
failure counts and open state are mirrored to Redis keys:
  cb:{name}:failures  (TTL = recovery_timeout * 2)
  cb:{name}:state     (open|half_open|closed)
  cb:{name}:opened_at (monotonic-ish epoch)

This way gateway and decision services share the same open/closed picture.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("circuit-breaker")

DEFAULT_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "12"))
DEFAULT_RECOVERY_TIMEOUT = float(os.getenv("CB_RECOVERY_TIMEOUT", "45"))
DEFAULT_HALF_OPEN_SUCCESS = int(os.getenv("CB_HALF_OPEN_SUCCESS", "2"))

_redis = None
_redis_init = False


def _get_redis():
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        return None
    try:
        from upstash_redis import Redis
        _redis = Redis(url=url, token=token)
        _redis.ping()
        logger.info("Circuit breaker Redis backend enabled")
    except Exception as e:
        logger.warning("Circuit breaker Redis unavailable: %s", e)
        _redis = None
    return _redis


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"circuit open for {name}; retry after {retry_after:.0f}s")


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_success: int = DEFAULT_HALF_OPEN_SUCCESS,
    ):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_success = max(1, int(half_open_success))
        self._lock = threading.Lock()
        self._failures = 0
        self._successes_half = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._last_error: Optional[str] = None
        self._load_remote()

    def _rk(self, suffix: str) -> str:
        return f"cb:{self.name}:{suffix}"

    def _load_remote(self) -> None:
        if os.getenv("CB_REDIS_SYNC", "0").lower() not in ("1", "true", "yes"):
            return
        r = _get_redis()
        if not r:
            return
        now = time.time()
        min_iv = float(os.getenv("CB_REDIS_MIN_INTERVAL", "15"))
        if (now - getattr(self, "_last_load_at", 0.0)) < min_iv:
            return
        try:
            st = r.get(self._rk("state"))
            if isinstance(st, bytes):
                st = st.decode()
            if st in ("open", "half_open", "closed"):
                self._state = st
            fails = r.get(self._rk("failures"))
            if fails is not None:
                self._failures = int(fails)
            opened = r.get(self._rk("opened_at"))
            if opened is not None:
                self._opened_at = float(opened)
            self._last_load_at = now
        except Exception as e:
            logger.debug("cb load remote %s: %s", self.name, e)

    def _persist(self) -> None:
        """Write breaker state to Redis at most once per CB_REDIS_MIN_INTERVAL seconds
        and only when state/failure count changes. Cuts Upstash command burn on free tier.
        """
        # Local-only mode (default recommended on free Upstash)
        if os.getenv("CB_REDIS_SYNC", "0").lower() not in ("1", "true", "yes"):
            return
        r = _get_redis()
        if not r:
            return
        now = time.time()
        min_iv = float(os.getenv("CB_REDIS_MIN_INTERVAL", "15"))
        last = getattr(self, "_last_persist_at", 0.0)
        sig = (self._state, self._failures)
        if sig == getattr(self, "_last_persist_sig", None) and (now - last) < min_iv:
            return
        try:
            ttl = int(max(60, self.recovery_timeout * 3))
            r.set(self._rk("state"), self._state, ex=ttl)
            r.set(self._rk("failures"), str(self._failures), ex=ttl)
            if self._opened_at:
                r.set(self._rk("opened_at"), str(self._opened_at), ex=ttl)
            self._last_persist_at = now
            self._last_persist_sig = sig
        except Exception as e:
            logger.debug("cb persist %s: %s", self.name, e)

    def state(self) -> str:
        with self._lock:
            self._maybe_half_open_unlocked()
            return self._state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._maybe_half_open_unlocked()
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "opened_at": self._opened_at or None,
                "last_error": self._last_error,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "redis_backed": bool(_get_redis()),
            }

    def _maybe_half_open_unlocked(self) -> None:
        if self._state != "open":
            return
        # Prefer wall clock when redis-shared
        opened = self._opened_at
        now = time.time() if opened > 1e9 else time.monotonic()
        if opened and (now - opened) >= self.recovery_timeout:
            self._state = "half_open"
            self._successes_half = 0
            self._persist()

    def allow(self) -> bool:
        with self._lock:
            # remote load is throttled; local state is authoritative on free tier
            self._load_remote()
            self._maybe_half_open_unlocked()
            if self._state == "closed":
                return True
            if self._state == "half_open":
                return True
            return False

    def retry_after(self) -> float:
        with self._lock:
            if self._state != "open" or not self._opened_at:
                return 0.0
            now = time.time() if self._opened_at > 1e9 else time.monotonic()
            return max(0.0, self.recovery_timeout - (now - self._opened_at))

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._successes_half += 1
                if self._successes_half >= self.half_open_success:
                    self._state = "closed"
                    self._failures = 0
                    self._opened_at = 0.0
                    self._last_error = None
                    logger.info("circuit %s → closed", self.name)
            else:
                self._failures = 0
            self._persist()

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self._last_error = (error or "")[:200]
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.time()
                self._failures = self.failure_threshold
                self._persist()
                logger.warning("circuit %s → open (half_open probe failed): %s", self.name, self._last_error)
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.time()
                logger.warning(
                    "circuit %s → open after %s failures: %s",
                    self.name, self._failures, self._last_error,
                )
            self._persist()

    def call(self, func: Callable, *args, **kwargs):
        if not self.allow():
            raise CircuitOpenError(self.name, self.retry_after())
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e))
            raise


_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
) -> CircuitBreaker:
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CircuitBreaker(
                name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _registry[name]


def all_snapshots() -> Dict[str, dict]:
    with _registry_lock:
        return {k: v.snapshot() for k, v in _registry.items()}
