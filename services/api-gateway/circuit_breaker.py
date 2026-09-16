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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("circuit-breaker")

DEFAULT_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "12"))
DEFAULT_RECOVERY_TIMEOUT = float(os.getenv("CB_RECOVERY_TIMEOUT", "30"))  # faster half-open after cold start
DEFAULT_HALF_OPEN_SUCCESS = int(os.getenv("CB_HALF_OPEN_SUCCESS", "2"))

_redis = None
_redis_init = False

# allow()/record_success()/record_failure() are called synchronously from
# async route handlers (_cb_get/_cb_post in main.py) on the event loop
# thread. When CB_REDIS_SYNC=1, _load_remote/_persist used to make blocking
# upstash_redis HTTP calls (including the one-time client init + ping)
# inline on that thread — the same "blocking call stalls the event loop"
# bug class already fixed in ws_client.py / angelone_session.py. All actual
# Redis I/O now runs on this small background pool instead; the lock only
# ever guards fast, in-memory local-state updates.
_redis_io_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cb-redis-io")


def _get_redis():
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True
    # Local-only by default (USE_REDIS / CB_REDIS_SYNC must both be on)
    if os.getenv("USE_REDIS", "0").lower() not in ("1", "true", "yes"):
        return None
    if os.getenv("CB_REDIS_SYNC", "0").lower() not in ("1", "true", "yes"):
        return None
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
        self._schedule_remote_load()

    def _rk(self, suffix: str) -> str:
        return f"cb:{self.name}:{suffix}"

    def _schedule_remote_load(self) -> None:
        """Throttle-check only (cheap, in-memory) — the actual Redis GETs
        (and the one-time client init/ping inside _get_redis()) happen on
        _redis_io_pool so this never blocks the caller."""
        if os.getenv("CB_REDIS_SYNC", "0").lower() not in ("1", "true", "yes"):
            return
        now = time.time()
        min_iv = float(os.getenv("CB_REDIS_MIN_INTERVAL", "15"))
        if (now - getattr(self, "_last_load_at", 0.0)) < min_iv:
            return
        self._last_load_at = now
        try:
            _redis_io_pool.submit(self._load_remote_blocking)
        except RuntimeError:
            pass  # interpreter shutting down; pool already closed

    def _load_remote_blocking(self) -> None:
        """Runs on a background thread — blocking network calls are fine here."""
        r = _get_redis()
        if not r:
            return
        try:
            st = r.get(self._rk("state"))
            if isinstance(st, bytes):
                st = st.decode()
            fails = r.get(self._rk("failures"))
            opened = r.get(self._rk("opened_at"))
            with self._lock:
                if st in ("open", "half_open", "closed"):
                    self._state = st
                if fails is not None:
                    self._failures = int(fails)
                if opened is not None:
                    self._opened_at = float(opened)
        except Exception as e:
            logger.debug("cb load remote %s: %s", self.name, e)

    def _schedule_persist(self) -> None:
        """Throttle/dedupe (cheap, in-memory, called with self._lock already
        held by the caller) then hand the actual Redis SETs to the
        background pool. Only fires at most once per CB_REDIS_MIN_INTERVAL
        seconds and only when state/failure count changed, to keep Upstash
        command burn low on the free tier.
        """
        if os.getenv("CB_REDIS_SYNC", "0").lower() not in ("1", "true", "yes"):
            return
        now = time.time()
        min_iv = float(os.getenv("CB_REDIS_MIN_INTERVAL", "15"))
        last = getattr(self, "_last_persist_at", 0.0)
        sig = (self._state, self._failures)
        if sig == getattr(self, "_last_persist_sig", None) and (now - last) < min_iv:
            return
        self._last_persist_at = now
        self._last_persist_sig = sig
        ttl = int(max(60, self.recovery_timeout * 3))
        state_snap, failures_snap, opened_snap = self._state, self._failures, self._opened_at
        try:
            _redis_io_pool.submit(self._persist_blocking, state_snap, failures_snap, opened_snap, ttl)
        except RuntimeError:
            pass  # interpreter shutting down; pool already closed

    def _persist_blocking(self, state: str, failures: int, opened_at: float, ttl: int) -> None:
        """Runs on a background thread — blocking network calls are fine here."""
        r = _get_redis()
        if not r:
            return
        try:
            r.set(self._rk("state"), state, ex=ttl)
            r.set(self._rk("failures"), str(failures), ex=ttl)
            if opened_at:
                r.set(self._rk("opened_at"), str(opened_at), ex=ttl)
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
                "redis_backed": bool(_redis) if _redis_init else False,
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
            self._schedule_persist()

    def allow(self) -> bool:
        with self._lock:
            # remote load is throttled and non-blocking; local state is authoritative on free tier
            self._schedule_remote_load()
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
            self._schedule_persist()

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self._last_error = (error or "")[:200]
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.time()
                self._failures = self.failure_threshold
                self._schedule_persist()
                logger.warning("circuit %s → open (half_open probe failed): %s", self.name, self._last_error)
                return
            self._failures += 1
            # Only transition + (re)stamp opened_at the moment the circuit
            # actually opens. Without the `self._state != "open"` guard, a
            # burst of concurrent calls already in flight when the breaker
            # tripped each land here afterward, and every one of them re-ran
            # this block (since self._failures stays >= threshold forever),
            # pushing self._opened_at forward each time. That kept the
            # recovery_timeout countdown perpetually restarting, so the
            # circuit could stay open far longer than recovery_timeout
            # instead of moving to half_open on schedule.
            if self._state != "open" and self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.time()
                logger.warning(
                    "circuit %s → open after %s failures: %s",
                    self.name, self._failures, self._last_error,
                )
            self._schedule_persist()

    def reset(self) -> None:
        """Force circuit closed — used by /ops/circuit-reset after intentional warm-up."""
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._successes_half = 0
            self._opened_at = 0.0
            self._last_error = None
            self._schedule_persist()
            logger.info("circuit %s → closed (manual reset)", self.name)

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


def reset_all_breakers() -> list:
    """Force-close every registered breaker. Returns list of names reset."""
    names = []
    with _registry_lock:
        for name, br in list(_registry.items()):
            try:
                br.reset()
                names.append(name)
            except Exception as e:
                logger.debug("reset %s: %s", name, e)
    return names

