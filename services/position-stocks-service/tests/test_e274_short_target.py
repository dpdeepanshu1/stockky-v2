"""E274 round 43: a reachable short target (orders/short_target.py) and its place in plan_short."""
import json
import types
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from capital import ledger
from orders import short_entry as se, short_gates, short_target as st, slot_ladder

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 2, 10, 30, tzinfo=IST)
S4 = {"id": "S4_CATALYST_DOWN", "size_mult": 0.5, "slot": "0930", "confirmations": ["tape_selling", "below_vwap"], "trigger": "x"}
CELL = {"n": 40, "gain_n": 20, "typical_gain_pct": 0.9, "ttp_n": 20, "ttp_min": 12.0}
KEY = "S4_CATALYST_DOWN|0930"


def lv(live=100.0, target_pct=2.0, stop_pct=1.0):
    return types.SimpleNamespace(target_price=round(live * (1 - target_pct / 100), 2), target_pct=target_pct, stop_price=round(live * (1 + stop_pct / 100), 2), stop_pct=stop_pct, side="SHORT")


def cells(monkeypatch, c):
    monkeypatch.setattr(slot_ladder, "cells", lambda db: c)


def run(L, **kw):
    kw.setdefault("setup", None)
    kw.setdefault("verdict", {})
    kw.setdefault("tick", 0.05)
    return st.adjust(L, 100.0, symbol="X", now=NOW, db=object(), **kw)


def test_an_s4_cell_with_enough_winners_pulls_the_target_nearer(monkeypatch):
    cells(monkeypatch, {KEY: CELL})
    L = lv()
    note = run(L, setup=S4)
    # shrunk toward the plan target 2.0: w = 20/50 = 0.4 -> 0.4*0.9 + 0.6*2.0 = 1.56 %
    assert note["learned"]["cell"] == KEY and 98.4 <= L.target_price <= 98.5 and L.target_pct == pytest.approx(1.55, abs=0.06)
    assert L.stop_price == 101.0 and L.stop_pct == 1.0                                   # the stop never moves


@pytest.mark.parametrize("c", [{}, {KEY: dict(CELL, n=29)}, {KEY: dict(CELL, gain_n=9)}])
def test_a_thin_cell_or_a_missing_cell_changes_nothing(monkeypatch, c):
    cells(monkeypatch, c)
    L = lv()
    assert run(L, setup=S4) == {} and L.target_price == 98.0


def test_the_all_slot_cell_is_the_fallback(monkeypatch):
    cells(monkeypatch, {"S4_CATALYST_DOWN|ALL": CELL})
    assert "learned" in run(lv(), setup=S4)


@pytest.mark.parametrize("setup", [{"id": None}, {"id": "LEGACY_SHORTLIST", "slot": "0930"}, None])
def test_a_legacy_or_missing_setup_is_never_retargeted_from_a_cell(monkeypatch, setup):
    cells(monkeypatch, {"LEGACY_SHORTLIST|0930": CELL, "LEGACY_SHORTLIST|ALL": CELL, KEY: CELL})
    L = lv()
    assert run(L, setup=setup) == {} and L.target_price == 98.0


def test_a_learned_gain_beyond_the_plan_target_is_never_used(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, typical_gain_pct=6.0)})                           # shrunk 0.4*6 + 0.6*2 = 3.6 % - farther than 2 %
    L = lv()
    assert run(L, setup=S4) == {} and L.target_price == 98.0


def test_a_learned_gain_below_the_minimum_keeps_the_plan_target(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, typical_gain_pct=0.1, gain_n=200)})              # shrunk ~0.3 %, under SHORT_TARGET_MIN_PCT (0.5)
    L = lv()
    assert run(L, setup=S4) == {} and L.target_price == 98.0


def test_the_wall_trims_the_target_one_tick_above_the_node(monkeypatch):
    cells(monkeypatch, {})
    L = lv()
    note = run(L, verdict={"reliable": True, "hvn_below": 98.9})
    assert note["wall"]["target"] == 98.95 and L.target_price == 98.95 and L.target_pct == pytest.approx(1.05, abs=0.01)


@pytest.mark.parametrize("verdict", [{}, {"reliable": False, "hvn_below": 98.9}, {"reliable": True}, {"reliable": True, "hvn_below": 97.0},
                                     {"reliable": True, "hvn_below": 101.0}, {"reliable": True, "hvn_below": 99.7}])
def test_a_wall_that_does_not_help_changes_nothing(monkeypatch, verdict):
    cells(monkeypatch, {})
    L = lv()
    assert run(L, verdict=verdict) == {} and L.target_price == 98.0


def test_no_verdict_and_the_flow_gate_off_means_no_new_call(monkeypatch):
    from orders import flow_gate
    monkeypatch.setattr(flow_gate, "mode", lambda: "off")
    monkeypatch.setattr(flow_gate, "fetch_verdict", lambda *a, **k: pytest.fail("no market-data call expected"))
    assert st.wall_price("X") is None


def test_both_rules_apply_in_order_and_only_nearer(monkeypatch):
    cells(monkeypatch, {KEY: CELL})
    L = lv()
    note = run(L, setup=S4, verdict={"reliable": True, "hvn_below": 99.2})
    assert "learned" in note and "wall" in note and L.target_price == 99.25


def test_the_switches(monkeypatch):
    cells(monkeypatch, {KEY: CELL})
    monkeypatch.setattr(config, "SHORT_TARGET_LEARNED_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "SHORT_TARGET_HVN_ENABLED", False, raising=False)
    L = lv()
    assert run(L, setup=S4, verdict={"reliable": True, "hvn_below": 99.2}) == {} and L.target_price == 98.0


def test_junk_never_raises():
    assert st.adjust(object(), "x", symbol="X") == {} and st.adjust(None, None, symbol="X") == {}
    assert st.stamp("not json", {"a": 1}) == "not json" and st.stamp('{"x":1}', {}) == '{"x":1}'


def test_stamp_adds_the_note_and_keeps_the_rest():
    out = json.loads(st.stamp('{"setup":{"id":"S4"}}', {"wall": {"target": 98.95}}))
    assert out["setup"] == {"id": "S4"} and out["short_target"]["wall"]["target"] == 98.95


# ---- plan_short -----------------------------------------------------------
@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = ledger._get_or_create(s)
    row.total_allocated_capital, row.available_capital = 100000.0, 100000.0
    s.commit()
    se.reset()
    box = {"lv": None, "setup": {"id": None, "skip": "NO_SETUP"}, "tm": [], "adjust": None}
    monkeypatch.setattr(config, "SHORT_MODE", "shadow", raising=False)
    monkeypatch.setattr(short_gates, "gather_facts", lambda *a, **k: {"live": 100.0, "quote": None})
    monkeypatch.setattr(short_gates, "evaluate", lambda f, now=None, **kw: None)
    monkeypatch.setattr(short_gates, "board_gate_short", lambda f, now=None: None)
    for mod, name in ((se.entry_pause, "all_paused"), (se.stopout_guard, "entry_reason"), (se.calendar_gate, "entry_block_reason"), (se.calendar_gate, "scalp_cutoff_reason"),
                      (se.depth_gate, "max_qty_from_book"), (se.depth_gate, "max_qty_from_depth20"), (se.cost_gate, "reject_reason"), (se.entry_pause, "symbol_blocked")):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda *a, **k: (None, None))
    from screening import opening_gate
    monkeypatch.setattr(opening_gate, "stop_reject", lambda *a, **k: None)
    monkeypatch.setattr(se, "compute_levels", lambda *a, **k: box["lv"])
    monkeypatch.setattr(se.tm_gate, "check_ev", lambda sym, side, stop_pct, target_pct, live: (box["tm"].append(target_pct) or None, None))
    monkeypatch.setattr(se.setup_short, "decide", lambda **k: box["setup"])
    monkeypatch.setattr(se.squeeze_guard, "reason", lambda **k: None)
    monkeypatch.setattr(se.short_target, "adjust", lambda levels, live, **k: (box["adjust"](levels) if box["adjust"] else {}))
    yield types.SimpleNamespace(db=s, box=box)
    s.close()


def cand():
    return types.SimpleNamespace(symbol="X", window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0)


NEG = types.SimpleNamespace(event_polarity=-1)
PLAIN = types.SimpleNamespace(event_polarity=None)


def test_the_plan_judges_the_new_target_and_does_not_loosen_the_floor(env, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 2.0, raising=False)               # conftest turns the floor off
    env.box["lv"] = lv(target_pct=3.0, stop_pct=1.0)                                       # 3:1, passes the 2:1 floor
    def move(L):
        L.target_price, L.target_pct = 98.5, 1.5                                           # 1.5 : 1
        return {"wall": {"target": 98.5}}
    env.box["adjust"] = move
    assert se.plan_short(env.db, cand(), PLAIN, None, NOW) is None
    assert [r.reason.split(":")[0] for r in env.db.query(models.ScalpCandidateLog).all()] == ["SHORT_LEVELS"]


def test_a_target_that_still_pays_goes_on_and_the_time_machine_sees_it(env, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 2.0, raising=False)
    env.box["lv"] = lv(target_pct=3.0, stop_pct=0.7)
    def move(L):
        L.target_price, L.target_pct = 98.5, 1.5                                           # 2.14 : 1
        return {"learned": {"gain_pct": 1.5}}
    env.box["adjust"] = move
    plan = se.plan_short(env.db, cand(), PLAIN, None, NOW)
    assert plan is not None and plan["target_note"] == {"learned": {"gain_pct": 1.5}} and plan["levels"].target_pct == 1.5
    assert env.box["tm"] == [1.5]


def test_no_change_means_an_empty_note_and_the_old_target(env):
    env.box["lv"] = lv(target_pct=2.0, stop_pct=0.9)
    plan = se.plan_short(env.db, cand(), PLAIN, None, NOW)
    assert plan is not None and plan["target_note"] == {} and plan["levels"].target_pct == 2.0

