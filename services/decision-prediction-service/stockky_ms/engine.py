"""stockky_ms.engine - the public entry point `analyze()`.

    analyze(raw_bars, params=None, ctx=None, stale=False, swing_mode="v1") -> dict   (the `market_structure` block)

Pure and deterministic: no I/O, no clock, no env. Same bars + params + ctx always give the same block. `ctx` carries optional inputs the
caller already has: ema20, vwap, extended, extended_short, entry_price. Missing inputs are never read as good news.
Bad / short / stale data never raises: the block carries `quality` and the sections it could not build are empty.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import fibonacci as _fib
from . import location as _loc
from .atr import atr_series
from .explain import explain_block
from .fvg import ACTIVE, find_fvgs, nearest_active
from .levels import build_levels
from .liquidity import find_sweeps, nearest_pools
from .ms_types import BEARISH, BULLISH, C, H, L, MS_ENGINE_VERSION, Q_LIMITED, Q_OK, Q_STALE, SCHEMA_VERSION, TRANSITION, r2, r3
from .order_blocks import displacement, find_order_blocks, finalize
from .params import MSParams, params_hash
from .prep import prep
from .scoring import score_block
from .structure import scan_structure
from .swings import raw_swings


def _event(ev: Optional[Dict], n: int, max_age: int) -> Optional[Dict[str, Any]]:
    """Latest BOS/CHOCH as a block, or an all-None block when absent or older than max_age bars."""
    empty = {"direction": None, "level": None, "bars_since": None, "strength": None, "failed": None, "gap_event": None}
    if not ev:
        return empty
    age = n - 1 - ev["break_idx"]
    if age > max_age:
        return empty
    return {"direction": ev["direction"], "level": r2(ev["level"]), "bars_since": age, "strength": ev["strength"],
            "failed": bool(ev["failed"]), "gap_event": bool(ev["gap_event"])}


def _trend_strength(struct: Sequence[Dict], state: str) -> Optional[float]:
    """Share of the last 4 labelled swings that agree with the trend state (None when there is no trend)."""
    if state not in (BULLISH, BEARISH):
        return None
    want = ("HH", "HL") if state == BULLISH else ("LH", "LL")
    lab = [s["label"] for s in struct if s.get("label") in ("HH", "HL", "LH", "LL", "EH", "EL")][-4:]
    return r3(sum(1 for x in lab if x in want) / len(lab)) if lab else None


def _unavailable(pr: Dict[str, Any], p: MSParams) -> Dict[str, Any]:
    dq = dict(pr["dq"])
    dq["last_bar_ts"] = pr["bars"][-1][0] if pr["bars"] else None
    block = {"schema_version": SCHEMA_VERSION, "engine_version": MS_ENGINE_VERSION, "params_hash": params_hash(p), "quality": pr["quality"],
             "data_quality": dq, "trend": None, "strength": None, "bos": None, "choch": None, "liquidity": None, "fvg": None,
             "order_block": None, "fibonacci": _fib.empty_fib(), "location": _loc.empty_location(), "structure_rr": _loc.empty_rr(),
             "levels": {"supports": [], "resistances": []}, "atr": None, "last_close": pr["bars"][-1][C] if pr["bars"] else None,
             "technical_quality_score": None, "independent_groups_positive": None, "groups": None}
    block["explain"] = explain_block(block)
    return block


def analyze(raw_bars: Sequence[Sequence[float]], params: Optional[MSParams] = None, ctx: Optional[Dict[str, Any]] = None,
            stale: bool = False, swing_mode: str = "v1") -> Dict[str, Any]:
    p = params or MSParams()
    ctx = ctx or {}
    pr = prep(raw_bars, p, stale=stale)
    bars = pr["bars"]
    if pr["quality"] == "insufficient" or not bars:
        return _unavailable(pr, p)
    n = len(bars)
    atr = atr_series(bars, p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)
    a = atr[-1]
    if not a or a <= 0:
        pr["dq"]["reasons"] = list(pr["dq"]["reasons"]) + ["no_atr"]
        pr["quality"] = "insufficient"
        return _unavailable(pr, p)
    price = bars[-1][C]
    gap, circ = pr["gap_event"], pr["circuit"]
    raw = raw_swings(bars, atr, p, swing_mode, circuit=circ)
    st = scan_structure(bars, atr, raw, gap, circ, p)
    struct, state = st["struct"], st["state"]
    disp = [displacement(b, atr[i], p) for i, b in enumerate(bars)]
    lq = find_sweeps(bars, atr, raw, p, st["bos"], disp)
    fvgs = find_fvgs(bars, atr, p, gap, st["bos"], circ)
    obs_raw = find_order_blocks(bars, atr, p, raw, gap, circ)
    obs = [finalize(z, price, a) for z in obs_raw]

    # block parts -----------------------------------------------------------------------------------------------------------
    bos = _event(st["bos"][-1] if st["bos"] else None, n, p.bos_max_age)
    choch = _event(st["choch"][-1] if st["choch"] else None, n, p.choch_max_age)
    sweeps = lq["sweeps"]
    sw = None
    if sweeps:
        s = sweeps[-1]
        age = n - 1 - s["confirmed_idx"]
        if age <= p.sweep_max_age:
            sw = {"direction": s["direction"], "level": r2(s["level"]), "bars_since": age, "strength": s["strength"],
                  "depth_atr": s["depth_atr"], "followthrough": s["followthrough"]}
    prev = {"high": bars[-2][H], "low": bars[-2][L]} if n >= 2 else None
    liquidity = {"sweep": sw or {"direction": None, "level": None, "bars_since": None, "strength": None, "depth_atr": None, "followthrough": None},
                 **nearest_pools(bars, a, lq["pools"], prev)}
    bull_obs = [z for z in obs if z["side"] == "bullish" and z["distance_atr"] is not None]
    ob = min(bull_obs, key=lambda z: (z["distance_atr"], -z["idx"])) if bull_obs else None
    fvg = nearest_active(fvgs, "bullish", price, a)

    lv = build_levels(bars, struct, lq["pools"], obs_raw, fvgs, p)
    last_choch_dir = st["choch"][-1]["dir"] if (state == TRANSITION and st["choch"]) else 0
    bands: List[Dict[str, float]] = []
    for z in (ob, fvg):
        if z:
            bands.append({"low": z["bottom"], "high": z["top"]})
    for s_ in lv["supports"][:2]:
        bands.append({"low": s_["price_low"], "high": s_["price_high"]})
    bands.append({"low": bars[-2][L], "high": bars[-2][L]} if n >= 2 else {})
    if ctx.get("vwap") is not None:
        bands.append({"low": ctx["vwap"], "high": ctx["vwap"]})
    fib = _fib.fib_block(struct, bars, state, last_choch_dir, a, p, bands)
    loc = _loc.location_block(price, a, ob, fvg, fib, lv, sw, ctx, p)
    rr = _loc.structure_rr(price, a, ob, struct, fvg, lv, fib, ctx, p)

    def zone_out(z: Optional[Dict[str, Any]], keys: Sequence[str]) -> Optional[Dict[str, Any]]:
        return {k: (r2(z[k]) if k in ("top", "bottom") else z.get(k)) for k in keys} if z else None

    fvg_out = zone_out(fvg, ("side", "status", "top", "bottom", "distance_pct", "distance_atr", "quality", "age_bars", "filled_pct", "gap_event"))
    ob_out = zone_out(ob, ("side", "status", "top", "bottom", "distance_pct", "distance_atr", "quality", "age_bars", "bos", "fvg", "impulse"))
    sc = score_block(state, bos, choch, sw, ob_out, fvg_out, loc, rr, pr["quality"], p)

    dq = dict(pr["dq"])
    dq["last_bar_ts"] = bars[-1][0]
    block: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "engine_version": MS_ENGINE_VERSION, "params_hash": params_hash(p), "quality": pr["quality"],
        "data_quality": dq, "swing_mode": swing_mode, "trend": state, "strength": _trend_strength(struct, state), "bos": bos, "choch": choch,
        "liquidity": liquidity, "fvg": fvg_out, "order_block": ob_out,
        "order_blocks": [{"side": z["side"], "top": r2(z["top"]), "bottom": r2(z["bottom"]), "status": z["status"], "quality": z["quality"],
                          "distance_atr": z["distance_atr"]} for z in obs],
        "fibonacci": fib, "location": loc, "structure_rr": rr, "levels": lv,
        "swings": [{"kind": s_["kind"], "price": r2(s_["price"]), "label": s_.get("label"), "ts": s_["ts"]} for s_ in struct[-8:]],
        "atr": r2(a), "last_close": r2(price),
        "technical_quality_score": sc["technical_quality_score"], "independent_groups_positive": sc["independent_groups_positive"],
        "groups": sc["groups"],
    }
    block["explain"] = explain_block(block)
    return block
