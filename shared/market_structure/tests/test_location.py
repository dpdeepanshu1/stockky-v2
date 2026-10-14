"""T-LOC: entry location (chase / stretch / room) and the structure R:R. Unknown references never read as good news; chase is the distance
above the nearest zone in ATR and is never negative; the structural stop sits below entry and the target above it."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import walk
from market_structure import fibonacci as fib_mod
from market_structure.engine import analyze
from market_structure.location import chase_reference, empty_location, location_block, structure_rr
from market_structure.params import MSParams

P = MSParams()
NOFIB = fib_mod.empty_fib()
NOLEVELS = {"supports": [], "resistances": []}


def _zone(top, bottom):
    return {"top": top, "bottom": bottom, "side": "bullish", "quality": 0.7}


def test_chase_is_distance_above_the_zone_top_in_atr():
    z = _zone(100.0, 98.0)
    loc = location_block(103.0, 2.0, z, None, NOFIB, NOLEVELS, None, {}, P)
    assert loc["chase_atr"] == 1.5 and loc["chase_ref"] == "ob"
    loc = location_block(99.0, 2.0, z, None, NOFIB, NOLEVELS, None, {}, P)          # inside the zone: no chase
    assert loc["chase_atr"] == 0.0


def test_no_reference_zone_means_no_chase_value_never_zero_by_default():
    loc = location_block(100.0, 2.0, None, None, NOFIB, NOLEVELS, None, {}, P)
    assert loc["chase_atr"] is None and loc["chase_ref"] is None


def test_zone_above_price_is_not_a_chase_reference():
    assert chase_reference(100.0, _zone(110.0, 105.0), None, NOFIB, []) is None


def test_stretch_needs_ema_and_is_signed():
    z = _zone(100.0, 98.0)
    assert location_block(103.0, 2.0, z, None, NOFIB, NOLEVELS, None, {}, P)["stretch_atr"] is None
    assert location_block(103.0, 2.0, z, None, NOFIB, NOLEVELS, None, {"ema20": 100.0}, P)["stretch_atr"] == 1.5
    assert location_block(97.0, 2.0, z, None, NOFIB, NOLEVELS, None, {"ema20": 100.0}, P)["stretch_atr"] == -1.5


def test_room_is_distance_to_the_next_resistance_above():
    lv = {"supports": [], "resistances": [{"price_low": 106.0, "price_high": 107.0, "strength": 0.2}]}
    loc = location_block(103.0, 2.0, None, None, NOFIB, lv, None, {}, P)
    assert loc["room_atr"] == 1.5
    assert location_block(103.0, 2.0, None, None, NOFIB, NOLEVELS, None, {}, P)["room_atr"] is None
    assert "no_resistance_known" in location_block(103.0, 2.0, None, None, NOFIB, NOLEVELS, None, {}, P)["flags"]


def test_far_chase_is_penalised():
    z = _zone(100.0, 98.0)
    near = location_block(100.5, 2.0, z, None, NOFIB, NOLEVELS, None, {}, P)
    far = location_block(110.0, 2.0, z, None, NOFIB, NOLEVELS, None, {}, P)
    assert far["entry_location_score"] < near["entry_location_score"] and "chase_gate" in far["flags"]


def test_bad_inputs_give_the_empty_block():
    assert location_block(float("nan"), 2.0, None, None, NOFIB, NOLEVELS, None, {}, P) == empty_location()
    assert location_block(100.0, 0.0, None, None, NOFIB, NOLEVELS, None, {}, P) == empty_location()


def test_score_is_clamped_0_100():
    z = _zone(100.0, 98.0)
    for px in (90.0, 100.0, 150.0):
        s = location_block(px, 2.0, z, None, NOFIB, NOLEVELS, None, {"extended": True, "extended_short": True}, P)["entry_location_score"]
        assert 0 <= s <= 100


def test_structure_rr_orders_stop_below_entry_below_target():
    for seed in range(10):
        b = analyze(walk(220, seed=seed, drift=0.25))
        rr = b["structure_rr"]
        if rr["stop"] is not None:
            assert rr["stop"] < rr["entry"]
        if rr["target"] is not None:
            assert rr["target"] > rr["entry"]
        if rr["rr"] is not None and rr["risk"] and rr["reward"]:
            assert abs(rr["rr"] - rr["reward"] / rr["risk"]) < 0.02


def test_structure_rr_without_any_structural_stop_is_invalid_not_a_number():
    rr = structure_rr(100.0, 2.0, None, [], None, NOLEVELS, NOFIB, {}, P)
    assert rr["stop"] is None and rr["rr"] is None and "invalid" in rr["flags"]
    rr = structure_rr(float("nan"), 2.0, None, [], None, NOLEVELS, NOFIB, {}, P)
    assert rr["rr"] is None and rr["flags"] == ["invalid"]


def test_ctx_entry_price_moves_the_rr():
    swings = [{"kind": "low", "price": 95.0, "label": "HL"}]
    lv = {"supports": [], "resistances": [{"price_low": 112.0, "price_high": 113.0, "strength": 0.5}]}
    a = structure_rr(100.0, 2.0, None, swings, None, lv, NOFIB, {}, P)
    b = structure_rr(100.0, 2.0, None, swings, None, lv, NOFIB, {"entry_price": 104.0}, P)
    assert a["rr"] is not None and b["rr"] is not None and b["rr"] < a["rr"]
