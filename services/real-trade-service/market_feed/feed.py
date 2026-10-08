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
# group246: small non-priority batches (the <=20 entry candidates of a cycle) used to skip bulk and cost one
# /live-quote plus one /quote per symbol (~40 calls) while the held positions were being priced. From this many
# symbols up they use /quotes/bulk first too. The large-batch rules (distress back-off) still key on
# FEED_BULK_MIN_SYMBOLS only, so a small batch is never starved. Set it >= FEED_BULK_MIN_SYMBOLS to switch off.
FEED_SMALL_BATCH_BULK_MIN_SYMBOLS = max(2, int(((os.getenv("FEED_SMALL_BATCH_BULK_MIN_SYMBOLS") or "").strip() or "5")))

# group248: when one /quotes/bulk chunk of a bulk-first batch times out (while other chunks were answered), the symbols
# of that chunk used to fall straight to the per-symbol /live-quote + /quote path - on 2026-10-08 that was 120 GET /quote
# calls, each shed by AngelOne's lane budget and sent to the saturated Yahoo path, ~15 ReadTimeouts. They are now asked
# once more as smaller bulk calls (FEED_BULK_RETRY_CHUNK_SIZE per call, FEED_BULK_RETRY_TIMEOUT_S each) before the
# per-symbol path. FEED_BULK_RETRY_FAILED=0 turns it off. Only done when at least one chunk answered (market-data is up).
FEED_BULK_RETRY_FAILED = ((os.getenv("FEED_BULK_RETRY_FAILED") or "").strip() or "1") not in ("0", "false", "False")
FEED_BULK_RETRY_CHUNK_SIZE = max(5, int(((os.getenv("FEED_BULK_RETRY_CHUNK_SIZE") or "").strip() or "25")))
FEED_BULK_RETRY_TIMEOUT_S = float(((os.getenv("FEED_BULK_RETRY_TIMEOUT_S") or "").strip() or "20"))
FEED_BULK_CHUNK_SIZE = max(5, int(((os.getenv("FEED_BULK_CHUNK_SIZE") or "").strip() or "100")))
FEED_BULK_CONCURRENCY = max(1, int(((os.getenv("FEED_BULK_CONCURRENCY") or "").strip() or "3")))
FEED_BULK_TIMEOUT_S = float(((os.getenv("FEED_BULK_TIMEOUT_S") or "").strip() or "12"))
FEED_BULK_MAX_AGE_S = float(((os.getenv("FEED_BULK_MAX_AGE_S") or "").strip() or "20"))

# ── group171 (item 3 of the 2026-10-06 list): fewer market-data calls for held symbols ───────────────────
# The exit cycle (~8-10 s) priced each of the 5 open positions with GET /live-quote and then GET /quote, i.e.
# ~10 requests per cycle for 5 symbols, from four callers (exit evaluation, two auto_pilot loops, the
# position-action route), and the big non-priority batch then sent every symbol bulk could not price through
# the same two-call cascade (224 symbols -> up to 448 calls). Changes:
#   * priority lane is bulk-first: ONE POST /quotes/bulk for all held symbols, accepted only when the
#     answer is at most FEED_PRIORITY_BULK_MAX_AGE_S old (default 10 s, tighter than the 20 s batch limit);
#     the per-symbol /live-quote + /quote cascade then runs only for symbols bulk did not price.
#     A failed bulk-first call turns bulk-first off for FEED_PRIORITY_BULK_COOLDOWN_S so a struggling
#     market-data is not asked twice per cycle. FEED_PRIORITY_BULK_FIRST=0 restores the old order.
#   * a priced held symbol is shared for FEED_PRIORITY_SHARE_S (default 3 s) between the four callers, so
#     two loops firing within a moment do not repeat the same request. The Tick keeps its real as_of.
#   * non-priority symbols that bulk already answered or missed skip /live-quote and go straight to /quote
#     (bulk had just read the same live feeds); FEED_LEFTOVER_SKIP_LIVE=0 restores it. The log line now says
#     why bulk left symbols unpriced (missing from the answer / older than the limit / no price).
def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        return default
    return v if v == v and v >= 0 else default


def _env_on(name: str, default: str = "1") -> bool:
    return ((os.getenv(name) or "").strip() or default) not in ("0", "false", "False", "off", "no")


FEED_PRIORITY_BULK_FIRST = _env_on("FEED_PRIORITY_BULK_FIRST")
FEED_PRIORITY_BULK_MAX_AGE_S = _env_float("FEED_PRIORITY_BULK_MAX_AGE_S", 10.0)
FEED_PRIORITY_BULK_TIMEOUT_S = _env_float("FEED_PRIORITY_BULK_TIMEOUT_S", 4.0)
FEED_PRIORITY_BULK_COOLDOWN_S = _env_float("FEED_PRIORITY_BULK_COOLDOWN_S", 30.0)
FEED_PRIORITY_SHARE_S = _env_float("FEED_PRIORITY_SHARE_S", 3.0)
# group252 (2026-10-08 log): the held-position bulk call (one chunk of 6) timed out after its full 4 s and ONLY THEN did the
# per-symbol path start, so every held-position price (and the 8 s exit cycle) lost 4 s while market-data answered the
# per-symbol calls at once. Retrying a 6-symbol chunk in smaller chunks cannot help; the wait is the cost. Now, when the bulk
# call has not answered within FEED_PRIORITY_HEDGE_S (default 1.5; 0 = old behaviour) the per-symbol lookups start
# alongside it and each symbol takes whichever answer arrives first. A bulk call that was still unanswered then counts as
# a failed bulk-first (same FEED_PRIORITY_BULK_COOLDOWN_S pause as before).
FEED_PRIORITY_HEDGE_S = _env_float("FEED_PRIORITY_HEDGE_S", 1.5)
FEED_LEFTOVER_SKIP_LIVE = _env_on("FEED_LEFTOVER_SKIP_LIVE")
_PRIO_LOCK = _threading.Lock()

# ── group225 (items 1/2 of the 2026-10-07 list): open positions keep a price while market-data is saturated ──
# At 09:43 the six REAL positions (ADANIGREEN, GODREJCP, SCI, IOC, CIEINDIA, COHANCE) hit ReadTimeout on bulk, on
# /live-quote and on /quote every 8 s exit cycle although market-data answered 200 (late). Cause: the same process
# also sent ~579 per-symbol lookups for the watchlist, so market-data's workers were busy when the position calls
# arrived. Two changes:
#   * last-good fallback: when the whole priority lane could not price a held symbol, return the last tick the lane
#     did price for it if that tick is at most FEED_PRIORITY_STALE_FALLBACK_S old (default 90 s, 0 = off). The Tick
#     keeps its REAL as_of and its source is tagged "stale_last_good(<orig>)", so nothing mistakes it for a fresh quote.
#   * back-pressure: a priority-lane failure (a held symbol still unpriced after every attempt) switches the per-symbol
#     leftovers of LARGE non-priority batches (> FEED_BULK_MIN_SYMBOLS) off for FEED_BACKPRESSURE_S
#     (default 20 s; small entry batches are never affected) and a single non-priority batch sends at most FEED_LEFTOVER_MAX (default 120) per-symbol lookups
#     (0 = no cap), so the watchlist poll can no longer starve the positions.
FEED_PRIORITY_STALE_FALLBACK_S = _env_float("FEED_PRIORITY_STALE_FALLBACK_S", 90.0)
FEED_BACKPRESSURE_S = _env_float("FEED_BACKPRESSURE_S", 20.0)
FEED_LEFTOVER_MAX = int(_env_float("FEED_LEFTOVER_MAX", 120.0))
_PRIO_LAST_GOOD: dict = {}         # clean symbol -> Tick (last tick the priority lane priced)
_PRIO_DISTRESS_UNTIL = [0.0]       # monotonic time until which non-priority per-symbol leftovers are skipped
_PRIO_SHARED: dict = {}            # clean symbol -> (monotonic ts, Tick)
_PRIO_BULK_OFF_UNTIL = [0.0]       # monotonic time until which bulk-first is skipped after a failure

# group249 (2026-10-08 11:03 IST log, after group 248): the retry priced 168 of 300 lost symbols, but the 138 still unpriced
# went to per-symbol GET /quote (capped at 120) and ~100 of those ended in ReadTimeout - market-data was saturated and
# AngelOne's quote lane was in cooldown. Those are watchlist symbols nobody holds. For a caller that opts in
# (allow_stale=True, today only the watchlist trigger, which merely QUEUES a candidate and never places an order) a symbol
# the bulk pass could not price now reuses the last tick the non-priority path priced for it, if that tick is at most
# FEED_LEFTOVER_STALE_S old (default 120 s, 0 = off). The Tick keeps its REAL as_of and its source is tagged
# "stale_last_good(<orig>)". Only symbols with no recent tick still go to the per-symbol path.
FEED_LEFTOVER_STALE_S = _env_float("FEED_LEFTOVER_STALE_S", 120.0)
_WL_LAST_GOOD: dict = {}           # clean symbol -> Tick (last tick the non-priority batch path priced)
_WL_LAST_GOOD_MAX = 3000


def _wl_remember_last_good(ticks: dict) -> None:
    """Remember the freshest priced tick per watchlist symbol for the group249 fallback (bounded, memory-only)."""
    if FEED_LEFTOVER_STALE_S <= 0 or not ticks:
        return
    with _PRIO_LOCK:
        if len(_WL_LAST_GOOD) > _WL_LAST_GOOD_MAX:
            _WL_LAST_GOOD.clear()
        for sym, tick in ticks.items():
            if tick is not None and not str(tick.source).startswith("stale_last_good"):
                _WL_LAST_GOOD[_clean_sym(sym)] = tick


def _wl_last_good_fallback(wanted: list) -> dict:
    """Last good ticks (<= FEED_LEFTOVER_STALE_S old by their own as_of) for watchlist symbols bulk could not price."""
    if FEED_LEFTOVER_STALE_S <= 0 or not wanted or not _WL_LAST_GOOD:
        return {}
    now = datetime.now(timezone.utc)
    out: dict = {}
    with _PRIO_LOCK:
        for sym in wanted:
            t = _WL_LAST_GOOD.get(_clean_sym(sym))
            if t is None:
                continue
            as_of = t.as_of if t.as_of.tzinfo else t.as_of.replace(tzinfo=timezone.utc)
            if 0 <= (now - as_of).total_seconds() <= FEED_LEFTOVER_STALE_S:
                out[sym] = Tick(symbol=t.symbol, price=t.price, as_of=t.as_of, atr=t.atr,
                                source=f"stale_last_good({t.source})", volume=t.volume,
                                day_high=t.day_high, day_low=t.day_low, prev_close=t.prev_close)
    return out


def clear_priority_share() -> None:
    """Forget the shared priority ticks and the bulk-first cool-down (tests, or an operator hook)."""
    with _PRIO_LOCK:
        _PRIO_SHARED.clear()
        _PRIO_LAST_GOOD.clear()
        _PRIO_BULK_OFF_UNTIL[0] = 0.0
        _PRIO_DISTRESS_UNTIL[0] = 0.0
        _WL_LAST_GOOD.clear()

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
# group245: after a restart _ATR_LAST_OK is empty while the ATR cache is warm from the DB, so EVERY symbol the first
# bulk/quote passes priced looked "due" and got a /history refresh (8 in flight, hundreds queued over the open): each is
# an AngelOne 1y/1d candle call in market-data (the 2026-10-08 log: 13 /history?period=1mo calls in one second, then
# getCandleData 403 "exceeding access rate" and a 30-60 s candle cooldown). A symbol whose ATR came from the DB and has
# not been refreshed by this process is now refreshed at most FEED_ATR_WARM_REFRESH_PER_MIN times a minute process-wide
# (default 6); a symbol with NO ATR is not limited (trading needs it). 0 = no limit (the old behaviour).
_ATR_WARM_REFRESH_PER_MIN = int(_env_float("FEED_ATR_WARM_REFRESH_PER_MIN", 6.0))
_ATR_WARM_STAMPS: list = []                  # monotonic times of recent warm-ATR refreshes (guarded by _ATR_STATE_LOCK)
_ATR_STATE_LOCK = _threading.Lock()          # plain threading lock: state is touched from >1 event loop/thread
_ATR_INFLIGHT: dict[str, float] = {}         # clean symbol -> monotonic start
_ATR_LAST_TRY: dict[str, float] = {}         # clean symbol -> monotonic time of last attempt
_ATR_LAST_OK: dict[str, float] = {}          # clean symbol -> monotonic time of last successful compute
_BG_TASKS: set = set()                       # strong refs so fire-and-forget tasks aren't GC'd mid-flight


def _clean_sym(symbol: str) -> str:
    """Canonical symbol: upper-case, trimmed, percent-decoded ("M%26M" -> "M&M"), exchange suffix removed.

    group159 (item 4 of the 2026-10-05 list): KOTAKBANK and KOTAKBANK.NS were fetched as two symbols and
    "ARE&M" / "M%26M" were keyed differently. Only a trailing .NS/.BO is stripped (the old replace() also
    cut ".NS" out of the middle of a string).
    """
    from urllib.parse import unquote as _unquote
    sym = _unquote(str(symbol or "")).strip().upper()
    for suf in (".NS", ".BO"):
        if sym.endswith(suf):
            sym = sym[: -len(suf)].strip()
            break
    return sym


def _path_sym(symbol: str) -> str:
    """Symbol as one URL path segment ("M&M" -> "M%26M", "ARE&M" -> "ARE%26M"); decoded first so it is never double-encoded."""
    from urllib.parse import quote as _quote, unquote as _unquote
    return _quote(_unquote(str(symbol or "")), safe="")


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
                f"{MARKET_DATA_URL}/history/{_path_sym(symbol)}",
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
        if have_atr and last_ok is None and _ATR_WARM_REFRESH_PER_MIN > 0:
            # group245: ATR loaded from the DB, never refreshed by this process: spread these out
            _ATR_WARM_STAMPS[:] = [t0 for t0 in _ATR_WARM_STAMPS if now - t0 < 60.0]
            if len(_ATR_WARM_STAMPS) >= _ATR_WARM_REFRESH_PER_MIN:
                return False
            _ATR_WARM_STAMPS.append(now)
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
    __slots__ = ("symbol", "price", "as_of", "atr", "source", "volume", "day_high", "day_low", "prev_close")

    def __init__(self, symbol: str, price: float, as_of: datetime, atr: Optional[float], source: str,
                 volume: Optional[int] = None,
                 day_high: Optional[float] = None,
                 day_low: Optional[float] = None,
                 prev_close: Optional[float] = None):
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
        # group155: previous session close when the source supplies it (None otherwise). Lets
        # entry_engine tell a stock that is UP today from one that is down, which a bare price
        # cannot. Never used for sizing or ordering.
        self.prev_close = prev_close


def _lq_prev_close(lq, ltp) -> Optional[float]:
    """group156: previous close from a market-data `/live-quote` answer, or None. Never raises.

    The AngelOne feed stores the broker quote's `close` (the PREVIOUS session's close, the same way
    market-data's movers sweep reads it) in `ohlc_json`. When that field is missing the writer falls
    back to the LTP, so a close that equals the LTP is not trustworthy and is treated as unknown
    (fail-open, as before). Used only by the watchlist day-change guard, never for sizing/orders.
    """
    try:
        ohlc = lq.get("ohlc") if isinstance(lq, dict) else None
        if not isinstance(ohlc, dict):
            return None
        pc = _safe_prev_close(ohlc.get("close"))
        if pc is None or abs(pc - float(ltp)) < 1e-9:
            return None
        return pc
    except Exception:
        return None


def _safe_prev_close(value) -> Optional[float]:
    """group155: parse an upstream previous-close into a positive float, or None. Never raises."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 and v == v else None


# ── group160 (item 5): symbols that have no price are paused instead of retried every cycle ─────────────
# QUALIANCE, BMISL, BAGMANE, EMBASSY, ANNAPURNA and AAKASH (delisted / renamed / not an equity) answered
# 404 or "no price" on every cycle, and each retry cost a /live-quote + /quote call (plus the bulk miss)
# against the rate-limit budget that real symbols need. After FEED_DEAD_AFTER_MISSES (default 3) definite
# misses in a row on /quote (HTTP 404, or HTTP 200 with no usable price), the symbol is left out of
# non-priority batches for FEED_DEAD_BACKOFF_S (default 30 min), doubling after each further miss up to
# FEED_DEAD_BACKOFF_MAX_S (default 6 h). Timeouts and 5xx are NOT misses (upstream trouble says nothing
# about the symbol). One real price clears the count. The priority lane (open positions) never skips.
# FEED_DEAD_SKIP=0 turns the whole thing off. group183b: paused symbols are also saved to the resilience DB (see
# resilience/pause_state.py) and restored at startup, so a restart no longer asks about each of them again
# (PAUSE_STATE_PERSIST=0 = per-process only, as before).
_DEAD_LOCK = _threading.Lock()
_DEAD_PERSIST_KEY = "market_feed:dead_symbols"
_DEAD_PERSIST_READY = False       # set by load_dead_symbols_from_db(); nothing is written before that
_DEAD: dict[str, list] = {}        # clean symbol -> [consecutive_misses, skip_until_monotonic]


def _dead_cfg() -> tuple:
    def _f(name: str, default: float) -> float:
        raw = (os.getenv(name) or "").strip()
        try:
            v = float(raw) if raw else default
        except ValueError:
            return default
        return v if v == v and v >= 0 else default
    on = ((os.getenv("FEED_DEAD_SKIP") or "").strip() or "1") not in ("0", "false", "False")
    return (on, max(1, int(_f("FEED_DEAD_AFTER_MISSES", 3))), _f("FEED_DEAD_BACKOFF_S", 1800.0),
            _f("FEED_DEAD_BACKOFF_MAX_S", 21600.0))


def _note_no_data(symbol: str) -> None:
    """Record one definite "no price for this symbol" answer. Never raises."""
    try:
        on, after, base, cap = _dead_cfg()
        sym = _clean_sym(symbol)
        if not on or not sym:
            return
        with _DEAD_LOCK:
            ent = _DEAD.setdefault(sym, [0, 0.0])
            ent[0] += 1
            if ent[0] >= after:
                wait = min(cap, base * (2 ** (ent[0] - after))) if base > 0 else 0.0
                ent[1] = _time.monotonic() + wait
                if ent[0] == after or ent[0] - after < 6:
                    logger.info("get_quotes: %s has had no price %d time(s) in a row — paused for %.0f min "
                                "(open positions are never paused)", sym, ent[0], wait / 60.0)
                _persist_dead_state()
    except Exception as e:  # noqa: BLE001
        logger.debug("_note_no_data(%s) failed: %s", symbol, e)


def _note_priced(symbol: str) -> None:
    """A real price arrived: forget any misses. Never raises."""
    try:
        if _DEAD:
            with _DEAD_LOCK:
                gone = _DEAD.pop(_clean_sym(symbol), None)
            if gone is not None and gone[1] > _time.monotonic():
                _persist_dead_state()          # a saved pause just ended: save the removal too
    except Exception:  # noqa: BLE001
        pass


def _paused_symbols(symbols: list) -> list:
    """The subset of (canonical) `symbols` currently paused. Never raises."""
    try:
        if not _DEAD or not _dead_cfg()[0]:
            return []
        now = _time.monotonic()
        with _DEAD_LOCK:
            return [x for x in symbols if x in _DEAD and _DEAD[x][1] > now]
    except Exception:  # noqa: BLE001
        return []


def _dead_snapshot() -> dict:
    """{symbol: {"m": misses, "u": wall-clock deadline}} for symbols paused right now."""
    from resilience.pause_state import mono_to_wall
    now = _time.monotonic()
    with _DEAD_LOCK:
        return {sym: {"m": int(ent[0]), "u": round(mono_to_wall(ent[1]), 1)}
                for sym, ent in _DEAD.items() if ent[1] > now}


def _persist_dead_state() -> None:
    if not _DEAD_PERSIST_READY:
        return
    try:
        from resilience.pause_state import schedule_flush
        schedule_flush(_DEAD_PERSIST_KEY, _dead_snapshot)
    except Exception:  # noqa: BLE001
        pass


def load_dead_symbols_from_db(db) -> None:
    """Call once at startup (main.py): restore pauses that are still running. Non-fatal."""
    global _DEAD_PERSIST_READY
    try:
        from resilience.pause_state import enabled, load_items, wall_to_mono
        if not enabled() or not _dead_cfg()[0]:
            return
        items = load_items(db, _DEAD_PERSIST_KEY)
        with _DEAD_LOCK:
            for sym, ent in items.items():
                try:
                    _DEAD[sym] = [max(1, int(ent.get("m", 1))), wall_to_mono(ent["u"])]
                except (TypeError, ValueError, KeyError):
                    continue
        _DEAD_PERSIST_READY = True
        if items:
            logger.info("feed: restored %d paused no-price symbol(s) from the DB (e.g. %s)",
                        len(items), ", ".join(sorted(items)[:5]))
    except Exception as e:  # noqa: BLE001
        logger.warning("load_dead_symbols_from_db failed (non-fatal): %s", e)


def clear_dead_symbols() -> None:
    """Forget every pause (tests, or an operator hook)."""
    with _DEAD_LOCK:
        _DEAD.clear()


async def get_quote(client: httpx.AsyncClient, symbol: str, *, for_display: bool = False,
                    timeout_scale: float = 1.0, skip_live_quote: bool = False) -> Optional[Tick]:
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
    _skip_live = skip_live_quote or (for_display and not _market_open_now())   # group171: skip_live_quote
    try:
        if _skip_live:
            raise _SkipLiveQuote()
        r_lq = await client.get(f"{MARKET_DATA_URL}/live-quote/{_path_sym(symbol)}", timeout=3.0 * timeout_scale)
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
                    _note_priced(symbol)
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
                        prev_close=_lq_prev_close(lq, ltp),
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

        r = await client.get(f"{MARKET_DATA_URL}/quote/{_path_sym(symbol)}", timeout=8.0 * timeout_scale)
        # the ATR refresh task runs in the background; we don't await it here.

        if r.status_code == 404:
            _note_no_data(symbol)   # group160: definite "unknown symbol" (delisted / renamed / not an equity)
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
            _note_no_data(symbol)   # group160: answered, but with nothing usable
            return None
        _note_priced(symbol)
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
            prev_close=_safe_prev_close(q.get("previous_close") or q.get("prev_close")),
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
# group243: the Candidates tab (limit 40) used to price its symbols with GET /live-quote + GET /quote each, ~80
# calls per poll right at the open, most of them shed by AngelOne's lane budget and sent on to Yahoo. Bulk first now.
FEED_DISPLAY_BULK = _env_on("FEED_DISPLAY_BULK")
FEED_DISPLAY_BULK_MIN_SYMBOLS = max(2, int(_env_float("FEED_DISPLAY_BULK_MIN_SYMBOLS", 8.0)))
FEED_DISPLAY_BULK_MAX_AGE_S = _env_float("FEED_DISPLAY_BULK_MAX_AGE_S", 60.0)   # market open; closed = FEED_PREVIEW_BULK_MAX_AGE_S
FEED_DISPLAY_LEFTOVER_MAX = int(_env_float("FEED_DISPLAY_LEFTOVER_MAX", 10.0))
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
            # group243: one chunked POST /quotes/bulk for the whole miss list first; only what bulk could not
            # price goes through the per-symbol cascade (and then without /live-quote, bulk read the same feeds),
            # capped at FEED_DISPLAY_LEFTOVER_MAX so a bulk miss never turns back into ~40 calls per poll.
            priced: dict[str, float] = {}
            bulk_answered = False
            if FEED_DISPLAY_BULK and len(misses2) >= FEED_DISPLAY_BULK_MIN_SYMBOLS:
                try:
                    _age = FEED_DISPLAY_BULK_MAX_AGE_S if _market_open_now() else FEED_PREVIEW_BULK_MAX_AGE_S
                    bstats: dict = {}
                    limits = httpx.Limits(max_connections=FEED_BULK_CONCURRENCY * 2)
                    async with httpx.AsyncClient(limits=limits) as bulk_client:
                        got = await _bulk_ticks(bulk_client, misses2, max_age_s=_age, stats=bstats, schedule_atr=False)
                    for sym in misses2:
                        t = got.get(_clean_sym(sym))
                        if t is not None and t.price and t.price > 0:
                            priced[sym] = float(t.price)
                    bulk_answered = not (bstats.get("failed") and not got)
                    logger.info("get_display_prices: bulk priced %d/%d symbol(s)", len(priced), len(misses2))
                except Exception as e:
                    logger.warning("get_display_prices: bulk pass failed (%s: %s) - per-symbol path", type(e).__name__, e)
                    priced, bulk_answered = {}, False
            left = [x for x in misses2 if x not in priced]
            attempted = left
            if bulk_answered and FEED_DISPLAY_LEFTOVER_MAX >= 0 and len(left) > FEED_DISPLAY_LEFTOVER_MAX:
                attempted = left[:FEED_DISPLAY_LEFTOVER_MAX]
                logger.info("get_display_prices: per-symbol lookups capped at %d of %d (FEED_DISPLAY_LEFTOVER_MAX)",
                            len(attempted), len(left))
            per_sym: dict[str, float | None] = {}
            if attempted:
                async def _one(client, sym):
                    return await get_quote(client, sym, for_display=True, skip_live_quote=bulk_answered)
                results = await _bounded_gather(attempted, _one, "get_display_prices")
                for sym, tick in zip(attempted, results):
                    per_sym[sym] = float(tick.price) if tick is not None and tick.price else None
            now = _time.monotonic()
            with _DISPLAY_CACHE_LOCK:
                if len(_DISPLAY_CACHE) > 4000:
                    _DISPLAY_CACHE.clear()
                for sym, price in priced.items():
                    _DISPLAY_CACHE[_clean_sym(sym)] = (now, price)
                    out[sym] = price
                for sym, price in per_sym.items():           # lookups that were not attempted stay uncached: retried next poll
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


def _tick_from_bulk_item(item, *, now: Optional[datetime] = None, max_age_s: Optional[float] = None) -> Optional[Tick]:
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
    if age_s > (FEED_BULK_MAX_AGE_S if max_age_s is None else max_age_s):
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
            prev_close=_safe_prev_close(item.get("previous_close") or item.get("prev_close")),
        )
    except (TypeError, ValueError):
        return None


def _bulk_reject_reason(item, *, now: Optional[datetime] = None, max_age_s: Optional[float] = None) -> str:
    """Why _tick_from_bulk_item turned this bulk row down: "no_price", "no_time" or "stale" (else "other")."""
    try:
        if not isinstance(item, dict):
            return "other"
        try:
            price = float(item.get("price") or item.get("cmp") or 0)
        except (TypeError, ValueError):
            return "no_price"
        if price <= 0 or price != price:
            return "no_price"
        ts = _parse_bulk_ts(item.get("fetched_at"))
        if ts is None:
            return "no_time"
        age_s = ((now or datetime.now(timezone.utc)) - ts).total_seconds()
        if age_s > (FEED_BULK_MAX_AGE_S if max_age_s is None else max_age_s):
            return "stale"
    except Exception:  # noqa: BLE001
        pass
    return "other"


async def _bulk_ticks(client: httpx.AsyncClient, symbols: list[str], *, timeout: Optional[float] = None,
                      max_age_s: Optional[float] = None, stats: Optional[dict] = None,
                      schedule_atr: bool = True, chunk_size: Optional[int] = None) -> dict[str, Tick]:
    """Best-effort chunked POST /quotes/bulk -> {symbol: Tick}. Never raises; a failed chunk just leaves
    its symbols out so the caller falls back to the per-symbol cascade for them.

    max_age_s: freshness limit for this call (default FEED_BULK_MAX_AGE_S).
    schedule_atr: False for dashboard-only callers (group243), which must not start ATR /history refreshes.
    chunk_size: symbols per request (default FEED_BULK_CHUNK_SIZE).
    stats (optional, filled in place): "chunks", "failed" (chunks that errored or answered non-200),
    "failed_symbols" (the symbols of those chunks, group248) and
    "reasons" ({no_price|no_time|stale|other: n}) for rows that were returned but not usable."""
    out: dict[str, Tick] = {}
    uniq = list(dict.fromkeys(s for s in symbols if s))
    if not uniq:
        return out
    _size = FEED_BULK_CHUNK_SIZE if not chunk_size else max(1, int(chunk_size))
    chunks = [uniq[i:i + _size] for i in range(0, len(uniq), _size)]
    sem = asyncio.Semaphore(FEED_BULK_CONCURRENCY)
    to = FEED_BULK_TIMEOUT_S if timeout is None else timeout

    if stats is not None:
        stats["chunks"] = len(chunks)
        stats.setdefault("failed", 0)
        stats.setdefault("reasons", {})

    def _failed(chunk: Optional[list] = None) -> None:
        if stats is not None:
            stats["failed"] = stats.get("failed", 0) + 1
            if chunk:
                stats.setdefault("failed_symbols", []).extend(chunk)

    async def _post(chunk: list[str]) -> None:
        async with sem:
            try:
                r = await client.post(f"{MARKET_DATA_URL}/quotes/bulk", json={"symbols": chunk}, timeout=to)
                if r.status_code != 200:
                    logger.warning("get_quotes: /quotes/bulk returned %d for %d symbol(s) — %s",
                                   r.status_code, len(chunk), r.text[:160])
                    _failed(chunk)
                    return
                body = r.json()
                now = datetime.now(timezone.utc)
                for item in (body.get("quotes") if isinstance(body, dict) else None) or []:
                    tick = _tick_from_bulk_item(item, now=now, max_age_s=max_age_s)
                    if tick is None and stats is not None:
                        why = _bulk_reject_reason(item, now=now, max_age_s=max_age_s)
                        stats["reasons"][why] = stats["reasons"].get(why, 0) + 1
                    if tick is not None:
                        out[tick.symbol] = tick
                        _note_priced(tick.symbol)
                        if schedule_atr:
                            _schedule_atr_refresh(client, tick.symbol)   # cold ATR -> background refresh, as get_quote()
            except Exception as e:
                _failed(chunk)
                logger.warning("get_quotes: /quotes/bulk chunk of %d failed: %s: %s", len(chunk), type(e).__name__, e)

    await asyncio.gather(*[_post(c) for c in chunks])
    return out


def _prio_shared_lookup(uniq: list[str]) -> dict[str, Tick]:
    """Ticks priced by a priority call within the last FEED_PRIORITY_SHARE_S seconds (clean symbol -> Tick)."""
    if FEED_PRIORITY_SHARE_S <= 0 or not _PRIO_SHARED:
        return {}
    now = _time.monotonic()
    out: dict[str, Tick] = {}
    with _PRIO_LOCK:
        for sym in uniq:
            ent = _PRIO_SHARED.get(_clean_sym(sym))
            if ent is not None and (now - ent[0]) <= FEED_PRIORITY_SHARE_S:
                out[sym] = ent[1]
    return out


def _prio_remember_last_good(ticks: dict[str, Tick]) -> None:
    """Keep the freshest priced tick per held symbol for the group225 fallback (bounded, memory-only)."""
    if FEED_PRIORITY_STALE_FALLBACK_S <= 0 or not ticks:
        return
    with _PRIO_LOCK:
        if len(_PRIO_LAST_GOOD) > 500:
            _PRIO_LAST_GOOD.clear()
        for sym, tick in ticks.items():
            if not str(tick.source).startswith("stale_last_good"):
                _PRIO_LAST_GOOD[_clean_sym(sym)] = tick


def _prio_last_good_fallback(wanted: list[str]) -> dict[str, Tick]:
    """Last good ticks (<= FEED_PRIORITY_STALE_FALLBACK_S old by their own as_of) for symbols the lane could not price."""
    if FEED_PRIORITY_STALE_FALLBACK_S <= 0 or not wanted:
        return {}
    now = datetime.now(timezone.utc)
    out: dict[str, Tick] = {}
    with _PRIO_LOCK:
        for sym in wanted:
            t = _PRIO_LAST_GOOD.get(_clean_sym(sym))
            if t is None:
                continue
            as_of = t.as_of if t.as_of.tzinfo else t.as_of.replace(tzinfo=timezone.utc)
            if (now - as_of).total_seconds() <= FEED_PRIORITY_STALE_FALLBACK_S:
                out[sym] = Tick(symbol=t.symbol, price=t.price, as_of=t.as_of, atr=t.atr,
                                source=f"stale_last_good({t.source})", volume=t.volume,
                                day_high=t.day_high, day_low=t.day_low, prev_close=t.prev_close)
    return out


def _prio_mark_distress() -> None:
    if FEED_BACKPRESSURE_S > 0:
        with _PRIO_LOCK:
            _PRIO_DISTRESS_UNTIL[0] = _time.monotonic() + FEED_BACKPRESSURE_S


def _prio_in_distress() -> bool:
    return _time.monotonic() < _PRIO_DISTRESS_UNTIL[0]


def _prio_shared_store(ticks: dict[str, Tick]) -> None:
    _prio_remember_last_good(ticks)
    if FEED_PRIORITY_SHARE_S <= 0 or not ticks:
        return
    now = _time.monotonic()
    with _PRIO_LOCK:
        if len(_PRIO_SHARED) > 500:
            _PRIO_SHARED.clear()
        for sym, tick in ticks.items():
            _PRIO_SHARED[_clean_sym(sym)] = (now, tick)


async def _priority_hedge(client: httpx.AsyncClient, todo: list[str], bulk_task: "asyncio.Future") -> tuple:
    """group252: run the per-symbol cascade for `todo` while `bulk_task` (still pending) keeps going. Returns
    (bulk_ticks_by_clean_symbol, per_symbol_ticks_by_requested_symbol). Stops as soon as every symbol has a tick from
    one source or both sources have finished; unfinished per-symbol lookups are cancelled. Never raises."""
    tasks = {s: asyncio.ensure_future(get_quote(client, s, timeout_scale=FEED_PRIORITY_TIMEOUT_SCALE)) for s in todo}
    per: dict = {}
    got: dict = {}
    try:
        while True:
            for sym, t in tasks.items():
                if t.done() and sym not in per and not t.cancelled() and t.exception() is None and isinstance(t.result(), Tick):
                    per[sym] = t.result()
            if bulk_task.done() and not got and not bulk_task.cancelled() and bulk_task.exception() is None:
                got = bulk_task.result() or {}
            covered = [s for s in todo if s in per or _clean_sym(s) in got]
            if len(covered) == len(todo):
                break
            waiting = {t for t in tasks.values() if not t.done()}
            if not bulk_task.done():
                waiting.add(bulk_task)
            if not waiting:
                break
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    except Exception as e:  # noqa: BLE001
        logger.debug("priority hedge ended early: %s", e)
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
    return got, per


async def _priority_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Priority lane for open positions: shared ticks, then bulk-first, then the per-symbol cascade on a
    dedicated client (own pool, scaled timeouts), then a /quotes/bulk fallback. See the group171 note."""
    uniq = list(dict.fromkeys(s for s in symbols if s))
    out: dict[str, Tick] = {}
    if not uniq:
        return out
    out.update(_prio_shared_lookup(uniq))
    todo = [s for s in uniq if s not in out]
    if not todo:
        return out
    limits = httpx.Limits(max_connections=max(8, len(todo) * 2), max_keepalive_connections=max(2, len(todo)))
    async with httpx.AsyncClient(limits=limits) as client:
        # 1) one bulk request for every held symbol that is not already shared
        _per_done = False
        if FEED_PRIORITY_BULK_FIRST and _time.monotonic() >= _PRIO_BULK_OFF_UNTIL[0]:
            stats: dict = {}
            bulk_task = asyncio.ensure_future(_bulk_ticks(client, todo, timeout=FEED_PRIORITY_BULK_TIMEOUT_S,
                                                          max_age_s=FEED_PRIORITY_BULK_MAX_AGE_S, stats=stats))
            got: dict = {}
            if FEED_PRIORITY_HEDGE_S > 0:
                await asyncio.wait({bulk_task}, timeout=FEED_PRIORITY_HEDGE_S)
            if bulk_task.done() or FEED_PRIORITY_HEDGE_S <= 0:
                got = await bulk_task
                if stats.get("failed") and not got:
                    with _PRIO_LOCK:
                        _PRIO_BULK_OFF_UNTIL[0] = _time.monotonic() + FEED_PRIORITY_BULK_COOLDOWN_S
                    logger.warning("priority quotes: bulk-first failed for %d symbol(s) — per-symbol path now, "
                                   "bulk-first paused for %.0fs", len(todo), FEED_PRIORITY_BULK_COOLDOWN_S)
            else:
                got, per = await _priority_hedge(client, todo, bulk_task)
                _per_done = True
                for sym, tick in per.items():
                    out[sym] = tick
                if not bulk_task.done() or (stats.get("failed") and not got):
                    with _PRIO_LOCK:
                        _PRIO_BULK_OFF_UNTIL[0] = _time.monotonic() + FEED_PRIORITY_BULK_COOLDOWN_S
                    logger.warning("priority quotes: bulk-first had not answered %d symbol(s) after %.1fs — per-symbol lookups "
                                   "started alongside it (%d priced per-symbol, %d from bulk), bulk-first paused for %.0fs",
                                   len(todo), FEED_PRIORITY_HEDGE_S, len(per), len(got), FEED_PRIORITY_BULK_COOLDOWN_S)
                if not bulk_task.done():
                    bulk_task.cancel()
            for want in todo:
                t = got.get(_clean_sym(want))
                if t is not None and want not in out:
                    out[want] = t
            todo = [s for s in todo if s not in out]
            if not todo:
                _prio_shared_store(out)
                return out
        # 2) per-symbol cascade for whatever is left (skipped when the hedge already tried every symbol)
        if not _per_done:
            ticks = await asyncio.gather(
                *[get_quote(client, s, timeout_scale=FEED_PRIORITY_TIMEOUT_SCALE) for s in todo],
                return_exceptions=True,
            )
            for sym, tick in zip(todo, ticks):
                if isinstance(tick, Tick):
                    out[sym] = tick
        missing = [s for s in todo if s not in out]
        if missing:
            # 3) last resort: bulk again with the longer (scaled) timeout and the normal age limit
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
            if still:
                _prio_mark_distress()     # only when a held symbol is STILL unpriced after every attempt
                stale = _prio_last_good_fallback(still)
                if stale:
                    out.update(stale)
                    logger.warning("priority quotes: %d held symbol(s) served from their last good tick (<= %.0fs old): %s",
                                   len(stale), FEED_PRIORITY_STALE_FALLBACK_S, sorted(stale)[:8])
    _prio_shared_store({s: t for s, t in out.items() if not str(t.source).startswith("stale_last_good")})
    return out


async def get_quotes(symbols: list[str], *, priority: bool = False, allow_stale: bool = False) -> dict[str, Tick]:
    """Bulk fetch over a shared client. Each distinct stock is fetched once however it is spelled
    (KOTAKBANK / kotakbank.ns / KOTAKBANK.NS, M&M / M%26M) and the tick is returned under every spelling that
    was asked for (group159).

    See _get_quotes_unique for the lanes.
    """
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    canon_of = {s: _clean_sym(s) for s in symbols}
    uniq = list(dict.fromkeys(c for c in canon_of.values() if c))
    if len(uniq) < len(set(symbols)):
        logger.debug("get_quotes: %d requested spellings collapse to %d distinct symbols",
                     len(set(symbols)), len(uniq))
    _kw = {"allow_stale": True} if allow_stale else {}      # only passed when asked for (older fakes take priority only)
    got = await _get_quotes_unique(uniq, priority=priority, **_kw) if uniq else {}
    for s, c in canon_of.items():
        t = got.get(c)
        if t is not None:
            out[s] = t
    return out


async def _get_quotes_unique(symbols: list[str], *, priority: bool = False, allow_stale: bool = False) -> dict[str, Tick]:
    """Bulk fetch over a shared client (symbols are already canonical and distinct).

    priority=True (open-position exit evaluation): dedicated pool, longer timeouts, /quotes/bulk fallback
    — see the group152 note by FEED_PRIORITY_TIMEOUT_SCALE.

    Default: above FEED_BULK_MIN_SYMBOLS symbols, chunked /quotes/bulk first (a few requests instead of one
    or two per symbol), then the per-symbol cascade only for symbols bulk could not price fresh. At or below
    that size it is the original concurrent per-symbol path.

    allow_stale=True (group249; watchlist trigger only): symbols still unpriced after the bulk pass reuse their last
    non-priority tick if it is at most FEED_LEFTOVER_STALE_S old, before any per-symbol lookup."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    if priority:
        return await _priority_quotes(symbols)

    paused = _paused_symbols(symbols)
    if paused:
        logger.debug("get_quotes: %d paused no-data symbol(s) left out: %s", len(paused), paused[:8])
        _p = set(paused)
        symbols = [x for x in symbols if x not in _p]
        if not symbols:
            return out

    todo = list(symbols)
    bulk_answered = False       # True once the bulk-first pass ran: its leftovers skip /live-quote (group171)
    # group246: bulk-first from FEED_SMALL_BATCH_BULK_MIN_SYMBOLS symbols (never above the large-batch limit)
    _bulk_first_min = min(FEED_BULK_MIN_SYMBOLS, FEED_SMALL_BATCH_BULK_MIN_SYMBOLS - 1)
    if len(set(symbols)) > _bulk_first_min:
        try:
            limits = httpx.Limits(max_connections=FEED_BULK_CONCURRENCY * 2)
            bulk_stats: dict = {}
            async with httpx.AsyncClient(limits=limits) as bulk_client:
                got = await _bulk_ticks(bulk_client, symbols, stats=bulk_stats)
                # group248: a chunk that timed out while others answered is asked again, in smaller pieces
                _lost = list(dict.fromkeys(bulk_stats.get("failed_symbols") or []))
                if FEED_BULK_RETRY_FAILED and _lost and got:
                    _rstats: dict = {}
                    _got2 = await _bulk_ticks(bulk_client, _lost, timeout=FEED_BULK_RETRY_TIMEOUT_S, stats=_rstats,
                                              chunk_size=FEED_BULK_RETRY_CHUNK_SIZE)
                    got.update(_got2)
                    logger.info("get_quotes: %d symbol(s) of failed bulk chunk(s) asked again in smaller bulk calls: "
                                "priced %d%s", len(_lost), len(_got2),
                                f", {_rstats['failed']} call(s) failed again" if _rstats.get("failed") else "")
                    bulk_stats["failed"] = _rstats.get("failed", 0)
            for sym in symbols:
                t = got.get(_clean_sym(sym))
                if t is not None:
                    out[sym] = t
            todo = [s for s in symbols if s not in out]
            _why = bulk_stats.get("reasons") or {}
            _unanswered = max(0, len(todo) - sum(_why.values()))
            logger.info("get_quotes: bulk-first priced %d/%d symbol(s); %d left for per-symbol lookups "
                        "(not in the answer %d, older than limit %d, no price %d, no timestamp %d%s)",
                        len(out), len(symbols), len(todo), _unanswered, _why.get("stale", 0),
                        _why.get("no_price", 0), _why.get("no_time", 0),
                        f", {bulk_stats['failed']} of {bulk_stats.get('chunks', 0)} chunk(s) failed"
                        if bulk_stats.get("failed") else "")
            bulk_answered = not (bulk_stats.get("failed") and not got)   # every chunk failed -> bulk told us nothing
        except Exception as e:
            logger.warning("get_quotes: bulk-first failed (%s: %s) — per-symbol path for all", type(e).__name__, e)
            out, todo = {}, list(symbols)
            bulk_answered = False

    if out:
        _wl_remember_last_good(out)
    if todo and allow_stale and FEED_LEFTOVER_STALE_S > 0:
        _stale = _wl_last_good_fallback(todo)
        if _stale:
            out.update(_stale)
            todo = [x for x in todo if x not in _stale]
            logger.info("get_quotes: %d unpriced symbol(s) served from their last good tick (<= %.0fs old, watchlist "
                        "trigger only); %d left for per-symbol lookups", len(_stale), FEED_LEFTOVER_STALE_S, len(todo))

    if todo and len(set(symbols)) > FEED_BULK_MIN_SYMBOLS and _prio_in_distress():
        # Large (watchlist-poll sized) batches only. A small batch - the <=20 entry candidates of a cycle - is never
        # starved: if a held symbol cannot be priced for its own reasons, entries must not stop with it.
        logger.info("get_quotes: %d per-symbol lookup(s) skipped for %.0fs — the priority lane (open positions) is "
                    "struggling and market-data is being left free for it", len(todo), FEED_BACKPRESSURE_S)
        todo = []
    elif todo and FEED_LEFTOVER_MAX > 0 and len(todo) > FEED_LEFTOVER_MAX:
        logger.info("get_quotes: per-symbol lookups capped at %d of %d (FEED_LEFTOVER_MAX); the rest wait for the next cycle",
                    FEED_LEFTOVER_MAX, len(todo))
        todo = todo[:FEED_LEFTOVER_MAX]
    if todo:
        if bulk_answered and FEED_LEFTOVER_SKIP_LIVE:
            async def _no_live(client, sym):
                return await get_quote(client, sym, skip_live_quote=True)
            results = await _bounded_gather(todo, _no_live, "get_quotes")
        else:
            results = await _bounded_gather(todo, get_quote, "get_quotes")
        _fresh: dict = {}
        for sym, tick in zip(todo, results):
            if tick is not None:
                out[sym] = tick
                _fresh[sym] = tick
        _wl_remember_last_good(_fresh)
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


# group233: preview prices (after-hours news-scan validation, the dashboard's "waiting at" preview) used to send
# GET /quote/{sym} then GET /last-close/{sym} for EVERY symbol - hundreds of per-symbol calls, each one a
# potential AngelOne-lane shed and Yahoo fallback. Now: chunked POST /quotes/bulk first (any age is fine for a
# non-tradeable preview; market-data answers a closed market from the last close), then GET /last-close/{sym}
# (cache / bhavcopy only, never AngelOne or Yahoo) for the symbols bulk did not price, and GET /quote/{sym} only
# for at most FEED_PREVIEW_QUOTE_FALLBACK_MAX of the rest (0 = never; a brand-new listing no cache knows yet).
FEED_PREVIEW_BULK_MAX_AGE_S = float(((os.getenv("FEED_PREVIEW_BULK_MAX_AGE_S") or "").strip() or str(7 * 86400)))
FEED_PREVIEW_QUOTE_FALLBACK_MAX = max(0, int(((os.getenv("FEED_PREVIEW_QUOTE_FALLBACK_MAX") or "").strip() or "5")))


async def _get_preview_last_close(client: httpx.AsyncClient, symbol: str) -> Optional[Tick]:
    """Preview price from GET /last-close/{sym} only (no AngelOne / Yahoo behind it)."""
    try:
        r = await client.get(f"{MARKET_DATA_URL}/last-close/{_path_sym(symbol)}", timeout=8.0)
        if r.status_code != 200:
            return None
        q = r.json()
        px = q.get("price") or q.get("close") or q.get("previous_close")
        if px and float(px) > 0:
            return Tick(symbol=symbol, price=float(px), as_of=datetime.now(timezone.utc),
                        atr=_cached_atr(symbol), source="preview:last_close")
    except Exception as e:
        logger.debug("get_preview(%s) via /last-close failed: %s", symbol, e)
    return None


async def get_preview_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Bulk best-effort preview (last-close) prices — see _get_preview. Only
    call this for symbols that came back empty from get_quotes()."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    uniq = list(dict.fromkeys(s for s in symbols if s))

    # 1) one chunked POST /quotes/bulk (age does not matter for a preview)
    got: dict[str, Tick] = {}
    try:
        limits = httpx.Limits(max_connections=FEED_BULK_CONCURRENCY * 2)
        async with httpx.AsyncClient(limits=limits) as bulk_client:
            got = await _bulk_ticks(bulk_client, uniq, max_age_s=FEED_PREVIEW_BULK_MAX_AGE_S)
    except Exception as e:
        logger.warning("get_preview_quotes: bulk pass failed (%s: %s) - /last-close path for all", type(e).__name__, e)
    for sym in uniq:
        t = got.get(_clean_sym(sym))
        if t is not None and t.price and t.price > 0:
            out[sym] = Tick(symbol=sym, price=float(t.price), as_of=datetime.now(timezone.utc),
                            atr=_cached_atr(sym), source="preview:last_close")

    # 2) /last-close for what bulk could not price (cheap: cache / bhavcopy only)
    left = [s for s in uniq if s not in out]
    if left:
        results = await _bounded_gather(left, _get_preview_last_close, "get_preview_quotes(last-close)")
        for sym, tick in zip(left, results):
            if tick is not None:
                out[sym] = tick

    # 3) a few per-symbol /quote calls at most, for names no cache or bhavcopy knows
    left = [s for s in uniq if s not in out][:FEED_PREVIEW_QUOTE_FALLBACK_MAX]
    if left:
        results = await _bounded_gather(left, _get_preview, "get_preview_quotes(quote)")
        for sym, tick in zip(left, results):
            if tick is not None:
                out[sym] = tick
    return out
