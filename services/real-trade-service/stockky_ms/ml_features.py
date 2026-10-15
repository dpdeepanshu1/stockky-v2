"""stockky_ms.ml_features - the numeric feature set for a FUTURE model version (WS-14, FEATURE_COLUMNS_V2). Nothing trains or predicts with it here:
the live model keeps its own 12 technical columns, and this module only turns a flat ms_* summary (+ optional weekly / volume-profile / Wyckoff
context) into a fixed-order numeric vector, so rows can be stored now and a V2 model compared later by walk-forward.

Rules: no outcome field is ever read; a missing value stays None (the caller decides how to impute - `vector(fill=...)`); the column order is
fixed and hashed (`columns_hash()`), so a changed order can never be loaded into an old model unnoticed; engine version and params hash travel
with every row (`meta`) because rows from different versions must not be mixed.
Pure stdlib, no I/O, no clock, no env.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from .ms_types import finite

_TREND = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0, "transition": 0.0}
_DIR = {"bullish": 1.0, "bearish": -1.0}
_FVG = {"fresh": 1.0, "partial": 0.66, "tested": 0.33}
_PHASE = {"markup": 1.0, "accumulation": 0.5, "distribution": -0.5, "markdown": -1.0, "unknown": 0.0}
_ALIGN = {"aligned_bull": 1.0, "aligned_bear": -1.0, "conflict": 0.0}
_VP = {"above_value": 1.0, "inside_value": 0.0, "below_value": -1.0}

FEATURE_COLUMNS_V2: tuple = (
    "ms_trend_num", "ms_strength", "ms_bos_dir_num", "ms_bos_bars", "ms_bos_strength", "ms_bos_failed_num", "ms_choch_dir_num", "ms_choch_bars",
    "ms_sweep_dir_num", "ms_sweep_bars", "ms_sweep_strength", "ms_fvg_side_num", "ms_fvg_status_num", "ms_fvg_dist_pct", "ms_fvg_quality",
    "ms_ob_side_num", "ms_ob_quality", "ms_in_golden_num", "ms_fib_dist_pct", "ms_loc_score", "ms_rr", "ms_reward_pct", "ms_chase_atr",
    "ms_stretch_atr", "ms_room_atr", "ms_tqs", "ms_groups",
    "wk_trend_num", "wk_alignment_num", "vp_position_num", "vp_price_vs_poc_pct", "wy_phase_num",
)


def columns_hash() -> str:
    return hashlib.sha1(json.dumps(list(FEATURE_COLUMNS_V2)).encode()).hexdigest()[:8]


def _num(v: Any) -> Optional[float]:
    return float(v) if finite(v) else None


def _bool(v: Any) -> Optional[float]:
    return None if v is None else (1.0 if v else 0.0)


def _enum(table: Dict[str, float], v: Any) -> Optional[float]:
    return table.get(v) if isinstance(v, str) else None


def build_features_v2(summary: Optional[Dict[str, Any]], weekly: Optional[Dict[str, Any]] = None, volume: Optional[Dict[str, Any]] = None,
                      wyckoff: Optional[Dict[str, Any]] = None, daily_trend: Optional[str] = None) -> Dict[str, Any]:
    """{"meta": {...}, "features": {column: float|None}}. An unusable summary (quality not ok/limited) gives all-None features."""
    s = summary if isinstance(summary, dict) else {}
    usable = s.get("ms_quality") in ("ok", "limited")
    f: Dict[str, Optional[float]] = {c: None for c in FEATURE_COLUMNS_V2}
    if usable:
        f.update({
            "ms_trend_num": _enum(_TREND, s.get("ms_trend")), "ms_strength": _num(s.get("ms_strength")),
            "ms_bos_dir_num": _enum(_DIR, s.get("ms_bos_dir")), "ms_bos_bars": _num(s.get("ms_bos_bars")),
            "ms_bos_strength": _num(s.get("ms_bos_strength")), "ms_bos_failed_num": _bool(s.get("ms_bos_failed")),
            "ms_choch_dir_num": _enum(_DIR, s.get("ms_choch_dir")), "ms_choch_bars": _num(s.get("ms_choch_bars")),
            "ms_sweep_dir_num": _enum(_DIR, s.get("ms_sweep_dir")), "ms_sweep_bars": _num(s.get("ms_sweep_bars")),
            "ms_sweep_strength": _num(s.get("ms_sweep_strength")), "ms_fvg_side_num": _enum(_DIR, s.get("ms_fvg_side")),
            "ms_fvg_status_num": _enum(_FVG, s.get("ms_fvg_status")), "ms_fvg_dist_pct": _num(s.get("ms_fvg_dist_pct")),
            "ms_fvg_quality": _num(s.get("ms_fvg_quality")), "ms_ob_side_num": _enum(_DIR, s.get("ms_ob_side")),
            "ms_ob_quality": _num(s.get("ms_ob_quality")), "ms_in_golden_num": _bool(s.get("ms_in_golden")),
            "ms_fib_dist_pct": _num(s.get("ms_fib_dist_pct")), "ms_loc_score": _num(s.get("ms_loc_score")), "ms_rr": _num(s.get("ms_rr")),
            "ms_reward_pct": _num(s.get("ms_reward_pct")), "ms_chase_atr": _num(s.get("ms_chase_atr")),
            "ms_stretch_atr": _num(s.get("ms_stretch_atr")), "ms_room_atr": _num(s.get("ms_room_atr")), "ms_tqs": _num(s.get("ms_tqs")),
            "ms_groups": _num(s.get("ms_groups")),
        })
    if isinstance(weekly, dict) and weekly.get("quality") in ("ok", "limited"):
        f["wk_trend_num"] = _enum(_TREND, weekly.get("trend"))
        from .weekly import alignment
        f["wk_alignment_num"] = _enum(_ALIGN, alignment(daily_trend or s.get("ms_trend"), weekly.get("trend")))
    if isinstance(volume, dict):
        f["vp_position_num"] = _enum(_VP, volume.get("position"))
        f["vp_price_vs_poc_pct"] = _num(volume.get("price_vs_poc_pct"))
    if isinstance(wyckoff, dict):
        f["wy_phase_num"] = _enum(_PHASE, wyckoff.get("phase"))
    return {"meta": {"engine_version": s.get("ms_ver"), "params_hash": s.get("ms_hash"), "columns_hash": columns_hash(), "usable": usable},
            "features": f}


def vector(built: Dict[str, Any], fill: Optional[float] = None) -> List[Optional[float]]:
    """The feature values in FEATURE_COLUMNS_V2 order; None stays None unless `fill` is given."""
    feats = (built or {}).get("features") or {}
    return [(feats.get(c) if feats.get(c) is not None else fill) for c in FEATURE_COLUMNS_V2]


def assert_comparable(rows: Sequence[Dict[str, Any]]) -> bool:
    """True when every row has the same engine version, params hash and column hash - rows that fail this must not be pooled for training."""
    keys = {(r.get("meta", {}).get("engine_version"), r.get("meta", {}).get("params_hash"), r.get("meta", {}).get("columns_hash")) for r in rows}
    return len(keys) <= 1
