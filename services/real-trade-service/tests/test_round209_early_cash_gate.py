"""Round 209: a candidate whose single share costs more than the spendable cash is WAITed before any fetch-based gate (VIX, order flow, structure, forecasts),
with the same verdict / risk event the risk engine's own cash cap would give later. ENTRY_EARLY_CASH_GATE=0 restores the old order."""
from __future__ import annotations

import math

import pytest

import models
from entry_engine import entry
from tests.test_entry_evaluate_mode import (  # noqa: F401
    pin, db, make_candidate, tick, quotes, approve_all, run,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENTRY_EARLY_CASH_GATE", raising=False)


class TestHelper:
    def test_unaffordable_gives_the_risk_engine_wording(self):
        assert entry._one_share_cash_reason(11900.0, 4800.0) == "\u20b94800.00 available is not enough for even 1 share at \u20b911900.00."

    def test_affordable_or_exact_is_none(self):
        assert entry._one_share_cash_reason(100.0, 100.0) is None
        assert entry._one_share_cash_reason(100.0, 5000.0) is None

    @pytest.mark.parametrize("price,cash", [(None, 10.0), (0, 10.0), (-5, 10.0), ("x", 10.0), (50.0, None), (50.0, "y"), (50.0, math.nan)])
    def test_unknown_inputs_fail_open(self, price, cash):
        assert entry._one_share_cash_reason(price, cash) is None

    @pytest.mark.parametrize("val,on", [(None, True), ("", True), ("1", True), ("true", True), ("0", False), ("false", False), ("No", False), (" OFF ", False)])
    def test_env_switch(self, monkeypatch, val, on):
        if val is None:
            monkeypatch.delenv("ENTRY_EARLY_CASH_GATE", raising=False)
        else:
            monkeypatch.setenv("ENTRY_EARLY_CASH_GATE", val)
        assert entry._early_cash_gate_enabled() is on


def _low_cash(db, cash=4800.0):
    acct = db.query(models.TradeAccount).filter_by(mode="DEMO").first()
    acct.cash_available = cash
    db.commit()


def _fetch_spy(monkeypatch):
    seen: list = []
    real = entry.vix_gate.snapshot

    def _snap(*a, **k):
        seen.append(1)
        return real(*a, **k)
    monkeypatch.setattr(entry.vix_gate, "snapshot", _snap)
    return seen


class TestEvaluateMode:
    def test_unaffordable_share_waits_before_any_fetch_gate(self, db, monkeypatch):
        _low_cash(db)
        make_candidate(db, symbol="MRFLIKE", signal_price=140000.0)
        quotes(monkeypatch, {"MRFLIKE": tick(140000.0, atr=2800.0, symbol="MRFLIKE")})
        approve_all(monkeypatch)
        spy = _fetch_spy(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        d = tally["entry_details"][0]
        assert d["action"] == "WAIT" and d["gate_tag"] == "cash_below_one_share" and d["risk_verdict"] == "REJECTED"
        assert "not enough for even 1 share" in d["reasoning"]
        assert tally["entered"] == 0 and spy == []
        ev = db.query(models.TradeRiskEvent).filter_by(symbol="MRFLIKE").one()
        assert ev.check_name == "cash_available_cap" and ev.verdict == "REJECTED"
        dec = db.query(models.TradeDecision).filter_by(symbol="MRFLIKE").one()
        assert dec.action == "WAIT" and dec.gate_tag == "cash_below_one_share" and dec.risk_verdict == "REJECTED"

    def test_affordable_share_is_not_stopped_by_the_gate(self, db, monkeypatch):
        _low_cash(db, 100000.0)
        make_candidate(db, symbol="OKCO", signal_price=100.0)
        quotes(monkeypatch, {"OKCO": tick(100.0, symbol="OKCO")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert all(d.get("gate_tag") != "cash_below_one_share" for d in tally["entry_details"])

    def test_env_off_leaves_the_decision_to_the_risk_engine(self, db, monkeypatch):
        monkeypatch.setenv("ENTRY_EARLY_CASH_GATE", "0")
        _low_cash(db)
        make_candidate(db, symbol="MRFLIKE", signal_price=140000.0)
        quotes(monkeypatch, {"MRFLIKE": tick(140000.0, atr=2800.0, symbol="MRFLIKE")})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert all(d.get("gate_tag") != "cash_below_one_share" for d in tally["entry_details"])
