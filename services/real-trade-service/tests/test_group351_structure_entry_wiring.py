"""group 351: entry.py wiring of the structure gate (DEMO mode, same harness as test_entry_evaluate_mode.py)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, structure_gate
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, pin, quotes, run, tick  # noqa: F401  (fixtures)


def _sum(**kw):
    s = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_trend": "bullish",
         "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0, "ms_stop": 96.0,
         "ms_target": 108.0, "ms_rr_flags": []}
    s.update(kw)
    return s


def _cand(db, summary, symbol="GOODCO"):
    c = make_candidate(db, symbol=symbol, conviction=80.0)
    c.raw_payload = json.dumps({"structure_summary": summary})
    db.commit()
    return c


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("STRUCTURE_GATE_MODE", "off"), ("STRUCTURE_GATE_DEMO_MODE", "off"), ("STRUCTURE_REQUIRE_DATA", False),
                 ("STRUCTURE_STOP_MODE", "off"), ("STRUCTURE_MAX_CHASE_ATR", 1.5), ("STRUCTURE_MAX_STRETCH_ATR", 3.0),
                 ("STRUCTURE_MIN_ROOM_ATR", 0.5), ("STRUCTURE_MIN_RR", 1.2), ("STRUCTURE_BEARISH_BLOCK", True),
                 ("STRUCTURE_REJECT_STOP_WIDE", True), ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0),
                 ("STRUCTURE_BEARISH_BOS_BARS", 5), ("STRUCTURE_STOP_MAX_RISK_MULT", 1.6), ("STRUCTURE_STOP_MIN_RISK_MULT", 0.5)):
        monkeypatch.setattr(config, k, v, raising=False)
    structure_gate.reset()


def _dec(db, symbol="GOODCO"):
    return db.query(models.TradeDecision).filter_by(symbol=symbol).one()


def test_old_shadow_value_is_live_and_waits(db, monkeypatch):
    # group370: there is no record-only mode; a stale "shadow" in an old .env is read as enforce
    assert config._sg_mode("STRUCTURE_GATE_MODE", "enforce") == "enforce"
    monkeypatch.setenv("STRUCTURE_GATE_DEMO_MODE", "shadow")
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", config._sg_mode("STRUCTURE_GATE_DEMO_MODE", "enforce"))
    _cand(db, _sum(ms_trend="bearish"))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert tally["entered"] == 0 and tally["waited"] == 1
    d = _dec(db)
    assert d.action == "WAIT" and d.gate_tag == "structure"
    doc = json.loads(d.structure_json)
    assert doc["would_reject"] is True and doc["enforced"] is True and doc["gate_mode"] == "enforce"


def test_enforce_waits_with_structure_tag(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    _cand(db, _sum(ms_chase_atr=2.5))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert tally["entered"] == 0 and tally["waited"] == 1
    d = _dec(db)
    assert d.action == "WAIT" and d.gate_tag == "structure" and "good stock, bad entry" in d.reasoning
    assert d.proposed_price and d.proposed_stop        # preview levels kept
    assert json.loads(d.structure_json)["enforced"] is True
    assert tally["entry_details"][0]["gate_tag"] == "structure"


def test_clean_structure_enters_even_when_enforced(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    _cand(db, _sum())
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    assert json.loads(_dec(db).structure_json)["would_reject"] is False


def test_no_summary_is_no_opinion_and_enters(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    make_candidate(db, symbol="GOODCO", conviction=80.0)
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    assert json.loads(_dec(db).structure_json)["opinion"] == "no_opinion"


def test_require_data_waits_on_no_opinion(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    monkeypatch.setattr(config, "STRUCTURE_REQUIRE_DATA", True)
    make_candidate(db, symbol="GOODCO", conviction=80.0)
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 0
    assert _dec(db).gate_tag == "structure_nodata"


def test_off_leaves_decision_untouched(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "off")
    _cand(db, _sum(ms_trend="bearish"))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    assert _dec(db).structure_json is None


def test_gate_crash_fails_open(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    _cand(db, _sum(ms_trend="bearish"))
    monkeypatch.setattr(structure_gate, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1


def test_structural_stop_not_used_when_kill_switch_off(db, monkeypatch):
    # STRUCTURE_STOP_MODE=off (kill switch): the ATR stop stays, no structural levels are computed
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    quotes_map = {"GOODCO": tick(100.0, symbol="GOODCO")}
    _cand(db, _sum(ms_stop=96.5))
    quotes(monkeypatch, quotes_map)
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    atr_stop = _dec(db).proposed_stop
    lv = json.loads(_dec(db).structure_json)["levels"]
    assert lv["use"] is False and atr_stop != pytest.approx(96.5, abs=0.01)


def test_structural_stop_on_replaces_atr_stop(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")   # group370: the structural levels are only computed while the gate is live
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on")
    _cand(db, _sum(ms_stop=96.5))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert _dec(db).proposed_stop == pytest.approx(96.5, abs=0.01)
    assert json.loads(_dec(db).structure_json)["levels"]["use"] is True


def test_structural_stop_on_ignored_when_insane(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on")
    _cand(db, _sum(ms_stop=80.0, ms_target=140.0))          # 20% stop = far wider than 1.6x the ATR risk
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert _dec(db).proposed_stop != pytest.approx(80.0, abs=0.01)
