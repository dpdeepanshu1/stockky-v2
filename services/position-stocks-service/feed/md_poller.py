"""
feed/md_poller.py - group301: the scalper's tick feed read from market-data-service instead of the AngelOne WebSocket.

Switched on with POSITION_FEED_SOURCE=market_data (default angelone_ws = the old feed, untouched). It fills the SAME buffers
feed/ws_client.py fills (tick buffer, day volume, day stats, on-tick callbacks) through ws_client._ingest_tick(), so
screening/engine.py, the adaptive levels and the entry path read it exactly as before.

How it polls
  * One POST {MARKET_DATA_URL}/quotes/bulk per POSITION_BULK_POLL_S (default 1 s - Dhan allows one bulk request a second).
  * Every request carries the symbols of open positions first (so a held name is refreshed every poll), then the next
    POSITION_BULK_CHUNK (default 500) symbols of the NSE-EQ universe, round robin. The universe is the same list the WS
    subscribed to (scrip master, ETFs/funds dropped).
  * market-data decides the provider (QUOTE_PROVIDER_ORDER; Dhan first on the Oracle VM), so this service needs no AngelOne
    login for ticks.

What differs from the WebSocket (read this before turning it on)
  * A quote row is a snapshot, not a trade. A row is stored as a tick only when its `fetched_at` moved since the last one
    stored for that symbol, so an unchanged cached row never adds a duplicate tick (which would inflate the tick-count
    volume proxy and shorten the 1m/5m windows). A symbol's tick rate is therefore the rate market-data refreshes it.
  * Rows older than POSITION_BULK_MAX_AGE_S (default 30 s) are dropped, not stored.
  * Rows carry no best bid/ask, so get_best_bid_ask() stays None and the MAX_SPREAD_PCT gate stays fail-open exactly as it is
    for a symbol the WS has not sent depth for. The entry depth gate (group 280/286/292) reads market-data /quote itself.
  * Rows carry day high / low / previous close but no open: the day stats are stored as (None, high, low, prev_close).
  * ws_status() keeps `connected` / `last_tick_at` / `reconnect_attempts` for the dashboard and adds `source` + `md_poller`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx

import config
from feed import ws_client
from feed.instrument_filter import drop_etfs
from feed.scrip_master import get_all_nse_eq

logger = logging.getLogger("position-stocks-md-poller")

_UNIVERSE_REFRESH_S = 3600.0
_ERROR_BACKOFF_MAX_S = 10.0
_ERROR_LOG_EVERY = 20            # consecutive failures between repeated WARNINGs

_hot_provider: Optional[Callable[[], List[str]]] = None
_universe: List[str] = []
_universe_loaded_at = 0.0
_cursor = 0
_last_row_ts: Dict[str, float] = {}
_stats = {
    "polls": 0, "errors": 0, "consecutive_errors": 0, "ticks": 0, "stale_skipped": 0, "unchanged_skipped": 0,
    "last_rows": 0, "last_batch": 0, "last_poll_at": None, "last_ok_at": None, "last_error": None,
}


def reset() -> None:
    """Forget all poller state (tests, and a clean restart)."""
    global _hot_provider, _universe, _universe_loaded_at, _cursor
    _hot_provider = None
    _universe = []
    _universe_loaded_at = 0.0
    _cursor = 0
    _last_row_ts.clear()
    for k in _stats:
        _stats[k] = None if k in ("last_poll_at", "last_ok_at", "last_error") else 0


def set_hot_symbols_provider(fn: Optional[Callable[[], List[str]]]) -> None:
    """Replace the function that lists symbols to refresh on EVERY poll (default: open positions from the DB)."""
    global _hot_provider
    _hot_provider = fn


def _clean(sym) -> str:
    return str(sym or "").strip().upper().replace(".NS", "").replace(".BO", "")


def open_position_symbols() -> List[str]:
    """Symbols of positions holding real exposure (OPEN / EXIT_LEGS_REJECTED). Blocking DB read: call off the loop."""
    import db as _db
    from models import ScalpPosition
    with _db.get_session_factory()() as s:
        rows = s.query(ScalpPosition.symbol).filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).all()
    return [_clean(r[0]) for r in rows if _clean(r[0])]


def _hot_symbols() -> List[str]:
    """Hot list, never raising: a DB or provider failure just means no hot names this poll."""
    try:
        fn = _hot_provider or open_position_symbols
        out, seen = [], set()
        for s in (fn() or []):
            c = _clean(s)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out
    except Exception as e:  # noqa: BLE001 - the feed must keep running without the hot list
        logger.warning("position-stocks md poller: open-position lookup failed (%s) - polling the universe only", e)
        return []


def next_batch(hot: List[str]) -> List[str]:
    """Hot symbols first, then the next slice of the universe (round robin), at most POSITION_BULK_CHUNK in all."""
    global _cursor
    chunk = config.POSITION_BULK_CHUNK
    batch = list(hot[:chunk])
    room = chunk - len(batch)
    if room > 0 and _universe:
        have = set(batch)
        n = len(_universe)
        taken = 0
        scanned = 0
        while taken < room and scanned < n:
            s = _universe[_cursor % n]
            _cursor = (_cursor + 1) % n
            scanned += 1
            if s not in have:
                batch.append(s)
                taken += 1
    return batch


def _row_ts(row: dict, now: float) -> float:
    """Epoch seconds of the row's price: fetched_at (naive UTC ISO, as market-data writes it), else now."""
    raw = row.get("fetched_at")
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return now


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None


def ingest_rows(rows, now: Optional[float] = None) -> int:
    """Store every usable, fresh, changed quote row as a tick. Returns how many ticks were stored."""
    now = time.time() if now is None else now
    max_age = config.POSITION_BULK_MAX_AGE_S
    stored = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = _clean(row.get("symbol"))
        price = _num(row.get("price"))
        if not sym or price is None:
            continue
        ts = min(_row_ts(row, now), now)            # a clock-skewed future stamp is clamped to now
        if now - ts > max_age:
            _stats["stale_skipped"] += 1
            continue
        if ts <= _last_row_ts.get(sym, 0.0):
            _stats["unchanged_skipped"] += 1
            continue
        _last_row_ts[sym] = ts
        vol = row.get("volume")
        try:
            vol = int(vol) if vol is not None and int(vol) > 0 else 0
        except (TypeError, ValueError):
            vol = 0
        hi, lo, prev = _num(row.get("day_high")), _num(row.get("day_low")), _num(row.get("previous_close"))
        day_stats = (None, hi, lo, prev) if any(v is not None for v in (hi, lo, prev)) else None
        ws_client._last_tick_at = ts
        ws_client._ingest_tick(sym, price, ts, vol, None, None, day_stats)
        stored += 1
    _stats["ticks"] += stored
    return stored


async def _fetch(client: "httpx.AsyncClient", symbols: List[str]) -> list:
    url = f"{config.MARKET_DATA_URL}/quotes/bulk"
    resp = await client.post(url, json={"symbols": symbols})
    resp.raise_for_status()
    data = resp.json()
    quotes = data.get("quotes") if isinstance(data, dict) else None
    return quotes if isinstance(quotes, list) else []


async def _load_universe() -> bool:
    """(Re)load the NSE-EQ symbol list the way the WS does. False when the scrip master is empty."""
    global _universe, _universe_loaded_at
    symbol_token_map = await asyncio.to_thread(get_all_nse_eq)
    if symbol_token_map:
        symbol_token_map, _skipped = drop_etfs(symbol_token_map)
    if not symbol_token_map:
        return False
    _universe = sorted(_clean(s) for s in symbol_token_map)
    _universe_loaded_at = time.time()
    logger.info("position-stocks md poller: polling %d NSE-EQ symbols via market-data /quotes/bulk", len(_universe))
    return True


async def poll_once(client: "httpx.AsyncClient") -> int:
    """One poll: build the batch, fetch, ingest. Returns ticks stored; raises on a failed request."""
    hot = await asyncio.to_thread(_hot_symbols)
    batch = next_batch(hot)
    _stats["last_batch"] = len(batch)
    if not batch:
        return 0
    rows = await _fetch(client, batch)
    _stats["last_rows"] = len(rows)
    return ingest_rows(rows)


async def _poll_loop() -> None:
    """Runs until ws_client.stop() clears ws_client._running. Never raises: every failure backs off and retries."""
    interval = config.POSITION_BULK_POLL_S
    idle_logged = False
    fails = 0
    async with httpx.AsyncClient(timeout=config.POSITION_BULK_TIMEOUT_S) as client:
        while ws_client._running:
            if ws_client._offhours_idle():
                ws_client._connected = False
                if not idle_logged:
                    logger.info("position-stocks md poller: outside market hours (IST) - idling, rechecking every %.0fs",
                                ws_client._OFFHOURS_RECHECK_S)
                    idle_logged = True
                await asyncio.sleep(ws_client._OFFHOURS_RECHECK_S)
                continue
            idle_logged = False
            started = time.monotonic()
            try:
                if not _universe or time.time() - _universe_loaded_at > _UNIVERSE_REFRESH_S:
                    if not await _load_universe() and not _universe:
                        raise RuntimeError("scrip master map is empty (download failed?)")
                await poll_once(client)
                fails = 0
                _stats["consecutive_errors"] = 0
                _stats["last_ok_at"] = time.time()
                ws_client._connected = True
                ws_client._reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure: count it, back off, keep going
                fails += 1
                _stats["errors"] += 1
                _stats["consecutive_errors"] = fails
                _stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                ws_client._reconnect_attempts += 1
                if fails >= 3:
                    ws_client._connected = False
                if fails == 1 or fails % _ERROR_LOG_EVERY == 0:
                    logger.warning("position-stocks md poller: poll failed (%s) - %d in a row, backing off", _stats["last_error"], fails)
                await asyncio.sleep(min(_ERROR_BACKOFF_MAX_S, interval * (2 ** min(fails, 5))))
                continue
            finally:
                _stats["polls"] += 1
                _stats["last_poll_at"] = time.time()
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
    logger.info("position-stocks md poller: loop stopped.")


def status() -> dict:
    return {
        **_stats,
        "universe": len(_universe),
        "poll_interval_s": config.POSITION_BULK_POLL_S,
        "chunk": config.POSITION_BULK_CHUNK,
    }
