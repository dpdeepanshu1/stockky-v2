"""stockky_ms.params - every threshold of the engine in one frozen dataclass. All values are HYPOTHESES: they are versioned through
params_hash(), which is stored with every output, and overridden only through from_dict()."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class MSParams:
    swing_left: int = 2
    swing_right: int = 2
    swing_min_amp_atr: float = 0.5
    atr_period: int = 14
    atr_min_bars: int = 5
    corp_action_jump_pct: float = 30.0
    gap_event_pct: float = 12.0
    min_bars: int = 60
    full_bars: int = 120
    bos_close_required: bool = True
    bos_min_atr: float = 0.10
    bos_fail_bars: int = 3
    bos_max_age: int = 15
    choch_max_age: int = 15
    equal_tol_pct: float = 0.15
    equal_tol_atr: float = 0.10
    sweep_min_atr: float = 0.10
    sweep_max_atr: float = 2.0
    sweep_reclaim_window: int = 3
    sweep_wick_frac: float = 0.5
    level_min_age_bars: int = 3
    sweep_max_age: int = 8
    fvg_min_gap_atr: float = 0.15
    fvg_min_body_atr: float = 0.5
    fvg_max_age: int = 30
    ob_atr_mult: float = 1.5
    ob_body_frac: float = 0.60
    ob_lookback: int = 5
    ob_max_age: int = 50
    ob_zone_mode: str = "body"
    ob_max_zones: int = 6
    fib_min_leg_atr: float = 3.0
    fib_min_leg_pct: float = 4.0
    fib_conf_tol_pct: float = 0.5
    level_merge_pct: float = 0.3
    level_n: int = 4
    max_chase_atr: float = 1.5
    rr_min: float = 1.5
    rr_pref: float = 2.0
    rr_abs_min: float = 1.2
    stop_buffer_atr: float = 0.15
    rr_min_risk_atr: float = 0.5
    rr_max_risk_atr: float = 4.0
    min_target_atr: float = 1.0
    objective_pct: float = 5.0
    tick: float = 0.01
    bar_tolerance_pct: float = 0.05
    limited_group_cap: float = 0.6
    vol_mean_bars: int = 20
    location_weights: Tuple[Tuple[str, int], ...] = (("zone", 30), ("fib", 15), ("support", 15), ("reclaim", 10), ("room", 20), ("vwap", 10))

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["location_weights"] = [list(x) for x in self.location_weights]
        return d


def params_hash(p: MSParams) -> str:
    blob = json.dumps(p.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


def from_dict(overrides: Optional[Dict[str, Any]]) -> MSParams:
    """MSParams with the given overrides. Unknown keys and values that cannot be coerced to the field's type are ignored."""
    if not overrides:
        return MSParams()
    fields = {f.name: f for f in dataclasses.fields(MSParams)}
    clean: Dict[str, Any] = {}
    default = MSParams()
    for k, v in overrides.items():
        if k not in fields or k == "location_weights":
            continue
        cur = getattr(default, k)
        try:
            if isinstance(cur, bool):
                clean[k] = bool(v)
            elif isinstance(cur, int):
                clean[k] = int(v)
            elif isinstance(cur, float):
                clean[k] = float(v)
            else:
                clean[k] = str(v)
        except (TypeError, ValueError):
            continue
    return dataclasses.replace(default, **clean)
