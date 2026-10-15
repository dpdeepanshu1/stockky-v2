"""stockky_ms.weekly - weekly structure from the same daily bars (WS-14). Context only: nothing in the decision path reads it.

to_weekly() groups daily bars by IST ISO week. With complete_only (default) the LAST week is dropped unless its last daily bar is a Friday, so a
forming week never repaints (a week cut short by a Friday holiday is dropped until the next Monday - conservative, never look-ahead).
weekly_context() runs the normal engine on the weekly bars with the history floors lowered (a year of weeks is ~52 bars).
Pure stdlib, no I/O, no clock, no env.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from .engine import analyze
from .ms_types import BEARISH, BULLISH, IST_OFFSET_S, finite
from .params import MSParams

WEEKLY_MIN_BARS = 30
WEEKLY_FULL_BARS = 52
_EPOCH = datetime(1970, 1, 1)


def _ist(ts: float) -> datetime:
    return _EPOCH + timedelta(seconds=float(ts) + IST_OFFSET_S)


def to_weekly(bars: Sequence[Sequence[float]], complete_only: bool = True) -> List[tuple]:
    """Daily (t,o,h,l,c,v) -> weekly bars, oldest first. Rows with non-finite OHLC are skipped; volume sums finite values only."""
    rows = [b for b in bars if len(b) >= 6 and all(finite(b[i]) for i in range(1, 5))]
    rows.sort(key=lambda b: b[0])
    weeks: List[List[Sequence[float]]] = []
    keys: List[tuple] = []
    for b in rows:
        iso = _ist(b[0]).isocalendar()
        key = (iso[0], iso[1])
        if keys and keys[-1] == key:
            weeks[-1].append(b)
        else:
            keys.append(key)
            weeks.append([b])
    if complete_only and weeks and _ist(weeks[-1][-1][0]).weekday() < 4:
        weeks.pop()
    out = []
    for w in weeks:
        vol = sum(float(x[5]) for x in w if finite(x[5]))
        out.append((w[0][0], float(w[0][1]), max(float(x[2]) for x in w), min(float(x[3]) for x in w), float(w[-1][4]), vol))
    return out


def weekly_params(p: Optional[MSParams] = None) -> MSParams:
    """The daily params with the history floors lowered for weekly bars (everything else, thresholds included, is the daily value)."""
    return dataclasses.replace(p or MSParams(), min_bars=WEEKLY_MIN_BARS, full_bars=WEEKLY_FULL_BARS)


def _last_swing(swings: Sequence[Dict[str, Any]], kind: str) -> Optional[float]:
    for s in reversed(swings or []):
        if s.get("kind") == kind:
            return s.get("price")
    return None


def weekly_context(bars: Sequence[Sequence[float]], params: Optional[MSParams] = None, complete_only: bool = True) -> Dict[str, Any]:
    wb = to_weekly(bars, complete_only)
    blk = analyze(wb, params=weekly_params(params))
    return {"timeframe": "1w", "bars": len(wb), "quality": blk.get("quality"), "trend": blk.get("trend"), "strength": blk.get("strength"),
            "bos": blk.get("bos"), "choch": blk.get("choch"), "last_swing_high": _last_swing(blk.get("swings"), "high"),
            "last_swing_low": _last_swing(blk.get("swings"), "low"), "atr": blk.get("atr"), "last_close": blk.get("last_close")}


def alignment(daily_trend: Optional[str], weekly_trend: Optional[str]) -> str:
    """aligned_bull / aligned_bear / conflict / unknown (neutral or missing on either side is 'unknown', never read as agreement)."""
    if daily_trend == BULLISH and weekly_trend == BULLISH:
        return "aligned_bull"
    if daily_trend == BEARISH and weekly_trend == BEARISH:
        return "aligned_bear"
    if {daily_trend, weekly_trend} == {BULLISH, BEARISH}:
        return "conflict"
    return "unknown"
