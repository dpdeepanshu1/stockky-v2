"""
market_feed/feed.py — price source for entry/exit evaluation.

DESIGN DECISION (Phase 2): DEMO mode reads prices from Stockky's EXISTING
market-data-service — the same free, already-deployed yfinance-backed
service every other tab uses — rather than Dhan's feed. Reasoning:

  * Dhan's market/quote endpoints require a connected account + valid
    access token, same as the trading API. DEMO mode is meant to work
    for anyone trying the system out BEFORE they've linked a real Dhan
    account — gating paper trading behind a real brokerage login would
    defeat the point of a risk-free rehearsal mode.
  * market-data-service already solves symbol resolution, delisted-symbol
    handling, and multi-source fallback (see the whole symbol_aliases.py
    robustness work from this session) — reusing it means DEMO fills are
    priced off real, already-hardened infrastructure instead of a second,
    parallel price pipeline that would need the same hardening again.

REAL mode's execution path (Phase 3) will read prices from Dhan's own feed
via execution/dhan_client.py once wired — the two paths are intentionally
decoupled: this module is DEMO-only. A REAL order's actual fill price
always comes from Dhan itself, never from this module.

This is a POLLING feed (HTTP GET on an interval), not a persistent
WebSocket — simpler, and the existing market-data-service doesn't expose a
streaming endpoint. The plan's "real-time chart via Dhan" is unaffected:
that's a REAL-mode-connected feature for Phase 3, once Dhan is linked.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading as _threading
import time as _time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("real-trade-market-feed")

def _env_url(name: str, default: str, rstrip: bool = True) -> str:
    """URL setting from the environment with a blank-safe fallback.

    os.getenv(name, default) only falls back when the variable is UNSET, so an empty or
    whitespace-only value (a blank Render dashboard variable, `NAME=` in an env_file) overrode a working
    default and every request went to "/path". Blank / whitespace-only (and, with rstrip, slash-only)
    values now use `default`; padded values are trimmed.
    """
    raw = (os.getenv(name) or "").strip()
    if rstrip:
        raw = raw.rstrip("/")
    return raw or (default.rstrip("/") if rstrip else default)


# Mirrors every other Stockky service's MARKET_DATA_URL env convention
MARKET_DATA_URL = _env_url("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com")

# §1 — max age for live_quotes rows before we consider them stale.
# If the AngelOne row is older than this, fall through to the yfinance path.
LIVE_QUOTE_MAX_AGE_S = float(((os.getenv("LIVE_QUOTE_MAX_AGE_S") or "").strip() or "5.0"))

# §2 — Process-scoped ATR cache.
#
# Root cause of the "ATR silent null" bug:
#   The AngelOne/Yahoo WS fast path (Source 1) returns price+volume from a
#   live tick, but a tick carries no ATR — ATR requires 14 candles of OHLC
#   history.  The code previously wrote atr=None unconditionally on every
#   AngelOne hit.  During market hours the AngelOne path is the common path
#   (fresh tick ≤ 5s almost always wins), so entry_engine._atr_stop_target_pct()
#   received None on virtually every real trade and fell back to the fixed
#   MIN_STOP_PCT / MIN_TARGET_PCT constants — ATR-adaptive sizing was
#   completely inactive in production.
#
#   Even Source 2 (market-data-service /quote) never returns ATR: the
#   QuoteResponse schema has no atr field and _yahoo_ohlcv_quote() doesn't
#   compute one, so q.get("atr") was also always None on that path.
#
# Fix:
#   We maintain a lightweight per-process dict: clean_symbol → float ATR.
#   Whenever we fall through to Source 2 (yfinance path), we fire a
#   non-blocking async history fetch, compute a 14-period ATR from the
#   candles, and store it here.  Subsequent calls — whether via AngelOne or
#   yfinance — serve the cached ATR.
#
#   Staleness is acceptable: ATR is a 14-day rolling metric; intraday drift
#   is small (a few paise on a ₹500 stock), and stop/target sizing is not
#   sensitive to that level of precision.  The cache resets on service
#   restart, which is fine — it re-warms within the first evaluation cycle.
#
#   The history fetch (8s timeout) runs as a background task concurrent with
#   the quote return so it adds zero latency to the hot path.

_ATR_CACHE: dict[str, float] = {}
_ATR_LOCK = _threading.Lock()
ATR_WINDOW = 14
_ATR_HISTORY_PERIOD = os.getenv("FEED_ATR_HISTORY_PERIOD", "1mo")  # 14+ daily candles

# ── 2026-09-21 fan-out controls (post-redeploy incident) ─────────────────────
# get_quotes() used to gather() EVERY symbol at once over one default
# httpx.AsyncClient (max 100 connections, and client.get(..., timeout=3.0) also
# sets the pool-wait to 3s). A full-watchlist evaluation therefore queued
# hundreds of requests on the client's own pool — the wall of `PoolTimeout`
# lines in the logs is THIS process failing before it ever reached
# market-data-service — and whatever did get through landed on market-data-
# service as one synchronized burst (`ReadTimeout`/`ConnectTimeout` while it
# was also busy warming up after a redeploy). Cap in-flight lookups per batch
# and bound the batch's wall-clock so a slow upstream degrades to "partial
# results, on time" instead of a stalled cycle.
FEED_QUOTE_CONCURRENCY = max(1, int(((os.getenv("FEED_QUOTE_CONCURRENCY") or "").strip() or "32")))
FEED_BATCH_DEADLINE_S = float(((os.getenv("FEED_BATCH_DEADLINE_S") or "").strip() or "45"))
FEED_HTTP_MAX_CONNECTIONS = max(8, int(((os.getenv("FEED_HTTP_MAX_CONNECTIONS") or "").strip() or "64")))

# ── group152 (log-audit 2026-10-05, items 1 + 2) ────────────────────────────
# At the open the five REAL positions' /live-quote and /quote calls all hit ReadTimeout on every 8s
# exit cycle (market-data answered 200, but later than the 3s / 8s client timeouts), because the same
# process was also running a ~923-symbol one-request-per-symbol batch that hit its 45s deadline
# (763 symbols never attempted). Two changes:
#   * priority lane (get_quotes(..., priority=True)): own connection pool (never queued behind a big
#     batch), timeouts scaled by FEED_PRIORITY_TIMEOUT_SCALE, and a POST /quotes/bulk fallback for any
#     symbol the per-symbol cascade still could not price. Used for exit evaluation (open positions).
#   * bulk-first for large non-priority batches: above FEED_BULK_MIN_SYMBOLS symbols, price them with
#     chunked POST /quotes/bulk first and only fall back to per-symbol /live-quote + /quote for the
#     ones bulk could not price (or priced with data older than FEED_BULK_MAX_AGE_S).
FEED_PRIORITY_TIMEOUT_SCALE = max(1.0, float(((os.getenv("FEED_PRIORITY_TIMEOUT_SCALE") or "").strip() or "2.0")))
FEED_BULK_MIN_SYMBOLS = max(2, int(((os.getenv("FEED_BULK_MIN_SYMBOLS") or "").strip() or "25")))
FEED_BULK_CHUNK_SIZE = max(5, int(((os.getenv("FEED_BULK_CHUNK_SIZE") or "").strip() or "100")))
FEED_BULK_CONCURRENCY = max(1, int(((os.getenv("FEED_BULK_CONCURRENCY") or "").strip() or "3")))
FEED_BULK_TIMEOUT_S = float(((os.getenv("FEED_BULK_TIMEOUT_S") or "").strip() or "12"))
FEED_BULK_MAX_AGE_S = float(((os.getenv("FEED_BULK_MAX_AGE_S") or "").strip() or "20"))

# ATR background refresh policy. Before: Source 2 fired a /history fetch on
# EVERY call for EVERY symbol (even with a warm ATR — the ATR cache has no
# expiry and is also persisted to the DB), i.e. one extra history request per
# quote per cycle across the whole watchlist; on AngelOne's ~3 req/s candle
# endpoint that is the 403 storm. Now: only when the ATR is missing or older
# than the TTL, never twice concurrently for one symbol, at most N in flight
# process-wide, and a failed attempt backs off instead of retrying next cycle.
_ATR_REFRESH_TTL_S = float(((os.getenv("FEED_ATR_REFRESH_TTL_S") or "").strip() or str(6 * 3600)))
_ATR_RETRY_BACKOFF_S = float(((os.getenv("FEED_ATR_RETRY_BACKOFF_S") or "").strip() or "300"))
_ATR_MAX_INFLIGHT = max(1, int(((os.getenv("FEED_ATR_MAX_INFLIGHT") or "").strip() or "8")))
_ATR_INFLIGHT_MAX_AGE_S = 30.0   # a slot older than this is presumed leaked (e.g. its loop was torn down)
_ATR_STATE_LOCK = _threading.Lock()          # plain threading lock: state is touched from >1 event loop/thread
_ATR_INFLIGHT: dict[str, float] = {}         # clean symbol -> monotonic start
_ATR_LAST_TRY: dict[str, float] = {}         # clean symbol -> monotonic time of last attempt
_ATR_LAST_OK: dict[str, float] = {}          # clean symbol -> monotonic time of last successful compute
_BG_TASKS: set = set()                       # strong refs so fire-and-forget tasks aren't GC'd mid-flight


def _clean_sym(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _compute_atr_from_candles(candles: list) -> Optional[float]:
    """14-period ATR (simple average of true ranges) from OHLC candle dicts.
    Returns None if there are fewer than ATR_WINDOW+1 valid candles."""
    if not candles or len(candles) < ATR_WINDOW + 1:
        return None
    try:
        trs = []
        for i in range(1, len(candles)):
            h  = float(candles[i].get("high")  or 0)
            l  = float(candles[i].get("low")   or 0)
            pc = float(candles[i - 1].get("close") or 0)
            if h <= 0 or l <= 0 or pc <= 0:
                continue
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < ATR_WINDOW:
            return None
        return sum(trs[-ATR_WINDOW:]) / ATR_WINDOW
    except Exception:
        return None


def _store_atr(symbol: str, atr: float) -> None:
    global _ATR_DIRTY_COUNT
    clean = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not (clean and atr > 0):
        return
    should_flush = False
    with _ATR_LOCK:
        _ATR_CACHE[clean] = atr
        # BUG FIX (2026-09-12): entry_engine/exit_engine read this cache from
        # auto_pilot's dedicated worker thread (its own event loop, per the
        # 2026-09-10 fix), while _bg_refresh_atr writes here from the main
        # event-loop thread. _ATR_CACHE itself was already lock-protected;
        # the dirty counter increment+threshold-check below was not — two
        # real OS threads could race on a bare `+= 1`, silently undercounting
        # and pushing flushes further apart than _ATR_FLUSH_EVERY intends.
        # Folding it into the same _ATR_LOCK section makes the whole
        # "increment, then decide whether to flush" step atomic.
        _ATR_DIRTY_COUNT += 1
        if _ATR_DIRTY_COUNT >= _ATR_FLUSH_EVERY:
            _ATR_DIRTY_COUNT = 0
            should_flush = True
    if should_flush:
        _schedule_atr_flush()


def _schedule_atr_flush() -> None:
    """Kick off a periodic flush without blocking the caller's event loop.
    BUG FIX (2026-09-12): _flush_atr_cache_periodic() does synchronous
    SQLAlchemy I/O (session open, query, commit). _store_atr is called
    synchronously from _bg_refresh_atr, an async task — calling that DB work
    inline would stall whichever event loop happens to be running it (main
    loop or auto_pilot's worker-thread loop), the exact class of bug fixed
    in api-gateway's /surprise/ipo/list earlier this session. When a loop is
    running, offload the actual DB work to a thread via asyncio.to_thread and
    fire-and-forget (any failure is already swallowed inside the target
    function, so an un-awaited task is safe). Outside a running loop (e.g.
    a sync test harness), just run it inline — there is no loop to block."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _flush_atr_cache_periodic()
        return
    task = loop.create_task(asyncio.to_thread(_flush_atr_cache_periodic))

    def _log_if_failed(t: "asyncio.Task") -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.warning("scheduled ATR flush task failed: %s", t.exception())

    task.add_done_callback(_log_if_failed)


def _flush_atr_cache_periodic() -> None:
    """Opportunistic flush triggered from _store_atr once _ATR_FLUSH_EVERY new
    values have accumulated. Opens its own short-lived DB session — _store_atr
    is called from deep inside background price-refresh callbacks that don't
    carry a `db` session, so this can't just reuse flush_atr_cache_to_db(db)
    directly. Any failure here is swallowed: losing a periodic flush just
    means the next one (or the next restart's cold-start) catches up."""
    try:
        from db import get_session_factory
        Session = get_session_factory()
        db = Session()
        try:
            flush_atr_cache_to_db(db)
        finally:
            db.close()
    except Exception as e:
        logger.warning("_flush_atr_cache_periodic failed (non-fatal): %s", e)


def _cached_atr(symbol: str) -> Optional[float]:
    """Return the most-recently-computed ATR for this symbol, or None."""
    clean = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with _ATR_LOCK:
        return _ATR_CACHE.get(clean)


# ATR cache persistence — flush to resilience DB every N updates so
# a service restart doesn't cold-miss all symbols and trigger a
# 50-concurrent-history-fetch storm (the root cause of 495s cycles).
_ATR_DIRTY_COUNT = 0
_ATR_FLUSH_EVERY = 10   # flush after this many new ATR values written
_ATR_CACHE_KEY   = "market_feed:atr_cache"


def load_atr_cache_from_db(db) -> None:
    """Call once at startup (from main.py) to warm _ATR_CACHE from the DB.
    Non-fatal — a missing snapshot just means a cold start."""
    try:
        from resilience.local_cache import load_snapshot
        snap = load_snapshot(db, _ATR_CACHE_KEY)
        if snap and isinstance(snap.get("atrs"), dict):
            with _ATR_LOCK:
                _ATR_CACHE.update({
                    k: float(v) for k, v in snap["atrs"].items()
                    if isinstance(v, (int, float)) and v > 0
                })
            logger.info(
                "ATR cache warmed from DB: %d symbol(s) loaded",
                len(snap["atrs"]),
            )
    except Exception as e:
        logger.warning("load_atr_cache_from_db failed (non-fatal): %s", e)


def flush_atr_cache_to_db(db) -> None:
    """Persist current _ATR_CACHE snapshot to the resilience DB.
    Non-fatal — a flush failure just means the cache won't survive the
    next restart (graceful degradation back to cold-start behaviour)."""
    global _ATR_DIRTY_COUNT
    try:
        from resilience.local_cache import save_snapshot
        with _ATR_LOCK:
            snapshot = dict(_ATR_CACHE)
        save_snapshot(db, _ATR_CACHE_KEY, {"atrs": snapshot})
        _ATR_DIRTY_COUNT = 0
        logger.debug("ATR cache flushed to DB: %d symbol(s)", len(snapshot))
    except Exception as e:
        logger.warning("flush_atr_cache_to_db failed (non-fatal): %s", e)


async def _bg_refresh_atr(client: Optional[httpx.AsyncClient], symbol: str) -> None:
    """Background task: fetch 1mo/1d history, compute ATR, store in cache.
    Non-blocking and non-raising — called via _schedule_atr_refresh().

    `client` is accepted for backward compatibility but deliberately NOT used:
    the batch client passed in by get_quote() is closed the moment its
    get_quotes() gather returns, so a history call still in flight at that
    point died with "client has been closed". The refresh owns a short-lived
    client of its own instead, so its outcome no longer depends on how fast
    the surrounding batch happened to finish."""
    clean = _clean_sym(symbol)
    ok = False
    try:
        async with httpx.AsyncClient(timeout=8.0) as own:
            r = await own.get(
                f"{MARKET_DATA_URL}/history/{symbol}",
                params={"period": _ATR_HISTORY_PERIOD, "interval": "1d"},
                timeout=8.0,
            )
        if r.status_code == 200:
            candles = (r.json() or {}).get("candles") or []
            atr = _compute_atr_from_candles(candles)
            if atr:
                _store_atr(symbol, atr)
                ok = True
                logger.debug("ATR cache updated: %s → %.4f", symbol, atr)
    except Exception as e:
        logger.debug("_bg_refresh_atr(%s) failed (non-fatal): %s", symbol, e)
    finally:
        with _ATR_STATE_LOCK:
            _ATR_INFLIGHT.pop(clean, None)
            if ok:
                _ATR_LAST_OK[clean] = _time.monotonic()


def _schedule_atr_refresh(client: Optional[httpx.AsyncClient], symbol: str) -> bool:
    """Fire a background ATR refresh for `symbol` only if one is actually
    warranted (see the policy comment at the top of this module). Returns True
    if a task was scheduled. Never blocks, never raises. Must be called from
    a running event loop."""
    clean = _clean_sym(symbol)
    if not clean:
        return False
    now = _time.monotonic()
    have_atr = _cached_atr(clean) is not None
    with _ATR_STATE_LOCK:
        for k in [k for k, t0 in _ATR_INFLIGHT.items() if now - t0 > _ATR_INFLIGHT_MAX_AGE_S]:
            _ATR_INFLIGHT.pop(k, None)             # reclaim leaked slots
        if clean in _ATR_INFLIGHT or len(_ATR_INFLIGHT) >= _ATR_MAX_INFLIGHT:
            return False
        last_ok = _ATR_LAST_OK.get(clean)
        if have_atr and last_ok is not None and (now - last_ok) < _ATR_REFRESH_TTL_S:
            return False                           # warm and recent — nothing to do
        last_try = _ATR_LAST_TRY.get(clean)
        if last_try is not None and (now - last_try) < _ATR_RETRY_BACKOFF_S and (last_ok is None or last_try > last_ok):
            return False                           # recent failed attempt — back off
        _ATR_INFLIGHT[clean] = now
        _ATR_LAST_TRY[clean] = now
    coro = _bg_refresh_atr(client, symbol)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:                           # no running loop
        # session109 fix: the coroutine object already exists at this point;
        # without close() Python emits "coroutine '_bg_refresh_atr' was never
        # awaited" (RuntimeWarning) every time this path is taken.
        coro.close()
        with _ATR_STATE_LOCK:
            _ATR_INFLIGHT.pop(clean, None)
        return False
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return True


class Tick:
    __slots__ = ("symbol", "price", "as_of", "atr", "source", "volume", "day_high", "day_low")

    def __init__(self, symbol: str, price: float, as_of: datetime, atr: Optional[float], source: str,
                 volume: Optional[int] = None,
                 day_high: Optional[float] = None,
                 day_low: Optional[float] = None):
        self.symbol   = symbol
        self.price    = price
        self.as_of    = as_of
        self.atr      = atr
        self.source   = source
        self.volume   = volume
        # Intraday day range — populated from /quote when available.
        # Used by exit_engine to accelerate breakeven/trail when price is
        # near the day-high (less upside room), and by entry_engine to
        # penalise entries that are already extended vs the day range.
        # 2026-09-15 (session41b): added to fix "buy at wrong time" losses.
        self.day_high = day_high
        self.day_low  = day_low


async def get_quote(client: httpx.AsyncClient, symbol: str, *, for_display: bool = False,
                    timeout_scale: float = 1.0) -> Optional[Tick]:
    """
    for_display=True (dashboard price column only — never trading): no ATR
    background refresh is scheduled (the dashboard never reads ATR, and each
    refresh is a /history call against AngelOne's candle limit), and while the
    market is closed the live_quotes lookup is skipped (a tick can never be
    fresher than LIVE_QUOTE_MAX_AGE_S then, so that call was always wasted).
    Default False leaves every trading caller exactly as before.

    §1 — Quote with live_quotes-first cascade:
      1. live_quotes table (AngelOne WS feed, freshness ≤ LIVE_QUOTE_MAX_AGE_S)
         → price from tick, ATR from _cached_atr(); fires _bg_refresh_atr if
           cache is cold so the next call will have a warm value.
      2. market-data-service /quote/{symbol} (yfinance-backed)
         → ATR from _cached_atr() (populated by a concurrent background history
           fetch fired on this call).

    The staleness guard: if the live_quotes row is older than LIVE_QUOTE_MAX_AGE_S
    seconds, treat it as stale and fall through to source 2. This blocks trading
    on frozen data rather than acting on a stale AngelOne tick.
    """
    # ── Source 1: live_quotes table (AngelOne / Yahoo WS) ─────────────────────
    _skip_live = for_display and not _market_open_now()
    try:
        if _skip_live:
            raise _SkipLiveQuote()
        r_lq = await client.get(f"{MARKET_DATA_URL}/live-quote/{symbol}", timeout=3.0 * timeout_scale)
        if r_lq.status_code == 200:
            lq = r_lq.json()
            ltp = lq.get("ltp")
            updated_at_str = lq.get("updated_at")
            if ltp and float(ltp) > 0 and updated_at_str:
                from dateutil import parser as _dp
                updated_at = _dp.parse(updated_at_str)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age_s <= LIVE_QUOTE_MAX_AGE_S:
                    vol = lq.get("volume")

                    # ATR fix: serve from cache; if cold, schedule a background
                    # refresh so the next evaluation cycle has a warm value.
                    atr = _cached_atr(symbol)
                    # Fire-and-forget — doesn't delay this return at all. The
                    # scheduler itself decides whether a refresh is warranted
                    # (cold / past TTL / not already in flight / not backing off).
                    if not for_display and _schedule_atr_refresh(client, symbol) and atr is None:
                        logger.debug(
                            "ATR cache cold for %s (AngelOne hit) — background refresh scheduled",
                            symbol,
                        )

                    return Tick(
                        symbol=symbol,
                        price=float(ltp),
                        as_of=updated_at,
                        atr=atr,   # None on first cycle; populated from next cycle onward
                        source=f"live_quotes({lq.get('source', 'angelone')})",
                        volume=int(vol) if vol not in (None, "") else None,
                    )
                else:
                    logger.debug(
                        "live_quotes %s is %.1fs old > %.1fs limit — falling through",
                        symbol, age_s, LIVE_QUOTE_MAX_AGE_S,
                    )
    except _SkipLiveQuote:
        pass
    except Exception as e:
        # 2026-09-21 visibility fix: same silent-debug problem as
        # candidate_engine.py's _fetch_quote — a genuine dashboard "No
        # current price available" WAIT for nearly the whole watchlist
        # gave zero log signal to explain why. Loud enough for a normal
        # `docker compose logs` grep.
        logger.warning("live_quotes read failed for %s (falling through to source 2): %s: %s", symbol, type(e).__name__, e)

    # ── Source 2: market-data-service /quote (yfinance-backed) ────────────────
    # Also fires a background ATR refresh (non-blocking) so the cache warms
    # concurrently with returning the price to the caller.
    try:
        # The quote is needed now; the ATR refresh (background, result stored
        # in cache for this and future calls) is only scheduled when the
        # scheduler says one is warranted — it used to fire unconditionally
        # here, i.e. one /history request per quote per cycle for every symbol.
        if not for_display:
            _schedule_atr_refresh(client, symbol)

        r = await client.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=8.0 * timeout_scale)
        # the ATR refresh task runs in the background; we don't await it here.

        if r.status_code != 200:
            # 2026-09-21 visibility fix: this branch used to return None with
            # zero log line at all — worse than the except below, since a
            # non-200 (e.g. market-data-service 500/timeout-ish response)
            # never even hit the except block. This is the path directly
            # behind the dashboard's "No current price available" WAIT text.
            logger.warning(
                "get_quote(%s): market-data-service /quote returned %d — %s",
                symbol, r.status_code, r.text[:200],
            )
            return None
        q = r.json()
        price = q.get("price") or q.get("cmp")
        if not price or float(price) <= 0:
            return None
        vol = q.get("volume")

        # Prefer freshly-refreshed ATR (may already be done by the time we
        # get here if the history call is fast), fall back to whatever was
        # already in cache.
        atr = _cached_atr(symbol)

        # Parse day range — market-data-service /quote returns day_high/day_low
        # from AngelOne WS or yfinance OHLC.  Used by exit_engine and entry_engine
        # for range-position-aware decisions (session41b fix).
        _day_high = q.get("day_high")
        _day_low  = q.get("day_low")

        return Tick(
            symbol=symbol,
            price=float(price),
            # market-data-service's own fetched_at isn't guaranteed to be a
            # cleanly-parseable tz-aware timestamp across every source branch
            # it can take, so this module stamps its OWN receipt time — which
            # is what risk_engine's staleness check (#7) is actually trying to
            # measure (age since WE last saw a price), not the upstream
            # provider's internal timestamp.
            as_of=datetime.now(timezone.utc),
            atr=atr,
            source=q.get("source") or "market-data-service",
            volume=int(vol) if vol not in (None, "") else None,
            day_high=float(_day_high) if _day_high else None,
            day_low=float(_day_low)  if _day_low  else None,
        )
    except Exception as e:
        logger.warning("get_quote(%s): source-2 (market-data-service /quote) failed: %s: %s", symbol, type(e).__name__, e)
        return None


class _SkipLiveQuote(Exception):
    """Internal control-flow marker: skip Source 1 (never escapes get_quote)."""


def _market_open_now() -> bool:
    """True during NSE cash hours. Fails OPEN (True) if the helper is
    unavailable, so a broken import can only ever cost one extra lookup,
    never hide a live tick."""
    try:
        from tz_utils import is_market_open_ist
        return bool(is_market_open_ist())
    except Exception:  # noqa: BLE001
        return True


# ── Dashboard price cache (2026-10-04, items 3 & 15) ─────────────────────────
# Positions / Orders / Candidates each priced their symbols independently on
# every poll, and the Real Trade tab polls all three (plus /pipeline/status)
# every few seconds: ~35 /live-quote + ~35 /quote + ~14 /history per burst, all
# for a display-only price column. Now one short-TTL cache is shared by the
# three routes, concurrent callers wait for the first one's fetch instead of
# repeating it, and the display path never triggers ATR /history refreshes.
DISPLAY_PRICE_TTL_OPEN_S = float(((os.getenv("DISPLAY_PRICE_TTL_OPEN_S") or "").strip() or "8"))
DISPLAY_PRICE_TTL_CLOSED_S = float(((os.getenv("DISPLAY_PRICE_TTL_CLOSED_S") or "").strip() or "120"))
# A symbol that returned nothing is remembered briefly so an unquotable symbol
# is not re-requested on every poll.
DISPLAY_PRICE_MISS_TTL_S = float(((os.getenv("DISPLAY_PRICE_MISS_TTL_S") or "").strip() or "30"))
_DISPLAY_CACHE: dict = {}                 # clean symbol -> (monotonic ts, price | None)
_DISPLAY_CACHE_LOCK = _threading.Lock()   # guards the dict only (never held across an await)
_DISPLAY_FETCH_LOCKS: dict = {}           # running loop id -> asyncio.Lock (single-flight per loop)


def _display_ttl() -> float:
    return DISPLAY_PRICE_TTL_OPEN_S if _market_open_now() else DISPLAY_PRICE_TTL_CLOSED_S


def _display_lookup(symbols: list, ttl: float) -> tuple:
    """Split `symbols` into (hits: {sym: price}, misses: [sym])."""
    now = _time.monotonic()
    hits: dict = {}
    misses: list = []
    with _DISPLAY_CACHE_LOCK:
        for sym in symbols:
            ent = _DISPLAY_CACHE.get(_clean_sym(sym))
            if ent is not None:
                ts, price = ent
                if price is not None and (now - ts) <= ttl:
                    hits[sym] = price
                    continue
                if price is None and (now - ts) <= DISPLAY_PRICE_MISS_TTL_S:
                    continue          # known-unquotable: skip the lookup, report no price
            misses.append(sym)
    return hits, misses


def clear_display_price_cache() -> None:
    with _DISPLAY_CACHE_LOCK:
        _DISPLAY_CACHE.clear()


async def get_display_prices(symbols: list[str]) -> dict[str, float]:
    """Dashboard-only LTP lookup: {symbol: price}. NEVER use for sizing,
    pricing or evaluating an order (entry/exit call get_quote(s) directly).
    Never raises into the caller beyond what get_quotes-style code would."""
    out: dict[str, float] = {}
    if not symbols:
        return out
    uniq = list(dict.fromkeys(symbols))
    ttl = _display_ttl()
    hits, misses = _display_lookup(uniq, ttl)
    out.update(hits)
    if not misses:
        return out

    loop_id = id(asyncio.get_running_loop())
    lock = _DISPLAY_FETCH_LOCKS.get(loop_id)
    if lock is None:
        lock = _DISPLAY_FETCH_LOCKS[loop_id] = asyncio.Lock()
        if len(_DISPLAY_FETCH_LOCKS) > 8:            # loops are short-lived in tests; don't grow forever
            for k in list(_DISPLAY_FETCH_LOCKS)[:-4]:
                _DISPLAY_FETCH_LOCKS.pop(k, None)
    async with lock:
        # Another request may have fetched these while we waited for the lock.
        hits2, misses2 = _display_lookup(misses, ttl)
        out.update(hits2)
        if misses2:
            async def _one(client, sym):
                return await get_quote(client, sym, for_display=True)
            results = await _bounded_gather(misses2, _one, "get_display_prices")
            now = _time.monotonic()
            with _DISPLAY_CACHE_LOCK:
                if len(_DISPLAY_CACHE) > 4000:
                    _DISPLAY_CACHE.clear()
                for sym, tick in zip(misses2, results):
                    price = float(tick.price) if tick is not None and tick.price else None
                    _DISPLAY_CACHE[_clean_sym(sym)] = (now, price)
                    if price is not None:
                        out[sym] = price
    return out


def _parse_bulk_ts(raw) -> Optional[datetime]:
    """fetched_at from /quotes/bulk -> aware UTC datetime, or None if missing/unparseable."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        from dateutil import parser as _dp
        dt = _dp.parse(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)   # market-data stamps naive UTC
    return dt


def _tick_from_bulk_item(item, *, now: Optional[datetime] = None) -> Optional[Tick]:
    """Turn one /quotes/bulk quote dict into a Tick, or None if it has no usable price or is older than
    FEED_BULK_MAX_AGE_S (a bulk answer may come from market-data's quote cache, so the trading staleness
    guard is applied here too). ATR comes from the local ATR cache exactly like get_quote()."""
    if not isinstance(item, dict):
        return None
    sym = _clean_sym(item.get("symbol") or "")
    if not sym:
        return None
    try:
        price = float(item.get("price") or item.get("cmp") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price != price:
        return None
    ts = _parse_bulk_ts(item.get("fetched_at"))
    if ts is None:
        return None
    age_s = ((now or datetime.now(timezone.utc)) - ts).total_seconds()
    if age_s > FEED_BULK_MAX_AGE_S:
        return None
    vol = item.get("volume")
    dh, dl = item.get("day_high"), item.get("day_low")
    try:
        return Tick(
            symbol=sym,
            price=price,
            as_of=ts,
            atr=_cached_atr(sym),
            source=f"bulk({item.get('source') or 'market-data-service'})",
            volume=int(vol) if vol not in (None, "") else None,
            day_high=float(dh) if dh else None,
            day_low=float(dl) if dl else None,
        )
    except (TypeError, ValueError):
        return None


async def _bulk_ticks(client: httpx.AsyncClient, symbols: list[str], *, timeout: Optional[float] = None) -> dict[str, Tick]:
    """Best-effort chunked POST /quotes/bulk -> {symbol: Tick}. Never raises; a failed chunk just leaves
    its symbols out so the caller falls back to the per-symbol cascade for them."""
    out: dict[str, Tick] = {}
    uniq = list(dict.fromkeys(s for s in symbols if s))
    if not uniq:
        return out
    chunks = [uniq[i:i + FEED_BULK_CHUNK_SIZE] for i in range(0, len(uniq), FEED_BULK_CHUNK_SIZE)]
    sem = asyncio.Semaphore(FEED_BULK_CONCURRENCY)
    to = FEED_BULK_TIMEOUT_S if timeout is None else timeout

    async def _post(chunk: list[str]) -> None:
        async with sem:
            try:
                r = await client.post(f"{MARKET_DATA_URL}/quotes/bulk", json={"symbols": chunk}, timeout=to)
                if r.status_code != 200:
                    logger.warning("get_quotes: /quotes/bulk returned %d for %d symbol(s) — %s",
                                   r.status_code, len(chunk), r.text[:160])
                    return
                body = r.json()
                now = datetime.now(timezone.utc)
                for item in (body.get("quotes") if isinstance(body, dict) else None) or []:
                    tick = _tick_from_bulk_item(item, now=now)
                    if tick is not None:
                        out[tick.symbol] = tick
                        _schedule_atr_refresh(client, tick.symbol)   # cold ATR -> background refresh, as get_quote()
            except Exception as e:
                logger.warning("get_quotes: /quotes/bulk chunk of %d failed: %s: %s", len(chunk), type(e).__name__, e)

    await asyncio.gather(*[_post(c) for c in chunks])
    return out


async def _priority_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Priority lane for open positions: dedicated client (own pool), scaled timeouts, bulk fallback."""
    uniq = list(dict.fromkeys(s for s in symbols if s))
    out: dict[str, Tick] = {}
    if not uniq:
        return out
    limits = httpx.Limits(max_connections=max(8, len(uniq) * 2), max_keepalive_connections=max(2, len(uniq)))
    async with httpx.AsyncClient(limits=limits) as client:
        ticks = await asyncio.gather(
            *[get_quote(client, s, timeout_scale=FEED_PRIORITY_TIMEOUT_SCALE) for s in uniq],
            return_exceptions=True,
        )
        for sym, tick in zip(uniq, ticks):
            if isinstance(tick, Tick):
                out[sym] = tick
        missing = [s for s in uniq if s not in out]
        if missing:
            got = await _bulk_ticks(client, missing, timeout=FEED_BULK_TIMEOUT_S * FEED_PRIORITY_TIMEOUT_SCALE / 2)
            for sym, tick in got.items():
                # bulk keys come back as clean symbols; map them onto the requested spelling
                for want in missing:
                    if _clean_sym(want) == sym and want not in out:
                        out[want] = tick
            still = [s for s in uniq if s not in out]
            logger.warning(
                "priority quotes: per-symbol path failed for %d/%d symbol(s) %s — /quotes/bulk recovered %d%s",
                len(missing), len(uniq), missing[:8], len(missing) - len(still),
                f", still missing {still[:8]}" if still else "",
            )
    return out


async def get_quotes(symbols: list[str], *, priority: bool = False) -> dict[str, Tick]:
    """Bulk fetch over a shared client.

    priority=True (open-position exit evaluation): dedicated pool, longer timeouts, /quotes/bulk fallback
    — see the group152 note by FEED_PRIORITY_TIMEOUT_SCALE.

    Default: above FEED_BULK_MIN_SYMBOLS symbols, chunked /quotes/bulk first (a few requests instead of one
    or two per symbol), then the per-symbol cascade only for symbols bulk could not price fresh. At or below
    that size it is the original concurrent per-symbol path."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    if priority:
        return await _priority_quotes(symbols)

    todo = list(symbols)
    if len(set(symbols)) > FEED_BULK_MIN_SYMBOLS:
        try:
            limits = httpx.Limits(max_connections=FEED_BULK_CONCURRENCY * 2)
            async with httpx.AsyncClient(limits=limits) as bulk_client:
                got = await _bulk_ticks(bulk_client, symbols)
            for sym in symbols:
                t = got.get(_clean_sym(sym))
                if t is not None:
                    out[sym] = t
            todo = [s for s in symbols if s not in out]
            logger.info("get_quotes: bulk-first priced %d/%d symbol(s); %d left for per-symbol lookups",
                        len(out), len(symbols), len(todo))
        except Exception as e:
            logger.warning("get_quotes: bulk-first failed (%s: %s) — per-symbol path for all", type(e).__name__, e)
            out, todo = {}, list(symbols)

    if todo:
        results = await _bounded_gather(todo, get_quote, "get_quotes")
        for sym, tick in zip(todo, results):
            if tick is not None:
                out[sym] = tick
    return out


async def _bounded_gather(symbols: list[str], fn, label: str) -> list:
    """Run `fn(client, symbol)` for every symbol over one shared client with
    (a) at most FEED_QUOTE_CONCURRENCY lookups in flight and (b) a wall-clock
    deadline after which not-yet-started lookups are skipped (returned as
    None) instead of being attempted. Results are returned in input order."""
    sem = asyncio.Semaphore(FEED_QUOTE_CONCURRENCY)
    deadline = _time.monotonic() + FEED_BATCH_DEADLINE_S
    skipped = 0

    limits = httpx.Limits(
        max_connections=FEED_HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=max(4, FEED_HTTP_MAX_CONNECTIONS // 2),
    )

    async def _one(client: httpx.AsyncClient, sym: str):
        nonlocal skipped
        async with sem:
            if _time.monotonic() > deadline:
                skipped += 1
                return None
            return await fn(client, sym)

    async with httpx.AsyncClient(limits=limits) as client:
        results = await asyncio.gather(*[_one(client, s) for s in symbols])
    if skipped:
        logger.warning(
            "%s: batch deadline of %.0fs hit — %d/%d symbol(s) were not attempted "
            "(market-data-service slow or overloaded?)",
            label, FEED_BATCH_DEADLINE_S, skipped, len(symbols),
        )
    return list(results)


async def _get_preview(client: httpx.AsyncClient, symbol: str) -> Optional[Tick]:
    """Best-effort last-close / previous-close lookup for a symbol whose LIVE
    tick is missing. This is a NON-TRADEABLE price — it can be yesterday's
    close — used only so the dashboard can show a preview Waiting-at / Stop /
    Target instead of blank '—' columns, and so a WAIT can explain itself. It
    is never used to size or place an order (the ENTER path only runs when a
    real live tick exists). Source is tagged 'preview:last_close' so nothing
    downstream can mistake it for a fresh quote.
    """
    for path in (f"/quote/{symbol}", f"/last-close/{symbol}"):
        try:
            r = await client.get(f"{MARKET_DATA_URL}{path}", timeout=8.0)
            if r.status_code != 200:
                continue
            q = r.json()
            px = (
                q.get("price") or q.get("cmp") or q.get("ltp")
                or q.get("prev_close") or q.get("previous_close")
                or q.get("close") or q.get("last_close")
            )
            if px and float(px) > 0:
                return Tick(
                    symbol=symbol,
                    price=float(px),
                    as_of=datetime.now(timezone.utc),
                    atr=_cached_atr(symbol),   # serve from cache; None is fine for preview
                    source="preview:last_close",
                )
        except Exception as e:
            logger.debug("get_preview(%s) via %s failed: %s", symbol, path, e)
            continue
    return None


async def get_preview_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Bulk best-effort preview (last-close) prices — see _get_preview. Only
    call this for symbols that came back empty from get_quotes()."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    results = await _bounded_gather(symbols, _get_preview, "get_preview_quotes")
    for sym, tick in zip(symbols, results):
        if tick is not None:
            out[sym] = tick
    return out
