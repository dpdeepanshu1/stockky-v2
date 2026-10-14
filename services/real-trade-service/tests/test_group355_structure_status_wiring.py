"""group 355: entry.py feeds the structure gate counters (same harness as test_group351_structure_entry_wiring.py; needs the service deps)."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from entry_engine import entry, structure_gate
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, quotes, run, tick  # noqa: F401  (fixtures)
from tests.test_group351_structure_entry_wiring import _cand, _cfg, _sum  # noqa: F401  (autouse config fixture + helpers)


def test_entry_records_counters_and_ring(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")   # group370: the gate is live (the shared fixture leaves it off)
    _cand(db, _sum(ms_trend="bearish"))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    st = structure_gate.stats()
    assert st["evaluated"] == 1 and st["ok"] == 1 and st["would_reject"] == 1 and st["by_reason"].get("bearish") == 1
    assert st["recent"][0]["symbol"] == "GOODCO" and st["recent"][0]["price"] == 100.0 and st["recent"][0]["source"] == "payload"
    # the stored snapshot the status route tallies
    t = structure_gate.tally_decisions([{"symbol": "GOODCO", "price": 100.0, "structure_json": _stored(db)}], {"GOODCO": 101.0})
    assert t["would_reject"] == 1 and t["rejects"][0]["since_pct"] == 1.0


def _stored(db):
    import models
    return db.query(models.TradeDecision).filter_by(symbol="GOODCO").one().structure_json


def test_entry_with_gate_off_records_nothing(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "off", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off", raising=False)
    _cand(db, _sum())
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert structure_gate.stats()["evaluated"] == 0
