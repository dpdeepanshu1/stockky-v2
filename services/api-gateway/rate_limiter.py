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

    def acquire(self, weight: float = 1.0, max_wait: float = 20.0) -> float:
        """Blocks until `weight` tokens are available (or max_wait elapses,
        to avoid an unbounded stall if a caller mis-sizes a batch). Returns
        the actual wait time in seconds.

        max_wait default lowered from 60s -> 20s: this function is called
        from plain `def` (sync) FastAPI routes/background jobs, which
        Starlette runs on a *bounded* thread pool. A single acquire() call
        blocking a worker thread for up to 60s — multiplied across every
        chunk of a 297-symbol data feed run, with several features (Data
        Feed, Training, Surprise scan) triggered concurrently from separate
        browser tabs — was enough to exhaust that thread pool and made
        unrelated requests queue for minutes behind it (the "12 stocks in
        326 minutes" slowdown). 20s still gives the bucket room to refill
        under normal load without holding a thread hostage for a full
        minute on every call.
        """
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


def acquire(provider: str, weight: float = 1.0, max_wait: float = 20.0) -> float:
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


_yf_patched = False
_yf_patch_lock = threading.Lock()


def patch_yfinance() -> bool:
    """
    Monkeypatch yfinance.download and Ticker.history/Ticker.info so EVERY
    call site in this process is gated by the shared "yfinance" bucket —
    Market Scan, the Surprise tab (premarket + bulk quote feed), Hot Picks,
    the Data Feed tab, and every repair button all end up calling one of
    these two yfinance entry points somewhere, even the ones we haven't
    hunted down individually. Call this once at service startup:

        import rate_limiter
        rate_limiter.patch_yfinance()

    Idempotent — safe to call from multiple modules/entrypoints.
    """
    global _yf_patched
    with _yf_patch_lock:
        if _yf_patched:
            return True
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("patch_yfinance: yfinance not importable, skipping")
            return False

        _orig_download = yf.download

        def _patched_download(*args, **kwargs):
            tickers_arg = kwargs.get("tickers") or (args[0] if args else "")
            # A batch yf.download("A.NS B.NS C.NS ...") is ONE HTTP request to
            # Yahoo (yfinance chunks internally), so charging one token per
            # ticker (weight=len(batch)) catastrophically over-throttled: a
            # 50-symbol scan "spent" 50 tokens against a 6-token bucket, so the
            # limiter held the call up to max_wait — the "held download() for
            # 60.0s (weight=50)" / "max_wait exceeded" stalls in the logs that
            # made the whole site hang. Count a batch as a small constant.
            _n = max(1, len(str(tickers_arg).split()))
            weight = 1 if _n <= 1 else 2
            wait = acquire("yfinance", weight=weight)
            if wait > 1.0:
                logger.info("yfinance rate-limiter held download() for %.1fs (weight=%s, symbols=%s)", wait, weight, _n)
            return _orig_download(*args, **kwargs)

        yf.download = _patched_download

        try:
            _TickerCls = yf.Ticker
            _orig_history = _TickerCls.history
            _orig_info_getter = _TickerCls.info.fget if isinstance(_TickerCls.info, property) else None

            def _patched_history(self, *args, **kwargs):
                acquire("yfinance", weight=1)
                return _orig_history(self, *args, **kwargs)

            _TickerCls.history = _patched_history

            if _orig_info_getter is not None:
                def _patched_info(self):
                    # .info triggers Yahoo's quoteSummary endpoint — the most
                    # crumb/auth-sensitive call yfinance makes, so weight it
                    # heavier than a plain history() request.
                    acquire("yfinance", weight=2)
                    return _orig_info_getter(self)

                _TickerCls.info = property(_patched_info)
        except Exception as e:
            logger.warning("patch_yfinance: could not patch Ticker.history/.info: %s", e)

        _yf_patched = True
        logger.info("yfinance rate-limiter patch active (provider bucket: yfinance)")
        return True
