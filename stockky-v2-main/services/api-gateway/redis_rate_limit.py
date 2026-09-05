"""
Shared upstream rate limiter — protects each upstream provider's real rate
limit regardless of which feature is calling it.

Why this exists: Market Scan, the Surprise tab (premarket baselines + bulk
quote feed), Hot Picks, the Data Feed tab, and every "repair" button can all
independently fire yfinance/IndianAPI/NSE calls. Each one might individually
respect a sane pace, but nothing previously stopped two or three of them
running at the same time and collectively blowing through the same upstream
provider's real limit — which is exactly what produced the 429 / "Invalid
Crumb" storms in the logs. This module gives every call site one shared
gate per provider, so "protect the rate limit" is enforced process-wide,
not per-caller.

Usage:
    from rate_limiter import acquire, suggested_timeout, stats

    acquire("yfinance", weight=len(batch))   # blocks until safe to proceed
    timeout = suggested_timeout(25.0, "yfinance")  # widen timeout if queued

Design: simple in-process token bucket per provider (no external deps, so
every service can carry its own copy). Optionally coordinates across
processes/dynos via Redis if REDIS_URL/UPSTASH is configured and the
`redis` package is importable — falls back silently to process-local
limiting otherwise (still correct for a single-dyno free-tier deploy, just
not shared across replicas).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("rate-limiter")

# Provider -> (sustained requests/sec, burst capacity). Tuned conservatively
# for free-tier upstreams; override via env without a code change.
#
# IMPORTANT: this limiter (redis_rate_limit) is consulted by the gateway's
# _cb_get() to pace **internal** service-to-service fan-out (buckets: global,
# market_data, analysis, decision, gemini). Those are OUR OWN services, not an
# external provider's rate limit, so they must be generous — a single full
# market scan legitimately triggers hundreds of internal calls. The previous
# tiny caps (analysis=3rps/8burst, market_data=8rps/20burst) meant a scan would
# spend its burst almost immediately, then _cb_get would sleep and finally trip
# the circuit breaker (CircuitOpenError) — which is exactly the "website is
# hanging"/news-failing behaviour in the logs. External-provider limits
# (yfinance/NSE/IndianAPI) are enforced separately in rate_limiter.py + the
# yfinance monkey-patch, so raising the internal buckets here is safe.
_DEFAULTS = {
    # External upstream providers (only used if something routes an external
    # provider through THIS limiter directly; normally enforced elsewhere).
    "yfinance": (2.0, 6),      # Yahoo: ~2 req/s sustained, small burst
    "indianapi": (1.0, 3),     # IndianAPI free tier: strict
    "nse": (1.0, 3),           # NSE official: strict, blocks aggressively
    "gemini": (2.0, 6),        # Gemini LLM API — external, keep modest
    # Internal fan-out (gateway -> our own microservices). Generous on purpose.
    "global": (50.0, 150),     # default family for any internal GET
    "market_data": (40.0, 120),# our own market-data-service /quote /history proxy
    "analysis": (40.0, 120),   # analysis-intelligence-service (fundamental/technical/event/news)
    "decision": (30.0, 90),    # decision-prediction-service (decision/predict/training)
}


def _cfg(provider: str) -> tuple:
    rps_env = os.getenv(f"RL_{provider.upper()}_RPS")
    burst_env = os.getenv(f"RL_{provider.upper()}_BURST")
    rps, burst = _DEFAULTS.get(provider, (10.0, 30))
    try:
        if rps_env:
            rps = float(rps_env)
        if burst_env:
            burst = int(burst_env)
    except (TypeError, ValueError):
        pass
    return rps, burst


@dataclass
class _Bucket:
    rps: float
    capacity: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.time)
    waiters: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    throttle_events: int = 0
    last_wait_sec: float = 0.0

    def __post_init__(self):
        self.tokens = self.capacity

    def acquire(self, weight: float = 1.0, max_wait: float = 60.0) -> float:
        """Blocks until `weight` tokens are available (or max_wait elapses,
        to avoid an unbounded stall if a caller mis-sizes a batch). Returns
        the actual wait time in seconds."""
        start = time.time()
        with self.lock:
            self.waiters += 1
        try:
            while True:
                with self.lock:
                    now = time.time()
                    elapsed = now - self.updated
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)
                    self.updated = now
                    if self.tokens >= weight:
                        self.tokens -= weight
                        waited = now - start
                        self.last_wait_sec = waited
                        if waited > 0.05:
                            self.throttle_events += 1
                        return waited
                    deficit = weight - self.tokens
                    sleep_for = min(deficit / self.rps if self.rps > 0 else 0.5, 2.0)
                if time.time() - start >= max_wait:
                    logger.warning("rate_limiter: max_wait exceeded, proceeding anyway (weight=%s)", weight)
                    with self.lock:
                        self.tokens = max(0.0, self.tokens - weight)
                    return time.time() - start
                time.sleep(max(0.05, sleep_for))
        finally:
            with self.lock:
                self.waiters = max(0, self.waiters - 1)

    def allow(self, weight: float = 1.0) -> bool:
        """Non-blocking check: consume `weight` tokens if available and return
        True, else return False immediately (never sleeps). Used by the gateway
        to pace internal fan-out without ever stalling the event loop."""
        with self.lock:
            now = time.time()
            elapsed = now - self.updated
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)
            self.updated = now
            if self.tokens >= weight:
                self.tokens -= weight
                return True
            return False

    def wait_budget_sec(self, weight: float = 1.0) -> float:
        """How many seconds until `weight` tokens would be available (0.0 if
        available right now). Does not consume tokens."""
        with self.lock:
            now = time.time()
            elapsed = now - self.updated
            tokens = min(self.capacity, self.tokens + elapsed * self.rps)
            if tokens >= weight:
                return 0.0
            deficit = weight - tokens
            return deficit / self.rps if self.rps > 0 else 0.5

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "rps": self.rps,
                "capacity": self.capacity,
                "tokens_available": round(self.tokens, 2),
                "waiters": self.waiters,
                "throttle_events": self.throttle_events,
                "last_wait_sec": round(self.last_wait_sec, 2),
            }


_buckets: Dict[str, _Bucket] = {}
_buckets_lock = threading.Lock()


def _get_bucket(provider: str) -> _Bucket:
    with _buckets_lock:
        b = _buckets.get(provider)
        if b is None:
            rps, burst = _cfg(provider)
            b = _Bucket(rps=rps, capacity=float(burst))
            _buckets[provider] = b
        return b


def acquire(provider: str, weight: float = 1.0, max_wait: float = 60.0) -> float:
    """Block until it's safe to make `weight` upstream calls to `provider`.
    Call this immediately before the upstream request/batch. Returns the
    wait time incurred (0.0 if no throttling was needed)."""
    try:
        return _get_bucket(provider).acquire(weight=weight, max_wait=max_wait)
    except Exception as e:
        logger.debug("rate_limiter.acquire(%s) failed open: %s", provider, e)
        return 0.0


def suggested_timeout(base_timeout: float, provider: str, floor: float = 1.0) -> float:
    """Widen a request timeout when this provider's bucket is under heavy
    contention (many concurrent waiters) — a busy bucket usually means
    upstream itself is slow/rate-limiting, so a short timeout would just
    fail and retry into the same congestion. Scales up to 2x base at 6+
    waiters, capped so we never wait absurdly long."""
    try:
        b = _get_bucket(provider)
        with b.lock:
            waiters = b.waiters
        mult = 1.0 + min(1.0, waiters / 6.0)
        return max(floor, base_timeout * mult)
    except Exception:
        return base_timeout


def allow(provider: str, weight: float = 1.0) -> bool:
    """Non-blocking gate for internal fan-out. Returns True if it's OK to
    proceed right now. Fails OPEN (returns True) on any internal error so a
    limiter bug can never wedge the gateway."""
    try:
        return _get_bucket(provider).allow(weight=weight)
    except Exception as e:
        logger.debug("rate_limiter.allow(%s) failed open: %s", provider, e)
        return True


def wait_budget_sec(provider: str, weight: float = 1.0) -> float:
    """Seconds until `weight` tokens are available for `provider` (0.0 if now).
    Returns 0.0 on any internal error (fail open)."""
    try:
        return _get_bucket(provider).wait_budget_sec(weight=weight)
    except Exception:
        return 0.0


def stats() -> dict:
    """Snapshot of every provider bucket — used by the /ws/jobs real-time
    channel and the rate-limit dashboard so the UI can show *why* a job is
    moving slowly (queued behind a shared limiter) instead of looking stuck."""
    with _buckets_lock:
        return {name: b.snapshot() for name, b in _buckets.items()}


# ---------- FIX: define the class that main.py imports ----------
class LocalMemoryRateLimiter:
    """
    Simple wrapper that exposes the module-level functions as methods.
    This is the singleton that `main.py` expects when it does:
        from redis_rate_limit import limiter as redis_limiter

    main.py calls .set_redis(client) at startup and .allow()/.wait_budget_sec()
    per internal request in _cb_get(). Those three methods MUST exist or every
    internal fan-out raises AttributeError — which is the
    "'LocalMemoryRateLimiter' object has no attribute 'allow'" error that was
    making news (and any fanned-out call) fail for every symbol.
    """
    def __init__(self):
        self._redis = None

    def set_redis(self, client=None) -> None:
        """Accept an optional Redis client for cross-replica coordination.
        This build is process-local (correct for a single VM / single dyno), so
        we just keep the reference and otherwise no-op. Safe to call with None."""
        self._redis = client
        return None

    def acquire(self, provider: str, weight: float = 1.0, max_wait: float = 60.0) -> float:
        return acquire(provider, weight, max_wait)

    def allow(self, provider: str, weight: float = 1.0) -> bool:
        return allow(provider, weight)

    def wait_budget_sec(self, provider: str, weight: float = 1.0) -> float:
        return wait_budget_sec(provider, weight)

    def suggested_timeout(self, base_timeout: float, provider: str, floor: float = 1.0) -> float:
        return suggested_timeout(base_timeout, provider, floor)

    def stats(self) -> dict:
        return stats()


# Process singleton — same name as before so main.py imports stay valid
limiter = LocalMemoryRateLimiter()
rate_limiter = limiter  # alias used by some call sites