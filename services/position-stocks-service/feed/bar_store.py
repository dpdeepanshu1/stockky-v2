"""feed/bar_store.py - group305: today's 1-minute OHLCV bars per symbol, built from the tick stream (both feeds, via the on-tick
callback). In memory; a new IST day clears it. Volume = delta of cumulative day volume (never negative). Only symbols with
>= BOARD_MIN_DAY_VOLUME shares are tracked; prune() keeps the BOARD_TOP_SYMBOLS most liquid. Thread-safe; never raises into ticks."""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("position-stocks-bar-store")
Bar = Tuple[float, float, float, float, float, float]
MAX_BARS = 400
IST_OFFSET_S = 330 * 60
_lock = threading.Lock()
_bars: Dict[str, Deque[list]] = {}
_last_cum_vol: Dict[str, float] = {}
_turnover: Dict[str, float] = {}
_day: Optional[int] = None
_installed = False
_backfilled: Dict[str, int] = {}          # group311: symbol -> history bars merged in (mid-session restart backfill)
_stats = {"ticks": 0, "skipped_low_volume": 0, "pruned": 0, "errors": 0, "backfilled_symbols": 0, "backfilled_bars": 0}


def _cfg_int(name: str, default: int) -> int:
    try:
        import config
        return int(getattr(config, name, default))
    except Exception:  # noqa: BLE001
        return default


def reset() -> None:
    global _day
    with _lock:
        _bars.clear(); _last_cum_vol.clear(); _turnover.clear(); _backfilled.clear()
        _day = None
        for k in _stats:
            _stats[k] = 0


def on_tick(symbol: str, ltp: float, cum_volume, ts: float) -> None:
    global _day
    try:
        if not symbol or not ltp or ltp <= 0 or not ts:
            return
        day = int((ts + IST_OFFSET_S) // 86400)
        minute_t = float(int(ts // 60) * 60)
        cum = float(cum_volume) if cum_volume else 0.0
        min_vol = _cfg_int("BOARD_MIN_DAY_VOLUME", 200000)
        with _lock:
            if _day is None:
                _day = day
            elif day != _day:
                if day < _day:
                    return
                _bars.clear(); _last_cum_vol.clear(); _turnover.clear(); _backfilled.clear()
                _day = day
            dq = _bars.get(symbol)
            if dq is None:
                if cum < min_vol:
                    _stats["skipped_low_volume"] += 1
                    return
                dq = _bars[symbol] = deque(maxlen=MAX_BARS)
            prev = _last_cum_vol.get(symbol)
            dvol = cum - prev if (prev is not None and cum > prev) else 0.0
            if cum:
                _last_cum_vol[symbol] = cum
                _turnover[symbol] = cum * ltp
            if dq and dq[-1][0] == minute_t:
                b = dq[-1]
                if ltp > b[2]:
                    b[2] = ltp
                if ltp < b[3]:
                    b[3] = ltp
                b[4] = ltp
                b[5] += dvol
            elif dq and minute_t < dq[-1][0]:
                return
            else:
                dq.append([minute_t, ltp, ltp, ltp, ltp, dvol])
            _stats["ticks"] += 1
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        logger.debug("bar_store.on_tick(%s): %s", symbol, e)


def install() -> None:
    global _installed
    if _installed:
        return
    from feed import ws_client
    ws_client.register_on_tick(on_tick)
    _installed = True


def get_bars(symbol: str) -> List[Bar]:
    with _lock:
        dq = _bars.get(symbol)
        return [tuple(b) for b in dq] if dq else []  # type: ignore[misc]


def symbols() -> List[str]:
    with _lock:
        return list(_bars.keys())


def turnover(symbol: str) -> float:
    with _lock:
        return _turnover.get(symbol, 0.0)


def day_number() -> Optional[int]:
    return _day


def prune(keep: Optional[int] = None) -> int:
    keep = max(1, keep if keep is not None else _cfg_int("BOARD_TOP_SYMBOLS", 400))
    with _lock:
        if len(_bars) <= keep:
            return 0
        order = sorted(_bars.keys(), key=lambda s: _turnover.get(s, 0.0), reverse=True)
        drop = order[keep:]
        for s in drop:
            _bars.pop(s, None); _last_cum_vol.pop(s, None); _turnover.pop(s, None); _backfilled.pop(s, None)
        _stats["pruned"] += len(drop)
        return len(drop)


def stats() -> dict:
    with _lock:
        return {"symbols": len(_bars), "bars": sum(len(d) for d in _bars.values()), "day": _day, **_stats}


def insert_history(symbol: str, hist: List[Bar]) -> int:
    """group311: merge EARLIER 1-minute bars (history fetch after a mid-session restart) in front of the live ones. Only minutes strictly
    before the first live bar are taken (a live bar is never overwritten); bad / duplicate rows are dropped. The symbol must already be
    tracked - this never creates one. Returns bars added."""
    try:
        if not symbol or not hist:
            return 0
        with _lock:
            dq = _bars.get(symbol)
            if not dq:
                return 0
            first_live = dq[0][0]
            seen = {}
            for b in hist:
                try:
                    t, o, h, l, c, v = (float(x) for x in b)
                except (TypeError, ValueError):
                    continue
                if not (t < first_live and o > 0 and h > 0 and l > 0 and c > 0 and h >= l) or v != v:
                    continue
                m = float(int(t // 60) * 60)
                seen[m] = [m, o, h, l, c, max(0.0, v)]
            if not seen:
                return 0
            _bars[symbol] = deque([seen[k] for k in sorted(seen)] + [list(b) for b in dq], maxlen=MAX_BARS)
            if symbol not in _backfilled:
                _stats["backfilled_symbols"] += 1
            _backfilled[symbol] = _backfilled.get(symbol, 0) + len(seen)
            _stats["backfilled_bars"] += len(seen)
            return len(seen)
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        logger.debug("bar_store.insert_history(%s): %s", symbol, e)
        return 0


def is_backfilled(symbol: str) -> bool:
    with _lock:
        return symbol in _backfilled


def first_bar_ts(symbol: str) -> Optional[float]:
    with _lock:
        dq = _bars.get(symbol)
        return dq[0][0] if dq else None
