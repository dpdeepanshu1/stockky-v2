"""B15 (2026-10-22) -> E285 round 88: the evidence downgrade is GONE. structure_gate.mode_for(mode, db) no longer turns enforce into log until
structure_observations rows have a measured outcome: the gate is live the moment it is on. The offline A/B verdict and G2 check keep their hard floor of 15 signals
(those are reports, not the entry path).

Run from services/real-trade-service:  python3 -m pytest tests/test_b15_enforce_guard.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts"))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import structure_gate as sg


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "enforce")
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    sg._OOS_CACHE.clear()
    yield s
    sg._OOS_CACHE.clear()
    s.close()


def test_enforce_is_live_with_no_outcomes_at_all(db):
    assert sg.mode_for("REAL", db) == "enforce" and sg.mode_for("DEMO", db) == "enforce"


def test_enforce_stays_live_with_few_unmeasured_or_other_mode_rows(db):
    for i in range(3):
        db.add(models.StructureObservation(symbol=f"S{i}", source="real_trade", mode="DEMO", obs_day="2026-10-01", obs_ts=float(i), price=100.0, verdict="passed", ret_5d=None))
    db.commit()
    assert sg.mode_for("REAL", db) == "enforce"


def test_an_unreadable_table_does_not_downgrade_it(db):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def rollback(self):
            pass
    assert sg.mode_for("REAL", Boom()) == "enforce"


def test_off_stays_off_and_a_leftover_log_is_enforce(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off")
    assert sg.mode_for("REAL", db) == "off"
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "log")
    assert sg.mode_for("REAL", db) == "enforce"


def test_g2_hard_floor_beats_a_low_min_n():
    import structure_gate_check as g
    rep = {"verdict": "pass", "kept_pct": 60.0, "edge_pp": 1.0, "signals": {"baseline": 12, "candidate": 8}, "bootstrap": {"ci90": [0.1, 2.0]}}
    assert g.check_ab(rep)["status"] == g.NED
    rep["signals"] = {"baseline": 400, "candidate": 250}
    assert g.check_ab(rep)["status"] == g.PASS


def test_ab_verdict_hard_floor():
    import ms_ab_report as m
    assert m.verdict(10, 400, 1.0, [0.1, 2.0], [1.0, 1.0], 1.0, min_n=1) == "insufficient_sample"
    assert m.verdict(300, 400, 1.0, [0.1, 2.0], [1.0, 1.0], 1.0, min_n=1) == "pass"
