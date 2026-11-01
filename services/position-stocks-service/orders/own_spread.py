"""orders/own_spread.py - E285 round 112: the stock's OWN normal quoted spread, learned from the spread recorder (the spread half of plan item 10).

WHY: the depth gate refuses a name whose best-5 spread is above ONE ceiling for every stock (ENTRY_DEPTH_MAX_SPREAD_PCT, 0.5 %). 0.5 % is a wide spread for a liquid large cap (a sign something
is wrong) and a normal one for a thin mid cap. Round 109 started recording each stock's spread by minute (feed/spread_recorder, <BAR_RECORD_DIR>/spread/<day>/<symbol>.json); this reads it.

HOW: for a symbol, read its recorded days (spread_recorder.history, today left out, the last ENTRY_OWN_SPREAD_DAYS (20) folders), keep each USABLE day (at least ENTRY_OWN_SPREAD_MIN_ROWS (60) minute
readings between 09:45 and 15:15 IST; the opening and closing minutes are always wider and say nothing about the normal book), take the 90th percentile of that day's per-minute median spread,
then the median of those over the days = how wide this stock's spread normally gets. The ceiling is that x ENTRY_OWN_SPREAD_MULT (1.5, my choice: "half as wide again as its normal wide moment"),
turned into the gate's number by adaptive_params.learned_value: the constant itself until ENTRY_OWN_SPREAD_MIN_DAYS (5) usable days, then shrunk toward it with w = days / (days + 10), then held inside
[constant x (1 - BAND), constant x (1 + BAND)] (BAND 0.5, a hard bound: with the 0.5 % constant the ceiling is always 0.25 %-0.75 %).
Each symbol is a cache read: the first ask starts ONE background load (never I/O on the entry path, never waits) and answers "unknown" (= the old constant). Failures and an empty recorder are cached
as unknown for FAIL_TTL_S. Never raises. Switches ENTRY_OWN_SPREAD_ENABLED and the master ADAPTIVE_ENTRY_PARAMS.

  ceiling_pct(symbol, constant)     {"value", "source", "weight", "days", "p90_pct"} or None
"""
from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from orders import adaptive_params as _ap

logger = logging.getLogger("position-stocks-own-spread")

_IST = 19800
PRIOR_DAYS = 10
MIN_DAYS = 5
OK_TTL_S = 6 * 3600.0
FAIL_TTL_S = 30 * 60.0
FROM_MIN = 30                      # 09:45 (minutes after 09:15)
TO_MIN = 360                       # 15:15

_lock = threading.Lock()
_cache: Dict[str, Tuple[Optional[Dict[str, Any]], float]] = {}
_pending: List[str] = []
_running = False


def _key(symbol: Any) -> str:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def reset() -> None:
    global _running
    with _lock:
        _cache.clear()
        _pending.clear()
        _running = False


def enabled() -> bool:
    return bool(getattr(config, "ENTRY_OWN_SPREAD_ENABLED", True)) and bool(getattr(config, "ADAPTIVE_ENTRY_PARAMS", True))


def _cfg_f(name: str, default: float) -> float:
    try:
        v = float(getattr(config, name, default))
        return v if v == v and v > 0 else default
    except (TypeError, ValueError):
        return default


def _min_days() -> int:
    try:
        return max(MIN_DAYS, int(getattr(config, "ENTRY_OWN_SPREAD_MIN_DAYS", MIN_DAYS)))
    except (TypeError, ValueError):
        return MIN_DAYS


def _ist_date(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ts) + _IST)).strftime("%Y-%m-%d")


def _pctile(xs: List[float], q: float) -> float:
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(len(s) - 1, lo + 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _median(xs: List[float]) -> float:
    return _pctile(xs, 0.5)


def day_p90(rows: List[List[float]], date: str, min_rows: Optional[int] = None) -> Optional[float]:
    """One day's 90th percentile of the per-minute median spread (%), over 09:45-15:15 only; None when the day has too few readings. Pure, never raises."""
    try:
        y, m, d = (int(x) for x in date.split("-"))
        t0 = calendar.timegm((y, m, d, 0, 0, 0)) - _IST + (9 * 3600 + 15 * 60)
        vals = [float(r[1]) for r in rows if FROM_MIN <= (float(r[0]) - t0) / 60.0 < TO_MIN and float(r[1]) > 0]
        need = int(min_rows if min_rows is not None else _cfg_f("ENTRY_OWN_SPREAD_MIN_ROWS", 60))
        return _pctile(vals, 0.9) if len(vals) >= max(1, need) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("own spread: day %s unreadable: %s", date, e)
        return None


def build(symbol: str, today: Optional[str] = None, days: Optional[int] = None, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """{"days", "p90_pct"} from the symbol's recorded spread days before `today`, or None when no day is usable. Never raises."""
    try:
        from feed import spread_recorder
        today = today or _ist_date(time.time())
        n_days = int(days if days is not None else _cfg_f("ENTRY_OWN_SPREAD_DAYS", 20))
        rows = spread_recorder.history(symbol, days=max(1, n_days), root=root, before_date=today)
        by_day: Dict[str, List[List[float]]] = {}
        for r in rows:
            by_day.setdefault(_ist_date(r[0]), []).append(r)
        per_day = [v for v in (day_p90(rs, dt) for dt, rs in by_day.items()) if v is not None]
        if not per_day:
            return None
        return {"days": len(per_day), "p90_pct": _median(per_day)}
    except Exception as e:  # noqa: BLE001
        logger.debug("own spread: build %s failed: %s", symbol, e)
        return None


def _worker() -> None:
    global _running
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            sym = _pending.pop(0)
        out = build(sym)
        with _lock:
            _cache[sym] = (out, time.monotonic() + (OK_TTL_S if out is not None else FAIL_TTL_S))


def _norms(symbol: Any) -> Optional[Dict[str, Any]]:
    global _running
    try:
        if not enabled():
            return None
        k = _key(symbol)
        if not k or k.startswith("^"):
            return None
        now = time.monotonic()
        with _lock:
            hit = _cache.get(k)
            if hit and hit[1] > now:
                return hit[0]
            if k not in _pending:
                _pending.append(k)
            if not _running:
                _running = True
                threading.Thread(target=_worker, name="own-spread", daemon=True).start()
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("own spread: lookup %s failed: %s", symbol, e)
        return None


def ceiling_pct(symbol: Any, constant: float) -> Optional[Dict[str, Any]]:
    """The spread ceiling (%) for this stock: its own normal wide spread x ENTRY_OWN_SPREAD_MULT, shrunk toward `constant` and held within +-BAND of it. None = no reading yet. Never raises."""
    try:
        nm = _norms(symbol)
        c = float(constant)
        if not nm or c <= 0:
            return None
        band = min(0.9, _cfg_f("ENTRY_OWN_SPREAD_BAND", 0.5))
        learned = nm["p90_pct"] * _cfg_f("ENTRY_OWN_SPREAD_MULT", 1.5)
        out = _ap.learned_value(learned, c, nm["days"], ceiling=c * (1.0 + band), floor=c * (1.0 - band), prior=PRIOR_DAYS, min_n=_min_days())
        out["days"] = int(nm["days"])
        out["p90_pct"] = round(nm["p90_pct"], 4)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("own spread: ceiling %s failed: %s", symbol, e)
        return None


def warm(symbols: Any) -> int:
    """Queue the symbols' loads now (the pre-open warm-up). Returns how many were queued. Never raises."""
    n = 0
    try:
        for s in list(symbols or [])[:int(_cfg_f("ENTRY_OWN_NORMS_WARM_MAX", 40))]:
            if _norms(s) is None:
                n += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("own spread: warm failed: %s", e)
    return n
