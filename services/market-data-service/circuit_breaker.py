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
DEFAULT_RECOVERY_TIMEOUT = float(os.getenv("CB_RECOVERY_TIMEOUT", "30"))  # faster half-open after cold start
DEFAULT_HALF_OPEN_SUCCESS = int(os.getenv("CB_HALF_OPEN_SUCCESS", "2"))

_redis = None
_redis_init = False


def _get_redis():
    """
    Redis for CB state is OPTIONAL and OFF by default.

    Upstash free tier is nearly exhausted when every quote miss persists
    circuit state — that alone can burn ~3 commands per failed symbol.
    Set USE_REDIS=1 only if you intentionally want shared CB state.
    """
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True

    use = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
    if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes"):
        use = False
    if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
        use = False
    if not use:
        logger.info("Circuit breaker: memory-only (USE_REDIS=0) — no Upstash commands")
        _redis = None
        return None

    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        return None
    try:
        from upstash_redis import Redis
        _redis = Redis(url=url, token=token)
        _redis.ping()
        logger.info("Circuit breaker Redis backend enabled (USE_REDIS=1)")
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
        r = _get_redis()
        if not r:
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
        except Exception as e:
            logger.debug("cb load remote %s: %s", self.name, e)

    def _persist(self) -> None:
        r = _get_redis()
        if not r:
            return
        try:
            ttl = int(max(60, self.recovery_timeout * 3))
            r.set(self._rk("state"), self._state, ex=ttl)
            r.set(self._rk("failures"), str(self._failures), ex=ttl)
            if self._opened_at:
                r.set(self._rk("opened_at"), str(self._opened_at), ex=ttl)
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
        opened = False
        with self._lock:
            self._last_error = (error or "")[:200]
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.time()
                self._failures = self.failure_threshold
                self._persist()
                logger.warning("circuit %s → open (half_open probe failed): %s", self.name, self._last_error)
                opened = True
            else:
                self._failures += 1
                # Only transition + (re)stamp opened_at the moment the circuit
                # actually opens. Without the `self._state != "open"` guard, a
                # burst of concurrent calls that were already in flight when
                # the breaker tripped each land here afterward, and every one
                # of them re-ran this block (since self._failures stays >=
                # threshold forever), pushing self._opened_at forward each
                # time. That kept the recovery_timeout countdown perpetually
                # restarting, so the circuit could stay open far longer than
                # recovery_timeout instead of moving to half_open on schedule.
                if self._state != "open" and self._failures >= self.failure_threshold:
                    self._state = "open"
                    self._opened_at = time.time()
                    logger.warning(
                        "circuit %s → open after %s failures: %s",
                        self.name, self._failures, self._last_error,
                    )
                    opened = True
                self._persist()
        # Cross-service Neon write so gateway Rate Limit Dashboard is not blind
        if opened or _looks_like_rate_limit(error):
            try:
                record_rate_limit_hit(
                    provider=_provider_from_breaker(self.name),
                    status=429 if _looks_like_rate_limit(error) else 503,
                    detail=error or f"circuit {self.name} open",
                )
            except Exception:
                pass

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

# Must match api-gateway rate_limit_monitor.NEON_STATS_KEY / NEON_EVENTS_KEY
NEON_STATS_KEY = "stockky:rate_limit_stats"
NEON_EVENTS_KEY = "stockky:rate_limit_events_neon"
NEON_TTL = 86400


def _looks_like_rate_limit(error: str = "") -> bool:
    msg = (error or "").lower()
    return any(x in msg for x in ("429", "rate limit", "too many", "quota", "throttl"))


def _provider_from_breaker(name: str) -> str:
    n = (name or "").lower()
    if "yahoo" in n or "yfinance" in n or "yf" in n:
        return "market_data"
    if "nse" in n:
        return "nse"
    if "alpha" in n:
        return "market_data"
    if "indian" in n:
        return "indianapi"
    if "gemini" in n:
        return "gemini"
    if "groq" in n:
        return "groq"
    return "market_data"


def record_rate_limit_hit(
    provider: str,
    status: int = 429,
    path: str = "",
    detail: str = "",
    symbol: str = "",
) -> None:
    """
    Push 429/503 failures directly to the central Neon stockky_kv table so the
    API Gateway Rate Limit Dashboard can read real worker hits (not empty zeros).

    Uses the same keys as services/api-gateway/rate_limit_monitor.py.
    Best-effort: never raises to callers.
    """
    try:
        from kv_cache import kv_get, kv_set
    except Exception as e:
        logger.debug("record_rate_limit_hit: kv_cache unavailable: %s", e)
        return

    src = (provider or "market_data").lower()[:40]
    now = time.time()
    event = {
        "ts": now,
        "source": src,
        "status": int(status),
        "path": (path or "")[:120],
        "detail": (detail or "")[:200],
        "symbol": (symbol or "")[:32],
        "origin": "market-data-service",
    }

    try:
        # Rolling events list
        raw_events = kv_get(NEON_EVENTS_KEY)
        events: list = []
        if isinstance(raw_events, list):
            events = list(raw_events)
        elif isinstance(raw_events, dict) and isinstance(raw_events.get("events"), list):
            events = list(raw_events["events"])
        events.insert(0, event)
        events = events[:500]
        kv_set(NEON_EVENTS_KEY, events, ttl=NEON_TTL)

        # Aggregate stats (last 1h by source)
        cutoff = now - 3600
        counts: Dict[str, int] = {}
        for e in events:
            if not isinstance(e, dict):
                continue
            if float(e.get("ts") or 0) < cutoff:
                continue
            s = str(e.get("source") or "unknown")
            counts[s] = counts.get(s, 0) + 1

        # Merge with any prior stats blob so concurrent writers don't fully clobber
        prior = kv_get(NEON_STATS_KEY)
        if isinstance(prior, dict) and isinstance(prior.get("by_source_1h"), dict):
            for k, v in prior["by_source_1h"].items():
                # Prefer live event-derived counts when present
                if k not in counts:
                    try:
                        counts[k] = int(v)
                    except (TypeError, ValueError):
                        pass

        stats = {
            "updated_at": now,
            "window_sec": 3600,
            "by_source_1h": counts,
            "events_1h": sum(counts.values()),
            "limits": {
                "market_data": {"limit": 500},
                "indianapi": {"limit": 250},
                "gemini": {"limit": 60},
                "groq": {"limit": 60},
                "nse": {"limit": 200},
            },
            "last_hit": event,
            # Flat provider counters for simple dashboard widgets
            "providers": {**counts},
        }
        kv_set(NEON_STATS_KEY, stats, ttl=NEON_TTL)

        # Plan-compatible simple counter key (system:rate_limit_stats)
        # Shape: { "market_data": N, "nse": M, ... } — cumulative session hits
        try:
            simple_key = "system:rate_limit_stats"
            prior_simple = kv_get(simple_key)
            simple = {}
            if isinstance(prior_simple, dict):
                simple = dict(prior_simple)
            elif isinstance(prior_simple, str):
                import json as _json
                try:
                    simple = _json.loads(prior_simple) or {}
                except Exception:
                    simple = {}
            simple[src] = int(simple.get(src, 0) or 0) + 1
            simple["_updated_at"] = now
            kv_set(simple_key, simple, ttl=NEON_TTL)
        except Exception as e:
            logger.debug("system:rate_limit_stats write: %s", e)

        logger.info("rate_limit_hit recorded provider=%s status=%s", src, status)
    except Exception as e:
        logger.warning("Failed to record rate limit stat: %s", e)


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
