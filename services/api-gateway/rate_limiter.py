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

import contextlib
import contextvars
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

# ─────────────────────────────────────────────────────────────────────────────
# Fail-fast + fair-share config.
#
# The old behaviour was "wait up to 20s, then log a warning and proceed anyway".
# In the logs that produced `max_wait exceeded, proceeding anyway` ten-plus times
# in a row, and it is the worst of both worlds: the request pays the full stall
# AND still hits Yahoo with a call that a drained bucket says is unsafe, so
# yfinance's own 429/backoff adds even more delay on top. Two changes fix it:
#
#   * MAX_WAIT is short by default (5s, not 20s), and the effective budget is
#     divided by the bucket's current queue depth. Previously twenty queued
#     callers could each burn the full 20s back-to-back — 400 wall-clock seconds
#     of stalling for a bucket that was never going to refill that fast. Dividing
#     by the waiter count means a deep queue drains or gives up quickly instead
#     of serialising N full waits.
#   * Background pipelines must leave an interactive RESERVE of tokens untouched.
#     One tab running a heavy IPO/Hot Picks/Surprise scan could previously drain
#     the shared bucket to zero and starve another tab's single-symbol lookup for
#     the full max_wait. Scans now stop drawing at the reserve line, so a quick
#     lookup always finds tokens waiting for it.
# ─────────────────────────────────────────────────────────────────────────────
MAX_WAIT_DEFAULT = float(os.getenv("RL_MAX_WAIT_SEC", "5.0"))
MIN_WAIT_FLOOR = float(os.getenv("RL_MIN_WAIT_SEC", "0.5"))
INTERACTIVE_RESERVE_FRACTION = float(os.getenv("RL_INTERACTIVE_RESERVE", "0.34"))

# Pipelines that run as bulk background jobs. Anything not listed here (a plain
# symbol lookup, a chart request, a repair button) counts as interactive and may
# draw the bucket all the way down.
BACKGROUND_PIPELINES = {
    p.strip().lower()
    for p in os.getenv(
        "RL_BACKGROUND_PIPELINES",
        "scan,surprise,ipo,hotpicks,datafeed,training,refill,hydrator,scheduler",
    ).split(",")
    if p.strip()
}

# Current pipeline label. A ContextVar carries it across `await` boundaries and
# is copied into Starlette's threadpool for sync endpoints; the threading.local
# mirror covers raw threads / run_in_executor, where a ContextVar would not
# propagate. Read prefers whichever is set.
_pipeline_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("rl_pipeline", default="")
_pipeline_tls = threading.local()


def current_pipeline() -> str:
    try:
        v = getattr(_pipeline_tls, "name", "") or _pipeline_ctx.get("")
        return (v or "").strip().lower()
    except Exception:
        return ""


@contextlib.contextmanager
def pipeline_scope(name: str):
    """Tag every rate-limited call made inside this block as belonging to `name`.

        with rate_limiter.pipeline_scope("hotpicks"):
            ...run the scan...

    Used so the limiter can tell a bulk scan apart from a user-facing lookup and
    apply the interactive reserve. Restores the previous label on exit, so nested
    scopes behave sensibly."""
    label = (name or "").strip().lower()
    prev_tls = getattr(_pipeline_tls, "name", "")
    token = _pipeline_ctx.set(label)
    _pipeline_tls.name = label
    try:
        yield
    finally:
        _pipeline_tls.name = prev_tls
        with contextlib.suppress(Exception):
            _pipeline_ctx.reset(token)


def _is_background(pipeline: Optional[str]) -> bool:
    name = (pipeline or current_pipeline() or "").strip().lower()
    return bool(name) and name in BACKGROUND_PIPELINES


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
    denied_events: int = 0
    last_warn: float = 0.0

    def __post_init__(self):
        self.tokens = self.capacity

    def _warn_throttled(self, msg: str, *args) -> None:
        """One warning per WARN_INTERVAL per bucket. The old code logged on every
        single give-up, which is how `max_wait exceeded, proceeding anyway`
        appeared 10+ times in a row and buried the actual cause in the logs."""
        now = time.time()
        if now - self.last_warn >= 30.0:
            self.last_warn = now
            logger.warning(msg, *args)
        else:
            logger.debug(msg, *args)

    def acquire(
        self,
        weight: float = 1.0,
        max_wait: Optional[float] = None,
        reserve: float = 0.0,
        fail_fast: bool = False,
    ) -> float:
        """Wait for `weight` tokens. Returns the wait incurred, or -1.0 when
        `fail_fast` is set and the tokens could not be obtained in time.

        `reserve` keeps that many tokens out of reach for this caller, so bulk
        background jobs cannot drain the bucket that user-facing lookups need.

        The wait budget is `max_wait / queue_depth` (floored at MIN_WAIT_FLOOR).
        With one caller that is the full max_wait; with a queue it shrinks, which
        is what stops twenty symbols from each burning the whole budget in series
        while the bucket has no realistic chance of refilling that fast.
        """
        budget = MAX_WAIT_DEFAULT if max_wait is None else float(max_wait)
        start = time.time()
        with self.lock:
            self.waiters += 1
            depth = max(1, self.waiters)
        # Divide the budget by how many callers are already queued here.
        budget = max(MIN_WAIT_FLOOR, budget / depth)
        try:
            while True:
                with self.lock:
                    now = time.time()
                    elapsed = now - self.updated
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)
                    self.updated = now
                    # Background callers must leave `reserve` tokens behind.
                    usable = self.tokens - reserve
                    if usable >= weight:
                        self.tokens -= weight
                        waited = now - start
                        self.last_wait_sec = waited
                        if waited > 0.05:
                            self.throttle_events += 1
                        return waited
                    deficit = weight - usable
                    sleep_for = min(deficit / self.rps if self.rps > 0 else 0.5, 2.0)
                if time.time() - start >= budget:
                    with self.lock:
                        self.denied_events += 1
                    if fail_fast:
                        # Caller asked to be told rather than stalled — it will
                        # skip the upstream call entirely, which is strictly
                        # better than issuing one the bucket says is unsafe.
                        self._warn_throttled(
                            "rate_limiter: budget %.1fs exhausted, skipping (weight=%s, queue=%s)",
                            budget, weight, depth,
                        )
                        return -1.0
                    self._warn_throttled(
                        "rate_limiter: budget %.1fs exhausted, proceeding (weight=%s, queue=%s, reserve=%s)",
                        budget, weight, depth, reserve,
                    )
                    with self.lock:
                        self.tokens = max(0.0, self.tokens - weight)
                    return time.time() - start
                time.sleep(max(0.05, min(sleep_for, budget)))
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
                "denied_events": self.denied_events,
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


def _reserve_for(bucket: _Bucket, pipeline: Optional[str]) -> float:
    """Tokens this caller must leave untouched. Background pipelines keep the
    interactive slice free; interactive callers reserve nothing."""
    if not _is_background(pipeline):
        return 0.0
    return max(0.0, bucket.capacity * INTERACTIVE_RESERVE_FRACTION)


def acquire(
    provider: str,
    weight: float = 1.0,
    max_wait: Optional[float] = None,
    pipeline: Optional[str] = None,
) -> float:
    """Block until it's safe to make `weight` upstream calls to `provider`.
    Call this immediately before the upstream request/batch. Returns the
    wait time incurred (0.0 if no throttling was needed).

    `pipeline` (or an enclosing pipeline_scope()) marks this as a bulk
    background job, which then leaves the interactive reserve alone."""
    try:
        b = _get_bucket(provider)
        return b.acquire(
            weight=weight, max_wait=max_wait, reserve=_reserve_for(b, pipeline)
        )
    except Exception as e:
        logger.debug("rate_limiter.acquire(%s) failed open: %s", provider, e)
        return 0.0


def try_acquire(
    provider: str,
    weight: float = 1.0,
    max_wait: Optional[float] = None,
    pipeline: Optional[str] = None,
) -> bool:
    """Fail-fast variant: True if tokens were obtained, False if the caller
    should SKIP the upstream call rather than stall behind a drained bucket.

    Use this wherever skipping is a legitimate outcome (a symbol we already
    believe is delisted, a speculative prefetch). Proceeding anyway just moves
    the cost to the upstream's own 429 + retry/backoff, which is slower than
    skipping and burns the shared budget for a call that cannot help."""
    try:
        b = _get_bucket(provider)
        return b.acquire(
            weight=weight,
            max_wait=max_wait,
            reserve=_reserve_for(b, pipeline),
            fail_fast=True,
        ) >= 0.0
    except Exception as e:
        logger.debug("rate_limiter.try_acquire(%s) failed open: %s", provider, e)
        return True


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


# ─────────────────────────────────────────────────────────────────────────────
# symbol_aliases bridge — the missing half of the delisted-symbol plumbing.
#
# symbol_aliases.py already has record_resolution_failure() / is_learned_delisted()
# and escalates a symbol to "probably delisted" after MAX_FAILURE_STREAK (5)
# consecutive failures, but nothing was calling them from the yfinance call
# sites, so the counter never moved and every dead ticker paid full price on
# every scan: acquire() wait + yfinance's own internal retry/backoff + a 404.
# Wiring it in HERE rather than at each call site means all of them are covered
# at once (Market Scan, Surprise, IPO, Hot Picks, Data Feed, repair buttons),
# because they all funnel through yf.download / Ticker.history / Ticker.info.
#
# Imported lazily and defensively: this module is copied verbatim into services
# that have no symbol_aliases.py, and there it must simply behave as before.
# ─────────────────────────────────────────────────────────────────────────────
def _aliases():
    try:
        import symbol_aliases

        return symbol_aliases
    except Exception:
        return None


def _base_symbol(value) -> str:
    return (
        str(value or "").upper().replace(".NS", "").replace(".BO", "").strip()
    )


def is_skippable(symbol) -> bool:
    """True when we already know this symbol cannot resolve, so the correct move
    is to skip the network call (and the rate limiter) entirely instead of
    queueing for a request that is guaranteed to 404."""
    sa = _aliases()
    if sa is None:
        return False
    base = _base_symbol(symbol)
    if not base:
        return False
    try:
        if bool(sa.is_learned_delisted(base)):
            return True
    except Exception:
        return False
    # Optional second gate: the static ₹5000+ list. OFF by default on purpose.
    # Those symbols are real and resolvable — they are merely outside the
    # ≤ MAX_UNIVERSE_PRICE trading universe, and that filter already runs on the
    # data-feed WRITE path. Skipping them here as well would also break a
    # deliberate one-off lookup of MARUTI/MRF from the search box, so it is
    # opt-in via RL_SKIP_HIGH_PRICE=1 for deployments that want the extra
    # savings on bulk scans.
    if not SKIP_HIGH_PRICE:
        return False
    try:
        return bool(sa.is_known_high_price(base))
    except Exception:
        return False


# ── Dynamic rename discovery (previously dead code) ─────────────────────────
# symbol_aliases.try_discover_rename() / resolve_with_fallback() existed but were
# called from nowhere, so a genuine NSE rename (ZOMATO -> ETERNAL) could only be
# fixed by hand-editing SYMBOL_RENAMES. Wiring it here means every yfinance call
# site gets self-healing at once, but with three guards so it stays cheap:
#
#   1. It fires at exactly DISCOVERY_AT_STREAK failures — not on the 1st (which
#      is usually a transient upstream blip) and not on every subsequent one.
#   2. Once per symbol per process, tracked in _DISCOVERY_TRIED.
#   3. In a daemon thread, so the scan that triggered it never waits on NSE.
#      learn_rename() persists the result to kv_cache, so the very next call for
#      that symbol resolves through the learned-rename table with no network hop.
RENAME_DISCOVERY = os.getenv("RL_RENAME_DISCOVERY", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
DISCOVERY_AT_STREAK = int(os.getenv("RL_DISCOVERY_AT_STREAK", "2"))
DISCOVERY_TIMEOUT = float(os.getenv("RL_DISCOVERY_TIMEOUT", "8"))
SKIP_HIGH_PRICE = os.getenv("RL_SKIP_HIGH_PRICE", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

_DISCOVERY_TRIED: set = set()
_DISCOVERY_LOCK = threading.Lock()
# One discovery at a time process-wide: a 500-symbol scan hitting a bad upstream
# must not open 200 concurrent NSE connections and get the whole IP blocked.
_DISCOVERY_SLOT = threading.BoundedSemaphore(1)


def _discover_rename_async(base: str) -> None:
    """Run try_discover_rename(base) off the caller's thread, at most once."""
    if not RENAME_DISCOVERY or not base:
        return
    with _DISCOVERY_LOCK:
        if base in _DISCOVERY_TRIED:
            return
        _DISCOVERY_TRIED.add(base)

    def _work() -> None:
        if not _DISCOVERY_SLOT.acquire(blocking=False):
            # Busy — drop the attempt and let the NEXT scan retry it by
            # forgetting we tried, so a skipped slot is not a permanent miss.
            with _DISCOVERY_LOCK:
                _DISCOVERY_TRIED.discard(base)
            return
        try:
            sa = _aliases()
            if sa is None:
                return
            new_symbol = sa.try_discover_rename(base, timeout=DISCOVERY_TIMEOUT)
            if new_symbol:
                # learn_rename() already persisted it inside try_discover_rename;
                # clearing the streak stops the old name being marked delisted
                # now that it resolves through the rename table.
                try:
                    sa.clear_resolution_failures(base)
                except Exception:
                    pass
                logger.warning(
                    "symbol rename discovered: %s -> %s (learned from NSE "
                    "corporate announcements; no code change needed)",
                    base, new_symbol,
                )
        except Exception as e:
            logger.debug("rename discovery for %s failed: %s", base, e)
        finally:
            try:
                _DISCOVERY_SLOT.release()
            except Exception:
                pass

    try:
        threading.Thread(
            target=_work, name=f"rename-discovery-{base}", daemon=True
        ).start()
    except Exception:
        pass


def note_symbol_failure(symbol) -> None:
    """Record ONE fast failure for `symbol`. After MAX_FAILURE_STREAK of these,
    is_learned_delisted() flips to True and is_skippable() short-circuits the
    symbol for good — which is what turns "symbol not found takes ages, every
    single scan" into "costs nothing from the 6th attempt onward".

    At DISCOVERY_AT_STREAK failures it also kicks off a background rename
    lookup, so a symbol that was renamed rather than delisted heals itself
    instead of silently counting down to the delisted list."""
    sa = _aliases()
    if sa is None:
        return
    base = _base_symbol(symbol)
    if not base:
        return
    try:
        n = sa.record_resolution_failure(base)
    except Exception:
        return
    try:
        if n == DISCOVERY_AT_STREAK:
            _discover_rename_async(base)
    except Exception:
        pass


def note_symbol_success(symbol) -> None:
    """Reset the streak so a symbol that was merely having a bad day (upstream
    blip, transient 429) is never mistaken for a delisting."""
    sa = _aliases()
    if sa is None:
        return
    base = _base_symbol(symbol)
    if not base:
        return
    try:
        sa.clear_resolution_failures(base)
    except Exception:
        pass


def _looks_empty(result) -> bool:
    """True when yfinance returned "no data" without raising — an empty
    DataFrame/dict. That is the shape a 404/delisted ticker comes back as, so it
    must count as a failure for streak purposes."""
    if result is None:
        return True
    try:
        empty = getattr(result, "empty", None)
        if empty is not None:
            return bool(empty)
    except Exception:
        return False
    if isinstance(result, dict):
        return len(result) == 0
    return False


def _empty_frame():
    """An empty DataFrame — the same "no data" value yfinance itself returns for
    a dead ticker, so skipping is transparent to every existing caller."""
    try:
        import pandas as pd

        return pd.DataFrame()
    except Exception:
        return None


# ── Hard wall-clock cap for every yfinance call ─────────────────────────────
# yfinance's own `timeout=` kwarg only bounds a single underlying HTTP
# request; internal retries/backoff — and Yahoo occasionally black-holing a
# connection instead of erroring — can still let one .history()/.info/
# download() call run far longer than any caller expects. That is exactly
# what produced the http_code=000 hangs seen in testing on /market/indices,
# /scan/watchlist, /stockky-hot, /decision/decide/{symbol}, etc.: the test
# client's own curl timeout (25s) fired while the server was still stuck
# inside a yfinance call that never returned and never raised. Running the
# real call on a small dedicated pool and enforcing a hard ceiling here means
# every call site — current and future — gets a clean, fast exception
# instead of hanging the whole request indefinitely. Callers that already
# catch Exception around these calls (market/indices, decide's technical
# fallback, etc.) fall back to cached/neutral data exactly as if yfinance
# had raised any other error.
import concurrent.futures as _cf

YFINANCE_HARD_TIMEOUT_SEC = float(os.getenv("YFINANCE_HARD_TIMEOUT_SEC", "18"))
_yf_hardcap_pool = _cf.ThreadPoolExecutor(
    max_workers=int(os.getenv("YFINANCE_POOL_WORKERS", "8")),
    thread_name_prefix="yf-hardcap",
)


def _yf_call_with_hard_timeout(fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` on a helper thread and enforce a hard
    wall-clock ceiling. Raises TimeoutError (caught by callers' existing
    except Exception blocks) instead of letting a stuck call hang forever.
    Note: the underlying thread may keep running in the background after
    we give up on it — unavoidable since yfinance/requests offers no
    cooperative cancellation — but the caller is freed immediately."""
    fut = _yf_hardcap_pool.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=YFINANCE_HARD_TIMEOUT_SEC)
    except _cf.TimeoutError:
        logger.warning(
            "yfinance call exceeded hard timeout of %.0fs — failing fast instead of hanging the request",
            YFINANCE_HARD_TIMEOUT_SEC,
        )
        raise TimeoutError(f"yfinance call exceeded {YFINANCE_HARD_TIMEOUT_SEC:.0f}s hard timeout")


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

    Also enforces the learned-delisted skip list (see is_skippable) so a symbol
    we already know is dead never reaches the rate limiter OR the network.

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
            symbols = str(tickers_arg).split()
            # Skip only when EVERY ticker in the batch is known-dead. Filtering a
            # partially-dead batch would change the returned frame's columns and
            # break callers that index it by symbol, so the conservative rule is
            # all-or-nothing.
            if symbols and all(is_skippable(s) for s in symbols):
                logger.debug(
                    "yfinance skip: all %s ticker(s) flagged delisted, no call made", len(symbols)
                )
                return _empty_frame()
            # A batch yf.download("A.NS B.NS C.NS ...") is ONE HTTP request to
            # Yahoo (yfinance chunks internally), so charging one token per
            # ticker (weight=len(batch)) catastrophically over-throttled: a
            # 50-symbol scan "spent" 50 tokens against a 6-token bucket, so the
            # limiter held the call up to max_wait — the "held download() for
            # 60.0s (weight=50)" / "max_wait exceeded" stalls in the logs that
            # made the whole site hang. Count a batch as a small constant.
            _n = max(1, len(symbols))
            weight = 1 if _n <= 1 else 2
            wait = acquire("yfinance", weight=weight)
            if wait > 1.0:
                logger.info("yfinance rate-limiter held download() for %.1fs (weight=%s, symbols=%s)", wait, weight, _n)
            result = _yf_call_with_hard_timeout(_orig_download, *args, **kwargs)
            # Only attribute success/failure per-symbol for single-ticker calls;
            # in a batch an empty frame does not say WHICH ticker was missing.
            if _n == 1:
                if _looks_empty(result):
                    note_symbol_failure(symbols[0])
                else:
                    note_symbol_success(symbols[0])
            return result

        yf.download = _patched_download

        try:
            _TickerCls = yf.Ticker
            _orig_history = _TickerCls.history
            _orig_info_getter = _TickerCls.info.fget if isinstance(_TickerCls.info, property) else None

            def _patched_history(self, *args, **kwargs):
                sym = getattr(self, "ticker", "")
                if is_skippable(sym):
                    return _empty_frame()
                acquire("yfinance", weight=1)
                try:
                    result = _yf_call_with_hard_timeout(_orig_history, self, *args, **kwargs)
                except Exception:
                    # Count the failure immediately instead of letting the caller
                    # (and yfinance's own retry loop) rediscover it next cycle.
                    note_symbol_failure(sym)
                    raise
                if _looks_empty(result):
                    note_symbol_failure(sym)
                else:
                    note_symbol_success(sym)
                return result

            _TickerCls.history = _patched_history

            if _orig_info_getter is not None:
                def _patched_info(self):
                    # .info triggers Yahoo's quoteSummary endpoint — the most
                    # crumb/auth-sensitive call yfinance makes, so weight it
                    # heavier than a plain history() request.
                    sym = getattr(self, "ticker", "")
                    if is_skippable(sym):
                        return {}
                    acquire("yfinance", weight=2)
                    try:
                        result = _yf_call_with_hard_timeout(_orig_info_getter, self)
                    except Exception:
                        note_symbol_failure(sym)
                        raise
                    if _looks_empty(result):
                        note_symbol_failure(sym)
                    else:
                        note_symbol_success(sym)
                    return result

                _TickerCls.info = property(_patched_info)
        except Exception as e:
            logger.warning("patch_yfinance: could not patch Ticker.history/.info: %s", e)

        _yf_patched = True
        logger.info("yfinance rate-limiter patch active (provider bucket: yfinance)")
        return True
