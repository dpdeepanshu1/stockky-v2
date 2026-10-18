"""group 425: entry.py wiring of the Kronos gate (Gate 4f) and of Gate 5.6's forecast target (DEMO mode, same harness as
test_group351_structure_entry_wiring.py). The worker is replaced by a fake `fetch_forecast`, so nothing touches the network."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, kronos_gate
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, pin, quotes, run, tick  # noqa: F401  (fixtures)


def _fc(**kw):
    f = {"symbol": "GOODCO", "n_paths": 10, "horizon": 5, "p_up": 0.6, "p_target_first": 0.3, "p_stop_first": 0.2, "ev_pct": 1.5}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("KRONOS_GATE_MODE", "off"), ("KRONOS_GATE_DEMO_MODE", "off"), ("KRONOS_MIN_EDGE_X_COST", 1.0), ("KRONOS_MIN_P_TARGET", 0.10)):
        monkeypatch.setattr(config, k, v, raising=False)
    kronos_gate.reset()


def _worker(monkeypatch, forecast=None, raises=False):
    calls = []

    async def _fetch(symbol, ref_price, stop_pct, target_pct):
        calls.append((symbol, ref_price, stop_pct, target_pct))
        if raises:
            raise RuntimeError("boom")
        return forecast
    monkeypatch.setattr(kronos_gate, "fetch_forecast", _fetch)
    return calls


def _setup(db, monkeypatch):
    make_candidate(db, symbol="GOODCO", conviction=80.0)
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)


def _dec(db):
    return db.query(models.TradeDecision).filter_by(symbol="GOODCO").one()


def test_off_never_calls_the_worker_and_enters(db, monkeypatch):
    calls = _worker(monkeypatch, _fc(ev_pct=-5.0, p_target_first=0.0))
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    assert calls == []


def test_log_mode_records_but_never_blocks(db, monkeypatch):
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "log")
    calls = _worker(monkeypatch, _fc(ev_pct=-5.0, p_target_first=0.0))
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    st = kronos_gate.stats()
    assert len(calls) == 1 and st["would_reject"] == 1 and st["enforced"] == 0
    assert calls[0][2] == pytest.approx(3.0, abs=0.5) and calls[0][3] > calls[0][2]      # ATR stop / target percentages were passed


def test_enforce_rejects_with_kronos_tag(db, monkeypatch):
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "enforce")
    _worker(monkeypatch, _fc(ev_pct=-1.0))
    _setup(db, monkeypatch)
    tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert tally["entered"] == 0 and tally["waited"] == 1
    d = _dec(db)
    assert d.action == "WAIT" and d.gate_tag == "kronos" and d.reasoning.startswith("KRONOS:")
    assert d.proposed_price and d.proposed_stop                    # preview levels kept
    assert tally["entry_details"][0]["gate_tag"] == "kronos"


def test_enforce_good_forecast_enters(db, monkeypatch):
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "enforce")
    _worker(monkeypatch, _fc())
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1


@pytest.mark.parametrize("fc,raises", [(None, False), ({}, False), (_fc(n_paths=1), False), (None, True)])
def test_enforce_without_an_opinion_enters(db, monkeypatch, fc, raises):
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "enforce")
    _worker(monkeypatch, fc, raises=raises)
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1


def test_gate_5_6_uses_forecast_ev_in_enforce_but_not_in_log(db, monkeypatch):
    # EV 0.4% is above Gate 4f's floor (0.1 x cost) but below the 10x-cost edge floor at Gate 5.6 (about 3.7x at today's CNC charges on this trade size, so the
    # floor is set well above that and the test does not depend on the exact charge constants); the fixed ~6% target (about 55x) would pass it.
    for k, v in (("COST_MODEL_ENABLED", True), ("MIN_TRADE_VALUE", 1000.0), ("MIN_EDGE_TO_COST_RATIO", 10.0), ("KRONOS_MIN_EDGE_X_COST", 0.1)):
        monkeypatch.setattr(config, k, v)
    _worker(monkeypatch, _fc(ev_pct=0.4))
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "log")
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1            # log: the fixed target is still used


def test_gate_5_6_enforce_waits_on_cost_model(db, monkeypatch):
    for k, v in (("COST_MODEL_ENABLED", True), ("MIN_TRADE_VALUE", 1000.0), ("MIN_EDGE_TO_COST_RATIO", 10.0), ("KRONOS_MIN_EDGE_X_COST", 0.1)):
        monkeypatch.setattr(config, k, v)
    _worker(monkeypatch, _fc(ev_pct=0.4))
    monkeypatch.setattr(config, "KRONOS_GATE_DEMO_MODE", "enforce")
    _setup(db, monkeypatch)
    tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert tally["entered"] == 0 and _dec(db).gate_tag == "cost_model"
