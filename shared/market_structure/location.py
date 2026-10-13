"""stockky_ms.location - entry-location score, chase distance and structure-based risk/reward (long side).

All distances are in ATR. Unknown inputs are never read as good news: a missing reference gives None / zero points, never a pass.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, finite, r2, r3
from .params import MSParams

LOC_KEYS = ("entry_location_score", "chase_atr", "chase_ref", "stretch_atr", "room_atr", "flags")
RR_KEYS = ("entry", "stop", "target", "risk", "reward", "rr", "reward_pct", "risk_pct", "stop_source", "target_source", "flags")


def empty_location() -> Dict[str, Any]:
    d: Dict[str, Any] = {k: None for k in LOC_KEYS}
    d["flags"] = []
    return d


def empty_rr() -> Dict[str, Any]:
    d: Dict[str, Any] = {k: None for k in RR_KEYS}
    d["flags"] = []
    return d


def _w(p: MSParams) -> Dict[str, float]:
    return {k: float(v) for k, v in p.location_weights}


def _zone_above_distance(z: Dict[str, Any], price: float) -> Optional[float]:
    """Distance of price above the zone top (0 when inside); None when the zone sits above price."""
    if z["bottom"] > price:
        return None
    return max(0.0, price - z["top"])


def chase_reference(price: float, ob: Optional[Dict], fvg: Optional[Dict], fib: Dict[str, Any], supports: Sequence[Dict]) -> Optional[Dict[str, Any]]:
    """Nearest zone at or below price: active bullish OB / FVG first, else the golden zone, else the nearest structural support."""
    cands = []
    for src, z in (("ob", ob), ("fvg", fvg)):
        if z and finite(z.get("top")) and finite(z.get("bottom")):
            d = _zone_above_distance(z, price)
            if d is not None:
                cands.append((d, 0 if src == "ob" else 1, src, z["top"], z["bottom"]))
    if cands:
        d, _, src, top, bottom = min(cands)
        return {"ref": src, "top": top, "bottom": bottom}
    if fib.get("golden_high") is not None and fib["golden_low"] <= price:
        return {"ref": "fib", "top": fib["golden_high"], "bottom": fib["golden_low"]}
    for s in supports:  # supports are ordered nearest first
        if s["price_high"] <= price:
            return {"ref": "support", "top": s["price_high"], "bottom": s["price_low"]}
    return None


def location_block(price: float, a: float, ob: Optional[Dict], fvg: Optional[Dict], fib: Dict[str, Any], levels: Dict[str, List[Dict]],
                   sweep: Optional[Dict], ctx: Dict[str, Any], p: MSParams) -> Dict[str, Any]:
    out = empty_location()
    if not finite(price) or not a or a <= 0:
        return out
    flags: List[str] = []
    sup, res = levels.get("supports") or [], levels.get("resistances") or []
    ref = chase_reference(price, ob, fvg, fib, sup)
    chase = None
    if ref is not None:
        chase = max(0.0, price - ref["top"]) / a
        out["chase_ref"] = ref["ref"]
    out["chase_atr"] = r3(chase)
    ema20 = ctx.get("ema20")
    out["stretch_atr"] = r3((price - float(ema20)) / a) if finite(ema20) else None
    room = None
    major = False
    for z in res:
        if z["price_low"] > price:
            room = (z["price_low"] - price) / a
            major = (z.get("strength") or 0.0) >= 0.6
            break
    out["room_atr"] = r3(room)
    w = _w(p)

    # zone proximity (OB / FVG at or below price)
    zfrac = 0.0
    for z in (ob, fvg):
        if z and finite(z.get("top")) and finite(z.get("bottom")):
            d = _zone_above_distance(z, price)
            if d is None:
                continue
            da = d / a
            f = 1.0 if d == 0 else 22.0 / 30.0 if da <= 0.5 else 12.0 / 30.0 if da <= 1.0 else 4.0 / 30.0 if da <= 1.5 else 0.0
            zfrac = max(zfrac, f)
    # fibonacci
    ffrac = 0.0
    if fib.get("retracement") is not None and not fib.get("leg_invalidated") and fib.get("direction") == "bullish":
        if fib.get("in_golden_zone"):
            ffrac = 1.0
        elif fib.get("nearest_level") in (0.382, 0.786) and (fib.get("distance_pct") or 99.0) <= p.fib_conf_tol_pct:
            ffrac = 0.6
    # structural support proximity (scaled over 1 ATR)
    sfrac = 0.0
    for s in sup:
        if s["price_high"] <= price:
            sfrac = 1.0 - min(1.0, (price - s["price_high"]) / a / 1.0)
            break
        if s["price_low"] <= price:
            sfrac = 1.0
            break
    # fresh liquidity reclaim (decays over 5 bars)
    rfrac = 0.0
    if sweep and sweep.get("direction") == "bullish" and sweep.get("bars_since") is not None:
        rfrac = max(0.0, 1.0 - float(sweep["bars_since"]) / 5.0)
    # room to resistance
    if room is None:
        qfrac = 0.5
        flags.append("no_resistance_known")
    else:
        qfrac = 1.0 if room >= 3 else 0.75 if room >= 2 else 0.5 if room >= 1.5 else 0.25 if room >= 1 else 0.0
        if room < 1:
            flags.append("resistance_ceiling")
    # VWAP (unknown = half)
    vw = ctx.get("vwap")
    vfrac = 0.5 if not finite(vw) else 1.0 if price >= float(vw) else 0.0
    score = (w["zone"] * zfrac + w["fib"] * ffrac + w["support"] * sfrac + w["reclaim"] * rfrac + w["room"] * qfrac + w["vwap"] * vfrac)
    pen = 0.0
    if chase is not None:
        if chase > 1.5:
            pen += 25.0
            flags.append("chase_gate")
        elif chase > 1.0:
            pen += 10.0
    ext = 0.0
    if ctx.get("extended"):
        ext += 10.0
    if ctx.get("extended_short"):
        ext += 10.0
    pen += min(20.0, ext)
    if room is not None and room < 0.5 and major:
        pen += 15.0
    out["entry_location_score"] = int(round(max(0.0, min(100.0, score - pen))))
    out["flags"] = flags
    return out


def structure_rr(price: float, a: float, ob: Optional[Dict], struct: Sequence[Dict], fvg: Optional[Dict], levels: Dict[str, List[Dict]],
                 fib: Dict[str, Any], ctx: Dict[str, Any], p: MSParams) -> Dict[str, Any]:
    out = empty_rr()
    if not finite(price) or not a or a <= 0:
        out["flags"] = ["invalid"]
        return out
    entry = float(ctx["entry_price"]) if finite(ctx.get("entry_price")) else float(price)
    flags: List[str] = []
    cands = []
    if ob and ob.get("side") == "bullish":
        cands.append((ob["bottom"], "ob_bottom"))
    lows = [s for s in struct if s["kind"] == "low" and s["price"] < entry]
    hl = [s for s in lows if s.get("label") == "HL"]
    if hl:
        cands.append((hl[-1]["price"], "swing_low"))
    elif lows:
        cands.append((lows[-1]["price"], "swing_low"))
    if fvg and fvg.get("side") == "bullish":
        cands.append((fvg["bottom"], "fvg_bottom"))
    # first candidate (priority order) whose risk is inside [rr_min_risk_atr, rr_max_risk_atr] ATR; a far-away zone no longer hides a
    # closer valid structural stop. When every valid candidate is too wide, the first one is kept and flagged `stop_too_wide`.
    stop = stop_src = None
    wide = None
    for raw, src in cands:
        s = raw - p.stop_buffer_atr * a
        r_ = entry - s
        if r_ < p.rr_min_risk_atr * a:
            continue
        if r_ <= p.rr_max_risk_atr * a:
            stop, stop_src = s, src
            break
        if wide is None:
            wide = (s, src)
    if stop is None and wide is not None:
        stop, stop_src = wide
    if stop is None:
        out["entry"], out["flags"] = r2(entry), ["invalid"]
        return out
    risk = entry - stop
    target = tgt_src = None
    for z in levels.get("resistances") or []:
        if z["price_low"] >= entry + p.min_target_atr * a:
            target, tgt_src = z["price_low"], "resistance"
            break
    if target is None and finite(fib.get("ext_1272")) and fib.get("direction") == "bullish" and fib["ext_1272"] >= entry + p.min_target_atr * a:
        target, tgt_src = fib["ext_1272"], "fib_ext"
    reward = (target - entry) if target is not None else None
    if risk > p.rr_max_risk_atr * a:
        flags.append("stop_too_wide")
    if target is None:
        flags.append("no_target")
    elif reward is not None and reward <= 0:
        flags.append("invalid")
    rr = reward / risk if reward is not None and risk > 0 else None
    reward_pct = reward / entry * 100.0 if reward is not None else None
    if reward_pct is not None and reward_pct < p.objective_pct:
        flags.append("target_below_objective")
    if risk <= 0 and "invalid" not in flags:
        flags.append("invalid")
    out.update({"entry": r2(entry), "stop": r2(stop), "target": r2(target), "risk": r2(risk), "reward": r2(reward), "rr": r2(rr),
                "reward_pct": r2(reward_pct), "risk_pct": r2(risk / entry * 100.0), "stop_source": stop_src, "target_source": tgt_src, "flags": flags})
    return out
