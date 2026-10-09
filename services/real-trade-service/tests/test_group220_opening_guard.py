"""tests/test_group220_opening_guard.py - entry_engine/opening_guard.py (group 220, review item 6).

No automatic entries before OPENING_ENTRY_NOT_BEFORE_IST (default 09:30 IST) while the market is open. Candidates stay
queued (not consumed, not rejected) and are evaluated normally once the guard lifts. ENTER_AT_OPEN moves to the guard
time when that is later. The guard is OFF for the rest of the suite (tests/conftest.py); this file turns it on.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group220_opening_guard.py -q --cov=entry_engine.opening_guard --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, opening_guard
from risk_engine.engine import RiskResult, RiskVerdict
from tz_utils import IST

# reuse the fixtures / helpers of the evaluate_mode suite (autouse pin, db, make_candidate, tick, quotes)
from tests.test_entry_evaluate_mode import (  # noqa: F401
    pin, db, make_candidate, tick, quotes, run,
)


@pytest.fixture(autouse=True)
def guard_on(monkeypatch):
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:30")
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL", "DEMO"))
    monkeypatch.setattr(config, "ENTER_AT_OPEN_TIME_IST", "09:20")
    opening_guard.reset_state()
    yield
    opening_guard.reset_state()


def at(monkeypatch, hhmm, market_open=True):
    """Fake IST clock for the guard."""
    h, m = (int(x) for x in hhmm.split(":"))
    monkeypatch.setattr(opening_guard, "ist_now", lambda now=None: datetime(2026, 10, 7, h, m, tzinfo=IST))
    monkeypatch.setattr(opening_guard, "is_market_open_ist", lambda now=None: market_open)


class TestReason:
    def test_blocks_inside_the_window_and_says_when_it_lifts(self, monkeypatch):
        at(monkeypatch, "09:20")
        r = opening_guard.reason("REAL")
        assert r and "before 09:30 IST" in r and "now 09:20" in r

    @pytest.mark.parametrize("hhmm", ["09:15", "09:19", "09:29"])
    def test_blocks_up_to_the_cutoff(self, monkeypatch, hhmm):
        at(monkeypatch, hhmm)
        assert opening_guard.reason("DEMO") is not None

    @pytest.mark.parametrize("hhmm", ["09:30", "09:31", "11:00", "15:20"])
    def test_allows_from_the_cutoff_on(self, monkeypatch, hhmm):
        at(monkeypatch, hhmm)
        assert opening_guard.reason("REAL") is None

    def test_pre_open_and_closed_market_are_not_this_guards_business(self, monkeypatch):
        at(monkeypatch, "08:50", market_open=False)
        assert opening_guard.reason("REAL") is None

    def test_switch_off(self, monkeypatch):
        at(monkeypatch, "09:20")
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", False)
        assert opening_guard.reason("REAL") is None

    def test_only_the_configured_modes_are_guarded_case_insensitively(self, monkeypatch):
        at(monkeypatch, "09:20")
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL",))
        assert opening_guard.reason("real") is not None
        assert opening_guard.reason("DEMO") is None

    def test_custom_cutoff(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:45")
        at(monkeypatch, "09:40")
        assert opening_guard.reason("REAL") is not None
        at(monkeypatch, "09:45")
        assert opening_guard.reason("REAL") is None

    @pytest.mark.parametrize("bad", ["", "xx", "9", "25:99"])
    def test_blank_or_bad_cutoff_falls_back_to_0915(self, monkeypatch, bad):
        monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", bad)
        assert opening_guard.not_before() == _time(9, 15)

    def test_a_cutoff_before_the_open_never_blocks(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:00")
        at(monkeypatch, "09:15")
        assert opening_guard.reason("REAL") is None

    def test_any_internal_error_means_no_guard(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("clock broke")
        monkeypatch.setattr(opening_guard, "ist_now", boom)
        monkeypatch.setattr(opening_guard, "is_market_open_ist", lambda now=None: True)
        assert opening_guard.reason("REAL") is None

    def test_missing_config_attribute_means_not_covered(self, monkeypatch):
        monkeypatch.delattr(config, "OPENING_ENTRY_GUARD_MODES")
        assert opening_guard.covers("REAL") is False


class TestRealCalendar:
    """The real tz_utils functions with an explicit `now`, no fakes."""

    def test_weekday_0920_ist_is_blocked(self):
        now = datetime(2026, 10, 7, 9, 20, tzinfo=IST)           # a Wednesday
        assert opening_guard.reason("REAL", now) is not None

    def test_weekday_0931_ist_is_allowed(self):
        assert opening_guard.reason("REAL", datetime(2026, 10, 7, 9, 31, tzinfo=IST)) is None

    def test_weekend_is_not_blocked(self):
        assert opening_guard.reason("REAL", datetime(2026, 10, 10, 9, 20, tzinfo=IST)) is None   # a Saturday

    def test_utc_timestamps_are_converted(self):
        # 03:50 UTC == 09:20 IST
        from datetime import timezone
        assert opening_guard.reason("REAL", datetime(2026, 10, 7, 3, 50, tzinfo=timezone.utc)) is not None


class TestLogThrottle:
    def test_logs_once_per_interval_per_mode(self):
        assert opening_guard.should_log("REAL") is True
        assert opening_guard.should_log("REAL") is False
        assert opening_guard.should_log("DEMO") is True
        opening_guard.reset_state()
        assert opening_guard.should_log("REAL") is True


class TestEnterAtOpenTime:
    def test_moves_to_the_guard_time_when_that_is_later(self):
        assert opening_guard.enter_at_open_time("REAL") == _time(9, 30)

    def test_keeps_its_own_time_when_the_guard_is_earlier(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:10")
        assert opening_guard.enter_at_open_time("REAL") == _time(9, 20)

    def test_keeps_its_own_time_when_the_guard_is_off_or_does_not_cover_the_mode(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("DEMO",))
        assert opening_guard.enter_at_open_time("REAL") == _time(9, 20)
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", False)
        assert opening_guard.enter_at_open_time("DEMO") == _time(9, 20)

    def test_a_later_enter_at_open_time_is_untouched(self, monkeypatch):
        monkeypatch.setattr(config, "ENTER_AT_OPEN_TIME_IST", "10:00")
        assert opening_guard.enter_at_open_time("REAL") == _time(10, 0)

    def test_bad_enter_at_open_time_falls_back_to_0920_then_the_guard(self, monkeypatch):
        monkeypatch.setattr(config, "ENTER_AT_OPEN_TIME_IST", "nonsense")
        assert opening_guard.enter_at_open_time("REAL") == _time(9, 30)


def _approve_all(monkeypatch):
    monkeypatch.setattr(entry, "risk_evaluate", lambda intent, state: RiskResult(
        verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty))


class TestEvaluateModeIntegration:
    def _queue(self, db, monkeypatch, *symbols):
        for s in symbols:
            make_candidate(db, symbol=s, conviction=80.0)
        quotes(monkeypatch, {s: tick(100.0, symbol=s) for s in symbols})
        _approve_all(monkeypatch)

    def test_inside_the_window_candidates_stay_queued_and_nothing_is_decided(self, db, monkeypatch):
        self._queue(db, monkeypatch, "AAA", "BBB")
        at(monkeypatch, "09:20")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0 and tally["rejected"] == 0 and tally["evaluated"] == 0
        assert tally["waited"] == 2 and tally["entry_details"] == [] and "09:30" in tally["opening_guard"]
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 2     # not consumed
        assert db.query(models.TradeDecision).count() == 0                                 # nothing logged as a decision

    def test_the_same_candidates_are_entered_once_the_guard_lifts(self, db, monkeypatch):
        self._queue(db, monkeypatch, "AAA")
        at(monkeypatch, "09:20")
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        at(monkeypatch, "09:31")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["evaluated"] == 1 and tally["entered"] == 1 and "opening_guard" not in tally
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 0

    def test_guard_off_enters_at_0920_as_before(self, db, monkeypatch):
        self._queue(db, monkeypatch, "AAA")
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", False)
        at(monkeypatch, "09:20")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1

    def test_an_uncovered_mode_still_enters_while_a_covered_one_waits(self, db, monkeypatch):
        self._queue(db, monkeypatch, "AAA")
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL",))
        at(monkeypatch, "09:20")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
        make_candidate(db, symbol="CCC", conviction=80.0, mode="REAL")
        blocked = run(entry.evaluate_mode(db, "REAL", gate_armed=True))     # returns before any broker / regime call
        assert blocked["entered"] == 0 and blocked["waited"] == 1 and "opening_guard" in blocked

    def test_no_candidates_is_unchanged(self, db, monkeypatch):
        at(monkeypatch, "09:20")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally == {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0, "entry_details": []}

    def test_a_pre_open_cycle_is_not_blocked_by_the_guard(self, db, monkeypatch):
        self._queue(db, monkeypatch, "AAA")
        at(monkeypatch, "08:50", market_open=False)
        assert "opening_guard" not in run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
