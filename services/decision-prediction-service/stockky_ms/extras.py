"""stockky_ms.extras - the optional WS-14 context in one call: weekly structure, volume profile, Wyckoff phase, candlestick / chart patterns (C18-C20), the V2 feature row and the log-only pattern feature row + shadow evidence (C21).

    analyze_extras(raw_bars, params=None, ctx=None, block=None) -> dict

Separate from analyze() on purpose: the schema-1 `market_structure` contract and its golden files stay untouched, and no decision, gate or
risk check reads this. It is for the operator panel (`/structure/{sym}?extras=1`) and for storing V2 feature rows. Any section that cannot be
built is None (never a guess); the whole call never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from .engine import analyze
from .ml_features import FEATURE_COLUMNS_V2, PATTERN_COLUMNS, build_features_v2, build_pattern_features, columns_hash, pattern_columns_hash
from .ms_types import MS_ENGINE_VERSION
from .params import MSParams
from .patterns import analyze_patterns
from .scoring import pattern_shadow
from .summary import flatten_summary
from .volume_profile import volume_profile
from .weekly import alignment, weekly_context
from .wyckoff import wyckoff_context


def analyze_extras(raw_bars: Sequence[Sequence[float]], params: Optional[MSParams] = None, ctx: Optional[Dict[str, Any]] = None,
                   block: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"engine_version": MS_ENGINE_VERSION, "context_only": True, "weekly": None, "alignment": None, "volume_profile": None,
                           "wyckoff": None, "patterns": None, "features_v2": None, "pattern_features": None, "pattern_shadow": None, "errors": []}
    try:
        blk = block if isinstance(block, dict) else analyze(raw_bars, params=params, ctx=ctx)
        daily_trend = blk.get("trend")
        for key, fn in (("weekly", lambda: weekly_context(raw_bars, params)), ("volume_profile", lambda: volume_profile(raw_bars)),
                        ("wyckoff", lambda: wyckoff_context(raw_bars, params)), ("patterns", lambda: analyze_patterns(raw_bars))):
            try:
                out[key] = fn()
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"{key}:{type(e).__name__}")
        if out["weekly"]:
            out["alignment"] = alignment(daily_trend, out["weekly"].get("trend"))
        out["features_v2"] = build_features_v2(flatten_summary(blk), out["weekly"], out["volume_profile"], out["wyckoff"], daily_trend)
        out["feature_columns"] = {"n": len(FEATURE_COLUMNS_V2), "hash": columns_hash()}
        # C21 (log-only): pattern features + shadow evidence, stored beside the V2 row, never read by a decision / gate / score / model.
        out["pattern_features"] = build_pattern_features(out["patterns"])
        out["pattern_shadow"] = pattern_shadow(out["patterns"])
        out["pattern_columns"] = {"n": len(PATTERN_COLUMNS), "hash": pattern_columns_hash()}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"analyze:{type(e).__name__}")
    return out
