"""stockky_ms.explain - short, deterministic, human-readable reasons for a `market_structure` block.

Output: {"positives": [...], "warnings": [...], "summary": str}. Pure text built from the block's own numbers (no model, no clock), so the
same block always explains the same way. Facts only: zones and levels are heuristics, never claims about who is buying or selling.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _g(d: Optional[Dict[str, Any]], k: str):
    return d.get(k) if isinstance(d, dict) else None


def explain_block(block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(block, dict) or block.get("quality") in (None, "unavailable"):
        return {"positives": [], "warnings": ["structure data unavailable"], "summary": "No market-structure read available."}
    pos: List[str] = []
    warn: List[str] = []
    q = block.get("quality")
    if q == "insufficient":
        return {"positives": [], "warnings": ["not enough clean history for a structure read"], "summary": "Insufficient history."}
    if q == "stale":
        warn.append("data is stale")
    if q == "limited":
        warn.append("limited history: scores are capped")
    for r in _g(block.get("data_quality"), "reasons") or []:
        if r == "corporate_action_discontinuity":
            warn.append("corporate-action jump: only post-event bars used")
    trend = block.get("trend")
    if trend == "bullish":
        pos.append("bullish structure (higher highs and higher lows)")
    elif trend == "bearish":
        warn.append("bearish structure (lower highs and lower lows)")
    elif trend == "transition":
        warn.append("structure in transition after a character change")
    bos, choch = block.get("bos"), block.get("choch")
    if _g(bos, "direction"):
        txt = f"{bos['direction']} break of structure {bos.get('bars_since')} bar(s) ago"
        if bos.get("failed"):
            warn.append(txt + " (failed: closed back through the level)")
        elif bos["direction"] == "bullish":
            pos.append(txt)
        else:
            warn.append(txt)
    if _g(choch, "direction"):
        txt = f"{choch['direction']} change of character {choch.get('bars_since')} bar(s) ago"
        (warn if choch.get("failed") or choch["direction"] == "bearish" else pos).append(txt)
    sw = _g(block.get("liquidity"), "sweep")
    if _g(sw, "direction") == "bullish":
        pos.append(f"sell-side liquidity swept and reclaimed {sw.get('bars_since')} bar(s) ago")
    elif _g(sw, "direction") == "bearish":
        warn.append(f"buy-side liquidity swept and rejected {sw.get('bars_since')} bar(s) ago")
    ob, fvg = block.get("order_block"), block.get("fvg")
    if _g(ob, "side") == "bullish":
        pos.append(f"bullish order block {ob.get('bottom'):.2f}-{ob.get('top'):.2f} ({ob.get('status')})")
    if _g(fvg, "side") == "bullish":
        pos.append(f"bullish fair-value gap {fvg.get('bottom'):.2f}-{fvg.get('top'):.2f} ({fvg.get('status')})")
    fib = block.get("fibonacci")
    if _g(fib, "in_golden_zone") and not fib.get("leg_invalidated") and fib.get("direction") == "bullish":
        pos.append("price in the 0.5-0.618 retracement zone of the latest up-leg")
    if _g(fib, "leg_invalidated"):
        warn.append("fibonacci leg invalidated by a close beyond its origin")
    loc = block.get("location")
    for f in _g(loc, "flags") or []:
        warn.append({"chase_gate": "price is extended above the nearest zone (chase)",
                     "resistance_ceiling": "resistance less than 1 ATR above",
                     "no_resistance_known": "no resistance level known above"}.get(f, f))
    rr = block.get("structure_rr")
    if _g(rr, "rr") is not None:
        txt = f"structure R:R {rr['rr']:.2f} (stop {rr.get('stop')}, target {rr.get('target')})"
        (pos if rr["rr"] >= 1.5 and not (rr.get("flags") or []) else warn).append(txt)
    for f in _g(rr, "flags") or []:
        warn.append({"stop_too_wide": "structural stop is wide", "no_target": "no structural target found",
                     "invalid": "risk/reward could not be built", "target_below_objective": "target is below the profit objective"}.get(f, f))
    tqs = block.get("technical_quality_score")
    summary = f"Structure {trend or 'n/a'}; quality {q}"
    if tqs is not None:
        summary += f"; TQS {tqs}/100 with {block.get('independent_groups_positive')} independent group(s) positive"
    return {"positives": pos, "warnings": warn, "summary": summary + "."}
