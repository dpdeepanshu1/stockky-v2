"""feed/bar_backfill.py - group311: fills the bar store's missing morning after a mid-session restart.

The bar store is built from ticks, so after a restart at e.g. 11:40 it only has bars from 11:40 on; such a symbol has no trustworthy day
open from bars (counted as `no_day_open`) and too few bars for zones. This module asks market-data-service for today's 1-minute candles
(GET /history/{symbol}?period=1d&interval=1m) and merges the earlier minutes in front of the live bars via bar_store.insert_history().

Rules: only symbols already tracked whose first bar starts later than the cold-start tolerance after 09:15, ranked by turnover, at most
BOARD_BACKFILL_TOP_N; at most BOARD_BACKFILL_PER_CYCLE fetches per runner pass, a few in parallel; a symbol is retried no sooner than
BOARD_BACKFILL_RETRY_S (hit or miss). Only candles dated today (IST) from 09:15 are used. Nothing here raises into the caller, touches
the DB or places an order; fetches run in worker threads. The day open from the first 1-minute candle is a PROXY for the exchange open
(the exchange open stays preferred whenever the feed has it)."""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("position-stocks-bar-backfill")

IST = timezone(timedelta(hours=5, minutes=30))
OPEN_MIN = 9 * 60 + 15
Bar = Tuple[float, float, float, float, float, float]

_lock = threading.Lock()
_pending: "OrderedDict[str, float]" = OrderedDict()
_tried: Dict[str, float] = {}
# group345 (13 Oct log: the backfill ran every ~9 s, 12 one-minute history calls each, and one run came back 11/12 empty - a large share of the
# market-data load and its ReadTimeouts). A symbol whose history came back empty / failed is not asked again for BOARD_BACKFILL_EMPTY_RETRY_S
# (default 1 h, not 15 min), and a batch that is mostly empty / failed pauses the whole backfill for BOARD_BACKFILL_PAUSE_S (default 5 min).
_hold_until: Dict[str, float] = {}
_pause_until = [0.0]
_state = {"requested": 0, "fetched": 0, "merged_symbols": 0, "merged_bars": 0, "empty": 0, "errors": 0, "last_error": None, "last_run": 0.0}


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def reset() -> None:
    with _lock:
        _pending.clear(); _tried.clear(); _hold_until.clear(); _pause_until[0] = 0.0
        _state.update(requested=0, fetched=0, merged_symbols=0, merged_bars=0, empty=0, errors=0, last_error=None, last_run=0.0)


def state() -> dict:
    with _lock:
        return dict(_state, pending=len(_pending))


def pending_count() -> int:
    with _lock:
        return len(_pending)


def request(symbols: Iterable[str], now: Optional[float] = None) -> int:
    """Queue symbols (cheap, never blocks). Skips symbols already queued or tried within BOARD_BACKFILL_RETRY_S."""
    if not _cfg("BOARD_BACKFILL_ENABLED", True):
        return 0
    now = time.time() if now is None else now
    retry = float(_cfg("BOARD_BACKFILL_RETRY_S", 900))
    added = 0
    with _lock:
        for s in symbols:
            if not s or s in _pending:
                continue
            last = _tried.get(s)
            if last is not None and now - last < retry:
                continue
            if _hold_until.get(s, 0.0) > now:
                continue
            _pending[s] = now
            added += 1
        _state["requested"] += added
    return added


def _fetch_candles(symbol: str) -> List[dict]:
    import httpx
    import config
    with httpx.Client(timeout=float(_cfg("BOARD_BACKFILL_TIMEOUT_S", 8.0))) as client:
        r = client.get(f"{config.MARKET_DATA_URL}/history/{quote(symbol, safe='&')}", params={"period": "1d", "interval": "1m"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        body = r.json()
    return list(body.get("candles") or []) if isinstance(body, dict) else []


def candles_to_bars(candles: Iterable[dict], day_number: Optional[int], before_ts: float) -> List[Bar]:
    """market-data candle rows ({"date": "YYYY-MM-DD HH:MM" IST, open, high, low, close, volume}) -> bar tuples for today's session,
    09:15 or later, strictly before `before_ts`. Bad rows are dropped."""
    out: Dict[float, Bar] = {}
    for c in candles or []:
        try:
            dt = datetime.strptime(str(c.get("date"))[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            if day_number is not None and int((dt.timestamp() + 330 * 60) // 86400) != day_number:
                continue
            if dt.hour * 60 + dt.minute < OPEN_MIN:
                continue
            t = float(int(dt.timestamp() // 60) * 60)
            if t >= before_ts:
                continue
            o, h, l, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
            v = float(c.get("volume") or 0.0)
            if not (o > 0 and h > 0 and l > 0 and cl > 0) or h < l or v != v:
                continue
            out[t] = (t, o, h, l, cl, max(0.0, v))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return [out[k] for k in sorted(out)]


def _one(symbol: str, fetch: Callable[[str], List[dict]]) -> Tuple[str, int, Optional[str]]:
    from feed import bar_store
    try:
        first = bar_store.first_bar_ts(symbol)
        if first is None:
            return symbol, 0, None
        bars = candles_to_bars(fetch(symbol), bar_store.day_number(), first)
        if not bars:
            return symbol, -1, None
        return symbol, bar_store.insert_history(symbol, bars), None
    except Exception as e:  # noqa: BLE001
        return symbol, 0, f"{type(e).__name__}: {str(e)[:100]}"


def run_pending(max_n: Optional[int] = None, now: Optional[float] = None,
                fetch: Optional[Callable[[str], List[dict]]] = None) -> dict:
    """Fetch and merge up to max_n queued symbols (blocking - call from a worker thread). Returns counts."""
    now = time.time() if now is None else now
    max_n = int(_cfg("BOARD_BACKFILL_PER_CYCLE", 6)) if max_n is None else max_n
    with _lock:
        if _pause_until[0] > now:
            return {"tried": 0, "merged_symbols": 0, "merged_bars": 0, "empty": 0, "errors": 0, "paused": True}
        batch = []
        while _pending and len(batch) < max_n:
            batch.append(_pending.popitem(last=False)[0])
    did = {"tried": len(batch), "merged_symbols": 0, "merged_bars": 0, "empty": 0, "errors": 0}
    if not batch:
        return did
    f = fetch or _fetch_candles
    with ThreadPoolExecutor(max_workers=min(4, len(batch)), thread_name_prefix="board-backfill") as ex:
        results = list(ex.map(lambda s: _one(s, f), batch))
    with _lock:
        empty_retry = float(_cfg("BOARD_BACKFILL_EMPTY_RETRY_S", 3600))
        for sym, n, err in results:
            _tried[sym] = now
            if err or n <= 0:
                _hold_until[sym] = now + empty_retry
            _state["fetched"] += 1
            if err:
                _state["errors"] += 1; _state["last_error"] = f"{sym}: {err}"; did["errors"] += 1
            elif n > 0:
                _state["merged_symbols"] += 1; _state["merged_bars"] += n
                did["merged_symbols"] += 1; did["merged_bars"] += n
            else:
                _state["empty"] += 1; did["empty"] += 1
        _state["last_run"] = now
        if did["tried"] >= 4 and (did["empty"] + did["errors"]) * 4 >= did["tried"] * 3:
            _pause_until[0] = now + float(_cfg("BOARD_BACKFILL_PAUSE_S", 300))
            did["paused_for_s"] = float(_cfg("BOARD_BACKFILL_PAUSE_S", 300))
            logger.info("board backfill: %d of %d came back empty/failed - pausing the backfill for %.0fs",
                        did["empty"] + did["errors"], did["tried"], did["paused_for_s"])
    if did["merged_symbols"] or did["errors"]:
        logger.info("board backfill: %d tried, %d filled (%d bars), %d empty, %d errors",
                    did["tried"], did["merged_symbols"], did["merged_bars"], did["empty"], did["errors"])
    return did
