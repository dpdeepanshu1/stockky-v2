"""B15 (2026-10-22): enforce needs evidence. structure_gate.mode_for(mode, db) downgrades enforce -> log until enough
structure_observations rows have a measured outcome; the offline A/B verdict and G2 check have a hard floor of 15 signals.

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
    monkeypatch.setattr(config, "STRUCTURE_ENFORCE_MIN_OOS", 15, raising=False)   # conftest turns the guard off; these tests turn it on
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "enforce")
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "enforce")
    sg._OOS_CACHE.clear()
    yield s
    sg._OOS_CACHE.clear()
    s.close()


def _obs(db, n, mode="REAL", measured=True):
    for i in range(n):
        db.add(models.StructureObservation(symbol=f"S{i}", source="real_trade", mode=mode, obs_day="2026-10-01", obs_ts=float(i),
                                           price=100.0, verdict="passed", ret_5d=(1.0 if measured else None)))
    db.commit()


def test_enforce_is_downgraded_with_too_few_outcomes(db):
    _obs(db, 14)
    assert sg.mode_for("REAL", db) == "log"


def test_enforce_allowed_at_the_floor(db):
    _obs(db, 15)
    assert sg.mode_for("REAL", db) == "enforce"


def test_unmeasured_rows_and_other_modes_do_not_count(db):
    _obs(db, 20, measured=False)
    _obs(db, 20, mode="DEMO")
    assert sg.mode_for("REAL", db) == "log"


def test_without_a_db_the_configured_mode_is_unchanged(db):
    assert sg.mode_for("REAL") == "enforce"


def test_log_and_off_are_never_changed(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off")
    assert sg.mode_for("REAL", db) == "off"
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "log")
    assert sg.mode_for("REAL", db) == "log"


def test_guard_can_be_disabled(db, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_ENFORCE_MIN_OOS", 0, raising=False)
    assert sg.mode_for("REAL", db) == "enforce"


def test_unreadable_table_counts_as_no_evidence(db):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def rollback(self):
            pass
    assert sg.mode_for("REAL", Boom()) == "log"


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
