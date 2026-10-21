"""orders/orb_shadow.py - opening-range-breakout recorder for position-stocks (group 479). RECORD ONLY, no live mode.

Same verdict as real-trade-service entry_engine/orb_shadow.py (group 470), computed from the cached market-data /quote row (day high / low, avg price
= VWAP, spread) at the moment a position is opened. Stored in scalp_positions.flow_json["orb"]. Nothing is ordered, blocked or resized.
RANGE: the first quote seen between ORB_RANGE_END_IST (09:20) and ORB_CAPTURE_UNTIL_IST (09:26) fixes range_high / range_low from the quote's day high / low.
A symbol first seen after the capture window has no range (reason "no_range"). The recorder is fed by record_quote() (called for every entry candidate)
so the range exists even for a symbol that is only bought later.
VERDICT: price above range_high, above VWAP, spread <= ORB_MAX_SPREAD_PCT, range width inside [ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT], time <= ORB_LATEST_ENTRY_IST.
limit = range_high x (1 + ORB_LIMIT_BUFFER_PCT %), stop = range_low. Every function never raises. ORB_MODE=off stops all recording.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import config
from tz_utils import ist_now, ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("position-stocks-orb-shadow")

_ranges: Dict[Tuple[str, str], Dict[str, float]] = {}
_stats: Dict[str, int] = {"recorded": 0, "would_enter": 0, "no_range": 0, "ranges": 0}


def _f(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _t(name: str, dh: int, dm: int):
    return parse_hhmm(str(getattr(config, name, "") or ""), dh, dm)


def _off() -> bool:
    return str(getattr(config, "ORB_MODE", "shadow") or "shadow").lower() == "off"


def record_quote(symbol: str, quote: Any, now: Optional[datetime] = None) -> None:
    """Fix the opening range from this quote when we are inside the capture window. Never raises."""
    try:
        if _off() or not isinstance(quote, dict):
            return
        n = ist_now(now)
        if n.weekday() >= 5:
            return
        in_capture = (ist_time_at_or_after(_t("ORB_RANGE_END_IST", 9, 20), now)
                      and not ist_time_at_or_after(_t("ORB_CAPTURE_UNTIL_IST", 9, 26), now))
        if not in_capture:
            return
        sym = str(symbol or "").strip().upper()
        key = (sym, n.date().isoformat())
        hi, lo = _f(quote.get("day_high")), _f(quote.get("day_low"))
        if key not in _ranges and hi and lo and hi > lo > 0:
            _ranges[key] = {"high": hi, "low": lo}
            _stats["ranges"] += 1
            if len(_ranges) > 3000:
                day = n.date().isoformat()
                for k in [k for k in _ranges if k[1] != day]:
                    _ranges.pop(k, None)
    except Exception as e:  # noqa: BLE001
        logger.debug("orb record_quote failed: %s", e)


def evaluate(symbol: str, quote: Any, ltp: Optional[float] = None, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The ORB verdict dict, or None when off / after ORB_LATEST_ENTRY_IST / before the open / unusable. Pure apart from the range store."""
    try:
        if _off():
            return None
        n = ist_now(now)
        if n.weekday() >= 5 or n.time() < parse_hhmm("09:15", 9, 15):
            return None
        q = quote if isinstance(quote, dict) else {}
        record_quote(symbol, q, now)
        if n.time() > _t("ORB_LATEST_ENTRY_IST", 10, 30):
            return None
        key = (str(symbol or "").strip().upper(), n.date().isoformat())
        rng = _ranges.get(key)
        px = _f(ltp) or _f(q.get("price"))
        out: Dict[str, Any] = {"v": 1, "mode": "shadow", "at": n.strftime("%H:%M:%S")}
        if rng is None or px is None:
            out.update(would_enter=False, reason="no_range" if rng is None else "no_price")
            return out
        rh, rl = rng["high"], rng["low"]
        width = (rh - rl) / rl * 100.0
        vwap, spread = _f(q.get("avg_price")), _f(q.get("spread_pct"))
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
        out.update(range_high=round(rh, 2), range_low=round(rl, 2), range_pct=round(width, 3), would_enter=not reasons, reasons=reasons,
                   limit_price=round(rh * (1 + buf / 100.0), 2), stop_price=round(rl, 2),
                   stop_pct=round((px - rl) / px * 100.0, 3) if px > rl else None)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb evaluate failed: %s", e)
        return None


def attach(flow_json: Optional[str], symbol: str, quote: Any, ltp: Optional[float] = None, now: Optional[datetime] = None) -> Optional[str]:
    """flow_json text with the ORB verdict merged under "orb"; the input unchanged when nothing was recorded. Never raises."""
    try:
        import json
        v = evaluate(symbol, quote, ltp, now)
        if v is None:
            return flow_json
        try:
            base = json.loads(flow_json) if flow_json else {}
            if not isinstance(base, dict):
                base = {}
        except (TypeError, ValueError):
            base = {}
        base["orb"] = v
        _stats["recorded"] += 1
        if v.get("would_enter"):
            _stats["would_enter"] += 1
        if v.get("reason") == "no_range":
            _stats["no_range"] += 1
        return json.dumps(base, separators=(",", ":"))
    except Exception as e:  # noqa: BLE001
        logger.debug("orb attach failed: %s", e)
        return flow_json


def stats() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    _ranges.clear()
    for k in _stats:
        _stats[k] = 0
