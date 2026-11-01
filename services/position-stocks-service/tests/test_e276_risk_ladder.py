"""E276 round 54: the risk ladder (orders/risk_ladder.py, pure) and its effect on the heat gate's limit. Plain runner: python3 tests/test_e276_risk_ladder.py"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import risk_ladder as rl


def test_stage_from_cell():
    assert rl.stage_from_cell(None) == 0 and rl.stage_from_cell({}) == 0
    assert rl.stage_from_cell({"r_n": 99, "lower_bound_r": 0.5}) == 0                 # too few trades
    assert rl.stage_from_cell({"r_n": 100, "lower_bound_r": 0.0}) == 0                # bound not above 0
    assert rl.stage_from_cell({"r_n": 100, "lower_bound_r": 0.01}) == 1
    assert rl.stage_from_cell({"r_n": 300, "lower_bound_r": 0.05}) == 1               # needs above +0.05
    assert rl.stage_from_cell({"r_n": 300, "lower_bound_r": 0.06}) == 2
    assert rl.stage_from_cell({"r_n": 300, "lower_bound_r": None}) == 0
    assert rl.stage_from_cell({"r_n": "x", "lower_bound_r": 1}) == 0                  # garbage never raises


def test_brake_drops_one_stage_and_never_below_zero():
    bad, good = [-1.0] * 20, [1.0] * 20
    assert rl.brake(bad) and not rl.brake(good) and not rl.brake([-1.0] * 19)         # a full window is needed
    c2 = {"r_n": 400, "lower_bound_r": 0.2}
    assert rl.stage(c2, good) == 2 and rl.stage(c2, bad) == 1
    assert rl.stage({"r_n": 120, "lower_bound_r": 0.1}, bad) == 0
    assert rl.stage(None, bad) == 0


def test_heat_limit():
    assert rl.heat_limit_pct(5.0, 0, 1.0) == 5.0 and rl.heat_limit_pct(5.0, 2, 1.0) == 7.0
    assert rl.heat_limit_pct(0.0, 2, 1.0) == 0.0                                       # no limit stays no limit
    assert rl.heat_limit_pct(5.0, 9, 1.0) == 8.0                                       # capped at stage 3


def test_heat_gate_limit_rises_with_stage():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import test_e276_heat_gate as hg
    with hg._Stub(own=(500.0, 0.0, 7288.0), peer=(0.0, 0.0, 0.0)) as st:             # own pool alone, 500 open
        fake = types.ModuleType("orders.risk_stage")
        saved = sys.modules.get("orders.risk_stage")
        try:
            fake.stage_for = lambda db, sid, side="LONG": 0
            sys.modules["orders.risk_stage"] = fake
            r0 = st.gate.reject_reason(None, 100.0, 98.0, 0, "LONG", "S1_CATALYST_UP")      # new risk 0: 500 > cap 364 at stage 0
            assert r0 and "stage=0" in r0, r0
            fake.stage_for = lambda db, sid, side="LONG": 2                                  # limit 7 % -> cap 510
            assert st.gate.reject_reason(None, 100.0, 98.0, 5, "LONG", "S1_CATALYST_UP") is None      # 500 + 10 = 510
            assert st.gate.reject_reason(None, 100.0, 98.0, 6, "LONG", "S1_CATALYST_UP") is not None  # 512 > 510
            assert st.gate.reject_reason(None, 100.0, 98.0, 5, "LONG", None) is not None              # no setup id: base limit
        finally:
            if saved is None:
                sys.modules.pop("orders.risk_stage", None)
            else:
                sys.modules["orders.risk_stage"] = saved


def test_short_cap_steps():
    assert [rl.short_cap(2, s) for s in (0, 1, 2, 3)] == [2, 3, 5, 5]
    assert rl.short_cap(4, 1) == 4 and rl.short_cap(1, 0) == 1                        # never below the configured base
    assert rl.short_cap("x", 9) == 5 and rl.short_cap(None, 0) == 1                   # garbage and out-of-range stages stay safe


def test_loss_days_brake():
    pool = 7288.0                                                                      # 4 % = Rs 291.52
    assert rl.loss_days_brake([-300, 50, -400, 10, 20], pool, 4.0)                     # 2 limit days in 5
    assert not rl.loss_days_brake([-300, 50, 80, 10, 20], pool, 4.0)                   # only 1
    assert not rl.loss_days_brake([-200, -250, -290], pool, 4.0)                       # close, but under the limit
    assert not rl.loss_days_brake([-300, 0, 0, 0, 0, -400], pool, 4.0)                 # the 6th day is outside the window
    assert not rl.loss_days_brake([-300, -400], 0, 4.0) and not rl.loss_days_brake([-300, -400], pool, 0)
    c2 = {"r_n": 400, "lower_bound_r": 0.2}
    assert rl.stage(c2, [1.0] * 20, True) == 1 and rl.stage(c2, [-1.0] * 20, True) == 0   # both brakes stack, floor 0
    assert rl.stage({"r_n": 120, "lower_bound_r": 0.1}, [1.0] * 20, True) == 0


def test_short_cap_for_uses_stage_and_switch():
    import config
    from orders import risk_stage as rs
    saved = rs.stage_for
    try:
        rs.stage_for = lambda db, sid, side="LONG": {"S4_CATALYST_DOWN": 2, "S2_RS_BREAKOUT": 1}.get(sid, 0)
        assert rs.short_cap_for(None, "S4_CATALYST_DOWN") == 5 and rs.short_cap_for(None, "S2_RS_BREAKOUT") == 3
        assert rs.short_cap_for(None, "S3_EXHAUSTION") == config.SHORT_MAX_POSITIONS
        assert rs.short_cap_for(None, None) == 5                                       # early guard: the best short setup
        config.RISK_LADDER_SHORT_CAP_ENABLED = False
        assert rs.short_cap_for(None, "S4_CATALYST_DOWN") == config.SHORT_MAX_POSITIONS
    finally:
        rs.stage_for = saved
        config.RISK_LADDER_SHORT_CAP_ENABLED = True


def test_risk_mult():
    assert [rl.risk_mult(x) for x in (0, 1, 2, 3, 9)] == [1.0, 1.25, 1.5, 1.5, 1.5]
    assert rl.risk_mult(2, -1) == 1.0 and rl.risk_mult("x") == 1.0
    import config
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capital", "ledger.py")).read()      # the helper only; ledger.py needs sqlalchemy
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_ladder_risk_mult")
    ns: dict = {}
    exec(compile(ast.Module([fn], []), "ledger_helper", "exec"), ns)
    f = ns["_ladder_risk_mult"]
    assert f(1.5) == 1.5 and f(0.5) == 1.0 and f(None) == 1.0 and f(9) == 2.0 and f(float("nan")) == 1.0
    from orders import risk_stage as rs
    saved = rs.stage_for
    try:
        rs.stage_for = lambda db, sid, side="LONG": 2
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.5 and rs.risk_mult_for(None, None) == 1.0
        config.RISK_LADDER_RISK_ENABLED = False
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0
    finally:
        rs.stage_for = saved
        config.RISK_LADDER_RISK_ENABLED = True


def test_stage3_sustained():
    now2 = {"r_n": 400, "lower_bound_r": 0.2}
    old2, old1 = {"r_n": 320, "lower_bound_r": 0.1}, {"r_n": 150, "lower_bound_r": 0.1}
    good, bad = [1.0] * 20, [-1.0] * 20
    assert rl.stage(now2, good, False, old2) == 3
    assert rl.stage(now2, good, False, old1) == 2 and rl.stage(now2, good, False, None) == 2     # not sustained
    assert rl.stage({"r_n": 150, "lower_bound_r": 0.1}, good, False, old2) == 1                    # stage 3 needs stage 2 now
    assert rl.stage(now2, bad, False, old2) == 2 and rl.stage(now2, bad, True, old2) == 1          # brakes drop it one level each
    assert rl.heat_limit_pct(5.0, 3, 1.0) == 8.0


def test_slot_blocked_and_stage_for():
    assert rl.slot_blocked({"r_n": 30, "avg_r": -0.01}) and not rl.slot_blocked({"r_n": 29, "avg_r": -0.5})
    assert not rl.slot_blocked({"r_n": 80, "avg_r": 0.0}) and not rl.slot_blocked({"r_n": 80, "avg_r": None}) and not rl.slot_blocked(None)
    import config
    from datetime import datetime
    from orders import risk_stage as rs
    import tz_utils
    saved_now, saved_data = tz_utils.ist_now, rs._data
    try:
        tz_utils.ist_now = lambda: datetime(2026, 11, 2, 9, 40)                       # slot 0930
        good = {"r_n": 150, "lower_bound_r": 0.1, "avg_r": 0.2}
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good, "S1_CATALYST_UP|0930": {"r_n": 40, "avg_r": -0.2}, "S1_CATALYST_UP|1000": {"r_n": 40, "avg_r": 0.3}}, "last": [], "loss_days": False}
        assert rs.stage_for(None, "S1_CATALYST_UP", "LONG") == 0                       # this slot loses
        config.RISK_LADDER_SLOT_ENABLED = False
        assert rs.stage_for(None, "S1_CATALYST_UP", "LONG") == 1                       # switch off: overall stage
        config.RISK_LADDER_SLOT_ENABLED = True
        tz_utils.ist_now = lambda: datetime(2026, 11, 2, 10, 10)                       # slot 1000, fine
        assert rs.stage_for(None, "S1_CATALYST_UP", "LONG") == 1
    finally:
        tz_utils.ist_now, rs._data = saved_now, saved_data
        config.RISK_LADDER_SLOT_ENABLED = True


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
