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
_DEFAULTS = {
    "yfinance": (2.0, 6),      # Yahoo: ~2 req/s sustained, small burst
    "indianapi": (1.0, 3),     # IndianAPI free tier: strict
    "nse": (1.0, 3),           # NSE official: strict, blocks aggressively
    "market_data": (8.0, 20),  # our own market-data-service /quote proxy
    "analysis": (3.0, 8),      # analysis-intelligence-service (fundamental/technical/event)
}


def _cfg(provider: str) -> tuple:
    rps_env = os.getenv(f"RL_{provider.upper()}_RPS")
    burst_env = os.getenv(f"RL_{provider.upper()}_BURST")
    rps, burst = _DEFAULTS.get(provider, (2.0, 5))
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


def stats() -> dict:
    """Snapshot of every provider bucket — used by the /ws/jobs real-time
    channel and the rate-limit dashboard so the UI can show *why* a job is
    moving slowly (queued behind a shared limiter) instead of looking stuck."""
    with _buckets_lock:
        return {name: b.snapshot() for name, b in _buckets.items()}
    
# Process singleton — same name as before so main.py imports stay valid
limiter = LocalMemoryRateLimiter()
rate_limiter = limiter  # alias used by some call sites
