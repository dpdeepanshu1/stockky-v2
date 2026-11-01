"""E273 round 26: the daily-structure gate's soft reasons are size penalties in the Real Trade long entry. Plain runner: python3 tests/test_e273_structure_penalty.py (service folder)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from entry_engine import soft_gates as sg

CFG = dict(SOFT_GATES_MODE="penalty", STRUCTURE_MAX_CHASE_ATR=1.5, STRUCTURE_MAX_STRETCH_ATR=3.0, STRUCTURE_MIN_ROOM_ATR=0.5, STRUCTURE_MIN_RR=0.0, STRUCTURE_HARD_MULT=1.75)


def run(ver, **over):
    old = {k: getattr(config, k, None) for k in list(CFG) + list(over)}
    for k, v in {**CFG, **over}.items():
        setattr(config, k, v)
    try:
        return sg.structure(ver)
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def ver(reasons, **metrics):
    m = dict(chase_atr=None, stretch_atr=None, room_atr=None, rr=None)
    m.update(metrics)
    return {"would_reject": True, "reasons": reasons, "reason": "STRUCTURE: good stock, bad entry - " + "; ".join(reasons), "metrics": m}


def test_chase_below_the_ceiling_is_a_penalty():
    why, p = run(ver(["chase 2.0 ATR above the nearest demand/imbalance zone (max 1.5)"], chase_atr=2.0))
    assert why is None and 0.40 < p["structure_chase"] < 0.85


def test_chase_at_the_ceiling_still_refuses():
    v = ver(["chase 2.7 ATR above the nearest demand/imbalance zone (max 1.5)"], chase_atr=2.7)      # 1.5 x 1.75 = 2.625
    why, p = run(v)
    assert why == v["reason"] and p == {}


def test_stretch_and_room_together_give_two_penalties():
    why, p = run(ver(["price stretched 3.5 ATR above EMA20 (max 3)", "only 0.3 ATR of room to the next resistance (min 0.5)"], stretch_atr=3.5, room_atr=0.3))
    assert why is None and set(p) == {"structure_stretch", "structure_room"}


def test_room_nearly_gone_refuses():
    v = ver(["only 0.1 ATR of room to the next resistance (min 0.5)"], room_atr=0.1)
    assert run(v)[0] == v["reason"]


def test_a_hard_reason_keeps_the_refusal():
    v = ver(["chase 2.0 ATR above the nearest demand/imbalance zone (max 1.5)", "daily structure is bearish"], chase_atr=2.0)
    assert run(v) == (v["reason"], {})
    v2 = ver(["structural stop is too wide"])
    assert run(v2)[0] == v2["reason"]


def test_rr_penalty_and_hard_floor():
    why, p = run(ver(["structure R:R 1.0 below 1.2"], rr=1.0), STRUCTURE_MIN_RR=1.2)
    assert why is None and "structure_rr" in p
    v = ver(["structure R:R 0.5 below 1.2"], rr=0.5)
    assert run(v, STRUCTURE_MIN_RR=1.2)[0] == v["reason"]


def test_veto_mode_unscored_and_empty_cases():
    v = ver(["chase 2.0 ATR above the nearest demand/imbalance zone (max 1.5)"], chase_atr=2.0)
    assert run(v, SOFT_GATES_MODE="veto") == (v["reason"], {})
    assert run(ver(["chase 2.0 ATR above the nearest demand/imbalance zone (max 1.5)"], chase_atr=None))[0] is not None      # metric missing: the gate's own refusal
    assert run({"would_reject": False, "reasons": [], "reason": None, "metrics": {}}) == (None, {})
    assert run(None)[1] == {}


def test_wiring():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ent = open(os.path.join(base, "entry_engine", "entry.py")).read()
    assert "soft_gates.structure(_sg_ver)" in ent
    assert ent.index("soft_gates.structure(_sg_ver)") < ent.index("decision.structure_json = structure_gate.snapshot(") < ent.index("soft_gates.combine(")
    assert 'STRUCTURE_HARD_MULT = _og_float("STRUCTURE_HARD_MULT"' in open(os.path.join(base, "config.py")).read()


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
