"""E274 round 41: plan_short behaviour in the 09:21-09:45 window and with the squeeze guard. Real models on in-memory SQLite, no order, no network."""
import types
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from capital import ledger
from orders import short_entry as se, short_gates

IST = timezone(timedelta(hours=5, minutes=30))
EARLY = datetime(2026, 11, 2, 9, 30, tzinfo=IST)
LATER = datetime(2026, 11, 2, 10, 30, tzinfo=IST)
S4 = {"id": "S4_CATALYST_DOWN", "size_mult": 0.25, "confirmations": ["tape", "below_vwap"], "trigger": "x"}


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = ledger._get_or_create(s)
    row.total_allocated_capital, row.available_capital = 100000.0, 100000.0
    s.commit()
    se.reset()
    calls = {"evaluate": [], "decide": 0, "squeeze": []}
    monkeypatch.setattr(config, "SHORT_MODE", "live", raising=False)
    monkeypatch.setattr(short_gates, "gather_facts", lambda *a, **k: {"live": 100.0, "quote": None})

    def _eval(f, now=None, early=False):
        calls["evaluate"].append(early)
        return None
    monkeypatch.setattr(short_gates, "evaluate", _eval)
    for mod, name in ((se.entry_pause, "all_paused"), (se.stopout_guard, "entry_reason"), (se.calendar_gate, "entry_block_reason"),
                      (se.calendar_gate, "scalp_cutoff_reason"), (se.depth_gate, "max_qty_from_book"), (se.depth_gate, "max_qty_from_depth20"),
                      (se.cost_gate, "reject_reason"), (se.entry_pause, "symbol_blocked")):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(se.flow_gate, "entry_check", lambda *a, **k: (None, '{"tape":{"reliable":true}}'))
    from screening import opening_gate
    monkeypatch.setattr(opening_gate, "stop_reject", lambda *a, **k: None)
    lv = types.SimpleNamespace(stop_price=101.0, target_price=98.0, stop_pct=1.0, target_pct=2.0, side="SHORT")
    monkeypatch.setattr(se, "compute_levels", lambda *a, **k: lv)
    monkeypatch.setattr(se.tm_gate, "check_ev", lambda *a, **k: (None, None))
    box = {"setup": {"id": None, "skip": "NO_SETUP"}}

    def _decide(**k):
        calls["decide"] += 1
        return box["setup"]
    monkeypatch.setattr(se.setup_short, "decide", _decide)
    monkeypatch.setattr(se.squeeze_guard, "reason", lambda **k: (calls["squeeze"].append(k) or box.get("sq")))
    yield types.SimpleNamespace(db=s, calls=calls, box=box)
    s.close()


def cand(sym="X"):
    return types.SimpleNamespace(symbol=sym, window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0)


def reasons(db):
    return [r.reason for r in db.query(models.ScalpCandidateLog).all()]


def test_before_0945_a_short_with_no_s4_match_is_refused(env):
    assert se.plan_short(env.db, cand(), None, {"conviction": 70}, EARLY) is None
    assert env.calls["evaluate"] == [True]                                  # the early walk was used
    assert any(r.startswith("SHORT_TOO_EARLY:") for r in reasons(env.db))
    assert env.calls["squeeze"] == []                                       # refused before the guard


def test_before_0945_an_s4_match_goes_on_at_the_engine_size(env):
    env.box["setup"] = S4
    base_plan = se.plan_short(env.db, cand(), None, {"conviction": 70}, LATER)       # 10:30, same S4 size for comparison below
    se.reset()
    plan = se.plan_short(env.db, cand(), None, {"conviction": 70}, EARLY)
    assert plan is not None and plan["early"] is True and plan["setup"]["id"] == "S4_CATALYST_DOWN"
    assert base_plan is not None and base_plan["early"] is False
    assert env.calls["evaluate"] == [False, True]
    assert plan["qty"] == base_plan["qty"] and plan["qty"] < 200                       # both sized by the engine's 0.25, below the plain 200


def test_after_0945_a_plain_short_still_goes_on_with_the_old_call(env):
    plan = se.plan_short(env.db, cand(), None, {"conviction": 70}, LATER)
    assert plan is not None and plan["early"] is False and plan["qty"] == 200 and plan["setup"]["id"] is None
    assert env.calls["evaluate"] == [False]


def test_the_early_window_switch_off_restores_0945(env, monkeypatch):
    monkeypatch.setattr(config, "SHORT_EARLY_S4_ENABLED", False, raising=False)
    env.box["setup"] = S4
    se.plan_short(env.db, cand(), None, {"conviction": 70}, EARLY)
    assert env.calls["evaluate"] == [False]                                 # plain walk: the real time gate refuses at 09:30 (here the fake lets it by, so the plan runs as a normal short)


def test_a_squeeze_refuses_a_non_s4_short_and_is_logged(env):
    env.box["sq"] = "SQUEEZE: price 101.00 is at or above the opening-range high 100.50, volume rising (1.8x the opening range) and the tape shows buyers"
    assert se.plan_short(env.db, cand(), None, {"conviction": 70}, LATER) is None
    assert any(r.startswith("SHORT_SQUEEZE:") for r in reasons(env.db))
    assert env.calls["squeeze"][0]["s4"] is False
    assert env.db.query(models.ScalpShortShadow).count() == 0


def test_the_guard_is_told_when_the_short_is_an_s4_match(env):
    env.box["setup"] = S4
    assert se.plan_short(env.db, cand(), None, {"conviction": 70}, LATER) is not None
    assert env.calls["squeeze"][0]["s4"] is True


def test_the_early_plan_needs_an_s4_match(env):
    assert se.plan_short(env.db, cand("A"), None, {"conviction": 70}, EARLY) is None          # no S4 before 09:45: no plan
    env.box["setup"] = S4
    r = se.plan_short(env.db, cand("B"), None, {"conviction": 70}, EARLY)
    assert r is not None and r["qty"] > 0
