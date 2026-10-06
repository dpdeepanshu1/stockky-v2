"""
group 214b (audit item A7, "capital check order") - the entry loop claims cash best-conviction-first.

Before: candidates were walked in received_at order; each staged candidate reserved cash before Gate 6 ranked them,
so a weak early candidate could use up the room and a stronger one queued seconds later was rejected for capital.
Now the batch is walked highest conviction first (ties keep received_at order). ENTRY_CAPITAL_ORDER_BY_CONVICTION=0
restores received_at order.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group214b_capital_order.py -q
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import models
from entry_engine import entry
from risk_engine.engine import RiskResult, RiskVerdict

# reuse the fixtures / helpers of the evaluate_mode suite (autouse pin, db, make_candidate, tick, quotes)
from tests.test_entry_evaluate_mode import (  # noqa: F401
    pin, db, make_candidate, tick, quotes, run,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENTRY_CAPITAL_ORDER_BY_CONVICTION", raising=False)


def _c(symbol, conviction):
    return SimpleNamespace(symbol=symbol, conviction_score=conviction)


class TestHelper:
    def test_highest_conviction_first(self):
        out = entry._order_candidates_for_capital([_c("A", 60), _c("B", 90), _c("C", 75)])
        assert [c.symbol for c in out] == ["B", "C", "A"]

    def test_ties_keep_received_order(self):
        out = entry._order_candidates_for_capital([_c("A", 70), _c("B", 70), _c("C", 70)])
        assert [c.symbol for c in out] == ["A", "B", "C"]

    def test_none_conviction_sorts_last(self):
        out = entry._order_candidates_for_capital([_c("A", None), _c("B", 10)])
        assert [c.symbol for c in out] == ["B", "A"]

    @pytest.mark.parametrize("val", ["0", "false", "No", "OFF", " 0 "])
    def test_env_off_keeps_received_order(self, monkeypatch, val):
        monkeypatch.setenv("ENTRY_CAPITAL_ORDER_BY_CONVICTION", val)
        cs = [_c("A", 60), _c("B", 90)]
        assert entry._order_candidates_for_capital(cs) == cs

    def test_single_and_empty_untouched(self):
        assert entry._order_candidates_for_capital([]) == []
        one = [_c("A", 1)]
        assert entry._order_candidates_for_capital(one) is one

    def test_bad_value_never_raises(self):
        cs = [SimpleNamespace(symbol="A", conviction_score="x"), _c("B", 5)]
        assert entry._order_candidates_for_capital(cs) == cs


def _one_slot_risk(seen):
    """Risk stub with room for exactly one entry: the first candidate it sees is approved, later ones rejected."""
    def _risk(intent, account_state):
        seen.append(intent.symbol)
        if len(seen) == 1:
            return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty)
        return RiskResult(verdict=RiskVerdict.REJECTED, check_name="cash_available_cap", reason="no cash left")
    return _risk


def _setup(db, monkeypatch):
    make_candidate(db, symbol="WEAKCO", conviction=62.0)     # queued first
    make_candidate(db, symbol="STRONGCO", conviction=92.0)   # queued second
    quotes(monkeypatch, {"WEAKCO": tick(100.0, symbol="WEAKCO"), "STRONGCO": tick(100.0, symbol="STRONGCO")})
    seen: list = []
    monkeypatch.setattr(entry, "risk_evaluate", _one_slot_risk(seen))
    return seen


class TestEvaluateModeCapitalOrder:
    def test_stronger_candidate_gets_the_only_slot(self, db, monkeypatch):
        seen = _setup(db, monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert seen == ["STRONGCO", "WEAKCO"]
        actions = {d["symbol"]: d["action"] for d in tally["entry_details"]}
        assert actions["STRONGCO"] != "WAIT" and actions["WEAKCO"] == "WAIT"
        assert tally["entered"] == 1

    def test_env_off_old_first_come_order(self, db, monkeypatch):
        monkeypatch.setenv("ENTRY_CAPITAL_ORDER_BY_CONVICTION", "0")
        seen = _setup(db, monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert seen == ["WEAKCO", "STRONGCO"]
        actions = {d["symbol"]: d["action"] for d in tally["entry_details"]}
        assert actions["WEAKCO"] != "WAIT" and actions["STRONGCO"] == "WAIT"

    def test_every_candidate_still_evaluated_and_consumed(self, db, monkeypatch):
        _setup(db, monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["evaluated"] == 2
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 0
