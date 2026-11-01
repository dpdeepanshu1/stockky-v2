"""E273 round 32: Position Stocks - the daily-structure gate's soft reasons (chase / stretch / room) are SIZE PENALTIES; hard reasons still refuse; late penalties
(round 31 rvol + round 32 structure) rescale the setup size and the stamp. Plain runner: python3 tests/test_e273_structure_penalty.py (service folder)."""
from __future__ import annotations

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

import config  # noqa: E402
from orders import soft_gates as sg  # noqa: E402
from orders import structure_gate as sgate  # noqa: E402
from orders import setup_stamp  # noqa: E402

CFG = dict(SOFT_GATES_MODE="penalty", STRUCTURE_MAX_CHASE_ATR=1.5, STRUCTURE_MAX_STRETCH_ATR=3.0, STRUCTURE_MIN_ROOM_ATR=0.5, STRUCTURE_HARD_MULT=1.75,
           SOFT_GATE_MAX_PENALTY=3.0, STRUCTURE_GATE_MODE="enforce", STRUCTURE_REQUIRE_DATA=False)


def _with(over=None):
    old = {k: getattr(config, k, None) for k in list(CFG) + list(over or {})}
    for k, v in {**CFG, **(over or {})}.items():
        setattr(config, k, v)
    return old


def _restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def run(ver, **over):
    old = _with(over)
    try:
        return sg.structure_penalties(ver)
    finally:
        _restore(old)


def ver(reasons, **metrics):
    m = dict(chase_atr=None, stretch_atr=None, room_atr=None)
    m.update(metrics)
    return {"would_reject": True, "reasons": reasons, "reason": "STRUCTURE: " + "; ".join(reasons), "metrics": m}


CHASE = "chase 2.0 daily ATR above the nearest demand zone (max 1.5)"
STRETCH = "stretched 3.5 daily ATR above EMA20 (max 3)"
ROOM = "only 0.3 daily ATR of room to the next resistance (min 0.5)"


def test_chase_below_the_ceiling_is_a_penalty_that_grows():
    why, p = run(ver([CHASE], chase_atr=1.6))
    why2, p2 = run(ver([CHASE], chase_atr=2.4))
    assert why is None and why2 is None and 1.0 <= p["structure_chase"] < p2["structure_chase"] <= sg.cap()


def test_chase_at_the_ceiling_still_refuses():
    v = ver([CHASE], chase_atr=2.7)                      # 1.5 x 1.75 = 2.625
    why, p = run(v)
    assert why == v["reason"] and p == {}


def test_stretch_and_room_together_give_two_penalties():
    why, p = run(ver([STRETCH, ROOM], stretch_atr=3.5, room_atr=0.3))
    assert why is None and set(p) == {"structure_stretch", "structure_room"}


def test_room_nearly_gone_refuses():
    v = ver(["only 0.1 daily ATR of room to the next resistance (min 0.5)"], room_atr=0.1)       # hard floor = 0.5 / 4 = 0.125
    assert run(v)[0] == v["reason"]


def test_a_hard_reason_keeps_the_refusal():
    v = ver([CHASE, "daily structure is bearish"], chase_atr=2.0)
    assert run(v) == (v["reason"], {})
    v2 = ver(["fresh bearish break of structure 2 bar(s) ago"])
    assert run(v2)[0] == v2["reason"]
    v3 = ver(["breakout 2 bar(s) ago came on thin volume (0.80x the 20-day average, wants 1.2x)"])
    assert run(v3)[0] == v3["reason"]


def test_veto_mode_unscored_and_empty_cases():
    v = ver([CHASE], chase_atr=2.0)
    assert run(v, SOFT_GATES_MODE="veto") == (v["reason"], {})
    assert run(ver([CHASE], chase_atr=None))[0] is not None          # metric missing: the gate's own refusal
    assert run({"would_reject": False, "reasons": [], "reason": None, "metrics": {}}) == (None, {})
    assert run(None)[1] == {}


def _fake_check(summary_verdict, penalties):
    """sgate.check with the summary fetch stubbed and evaluate returning a fixed verdict."""
    old = _with()
    f0, e0 = sgate.fetch_summary, sgate.evaluate
    sgate.fetch_summary = lambda s: {"ms_quality": "ok"}
    sgate.evaluate = lambda summ, ltp: dict(summary_verdict, opinion="ok", observations=[], no_data=None)
    try:
        return sgate.check("TESTCO", 100.0, penalties=penalties)
    finally:
        sgate.fetch_summary, sgate.evaluate = f0, e0
        _restore(old)
        sgate.reset()


def test_check_sizes_down_instead_of_refusing_and_snapshot_is_not_enforced():
    pens: dict = {}
    block, snap = _fake_check(ver([CHASE], chase_atr=1.8), pens)
    assert block is None and "structure_chase" in pens
    assert json.loads(snap)["enforced"] is False


def test_check_without_a_penalty_dict_or_with_a_hard_reason_still_refuses():
    block, _ = _fake_check(ver([CHASE], chase_atr=1.8), None)
    assert block and block.startswith("STRUCTURE_BAD_LOCATION:")
    pens: dict = {}
    block2, _ = _fake_check(ver([CHASE, "daily structure is bearish"], chase_atr=1.8), pens)
    assert block2 and pens == {}


def test_late_size_rescales_the_setup_size():
    assert sg.late_size(0.5, {}, {}) == 0.5                                   # nothing late = unchanged
    assert sg.late_size(None, {}, {"rvol": 1.0}) == 0.5                       # no setup, no early penalty: 1 / (1 + 1)
    assert sg.late_size(0.5, {"near_day_high": 1.0}, {"structure_chase": 1.0}) < 0.5          # 1.0*(2)/(3) *0.5*... shrinks further
    assert sg.late_size(0.5, {"near_day_high": 1.0}, {"structure_chase": 1.0}) == round(max(0.25, 0.5 * 2.0 / 3.0), 4)
    assert sg.late_size(0.3, {}, {"rvol": 3.0}) == 0.25                       # never under the floor
    assert sg.late_size("x", {}, {"rvol": 1.0}) == "x"                        # unusable input = unchanged


def test_restamp_updates_penalties_and_size_only_when_stamped():
    fj = json.dumps({"setup": {"id": "S1_CATALYST_UP", "penalties": {"a": 1.0}, "size_mult": 0.5}, "tape": {"x": 1}})
    out = json.loads(setup_stamp.restamp(fj, {"a": 1.0, "rvol": 1.5}, 0.2))
    assert out["setup"]["penalties"] == {"a": 1.0, "rvol": 1.5} and out["setup"]["size_mult"] == 0.2 and out["setup"]["id"] == "S1_CATALYST_UP" and out["tape"] == {"x": 1}
    assert setup_stamp.restamp("{}", {"a": 1}, 0.2) == "{}" and setup_stamp.restamp(None, {}, 0.2) is None


def test_default_and_wiring():
    assert config.STRUCTURE_HARD_MULT == 1.75
    src = open(os.path.join(ROOT, "orders", "entry.py")).read()
    assert "penalties=_struct_pen" in src and "soft_gates.late_size(" in src and "setup_stamp.restamp(" in src and "_pen_early = dict(_penalties)" in src
    assert src.index("_pen_early = dict(_penalties)") < src.index("setup_live.classify_long(") < src.index("structure_gate.check(") < src.index("soft_gates.late_size(")
    assert src.index('_penalties["rvol"]') < src.index("soft_gates.late_size(")      # the rvol penalty of round 31 is rescaled too
    assert src.index("soft_gates.late_size(") < src.index("setup_live.extra_factor(")      # the size is final before shares are cut


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1; print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
