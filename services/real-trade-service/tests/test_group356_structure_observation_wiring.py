"""group 356: entry.py stores one structure observation per symbol/day next to the decision (same harness as test_group355_structure_status_wiring.py;
needs the service deps - not runnable without sqlalchemy/fastapi)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from entry_engine import entry, structure_observation
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, quotes, run, tick  # noqa: F401  (fixtures)
from tests.test_group351_structure_entry_wiring import _cand, _cfg, _sum  # noqa: F401  (autouse config fixture + helpers)


@pytest.fixture(autouse=True)
def _reset_obs():
    structure_observation.reset()
    yield
    structure_observation.reset()


def test_entry_stores_the_verdict_as_an_observation(db, monkeypatch):
    import models
    _cand(db, _sum(ms_trend="bearish"))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    rows = db.query(models.StructureObservation).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "GOODCO" and r.mode == "DEMO" and r.price == 100.0 and r.verdict in ("rejected_enforced", "rejected_shadow") and r.is_complete is False
    assert r.stop is not None and r.target is not None


def test_a_second_cycle_the_same_day_does_not_add_a_second_row(db, monkeypatch):
    import models
    _cand(db, _sum(ms_trend="bearish"))
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert db.query(models.StructureObservation).count() == 1


def test_gate_off_stores_nothing(db, monkeypatch):
    import config, models
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "off", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off", raising=False)
    _cand(db, _sum())
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)
    run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    assert db.query(models.StructureObservation).count() == 0
