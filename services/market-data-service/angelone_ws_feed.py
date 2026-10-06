"""
services/market-data-service/angelone_ws_feed.py  §1 — AngelOne live tick feed.

IMPLEMENTATION NOTE: despite the filename (kept for compatibility with
callers written against it), this is a REST-polling feed, not a true
persistent WebSocket. A previous version of this file was a skeleton that
logged "subscribed" once and then only slept — it never opened a
connection, never resolved symbol tokens, and never called the upsert
function, so live_quotes/_LIVE was never actually populated no matter how
long the process ran. Building a correct SmartWebSocketV2 client (binary
frame parsing, resubscribe/heartbeat handling) is a larger undertaking
than is safe to hand-roll quickly for a system that feeds real trading
decisions — REST polling using angelone_client.get_quotes_batch(), which
is a complete and already-tested method, is the safer path to something
that actually works today. Swap in a true WS client later if per-tick
latency becomes the bottleneck; nothing here needs to change for callers
if you do (get_live_quote / get_live_quotes_bulk / start_feed_background
keep the same signatures).

Populates BOTH the in-memory _LIVE cache (same-process reads, e.g. this
service's own /quote endpoint) AND the live_quotes DB table (cross-service
reads, e.g. real-trade-service/market_feed/feed.py) on every poll cycle.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from market_hours import is_feed_window_ist


_UNRESOLVED_NAMES_MAX = 20


def _unresolved_suffix(symbols, token_map) -> str:
    """Group 124: the "resolved 248/250" warning never said WHICH symbols had no
    AngelOne token (usually delisted/renamed names such as AAKASH). Returns
    ": SYM1, SYM2, ..." (at most 20, then "+N more"), or "" when nothing is missing
    or the names cannot be worked out. Never raises."""
    try:
        have = set(token_map)
        missing = sorted({
            (str(x or "").upper().replace(".NS", "").replace(".BO", "").strip())
            for x in symbols
        } - have - {""})
        if not missing:
            return ""
        shown = ", ".join(missing[:_UNRESOLVED_NAMES_MAX])
        extra = len(missing) - _UNRESOLVED_NAMES_MAX
        return f"; unresolved: {shown}" + (f" (+{extra} more)" if extra > 0 else "")
    except Exception:
        return ""

logger = logging.getLogger("angelone-ws-feed")

# Group 148: the scrip master downloads in the background after boot, so the feed thread normally
# finds it "not loaded yet" for the first few tries (10 s, 20 s, 30 s). That was logged at ERROR on
# EVERY boot even though it resolves itself. Early attempts are now WARNING; ERROR only once the wait
# has gone on past _SCRIP_WAIT_ERROR_AFTER attempts (about a minute) and is a real problem.
_SCRIP_WAIT_ERROR_AFTER = 3


def _scrip_wait_log_level(attempt: int) -> int:
    return logging.WARNING if attempt <= _SCRIP_WAIT_ERROR_AFTER else logging.ERROR

_running = False
_thread: Optional[threading.Thread] = None
# 2026-10-04 (log-audit item 26): generation counter. Every start_feed_background()
# bumps it and the new thread captures its own number; a thread whose number is no
# longer current is "superseded" and exits at its next check. Before this, a thread
# that outlived stop_feed_background()'s join timeout was RE-ENABLED by the next
# start (which set the shared _running flag back to True) so two feed threads
# polled at once, and the old thread's `finally` then cleared _running under the
# new one.
_generation = 0

# How often an idling (outside market hours) loop rechecks whether the
# window has opened. Cheap — just a datetime compare, no upstream call.
IDLE_RECHECK_S = 60.0

# How often a full poll cycle restarts once it finishes the whole universe.
# The file itself only updates once/day, but this governs freshness of
# live_quotes during market hours.
POLL_INTERVAL_S = float(((os.getenv("ANGELONE_POLL_INTERVAL_S") or "").strip() or "3.0"))
# Small pause between successive batches within one cycle, so a large
# universe doesn't fire every batch back-to-back with zero spacing.
BATCH_GAP_S = float(((os.getenv("ANGELONE_BATCH_GAP_S") or "").strip() or "0.35"))
BATCH_SIZE = 50  # AngelOne's documented per-request token cap for this endpoint

# group153: live_quotes used to be written ONE ROW PER TRANSACTION (491 sequential
# MERGE/INSERT round trips per poll cycle on a 491-symbol universe, on the polling
# thread itself), so on a slow Oracle link the DB writes - not AngelOne - stretched a
# cycle past the 20 s freshness window real-trade-service applies to live_quotes rows,
# and every position/candidate then fell through to the slow per-symbol /quote route.
# Rows of one AngelOne batch (<= 50) are now written in ONE transaction (executemany).
# ANGELONE_FEED_DB_BATCH=0 restores the old per-row writes.
DB_BATCH_WRITES = ((os.getenv("ANGELONE_FEED_DB_BATCH") or "").strip() or "1") not in ("0", "false", "False")
# A poll cycle longer than this logs one WARNING (at most every SLOW_CYCLE_LOG_EVERY_S):
# it means live_quotes rows are about to go stale for the symbols polled first.
SLOW_CYCLE_WARN_S = float(((os.getenv("ANGELONE_FEED_SLOW_CYCLE_WARN_S") or "").strip() or "15.0"))
SLOW_CYCLE_LOG_EVERY_S = 300.0
_last_cycle_s: Optional[float] = None
_last_slow_cycle_log = 0.0

# group211: the poll no longer sends the whole universe every cycle. Symbols held in an open position and
# symbols recently asked for through /quote (the "hot" set, see angelone_budget.py) are polled EVERY cycle in
# their own priority lanes; the rest of the universe is refreshed at most every FEED_COLD_INTERVAL_S seconds
# as BACKGROUND (which keeps a token reserve free for the lanes above it). 0 = the old behaviour: every
# symbol every cycle, no lanes. ANGELONE_FEED_HOT_MAX caps the hot (non-held) set.
FEED_COLD_INTERVAL_S = float(((os.getenv("ANGELONE_FEED_COLD_INTERVAL_S") or "").strip() or "30"))
FEED_HOT_MAX = int(float(((os.getenv("ANGELONE_FEED_HOT_MAX") or "").strip() or "100")))
_last_cold_poll = 0.0


def _plan_batches(tokens: list, token_map: dict, now: Optional[float] = None) -> list:
    """group211: this cycle's [(token_batch, lane), ...]. Held symbols first (POSITION), then recently asked-for
    symbols (CANDIDATE), then - only when FEED_COLD_INTERVAL_S has passed since the last time - the rest of the
    universe (BACKGROUND). Falls back to the old plan (all tokens, lane None) when the cold interval is 0, the
    budget is off or anything goes wrong. Never raises."""
    global _last_cold_poll
    old_plan = [(tokens[i:i + BATCH_SIZE], None) for i in range(0, len(tokens), BATCH_SIZE)]
    try:
        import angelone_budget as _b
        if FEED_COLD_INTERVAL_S <= 0 or not _b.enabled():
            return old_plan
        now = time.time() if now is None else now
        held_syms = [c for c in sorted(_b.position_symbols()) if c in token_map]
        held_set = set(held_syms)
        hot_syms = [c for c in _b.hot_symbols(max(0, FEED_HOT_MAX)) if c in token_map and c not in held_set]
        held_tok = [token_map[c] for c in held_syms]
        hot_tok = [token_map[c] for c in hot_syms]
        taken = set(held_tok) | set(hot_tok)
        plan = []
        for toks, lane in ((held_tok, _b.POSITION), (hot_tok, _b.CANDIDATE)):
            for i in range(0, len(toks), BATCH_SIZE):
                plan.append((toks[i:i + BATCH_SIZE], lane))
        if _last_cold_poll <= 0 or (now - _last_cold_poll) >= FEED_COLD_INTERVAL_S:
            cold = [t for t in tokens if t not in taken]
            for i in range(0, len(cold), BATCH_SIZE):
                plan.append((cold[i:i + BATCH_SIZE], _b.BACKGROUND))
            _last_cold_poll = now
        return plan
    except Exception as e:  # noqa: BLE001
        logger.debug("angelone feed: batch plan failed, polling everything: %s", e)
        return old_plan


# In-memory dict — same pattern as yahoo_ws_feed.py
_LIVE: Dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()


def _clean(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _get_live_quotes_engine():
    """Reuse market-data-service's existing, already-working dual-dialect
    (Oracle Autonomous DB + Neon/Postgres) durable engine from kv_cache.py,
    rather than `from db import get_engine` — that module does not exist
    in this service (it's a real-trade-service-only file); importing it
    here would raise ModuleNotFoundError on every call. Returns
    (engine_or_None, dialect_str)."""
    try:
        from kv_cache import _get_neon, _dialect
        return _get_neon(), _dialect()
    except Exception as e:
        logger.debug("live_quotes: could not get durable engine: %s", e)
        return None, "postgresql"


_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema(engine, dialect: str) -> None:
    """Create live_quotes (+ index) if missing. Idempotent on both
    dialects — mirrors kv_cache._init_durable_schema's exact pattern for
    creating durable tables safely on Oracle vs Postgres. Nothing in this
    codebase ever ran migrations/live_quotes.sql, so without this the
    table may simply not exist yet on a fresh deploy."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            if dialect == "oracle":
                import oracle_compat as _oc
                _oc.exec_ddl_safe(
                    engine,
                    "CREATE TABLE live_quotes ("
                    "symbol VARCHAR2(32) PRIMARY KEY, "
                    "ltp NUMBER, "
                    "ohlc_json CLOB, "
                    "volume NUMBER, "
                    "source VARCHAR2(32), "
                    "updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)",
                    "oracle",
                )
                _oc.exec_ddl_safe(
                    engine,
                    "CREATE INDEX ix_live_quotes_updated ON live_quotes (updated_at)",
                    "oracle",
                )
            else:
                from sqlalchemy import text
                with engine.begin() as conn:
                    conn.execute(text(
                        "CREATE TABLE IF NOT EXISTS live_quotes ("
                        "symbol TEXT PRIMARY KEY, "
                        "ltp NUMERIC, "
                        "ohlc_json JSONB, "
                        "volume BIGINT, "
                        "source TEXT, "
                        "updated_at TIMESTAMPTZ DEFAULT now())"
                    ))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_live_quotes_updated ON live_quotes (updated_at)"
                    ))
            _schema_ready = True
        except Exception as e:
            logger.warning("live_quotes: schema ensure failed (non-fatal, will retry next call): %s", e)


def _upsert_tick_sync(sym: str, ltp, o, h, l, c, vol) -> None:
    """Write one row into live_quotes. Dual-dialect: Oracle needs a MERGE
    (no ON CONFLICT support), Postgres uses ON CONFLICT DO UPDATE."""
    if not sym or not ltp:
        return
    engine, dialect = _get_live_quotes_engine()
    if engine is None:
        return  # no durable DB configured — in-memory _LIVE cache still works fine
    _ensure_schema(engine, dialect)
    try:
        from sqlalchemy import text as _text
        ohlc = json.dumps({"open": o, "high": h, "low": l, "close": c or ltp})
        if dialect == "oracle":
            sql = (
                "MERGE INTO live_quotes d USING ("
                "SELECT :s AS symbol, :l AS ltp, :o AS ohlc_json, :v AS volume FROM dual"
                ") s ON (d.symbol = s.symbol) "
                "WHEN MATCHED THEN UPDATE SET d.ltp = s.ltp, d.ohlc_json = s.ohlc_json, "
                "d.volume = s.volume, d.source = 'angelone', d.updated_at = SYSTIMESTAMP "
                "WHEN NOT MATCHED THEN INSERT (symbol, ltp, ohlc_json, volume, source, updated_at) "
                "VALUES (s.symbol, s.ltp, s.ohlc_json, s.volume, 'angelone', SYSTIMESTAMP)"
            )
        else:
            sql = (
                "INSERT INTO live_quotes (symbol, ltp, ohlc_json, volume, source, updated_at) "
                "VALUES (:s, :l, :o, :v, 'angelone', now()) "
                "ON CONFLICT (symbol) DO UPDATE "
                "SET ltp=EXCLUDED.ltp, ohlc_json=EXCLUDED.ohlc_json, "
                "volume=EXCLUDED.volume, source=EXCLUDED.source, updated_at=now()"
            )
        with engine.begin() as conn:
            conn.execute(_text(sql), {"s": sym, "l": float(ltp), "o": ohlc, "v": int(vol or 0)})
    except Exception as e:
        logger.debug("live_quotes upsert failed for %s (non-fatal): %s", sym, e)


def _upsert_ticks_batch_sync(rows: list) -> None:
    """group153: write many live_quotes rows in ONE transaction (executemany).

    `rows` is a list of (sym, ltp, o, h, l, c, vol) tuples, the same arguments
    _upsert_tick_sync takes. Rows without a symbol/price are skipped, like the
    single-row writer. Never raises (the in-memory cache is the primary store)."""
    clean = [r for r in rows if r and r[0] and r[1]]
    if not clean:
        return
    engine, dialect = _get_live_quotes_engine()
    if engine is None:
        return
    _ensure_schema(engine, dialect)
    try:
        from sqlalchemy import text as _text
        params = []
        for sym, ltp, o, h, l, c, vol in clean:
            params.append({
                "s": sym,
                "l": float(ltp),
                "o": json.dumps({"open": o, "high": h, "low": l, "close": c or ltp}),
                "v": int(vol or 0),
            })
        if dialect == "oracle":
            sql = (
                "MERGE INTO live_quotes d USING ("
                "SELECT :s AS symbol, :l AS ltp, :o AS ohlc_json, :v AS volume FROM dual"
                ") s ON (d.symbol = s.symbol) "
                "WHEN MATCHED THEN UPDATE SET d.ltp = s.ltp, d.ohlc_json = s.ohlc_json, "
                "d.volume = s.volume, d.source = 'angelone', d.updated_at = SYSTIMESTAMP "
                "WHEN NOT MATCHED THEN INSERT (symbol, ltp, ohlc_json, volume, source, updated_at) "
                "VALUES (s.symbol, s.ltp, s.ohlc_json, s.volume, 'angelone', SYSTIMESTAMP)"
            )
        else:
            sql = (
                "INSERT INTO live_quotes (symbol, ltp, ohlc_json, volume, source, updated_at) "
                "VALUES (:s, :l, :o, :v, 'angelone', now()) "
                "ON CONFLICT (symbol) DO UPDATE "
                "SET ltp=EXCLUDED.ltp, ohlc_json=EXCLUDED.ohlc_json, "
                "volume=EXCLUDED.volume, source=EXCLUDED.source, updated_at=now()"
            )
        with engine.begin() as conn:
            conn.execute(_text(sql), params)
    except Exception as e:
        logger.debug("live_quotes batch upsert of %d rows failed (non-fatal): %s", len(clean), e)


def _on_tick_sync(tick: dict, write_db: bool = True):
    """Update the in-memory cache AND the live_quotes DB row for one tick.

    group153: with write_db=False only the in-memory cache is updated and the
    (sym, ltp, o, h, l, c, vol) tuple is returned so the caller can write a whole
    batch in one transaction (_upsert_ticks_batch_sync)."""
    sym = _clean(tick.get("tradingSymbol") or tick.get("symbol") or "")
    if not sym:
        return None
    ltp = tick.get("ltp") or tick.get("last_price")
    o, h, l, c = tick.get("open"), tick.get("high"), tick.get("low"), tick.get("close")
    vol = tick.get("tradeVolume") or tick.get("volume")
    with _LIVE_LOCK:
        _LIVE[sym] = {
            "symbol":     sym,
            "price":      ltp,
            "open":       o,
            "high":       h,
            "low":        l,
            "close":      c,
            "volume":     vol,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ts":         time.time(),   # cheap float for staleness math, mirrors yahoo_ws_feed.py
            "source":     "angelone_ws",
        }
    if not write_db:
        return (sym, ltp, o, h, l, c, vol)
    _upsert_tick_sync(sym, ltp, o, h, l, c, vol)
    return None


def get_live_quote(symbol: str, max_age_sec: float = 20.0) -> Optional[dict]:
    """Instant, zero-HTTP quote lookup. Returns None (never a stale value)
    if we've never gotten a tick for this symbol, or the last tick is
    older than max_age_sec — caller should fall through to REST/Yahoo in
    that case. Mirrors yahoo_ws_feed.get_live_quote()'s exact contract so
    both feeds behave identically to every caller."""
    sym = _clean(symbol)
    with _LIVE_LOCK:
        q = _LIVE.get(sym)
    if not q:
        return None
    if time.time() - q["ts"] > max_age_sec:
        return None
    return dict(q)


def get_live_quotes_bulk(symbols: list, max_age_sec: float = 20.0) -> Dict[str, dict]:
    out = {}
    for s in symbols:
        q = get_live_quote(s, max_age_sec=max_age_sec)
        if q:
            out[q["symbol"]] = q
    return out


def feed_status() -> dict:
    with _LIVE_LOCK:
        n = len(_LIVE)
        newest = max((q["ts"] for q in _LIVE.values()), default=None)
    return {
        "running": _running,
        "cached_symbols": n,
        "newest_tick_age_s": (time.time() - newest) if newest else None,
        "in_market_window": is_feed_window_ist(),
        "last_cycle_s": _last_cycle_s,
    }


def _note_cycle(elapsed_s: float, n_symbols: int) -> None:
    """group153: remember the last full-cycle wall time and warn (rate-limited) when it
    exceeds SLOW_CYCLE_WARN_S, i.e. when early-polled symbols will be older than the
    20 s freshness window by the time the cycle ends."""
    global _last_cycle_s, _last_slow_cycle_log
    _last_cycle_s = elapsed_s
    if elapsed_s > SLOW_CYCLE_WARN_S:
        now = time.time()
        if now - _last_slow_cycle_log >= SLOW_CYCLE_LOG_EVERY_S:
            _last_slow_cycle_log = now
            logger.warning(
                "AngelOne feed: a poll cycle over %d symbols took %.1fs (> %.0fs) - live_quotes rows "
                "polled early in the cycle go stale before the next pass; check DB write latency "
                "(ANGELONE_FEED_DB_BATCH) and AngelOne batch latency",
                n_symbols, elapsed_s, SLOW_CYCLE_WARN_S,
            )


def start_feed_background(symbols: list) -> None:
    """
    Start the AngelOne polling feed in a background thread.
    Idempotent — safe to call multiple times.
    """
    global _running, _thread, _generation
    if _running and _thread and _thread.is_alive():
        return
    if _thread is not None and _thread.is_alive():
        logger.warning(
            "AngelOne feed: previous thread is still winding down — it is superseded "
            "and will exit at its next check; starting the new feed thread"
        )
    _generation += 1
    my_gen = _generation
    _running = True

    def _current() -> bool:
        """True while THIS thread is the live feed (not stopped, not superseded)."""
        return _running and _generation == my_gen

    def _run():
        global _running
        try:
            from angelone_client import get_session
            import angelone_scrip_master as scrip_master

            session = get_session()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 2026-09-21 fix: this used to resolve tokens exactly ONCE and, if the
            # scrip master hadn't loaded at that instant (routine right after a
            # redeploy — the download competes with every other startup call),
            # log an error and `return`, permanently killing the AngelOne feed
            # thread. Nothing restarted it until the 15-min universe refresh, so
            # every live_quotes row went stale and real-trade-service's quote
            # path fell through to the slow per-symbol /quote route for
            # everything. Retry with backoff while the scrip master itself is
            # still not loaded; only give up when it IS loaded but genuinely
            # resolves none of the requested symbols.
            token_map: dict = {}
            attempt = 0
            while _current():
                token_map = scrip_master.get_tokens_bulk(symbols, wait_s=5.0)   # {clean_symbol: token}
                if token_map:
                    break
                if scrip_master.status().get("loaded_symbols", 0) > 0:
                    logger.error(
                        "AngelOne feed: scrip master resolved 0/%d requested symbols to tokens — "
                        "feed will not produce any ticks. Check ANGELONE_SCRIP_MASTER_URL is "
                        "reachable and its schema hasn't changed.",
                        len(symbols),
                    )
                    return
                attempt += 1
                delay = min(60.0, 10.0 * attempt)
                logger.log(
                    _scrip_wait_log_level(attempt),
                    "AngelOne feed: scrip master not loaded yet (attempt %d) — retrying in %.0fs",
                    attempt, delay,
                )
                slept = 0.0
                while _current() and slept < delay:   # short slices so stop_feed_background() isn't held up
                    time.sleep(1.0)
                    slept += 1.0
            if not token_map:
                return   # stop requested before tokens ever resolved
            if len(token_map) < len(symbols):
                logger.warning(
                    "AngelOne feed: resolved %d/%d requested symbols to tokens "
                    "(unresolved ones will simply never appear in live_quotes; "
                    "the quote waterfall falls through to Yahoo for those)%s",
                    len(token_map), len(symbols), _unresolved_suffix(symbols, token_map),
                )
            reverse_map = {v: k for k, v in token_map.items()}   # token -> clean symbol
            tokens = list(token_map.values())
            logger.info("AngelOne feed: polling %d resolved symbols every ~%.1fs", len(tokens), POLL_INTERVAL_S)

            async def _poll_cycle():
                # 2026-09-21 fix (same class of bug as the scrip-master retry
                # above, and the direct cause of that session's ReadTimeout
                # storm): ensure_session() used to be called with no
                # try/except here, OUTSIDE the get_quotes_batch try/except
                # below. Any exception it raised — including the
                # AngelOneSession cross-event-loop lock bug fixed in
                # angelone_client.py this session — propagated all the way
                # up through _poll_forever() to the outer handler at the
                # bottom of _run(), which set _running = False and let this
                # daemon thread die for good. Nothing ever restarted it:
                # live_quotes went stale permanently, and every dependent
                # service's quote lookups fell through to the slow per-symbol
                # yfinance /quote path for the ENTIRE universe on every
                # cycle — exactly the wall of "get_quote(...) failed:
                # ReadTimeout" lines seen across real-trade-service and
                # position-stocks-service. Skipping just this cycle on a
                # session-refresh failure (and retrying next cycle) keeps
                # one bad refresh from taking the whole feed down.
                try:
                    await session.ensure_session()
                except Exception as e:
                    logger.error(
                        "AngelOne feed: ensure_session() failed (%s: %s) — "
                        "skipping this poll cycle, will retry next cycle",
                        type(e).__name__, e,
                    )
                    return
                try:   # group211: every AngelOne caller is skipping AngelOne - do not walk the batches for nothing
                    import angelone_budget as _bud
                    if _bud.in_global_cooldown():
                        return
                except Exception:  # noqa: BLE001
                    pass
                i = 0
                for batch, lane in _plan_batches(tokens, token_map):
                    if not _current():
                        return
                    i += len(batch)
                    try:
                        if lane is None:
                            fetched = await session.get_quotes_batch("NSE", batch)
                        else:
                            fetched = await session.get_quotes_batch("NSE", batch, lane=lane)
                        if not _current():
                            return   # superseded/stopped while the request was in flight: drop the stale ticks
                        db_rows = []
                        for row in fetched:
                            tok = str(row.get("symbolToken") or "")
                            sym = reverse_map.get(tok)
                            if not sym:
                                continue
                            db_row = _on_tick_sync({
                                "tradingSymbol": sym,
                                "ltp":           row.get("ltp"),
                                "open":          row.get("open"),
                                "high":          row.get("high") or row.get("tradeHigh"),
                                "low":           row.get("low") or row.get("tradeLow"),
                                "close":         row.get("close"),
                                "tradeVolume":   row.get("tradeVolume") or row.get("volume"),
                            }, write_db=not DB_BATCH_WRITES)
                            if db_row is not None:
                                db_rows.append(db_row)
                        if db_rows:
                            # One transaction for the whole AngelOne batch, off the poll loop
                            # so the DB round trip does not block the event loop.
                            await asyncio.to_thread(_upsert_ticks_batch_sync, db_rows)
                    except Exception as e:
                        # BUG FIX (2026-09-17): httpx timeout exceptions
                        # stringify to "" — bare `e` produced "quote batch
                        # (X-Y) failed:" with no indication of timeout vs.
                        # connection error vs. anything else. Fall back to
                        # the exception's class name when str(e) is empty.
                        logger.warning(
                            "AngelOne quote batch (%d-%d) failed: %s",
                            i - len(batch), i, str(e) or type(e).__name__,
                        )
                    await asyncio.sleep(BATCH_GAP_S)

            async def _sleep_while_current(total_s: float) -> None:
                """Sleep in 1s slices so stop_feed_background() (10s join) is honoured
                even during the 60s off-hours idle wait — that single 60s sleep was why
                the thread 'did not stop within 10.0s' on every off-hours restart."""
                end = time.time() + total_s
                while _current():
                    left = end - time.time()
                    if left <= 0:
                        return
                    await asyncio.sleep(min(1.0, left))

            async def _poll_forever():
                was_idle = False
                while _current():
                    # 2026-09-01 fix: this loop used to poll AngelOne for the
                    # whole universe every ~3s, 24/7, with no market-hours
                    # awareness — the trading-decision loop (auto_pilot.py)
                    # was already correctly gated to market hours, but this
                    # background tick feed was not, which is what showed up
                    # in logs as AngelOne/yfinance activity pre-market with
                    # "nothing running." Idle outside the window instead.
                    if not is_feed_window_ist():
                        if not was_idle:
                            logger.info(
                                "AngelOne feed: outside market hours (IST) — idling, "
                                "rechecking every %.0fs", IDLE_RECHECK_S,
                            )
                            was_idle = True
                        await _sleep_while_current(IDLE_RECHECK_S)
                        continue
                    if was_idle:
                        logger.info("AngelOne feed: market window open — resuming polling")
                        was_idle = False
                    cycle_start = time.time()
                    await _poll_cycle()
                    elapsed = time.time() - cycle_start
                    _note_cycle(elapsed, len(tokens))
                    # Note: a full cycle's wall time scales with universe size
                    # (len(tokens)/BATCH_SIZE batches * BATCH_GAP_S, plus network
                    # latency per batch). For a large universe this can exceed
                    # POLL_INTERVAL_S and even the default max_age_sec staleness
                    # window on get_live_quote() for symbols polled early in the
                    # cycle. If logs show frequent staleness fallthrough, narrow
                    # `symbols` passed into start_feed_background() to the
                    # trade-critical set (open positions + watchlist) rather than
                    # the full scan universe, or raise max_age_sec/LIVE_QUOTE_MAX_AGE_S.
                    if elapsed < POLL_INTERVAL_S:
                        await _sleep_while_current(POLL_INTERVAL_S - elapsed)

            loop.run_until_complete(_poll_forever())
        except Exception as e:
            logger.error("AngelOne feed error: %s", e)
        finally:
            # Only the CURRENT thread may clear the shared flag; a superseded
            # thread exiting late must not stop the thread that replaced it.
            if _generation == my_gen:
                _running = False

    _thread = threading.Thread(target=_run, daemon=True, name="angelone-ws-feed")
    _thread.start()
    logger.info("AngelOne feed background thread started (%d symbols requested)", len(symbols))


def stop_feed_background(timeout: float = 10.0) -> None:
    """Signal the polling loop to stop and block until the thread has
    actually exited (or `timeout` elapses). Always call this before a
    subsequent start_feed_background() with a different symbol list —
    calling start_feed_background() again while the old thread is still
    winding down races on the `_running` flag (the old thread's
    `finally: _running = False` would stomp a freshly-started new thread's
    state)."""
    global _running
    _running = False
    if _thread is not None:
        _thread.join(timeout=timeout)
        if _thread.is_alive():
            logger.warning(
                "AngelOne feed: thread did not stop within %.1fs (a following "
                "start_feed_background() supersedes it, so only one feed thread polls)",
                timeout,
            )
