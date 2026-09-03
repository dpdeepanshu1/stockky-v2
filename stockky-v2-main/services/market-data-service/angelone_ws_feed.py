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

logger = logging.getLogger("angelone-ws-feed")

_running = False
_thread: Optional[threading.Thread] = None

# How often an idling (outside market hours) loop rechecks whether the
# window has opened. Cheap — just a datetime compare, no upstream call.
IDLE_RECHECK_S = 60.0

# How often a full poll cycle restarts once it finishes the whole universe.
# The file itself only updates once/day, but this governs freshness of
# live_quotes during market hours.
POLL_INTERVAL_S = float(os.getenv("ANGELONE_POLL_INTERVAL_S", "3.0"))
# Small pause between successive batches within one cycle, so a large
# universe doesn't fire every batch back-to-back with zero spacing.
BATCH_GAP_S = float(os.getenv("ANGELONE_BATCH_GAP_S", "0.35"))
BATCH_SIZE = 50  # AngelOne's documented per-request token cap for this endpoint

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


def _on_tick_sync(tick: dict) -> None:
    """Update the in-memory cache AND the live_quotes DB row for one tick."""
    sym = _clean(tick.get("tradingSymbol") or tick.get("symbol") or "")
    if not sym:
        return
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
    _upsert_tick_sync(sym, ltp, o, h, l, c, vol)


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
    }


def start_feed_background(symbols: list) -> None:
    """
    Start the AngelOne polling feed in a background thread.
    Idempotent — safe to call multiple times.
    """
    global _running, _thread
    if _running and _thread and _thread.is_alive():
        return
    _running = True

    def _run():
        global _running
        try:
            from angelone_client import get_session
            import angelone_scrip_master as scrip_master

            session = get_session()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            token_map = scrip_master.get_tokens_bulk(symbols)   # {clean_symbol: token}
            if not token_map:
                logger.error(
                    "AngelOne feed: scrip master resolved 0/%d requested symbols to tokens — "
                    "feed will not produce any ticks. Check ANGELONE_SCRIP_MASTER_URL is "
                    "reachable and its schema hasn't changed.",
                    len(symbols),
                )
                return
            if len(token_map) < len(symbols):
                logger.warning(
                    "AngelOne feed: resolved %d/%d requested symbols to tokens "
                    "(unresolved ones will simply never appear in live_quotes; "
                    "the quote waterfall falls through to Yahoo for those)",
                    len(token_map), len(symbols),
                )
            reverse_map = {v: k for k, v in token_map.items()}   # token -> clean symbol
            tokens = list(token_map.values())
            logger.info("AngelOne feed: polling %d resolved symbols every ~%.1fs", len(tokens), POLL_INTERVAL_S)

            async def _poll_cycle():
                await session.ensure_session()
                for i in range(0, len(tokens), BATCH_SIZE):
                    if not _running:
                        return
                    batch = tokens[i:i + BATCH_SIZE]
                    try:
                        fetched = await session.get_quotes_batch("NSE", batch)
                        for row in fetched:
                            tok = str(row.get("symbolToken") or "")
                            sym = reverse_map.get(tok)
                            if not sym:
                                continue
                            _on_tick_sync({
                                "tradingSymbol": sym,
                                "ltp":           row.get("ltp"),
                                "open":          row.get("open"),
                                "high":          row.get("high") or row.get("tradeHigh"),
                                "low":           row.get("low") or row.get("tradeLow"),
                                "close":         row.get("close"),
                                "tradeVolume":   row.get("tradeVolume") or row.get("volume"),
                            })
                    except Exception as e:
                        logger.warning("AngelOne quote batch (%d-%d) failed: %s", i, i + len(batch), e)
                    await asyncio.sleep(BATCH_GAP_S)

            async def _poll_forever():
                was_idle = False
                while _running:
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
                        await asyncio.sleep(IDLE_RECHECK_S)
                        continue
                    if was_idle:
                        logger.info("AngelOne feed: market window open — resuming polling")
                        was_idle = False
                    cycle_start = time.time()
                    await _poll_cycle()
                    elapsed = time.time() - cycle_start
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
                        await asyncio.sleep(POLL_INTERVAL_S - elapsed)

            loop.run_until_complete(_poll_forever())
        except Exception as e:
            logger.error("AngelOne feed error: %s", e)
        finally:
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
                "AngelOne feed: thread did not stop within %.1fs", timeout
            )
