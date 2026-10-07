"""tests/test_group220_enter_at_open_schedule.py - the ENTER_AT_OPEN trigger respects the opening guard (group 220).

ENTER_AT_OPEN runs ONCE per day. With the guard on (09:30) and the trigger at 09:20, it would fire at 09:20, find every
entry blocked, and spend the day's only run. The trigger therefore moves to the guard time.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group220_enter_at_open_schedule.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import auto_pilot as ap

# reuse the schedule-tick rig (in-memory db, session factory pin, gate row, async recorder)
from tests.test_auto_pilot_orchestration import (  # noqa: F401
    db, _fresh_session_factory, _gate, _async_recorder, run,
)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:30")
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL", "DEMO"))
    monkeypatch.setattr(config, "ENTER_AT_OPEN_TIME_IST", "09:20")


def _fire(db, monkeypatch, clock):
    """Run one schedule tick with the IST clock at `clock` ('HH:MM'); returns the recorded _enter_at_open calls."""
    h, m = (int(x) for x in clock.split(":"))
    db.add(_gate(enter_at_open_enabled=True, enter_at_open_last_run=None))
    db.commit()
    monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-10-07")
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: _time(h, m) >= t)
    monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
    calls = []
    monkeypatch.setattr(ap, "_enter_at_open", _async_recorder(calls))
    run(ap._schedule_tick_body("REAL"))
    return calls


class TestEnterAtOpenSchedule:
    def test_does_not_fire_at_0925_while_the_guard_is_on(self, db, monkeypatch):
        assert _fire(db, monkeypatch, "09:25") == []

    def test_a_skipped_early_tick_does_not_spend_the_days_run(self, db, monkeypatch):
        assert _fire(db, monkeypatch, "09:25") == []
        import models
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert gate.enter_at_open_last_run is None                      # still available at 09:30

    def test_fires_at_the_guard_time(self, db, monkeypatch):
        assert len(_fire(db, monkeypatch, "09:30")) == 1

    def test_with_the_guard_off_it_still_fires_at_its_own_0920(self, db, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", False)
        assert len(_fire(db, monkeypatch, "09:25")) == 1

    def test_guard_not_covering_the_mode_leaves_the_schedule_alone(self, db, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("DEMO",))
        assert len(_fire(db, monkeypatch, "09:25")) == 1

    def test_helper_returns_the_effective_time(self):
        assert ap._enter_at_open_time("REAL") == _time(9, 30)
