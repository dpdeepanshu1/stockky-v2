"""stockky_ms.ml_features - the numeric feature set for a FUTURE model version (WS-14, FEATURE_COLUMNS_V2). Nothing trains or predicts with it here:
the live model keeps its own 12 technical columns, and this module only turns a flat ms_* summary (+ optional weekly / volume-profile / Wyckoff
context) into a fixed-order numeric vector, so rows can be stored now and a V2 model compared later by walk-forward.

Rules: no outcome field is ever read; a missing value stays None (the caller decides how to impute - `vector(fill=...)`); the column order is
fixed and hashed (`columns_hash()`), so a changed order can never be loaded into an old model unnoticed; engine version and params hash travel
with every row (`meta`) because rows from different versions must not be mixed.
Pure stdlib, no I/O, no clock, no env.

C21 (log-only): the pattern signals (candles, chart patterns, Bollinger squeeze, golden / death cross, MACD+RSI confluence, support / resistance
flip) get their OWN fixed column set, `PATTERN_COLUMNS`, with its own hash (`pattern_columns_hash()`). FEATURE_COLUMNS_V2 is deliberately NOT
changed: rows already stored keep the same columns_hash and stay comparable. Pattern rows are stored next to V2 rows and only joined to them
after the B-tools (random-entry benchmark, shuffle, +-20% sweep, MA baseline) show an edge. Nothing here is read by a decision, gate or model.
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


# ---------------------------------------------------------------- C21: pattern features (log-only, separate column set)
PATTERN_COLUMNS: tuple = (
    "pt_candle_bull_n", "pt_candle_bear_n", "pt_candle_vol_confirmed_n",
    "pt_chart_bull_confirmed_n", "pt_chart_bull_forming_n", "pt_chart_bear_confirmed_n", "pt_chart_bear_forming_n",
    "pt_bb_width", "pt_bb_percentile", "pt_bb_squeeze_num", "pt_bb_release_num",
    "pt_ma50_200_above_num", "pt_ma50_200_event_num", "pt_ma50_200_event_bars",
    "pt_ma20_50_above_num", "pt_ma20_50_event_num", "pt_ma20_50_event_bars",
    "pt_conf_state_num", "pt_conf_rsi", "pt_conf_macd_above_num",
    "pt_srflip_support_num", "pt_srflip_resist_num", "pt_srflip_retest_bars",
)
_CROSS_EVENT = {"golden_cross": 1.0, "death_cross": -1.0}


def pattern_columns_hash() -> str:
    return hashlib.sha1(json.dumps(list(PATTERN_COLUMNS)).encode()).hexdigest()[:8]


def _count(items: Any, bias: str, status: Optional[str] = None, flag: Optional[str] = None) -> Optional[float]:
    """Number of list items with this bias (and status / truthy flag). A section that is not a list (None = too few bars) stays None, never 0."""
    if not isinstance(items, list):
        return None
    n = 0
    for it in items:
        if not isinstance(it, dict) or it.get("bias") != bias:
            continue
        if status is not None and it.get("status") != status:
            continue
        if flag is not None and not it.get(flag):
            continue
        n += 1
    return float(n)


def build_pattern_features(patterns: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """{"meta": {...}, "features": {PATTERN_COLUMNS: float|None}} from `analyze_patterns()` output. A section that is None (too few bars, or the
    detector failed) leaves its columns None - never 0. `meta.usable` is False when no section could be built. Never raises."""
    f: Dict[str, Optional[float]] = {c: None for c in PATTERN_COLUMNS}
    pat = patterns if isinstance(patterns, dict) else {}
    try:
        cd, ch = pat.get("candles"), pat.get("chart")
        f["pt_candle_bull_n"], f["pt_candle_bear_n"] = _count(cd, "bullish"), _count(cd, "bearish")
        if isinstance(cd, list):
            f["pt_candle_vol_confirmed_n"] = float(sum(1 for c in cd if isinstance(c, dict) and c.get("volume_confirmed") and c.get("bias") in ("bullish", "bearish")))
        f["pt_chart_bull_confirmed_n"], f["pt_chart_bull_forming_n"] = _count(ch, "bullish", "confirmed"), _count(ch, "bullish", "forming")
        f["pt_chart_bear_confirmed_n"], f["pt_chart_bear_forming_n"] = _count(ch, "bearish", "confirmed"), _count(ch, "bearish", "forming")
        bb = pat.get("bollinger")
        if isinstance(bb, dict):
            f["pt_bb_width"], f["pt_bb_percentile"] = _num(bb.get("width")), _num(bb.get("percentile"))
            f["pt_bb_squeeze_num"] = _bool(bb.get("squeeze"))
            f["pt_bb_release_num"] = 0.0 if bb.get("release") is None else _enum(_DIR, bb.get("release"))
        cr = pat.get("crosses")
        for key, pre in (("ma50_200", "pt_ma50_200"), ("ma20_50", "pt_ma20_50")):
            c = cr.get(key) if isinstance(cr, dict) else None
            if isinstance(c, dict):
                f[pre + "_above_num"] = _bool(c.get("fast_above_slow"))
                ev = c.get("event")
                f[pre + "_event_num"] = _enum(_CROSS_EVENT, ev.get("type")) if isinstance(ev, dict) else 0.0
                f[pre + "_event_bars"] = _num(ev.get("bars_ago")) if isinstance(ev, dict) else None
        cf = pat.get("confluence")
        if isinstance(cf, dict):
            f["pt_conf_state_num"] = 0.0 if cf.get("state") == "neutral" else _enum(_DIR, cf.get("state"))
            f["pt_conf_rsi"], f["pt_conf_macd_above_num"] = _num(cf.get("rsi")), _bool(cf.get("macd_above_signal"))
        sr = pat.get("sr_flip")
        if isinstance(sr, list):
            kinds = [x.get("type") for x in sr if isinstance(x, dict)]
            f["pt_srflip_support_num"], f["pt_srflip_resist_num"] = float("support_flip" in kinds), float("resistance_flip" in kinds)
            rts = [x.get("retest_bars_ago") for x in sr if isinstance(x, dict) and finite(x.get("retest_bars_ago"))]
            f["pt_srflip_retest_bars"] = float(min(rts)) if rts else None
    except Exception:  # noqa: BLE001
        pass
    return {"meta": {"columns_hash": pattern_columns_hash(), "usable": any(v is not None for v in f.values()), "log_only": True,
                     "bars": pat.get("bars")},
            "features": f}


def pattern_vector(built: Dict[str, Any], fill: Optional[float] = None) -> List[Optional[float]]:
    """The pattern feature values in PATTERN_COLUMNS order; None stays None unless `fill` is given."""
    feats = (built or {}).get("features") or {}
    return [(feats.get(c) if feats.get(c) is not None else fill) for c in PATTERN_COLUMNS]
