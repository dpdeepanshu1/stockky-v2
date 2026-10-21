"""entry_engine/orb_shadow.py - opening-range-breakout recorder (group 470). RECORD ONLY, no live mode.

WHY: the 20 Oct review found entries in the 09:xx hour win 18% of the time (45 trades, gross -866) when made by a quality check alone.
The opening-range-breakout literature enters only AFTER a 5-minute range has formed, on a break of its high, with price above VWAP, a tight
spread and a limit order. Nothing here tests that on this account's data, so it is recorded first: for every entry decision the bot
evaluates, flow_json["orb"] says what an opening-range entry would have done. Nothing is ordered, blocked or resized.

RANGE: the first tick seen between ORB_RANGE_END_IST and ORB_CAPTURE_UNTIL_IST fixes range_high / range_low from the tick's day high / low
(the session so far = the opening range). A symbol first seen after the capture window has no range (reason "no_range").
VERDICT (orb.would_enter): price above range_high, above VWAP, spread <= ORB_MAX_SPREAD_PCT, range width inside
[ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT], time <= ORB_LATEST_ENTRY_IST. limit = range_high x (1 + ORB_LIMIT_BUFFER_PCT %), stop = range_low.
Every function never raises. ORB_MODE=off stops all recording.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import config
from tz_utils import ist_now, ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("real-trade-orb-shadow")

_ranges: Dict[Tuple[str, str], Dict[str, float]] = {}
_stats: Dict[str, int] = {"recorded": 0, "would_enter": 0, "no_range": 0, "ranges": 0}


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _t(name: str, dh: int, dm: int):
    return parse_hhmm(str(getattr(config, name, "") or ""), dh, dm)


def evaluate(symbol: str, tick, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The ORB verdict dict for this tick, or None when off / outside 09:15-ORB_LATEST_ENTRY_IST / unusable. Updates the range store."""
    try:
        if getattr(config, "ORB_MODE", "shadow") == "off":
            return None
        n = ist_now(now)
        if n.weekday() >= 5 or n.time() < _t("OPENING_ENTRY_NOT_BEFORE_IST", 9, 15):
            return None
        sym = str(symbol or "").strip().upper()
        day = n.date().isoformat()
        key = (sym, day)
        px, hi, lo = _f(getattr(tick, "price", None)), _f(getattr(tick, "day_high", None)), _f(getattr(tick, "day_low", None))
        in_capture = (ist_time_at_or_after(_t("ORB_RANGE_END_IST", 9, 20), now)
                      and not ist_time_at_or_after(_t("ORB_CAPTURE_UNTIL_IST", 9, 26), now))
        if key not in _ranges and in_capture and hi and lo and hi > lo > 0:
            _ranges[key] = {"high": hi, "low": lo}
            _stats["ranges"] += 1
            if len(_ranges) > 3000:
                for k in [k for k in _ranges if k[1] != day]:
                    _ranges.pop(k, None)
        if n.time() > _t("ORB_LATEST_ENTRY_IST", 10, 30):
            return None
        rng = _ranges.get(key)
        out: Dict[str, Any] = {"v": 1, "mode": "shadow", "at": n.strftime("%H:%M:%S")}
        if rng is None or px is None:
            out.update(would_enter=False, reason="no_range" if rng is None else "no_price")
            return out
        rh, rl = rng["high"], rng["low"]
        width = (rh - rl) / rl * 100.0
        vwap, spread = _f(getattr(tick, "vwap", None)), _f(getattr(tick, "spread_pct", None))
        reasons = []
        if px <= rh:
            reasons.append("below_range_high")
        if vwap is not None and vwap > 0 and px <= vwap:
            reasons.append("below_vwap")
        max_sp = float(getattr(config, "ORB_MAX_SPREAD_PCT", 0.0) or 0.0)
        if max_sp > 0 and spread is not None and spread > max_sp:
            reasons.append("wide_spread")
        if width < float(getattr(config, "ORB_MIN_RANGE_PCT", 0.0) or 0.0):
            reasons.append("range_too_narrow")
        max_w = float(getattr(config, "ORB_MAX_RANGE_PCT", 0.0) or 0.0)
        if max_w > 0 and width > max_w:
            reasons.append("range_too_wide")
        buf = float(getattr(config, "ORB_LIMIT_BUFFER_PCT", 0.0) or 0.0)
        out.update(range_high=round(rh, 2), range_low=round(rl, 2), range_pct=round(width, 3),
                   would_enter=not reasons, reasons=reasons,
                   limit_price=round(rh * (1 + buf / 100.0), 2), stop_price=round(rl, 2),
                   stop_pct=round((px - rl) / px * 100.0, 3) if px > rl else None)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb evaluate failed: %s", e)
        return None


def record(decision, tick, now: Optional[datetime] = None) -> bool:
    """Merge the ORB verdict into decision.flow_json under "orb". Never raises; False when nothing was recorded."""
    try:
        v = evaluate(getattr(decision, "symbol", ""), tick, now)
        if v is None:
            return False
        try:
            base = json.loads(getattr(decision, "flow_json", None) or "{}")
            if not isinstance(base, dict):
                base = {}
        except (TypeError, ValueError):
            base = {}
        base["orb"] = v
        decision.flow_json = json.dumps(base, separators=(",", ":"))
        _stats["recorded"] += 1
        if v.get("would_enter"):
            _stats["would_enter"] += 1
        if v.get("reason") == "no_range":
            _stats["no_range"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("orb record failed: %s", e)
        return False


def stats() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    _ranges.clear()
    for k in _stats:
        _stats[k] = 0
