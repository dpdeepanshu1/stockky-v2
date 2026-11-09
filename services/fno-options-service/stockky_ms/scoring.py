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
# group423: the five groups above are NOT all independent. `location` is built from zone / fib / support / room, so it re-reads what `zones` already saw;
# `risk` is computed from the stop / target that come from the same swings (and room is P6's input). So "2 groups positive" is often one piece of
# evidence counted twice. The strict count merges zones + location into one "entry zone" evidence and drops risk (P4 gates R:R on its own):
#   structure (trend / BOS / CHOCH) - entry_zone (zones OR location) - liquidity (sweep and reclaim)      -> 0..3


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


def strict_groups(raw: Dict[str, float]) -> int:
    """group423: positive count over the three genuinely separate evidences (structure, entry zone = max(zones, location), liquidity)."""
    entry = max(raw.get("zones") or 0.0, raw.get("location") or 0.0)
    return sum(1 for v in (raw.get("structure") or 0.0, entry, raw.get("liquidity") or 0.0) if v >= GROUP_POSITIVE)


def score_block(trend, bos, choch, sweep, ob, fvg, location, rr, quality: str, p: MSParams) -> Dict[str, Any]:
    out: Dict[str, Any] = {"technical_quality_score": None, "independent_groups_positive": None, "groups": None, "independent_groups_strict": None}
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
    out["independent_groups_strict"] = strict_groups(raw)
    out["groups"] = groups
    return out


# ---------------------------------------------------------------- C21: pattern shadow evidence (LOG-ONLY)
def pattern_shadow(patterns: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """C21 - what the pattern signals WOULD add, recorded next to the score but never part of it. `score_block` and the technical quality score
    are untouched; nothing reads this to allow, block or size a trade. It exists so rows can be stored now and the B-tools can test whether the
    signals carry any edge later.

    Five separate evidence groups (a candle and a chart pattern share one group, so one move is not counted twice), each +1 (bullish), -1
    (bearish), 0 (seen, no direction) or None (not computable - never read as 0):
        pattern     confirmed bullish / bearish chart pattern or a bullish / bearish candle pattern (opposite kinds cancel to 0)
        squeeze     Bollinger squeeze released upward (+1) / downward (-1)
        trend       SMA50/200 above / below (SMA20/50 when there is not enough history)
        momentum    MACD + RSI confluence bullish / bearish
        sr_flip     support flip (+1) / resistance flip (-1); both at once = 0
    Never raises.
    """
    out: Dict[str, Any] = {"log_only": True, "in_tqs": False, "groups": None, "bull_groups": None, "bear_groups": None, "net": None}
    try:
        pat = patterns if isinstance(patterns, dict) else {}
        g: Dict[str, Optional[int]] = {"pattern": None, "squeeze": None, "trend": None, "momentum": None, "sr_flip": None}
        cd, ch = pat.get("candles"), pat.get("chart")
        if isinstance(cd, list) or isinstance(ch, list):
            bull = bear = 0
            for it in list(cd or []) + [c for c in (ch or []) if isinstance(c, dict) and c.get("status") == "confirmed"]:
                b = it.get("bias") if isinstance(it, dict) else None
                bull += 1 if b == "bullish" else 0
                bear += 1 if b == "bearish" else 0
            g["pattern"] = 1 if bull and not bear else -1 if bear and not bull else 0
        bb = pat.get("bollinger")
        if isinstance(bb, dict):
            g["squeeze"] = {"bullish": 1, "bearish": -1}.get(bb.get("release"), 0)
        cr = pat.get("crosses")
        if isinstance(cr, dict):
            c = cr.get("ma50_200") if isinstance(cr.get("ma50_200"), dict) else cr.get("ma20_50")
            if isinstance(c, dict) and c.get("fast_above_slow") is not None:
                g["trend"] = 1 if c.get("fast_above_slow") else -1
        cf = pat.get("confluence")
        if isinstance(cf, dict):
            g["momentum"] = {"bullish": 1, "bearish": -1}.get(cf.get("state"), 0)
        sr = pat.get("sr_flip")
        if isinstance(sr, list):
            kinds = {x.get("type") for x in sr if isinstance(x, dict)}
            up, dn = "support_flip" in kinds, "resistance_flip" in kinds
            g["sr_flip"] = 1 if up and not dn else -1 if dn and not up else 0
        known = [v for v in g.values() if v is not None]
        if known:
            out["groups"] = g
            out["bull_groups"] = sum(1 for v in known if v > 0)
            out["bear_groups"] = sum(1 for v in known if v < 0)
            out["net"] = out["bull_groups"] - out["bear_groups"]
    except Exception:  # noqa: BLE001
        pass
    return out
