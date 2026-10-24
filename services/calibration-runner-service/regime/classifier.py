"""Features -> market state. Deterministic, no randomness, no network. A weighted stress score (0..100) from whatever features exist,
plus hard floors for events that are never "just a number" (VIX spike, big gap down). Missing features are left out and the rest re-weighted;
if too little is known the answer is `normal` with data_ok=False (the layer never tightens on a guess).

The cut-offs and weights are a FIRST draft: regime_history_replay judges them and they are meant to change by a new CLASSIFIER_VERSION, never silently."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import STATES

CLASSIFIER_VERSION = "c1"

# (feature, weight, value at which the stress share is 0, value at which it is 1). lo > hi means "lower is worse".
COMPONENTS: Tuple[Tuple[str, float, float, float], ...] = (
    ("vix", 25, 12.0, 30.0),
    ("vix_pctile", 10, 50.0, 100.0),
    ("vix_chg_1d_pct", 10, 0.0, 15.0),
    ("atr14_pct", 10, 0.7, 1.8),
    ("rv10_pct", 10, 0.5, 1.5),
    ("ret_5d", 10, 0.0, -4.0),
    ("drawdown_20d_pct", 10, 1.0, 7.0),
    ("below_sma20_pct", 5, 0.0, 4.0),
    ("last_range_x_atr", 5, 1.0, 2.5),
    ("down_days_5", 5, 2.0, 5.0),
    # the open (refine / lock stage)
    ("gap_pct", 20, 0.0, -2.0),
    ("gap_x_atr", 10, 1.0, 3.0),
    # the session so far (tick stage)
    ("fall_from_high_pct", 20, 0.3, 2.0),
    ("day_ret_pct", 15, 0.0, -2.5),
    ("range_x_atr", 10, 0.8, 2.0),
)
# a score below the first number is calm, below the second normal, ... at or above the last one panic
CUTOFFS: Tuple[float, float, float, float] = (22.0, 42.0, 60.0, 78.0)
MIN_WEIGHT_SHARE = 0.35          # share of the daily-weight (the first ten components) that must be present, else data_ok=False

_DAILY = {c[0] for c in COMPONENTS[:10]}
_DAILY_WEIGHT = sum(c[1] for c in COMPONENTS[:10])

# (feature, test, minimum state, why): events that set a floor whatever the score says
FLOORS: Tuple[Tuple[str, str, float, str, str], ...] = (
    ("vix", ">=", 38.0, "panic", "VIX at or above 38"),
    ("vix", ">=", 30.0, "stressed", "VIX at or above 30"),
    ("vix_chg_1d_pct", ">=", 25.0, "stressed", "VIX up 25% or more in a day"),
    ("gap_pct", "<=", -3.0, "panic", "gap down of 3% or more"),
    ("gap_pct", "<=", -2.0, "stressed", "gap down of 2% or more"),
    ("day_ret_pct", "<=", -3.0, "panic", "index down 3% or more on the day"),
    ("day_ret_pct", "<=", -2.0, "stressed", "index down 2% or more on the day"),
    ("ret_1d", "<=", -3.0, "stressed", "index fell 3% or more yesterday"),
)


def _share(x: float, lo: float, hi: float) -> float:
    if lo == hi:
        return 0.0
    s = (x - lo) / (hi - lo)
    return 0.0 if s < 0 else 1.0 if s > 1 else s


def ordinal(state: str) -> int:
    return STATES.index(state)


def state_of(score: float) -> str:
    for i, cut in enumerate(CUTOFFS):
        if score < cut:
            return STATES[i]
    return STATES[-1]


def score(features: Mapping[str, Any]) -> Tuple[Optional[float], float, List[Dict[str, Any]]]:
    """(score or None, daily-weight share present, parts). parts = [{"feature","value","share","weight"}] for every feature used."""
    parts: List[Dict[str, Any]] = []
    wsum = got = daily_got = 0.0
    for name, w, lo, hi in COMPONENTS:
        v = features.get(name)
        if v is None or v != v:
            continue
        s = _share(float(v), lo, hi)
        parts.append({"feature": name, "value": round(float(v), 3), "share": round(s, 3), "weight": w})
        wsum += w
        got += w * s
        if name in _DAILY:
            daily_got += w
    if wsum <= 0:
        return None, 0.0, parts
    return 100.0 * got / wsum, daily_got / _DAILY_WEIGHT, parts


def _floor(features: Mapping[str, Any]) -> Tuple[int, List[str]]:
    lvl, why = 0, []
    for name, op, thr, st, text in FLOORS:
        v = features.get(name)
        if v is None or v != v:
            continue
        hit = float(v) >= thr if op == ">=" else float(v) <= thr
        if hit:
            why.append(text)
            lvl = max(lvl, ordinal(st))
    return lvl, why


def classify(features: Mapping[str, Any], prev_state: Optional[str] = None, ratchet: bool = False) -> Dict[str, Any]:
    """The state for these features.
    prev_state + ratchet=True: the state may only go UP from prev_state (used between 09:15 and the close so the day cannot flap down and
    loosen an overlay half way through). Returns {"state","score","data_ok","coverage","reasons","floor_reasons","parts","held","version"}."""
    sc, cover, parts = score(features)
    floor_lvl, floor_why = _floor(features)
    data_ok = sc is not None and cover >= MIN_WEIGHT_SHARE
    if not data_ok and floor_lvl == 0:
        st, held = "normal", False                       # too little known: never tighten on a guess
    else:
        base = ordinal(state_of(sc)) if sc is not None and data_ok else 0
        st, held = STATES[max(base, floor_lvl)], False
    if ratchet and prev_state in STATES and ordinal(prev_state) > ordinal(st):
        st, held = prev_state, True
    top = sorted(parts, key=lambda p: p["weight"] * p["share"], reverse=True)[:3]
    reasons = [f"{p['feature']}={p['value']}" for p in top if p["share"] > 0]
    return {"state": st, "score": None if sc is None else round(sc, 1), "data_ok": data_ok, "coverage": round(cover, 2), "reasons": reasons,
            "floor_reasons": floor_why, "parts": parts, "held": held, "version": CLASSIFIER_VERSION}
