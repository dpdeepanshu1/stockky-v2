"""stockky_ms.summary - flatten the nested `market_structure` block into the flat `ms_*` summary that travels through feed rows, scan
payloads and the ML-ready snapshot. Pure; tolerant of a missing / partial / unavailable block (-> quality only)."""
from __future__ import annotations

from typing import Any, Dict, Optional

SUMMARY_KEYS = (
    "ms_quality", "ms_ver", "ms_hash", "ms_asof", "ms_trend", "ms_strength", "ms_bos_dir", "ms_bos_bars", "ms_bos_strength",
    "ms_bos_failed", "ms_bos_vol_ratio", "ms_choch_dir", "ms_choch_bars", "ms_sweep_dir", "ms_sweep_bars", "ms_sweep_strength", "ms_fvg_side",
    "ms_fvg_status", "ms_fvg_top", "ms_fvg_bottom", "ms_fvg_dist_pct", "ms_fvg_quality", "ms_ob_side", "ms_ob_status", "ms_ob_top",
    "ms_ob_bottom", "ms_ob_quality", "ms_fib_level", "ms_fib_dist_pct", "ms_in_golden", "ms_loc_score", "ms_rr", "ms_stop",
    "ms_target", "ms_reward_pct", "ms_chase_atr", "ms_stretch_atr", "ms_room_atr", "ms_atr", "ms_tqs", "ms_groups", "ms_groups_strict",
    "ms_rr_flags", "ms_price",
)


def _g(d: Optional[Dict[str, Any]], k: str):
    return d.get(k) if isinstance(d, dict) else None


def flatten_summary(block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: None for k in SUMMARY_KEYS}
    if not isinstance(block, dict):
        out["ms_quality"] = "unavailable"
        return out
    out["ms_quality"] = block.get("quality") or "unavailable"
    out["ms_ver"] = block.get("engine_version")
    out["ms_hash"] = block.get("params_hash")
    out["ms_asof"] = _g(block.get("data_quality"), "last_bar_ts")
    out["ms_trend"] = block.get("trend")
    out["ms_strength"] = block.get("strength")
    bos, choch, sw = block.get("bos"), block.get("choch"), _g(block.get("liquidity"), "sweep")
    out["ms_bos_dir"], out["ms_bos_bars"] = _g(bos, "direction"), _g(bos, "bars_since")
    out["ms_bos_strength"], out["ms_bos_failed"] = _g(bos, "strength"), _g(bos, "failed")
    out["ms_bos_vol_ratio"] = _g(bos, "vol_ratio")
    out["ms_choch_dir"], out["ms_choch_bars"] = _g(choch, "direction"), _g(choch, "bars_since")
    out["ms_sweep_dir"], out["ms_sweep_bars"], out["ms_sweep_strength"] = _g(sw, "direction"), _g(sw, "bars_since"), _g(sw, "strength")
    f = block.get("fvg")
    out["ms_fvg_side"], out["ms_fvg_status"], out["ms_fvg_top"], out["ms_fvg_bottom"] = _g(f, "side"), _g(f, "status"), _g(f, "top"), _g(f, "bottom")
    out["ms_fvg_dist_pct"], out["ms_fvg_quality"] = _g(f, "distance_pct"), _g(f, "quality")
    o = block.get("order_block")
    out["ms_ob_side"], out["ms_ob_status"], out["ms_ob_top"], out["ms_ob_bottom"] = _g(o, "side"), _g(o, "status"), _g(o, "top"), _g(o, "bottom")
    out["ms_ob_quality"] = _g(o, "quality")
    fb = block.get("fibonacci")
    out["ms_fib_level"], out["ms_fib_dist_pct"], out["ms_in_golden"] = _g(fb, "nearest_level"), _g(fb, "distance_pct"), _g(fb, "in_golden_zone")
    loc, rr = block.get("location"), block.get("structure_rr")
    out["ms_loc_score"] = _g(loc, "entry_location_score")
    out["ms_chase_atr"], out["ms_stretch_atr"], out["ms_room_atr"] = _g(loc, "chase_atr"), _g(loc, "stretch_atr"), _g(loc, "room_atr")
    out["ms_rr"], out["ms_stop"], out["ms_target"], out["ms_reward_pct"] = _g(rr, "rr"), _g(rr, "stop"), _g(rr, "target"), _g(rr, "reward_pct")
    out["ms_rr_flags"] = list(rr["flags"]) if isinstance(rr, dict) and rr.get("flags") else ([] if rr else None)
    out["ms_atr"], out["ms_price"] = block.get("atr"), block.get("last_close")
    out["ms_tqs"], out["ms_groups"] = block.get("technical_quality_score"), block.get("independent_groups_positive")
    out["ms_groups_strict"] = block.get("independent_groups_strict")
    return out
