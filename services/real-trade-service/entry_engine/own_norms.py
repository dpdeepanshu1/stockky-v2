"""entry_engine/own_norms.py - E285 round 111: Real Trade's copy of the stock's OWN normal 1-minute range and volume pace, learned from its recorded minute bars (plan item 10, the per-stock half of
ADAPTIVE_ENTRY_PARAMS).

WHY: two entry numbers were the same for every stock. The VWAP tolerance (ENTRY_VWAP_TOLERANCE_PCT, 0.15 %) is how far under the day's VWAP a long may be: 0.15 % is a normal
one-minute wiggle on a lively stock and a real drop on a sleepy one. The relative-volume floor compared today's volume with `20-day mean daily volume x a market-wide share
of the session` (liquidity_gate, volume_profile), which assumes every stock trades its day on the market's average shape.

HOW: for a symbol, read its recorded days (the recorder day folders, today left out, the last OWN_NORMS_DAYS (20) folders), keep each COMPLETE day (first bar by 09:20, at
least 80 % of the 375 minutes present, at least half of them with volume; a half-recorded or backfilled-without-volume day says nothing) and read two things off it:
  * range_pct      the median of (high - low) / close over the day's 09:45-15:15 minutes, then the median over days  (the stock's usual one-minute move, in %)
  * expected_volume(mins)   the median over days of the cumulative volume traded by that minute of the session (linearly between minutes)
Both are turned into the number the gate uses by adaptive_params.learned_value: the constant itself until MIN_DAYS (5) complete days, then shrunk toward the constant with
w = days / (days + 10) (a day is one sample; the minutes inside a day move together), and held inside [constant x (1 - BAND), constant x (1 + BAND)] (BAND 0.5, a hard bound).
Each symbol is a cache read: the first ask starts ONE background load (never I/O on the entry path, never waits) and answers "unknown" (= the old constant) until it is done.
Failures and an empty recorder are cached as unknown for FAIL_TTL_S. Never raises. Switch ENTRY_OWN_NORMS_ENABLED.

  vwap_tolerance_pct(symbol, constant)             {"value", "source", "weight", "days", "range_pct"} or None
  expected_volume(symbol, constant, now)           the cumulative volume to compare today's with: {"value", "source", "weight", "days"} or None

Real Trade records no minute bars itself. It reads the SAME recorder folders Position Stocks writes (the bar-records volume, mounted at /data/bars in both services), and it already
asks Position Stocks to record every symbol it evaluated (the wanted list, candidate_outcomes), so the names Real Trade trades are the names with history. Read-only here: this module
never writes under /data/bars. The spread ceiling is NOT learned here: the recorder keeps no spread.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import config
from entry_engine import adaptive_params as _ap

logger = logging.getLogger("real-trade-own-norms")

SESSION_MIN = 375
_IST = 19800
MIN_DAYS = 5
PRIOR_DAYS = 10
OK_TTL_S = 6 * 3600.0
FAIL_TTL_S = 30 * 60.0
RANGE_FROM_MIN = 30                # 09:45: the opening minutes are not this stock's normal range
RANGE_TO_MIN = 360                 # 15:15

_lock = threading.Lock()
_cache: Dict[str, Tuple[Optional[Dict[str, Any]], float]] = {}     # SYMBOL -> (norms or None, expires at monotonic s)
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
    return bool(getattr(config, "ENTRY_OWN_NORMS_ENABLED", True)) and bool(getattr(config, "ADAPTIVE_ENTRY_PARAMS", True))     # E285 round 110 / 111: ADAPTIVE_ENTRY_PARAMS is the master switch


def _cfg_f(name: str, default: float) -> float:
    try:
        v = float(getattr(config, name, default))
        return v if v == v and v > 0 else default
    except (TypeError, ValueError):
        return default


def _min_days() -> int:
    try:
        return max(MIN_DAYS, int(getattr(config, "ENTRY_OWN_NORMS_MIN_DAYS", MIN_DAYS)))
    except (TypeError, ValueError):
        return MIN_DAYS


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _ist_date(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ts) + _IST)).strftime("%Y-%m-%d")


def _t0(date: str) -> float:
    y, m, d = (int(x) for x in date.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0)) - _IST + (9 * 3600 + 15 * 60)


def _clean(bars: Any) -> List[List[float]]:
    """The recorder's own cleaning rule (feed/bar_recorder._clean in Position Stocks): [t, o, h, l, c, v] rows, one per timestamp, positive prices, high >= low, volume >= 0."""
    out: Dict[float, List[float]] = {}
    for b in bars or []:
        try:
            t, o, h, l, c, v = (float(x) for x in list(b)[:6])
        except (TypeError, ValueError):
            continue
        if not (t > 0 and o > 0 and h > 0 and l > 0 and c > 0 and h >= l) or v != v or v < 0:
            continue
        out[t] = [t, o, h, l, c, v]
    return [out[k] for k in sorted(out)]


def _read_day(root: str, date: str, symbol: str) -> List[List[float]]:
    """One symbol's recorded bars for one day: <root>/<date>/<quoted symbol>.json -> {"bars": [...]}. [] when the file is missing or unreadable. Read only."""
    try:
        path = os.path.join(root, date, quote(symbol, safe="") + ".json")
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return _clean(doc.get("bars")) if isinstance(doc, dict) and isinstance(doc.get("bars"), list) else []
    except (OSError, ValueError):
        return []


def bars_root() -> str:
    """The recorder root (the day folders live directly under it): ENTRY_OWN_NORMS_BARS_DIR, else the parent of BAR_RECORDS_DIR (/data/bars/rt -> /data/bars)."""
    explicit = str(getattr(config, "ENTRY_OWN_NORMS_BARS_DIR", "") or "").strip()
    if explicit:
        return explicit
    own = str(getattr(config, "BAR_RECORDS_DIR", "/data/bars/rt") or "/data/bars/rt").rstrip("/")
    return os.path.dirname(own) or "/data/bars"


def day_norms(bars: List[List[float]], date: str) -> Optional[Dict[str, Any]]:
    """One complete day -> {"range": median 1-minute range %, "cumx": [376 cumulative volumes before each minute offset]}, or None when the day is not complete. Pure, never raises."""
    try:
        t0 = _t0(date)
        vol = [0.0] * SESSION_MIN
        seen = 0
        first = None
        rng: List[float] = []
        for b in bars:
            off = int(round((float(b[0]) - t0) / 60.0))
            if not 0 <= off < SESSION_MIN:
                continue
            seen += 1
            first = off if first is None else min(first, off)
            vol[off] += max(0.0, float(b[5]))
            c = float(b[4])
            if RANGE_FROM_MIN <= off < RANGE_TO_MIN and c > 0 and float(b[2]) >= float(b[3]) > 0:
                rng.append((float(b[2]) - float(b[3])) / c * 100.0)
        if first is None or first > 5 or seen < 0.8 * SESSION_MIN or len(rng) < 0.6 * (RANGE_TO_MIN - RANGE_FROM_MIN):
            return None
        if sum(1 for v in vol if v > 0) < 0.5 * SESSION_MIN or sum(vol) <= 0:
            return None
        cumx = [0.0]
        for v in vol:
            cumx.append(cumx[-1] + v)
        return {"range": _median(rng), "cumx": cumx}
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: day %s unreadable: %s", date, e)
        return None


def build(symbol: str, today: Optional[str] = None, days: Optional[int] = None, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The symbol's norms from its recorded days: {"days", "range_pct", "cumx": [per day arrays]} or None when there is no complete day. Never raises."""
    try:
        today = today or _ist_date(time.time())
        n_days = int(days if days is not None else _cfg_f("ENTRY_OWN_NORMS_DAYS", 20))
        root = root or bars_root()
        names = sorted((n for n in os.listdir(root) if len(n) == 10 and n[4] == "-" and n[7] == "-" and n < today), reverse=True)[:max(1, n_days)]     # "rt" and other folders are not dates
        per_day: List[Dict[str, Any]] = []
        for n in names:
            bars = _read_day(root, n, symbol)                                                    # that day's folder only
            dn = day_norms(bars, n) if bars else None
            if dn:
                per_day.append(dn)
        if not per_day:
            return None
        return {"days": len(per_day), "range_pct": _median([d["range"] for d in per_day]), "cumx": [d["cumx"] for d in per_day]}
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: build %s failed: %s", symbol, e)
        return None


def _worker() -> None:
    global _running
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            sym = _pending.pop(0)
        norms = build(sym)
        ok = norms is not None
        with _lock:
            _cache[sym] = (norms, time.monotonic() + (OK_TTL_S if ok else FAIL_TTL_S))


def _norms(symbol: Any) -> Optional[Dict[str, Any]]:
    """Cached norms or None. A miss queues one background load. Never blocks, never raises."""
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
                threading.Thread(target=_worker, name="rt-own-norms", daemon=True).start()
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: lookup %s failed: %s", symbol, e)
        return None


def _band() -> float:
    return min(0.9, _cfg_f("ENTRY_OWN_NORMS_BAND", 0.5))


def _bounded(learned: float, constant: float, days: int) -> Dict[str, Any]:
    b = _band()
    out = _ap.learned_value(learned, constant, days, ceiling=constant * (1.0 + b), floor=constant * (1.0 - b), prior=PRIOR_DAYS, min_n=_min_days())
    out["days"] = int(days)
    return out


def vwap_tolerance_pct(symbol: Any, constant: float) -> Optional[Dict[str, Any]]:
    """The VWAP tolerance (%) for this stock: its own median one-minute range x ENTRY_OWN_NORMS_VWAP_RANGE_MULT (1.0), shrunk toward `constant` and held within +-BAND of it.
    None = no reading yet (use the constant). Never raises."""
    try:
        nm = _norms(symbol)
        c = float(constant)
        if not nm or c <= 0:
            return None
        learned = nm["range_pct"] * _cfg_f("ENTRY_OWN_NORMS_VWAP_RANGE_MULT", 1.0)
        out = _bounded(learned, c, nm["days"])
        out["range_pct"] = round(nm["range_pct"], 4)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: vwap tolerance %s failed: %s", symbol, e)
        return None


def own_cum_volume(nm: Dict[str, Any], mins: float) -> Optional[float]:
    """Median over the days of the cumulative volume by `mins` minutes after 09:15 (linear inside a minute). Pure, never raises."""
    try:
        if mins <= 0:
            return None
        k = min(SESSION_MIN - 1, int(mins))
        frac = min(1.0, mins - k) if mins < SESSION_MIN else 1.0
        vals = [cx[k] + frac * (cx[k + 1] - cx[k]) for cx in nm["cumx"]]
        return _median(vals) if vals else None
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: cum volume failed: %s", e)
        return None


def expected_volume(symbol: Any, constant: float, mins: float) -> Optional[Dict[str, Any]]:
    """The volume this stock has normally traded by `mins` minutes after 09:15, as the gate's comparison base: its own median shrunk toward `constant` (the old
    `average daily volume x market share of the session`) and held within +-BAND of it. None = no reading yet. Never raises."""
    try:
        nm = _norms(symbol)
        c = float(constant)
        if not nm or c <= 0:
            return None
        own = own_cum_volume(nm, mins)
        if own is None or own <= 0:
            return None
        return _bounded(own, c, nm["days"])
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: expected volume %s failed: %s", symbol, e)
        return None


def warm(symbols: Any) -> int:
    """Queue the symbols' loads now (the pre-open warm-up) so the first entry already has norms. Returns how many were queued. Never raises."""
    n = 0
    try:
        for s in list(symbols or [])[:int(_cfg_f("ENTRY_OWN_NORMS_WARM_MAX", 40))]:
            if _norms(s) is None:
                n += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("own norms: warm failed: %s", e)
    return n
