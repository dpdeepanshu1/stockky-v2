"""stockky_ms.scoring - technical quality score (TQS) from independent evidence groups (long side).

Correlated signals are folded into ONE group so they cannot be counted twice (an order block and an FVG are both "imbalance zones";
a BOS and the trend state are both "structure"):

    structure  trend state + recent BOS / CHOCH                 weight 30
    zones      active bullish order block / FVG near price      weight 20
    liquidity  recent bullish sweep with reclaim                weight 10
    location   entry-location score (zone/fib/support/room/..)  weight 25
    risk       structure risk:reward                            weight 15

Every group is a fraction in [0, 1]; a group is "positive" at >= GROUP_POSITIVE. When the data quality is `limited`, a group can
contribute at most limited_group_cap of its weight. Unknown inputs score 0 (never read as good news). Heuristic weights, not a fitted
model: they are hypotheses to be calibrated by the backtest workstream. Pure; no clock, no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .ms_types import BEARISH, BULLISH, NEUTRAL, Q_LIMITED, Q_OK, TRANSITION, clamp, finite, r3
from .params import MSParams

GROUP_WEIGHTS = (("structure", 30), ("zones", 20), ("liquidity", 10), ("location", 25), ("risk", 15))
GROUP_POSITIVE = 0.5


def _g(d: Optional[Dict[str, Any]], k: str):
    return d.get(k) if isinstance(d, dict) else None


def structure_group(trend: Optional[str], bos: Optional[Dict], choch: Optional[Dict]) -> float:
    base = {BULLISH: 0.55, NEUTRAL: 0.25, BEARISH: 0.0, TRANSITION: 0.25}.get(trend or "", 0.0)
    if trend == TRANSITION and _g(choch, "direction") == "bullish":
        base = 0.35
    if trend == TRANSITION and _g(choch, "direction") == "bearish":
        base = 0.0
    s = base
    if _g(bos, "direction") == "bullish" and not _g(bos, "failed"):
        s += 0.25 + 0.2 * float(_g(bos, "strength") or 0.0)
    elif _g(bos, "direction") == "bearish" and not _g(bos, "failed"):
        s -= 0.3
    if _g(choch, "direction") == "bullish" and not _g(choch, "failed"):
        s += 0.15
    elif _g(choch, "direction") == "bearish" and not _g(choch, "failed"):
        s -= 0.3
    return clamp(s)


def _zone_frac(z: Optional[Dict[str, Any]]) -> float:
    if not z or not finite(z.get("quality")):
        return 0.0
    d = z.get("distance_atr")
    prox = 0.0 if d is None else 1.0 if d <= 0.5 else 0.5 if d <= 1.5 else 0.0
    return float(z["quality"]) * prox


def zones_group(ob: Optional[Dict], fvg: Optional[Dict]) -> float:
    a, b = _zone_frac(ob), _zone_frac(fvg)
    s = max(a, b)
    if a > 0 and b > 0:
        s += 0.1  # overlap of two zone types is mild confirmation, never double counting
    return clamp(s)


def liquidity_group(sweep: Optional[Dict], p: MSParams) -> float:
    if not sweep or sweep.get("direction") != "bullish" or sweep.get("bars_since") is None:
        return 0.0
    if sweep["bars_since"] > p.sweep_max_age:
        return 0.0
    return clamp(0.4 + 0.6 * float(sweep.get("strength") or 0.0))


def risk_group(rr: Optional[Dict], p: MSParams) -> float:
    if not rr:
        return 0.0
    flags = rr.get("flags") or []
    val = rr.get("rr")
    if not finite(val):
        return 0.0
    v = float(val)
    f = 1.0 if v >= p.rr_pref else 0.7 if v >= p.rr_min else 0.4 if v >= p.rr_abs_min else 0.1
    if "invalid" in flags or "stop_too_wide" in flags:
        f = min(f, 0.3)
    return f


def score_block(trend, bos, choch, sweep, ob, fvg, location, rr, quality: str, p: MSParams) -> Dict[str, Any]:
    out: Dict[str, Any] = {"technical_quality_score": None, "independent_groups_positive": None, "groups": None}
    if quality not in (Q_OK, Q_LIMITED):
        return out
    raw = {
        "structure": structure_group(trend, bos, choch),
        "zones": zones_group(ob, fvg),
        "liquidity": liquidity_group(sweep, p),
        "location": clamp(float(_g(location, "entry_location_score") or 0.0) / 100.0),
        "risk": risk_group(rr, p),
    }
    cap = p.limited_group_cap if quality == Q_LIMITED else 1.0
    groups: Dict[str, Any] = {}
    total = wsum = 0.0
    positive = 0
    for name, w in GROUP_WEIGHTS:
        v = min(raw[name], cap)
        groups[name] = {"score": r3(v), "weight": w, "positive": bool(raw[name] >= GROUP_POSITIVE)}
        total += w * v
        wsum += w
        positive += 1 if raw[name] >= GROUP_POSITIVE else 0
    out["technical_quality_score"] = int(round(max(0.0, min(100.0, total / wsum * 100.0))))
    out["independent_groups_positive"] = positive
    out["groups"] = groups
    return out
