"""tests/test_group329_depth_weekend.py - spread re-check at BUY time and the weekend / holiday carry bar (group 329)."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import entry
from execution.auto_pilot import _carry_gap_gain_ok, _closed_days_before_next_session
from tz_utils import IST


class _T:
    def __init__(self, spread=None, book=None):
        self.spread_pct, self.book_value_5 = spread, book


class TestDepthRecheckHelper:
    def test_wide_spread_is_flagged_and_unknown_or_tight_passes(self):
        assert entry._watchlist_depth_reason(_T(spread=0.9)) is not None
        assert entry._watchlist_depth_reason(_T(spread=0.2)) is None
        assert entry._watchlist_depth_reason(_T(spread=None, book=None)) is None
        assert entry._watchlist_depth_reason(object()) is None

    def test_switch_exists_and_defaults_on(self):
        assert config.ENTRY_DEPTH_RECHECK is True


FRI = datetime(2026, 10, 9, 12, 0, tzinfo=IST)       # next session Monday -> 2 extra closed days
THU = datetime(2026, 10, 8, 12, 0, tzinfo=IST)       # next session Friday  -> 0
MON_BEFORE_HOLIDAY = datetime(2026, 11, 9, 12, 0, tzinfo=IST)   # 2026-11-10 is an NSE holiday -> next session Wed -> 1


class TestWeekendCarryBar:
    @pytest.fixture(autouse=True)
    def on(self, monkeypatch):
        monkeypatch.setattr(config, "OVERNIGHT_HOLD_EXTRA_GAIN_PER_CLOSED_DAY_PCT", 0.5)
        monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 1.5)

    def test_closed_day_counts(self):
        assert _closed_days_before_next_session(FRI) == 2
        assert _closed_days_before_next_session(THU) == 0
        assert _closed_days_before_next_session(MON_BEFORE_HOLIDAY) == 1

    def test_friday_needs_1_5_plus_two_halves(self):
        assert _carry_gap_gain_ok(100.0, 102.5, FRI) is True        # 1.5 + 2*0.5 = 2.5
        assert _carry_gap_gain_ok(100.0, 102.49, FRI) is False

    def test_a_normal_overnight_adds_nothing(self):
        assert _carry_gap_gain_ok(100.0, 100.01, THU) is True

    def test_holiday_adds_one_step(self):
        assert _carry_gap_gain_ok(100.0, 102.0, MON_BEFORE_HOLIDAY) is True
        assert _carry_gap_gain_ok(100.0, 101.99, MON_BEFORE_HOLIDAY) is False

    def test_off_when_zero_and_bad_price_fails_closed(self, monkeypatch):
        assert _carry_gap_gain_ok(0, 105.0, FRI) is False
        assert _carry_gap_gain_ok(100.0, "x", FRI) is False
        monkeypatch.setattr(config, "OVERNIGHT_HOLD_EXTRA_GAIN_PER_CLOSED_DAY_PCT", 0.0)
        assert _carry_gap_gain_ok(100.0, 100.01, FRI) is True


# ── end to end: the gates really sit on the evaluate_mode path ──────────────────────────────────────────────────────
from market_feed.feed import Tick                                          # noqa: E402
from tests.test_entry_evaluate_mode import (                               # noqa: E402,F401
    pin, db, make_candidate, quotes, approve_all, run, tick as _base_tick,
)
from entry_engine import event_risk                                       # noqa: E402


def _tick_with(**kw):
    t = _base_tick(100.0)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


class TestEvaluateModeWiring:
    def test_upper_circuit_tick_waits_with_gate_tag(self, db, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", True)
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with(upper_circuit=100.4, lower_circuit=80.0)})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        d = tally["entry_details"][0]
        assert tally["entered"] == 0 and d["action"] == "WAIT" and d["gate_tag"] == "circuit_band"
        assert d["reasoning"].startswith("CIRCUIT_UPPER")

    def test_tick_without_limits_is_not_blocked_by_the_circuit_gate(self, db, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", True)
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with()})
        approve_all(monkeypatch)
        d = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entry_details"][0]
        assert d.get("gate_tag") != "circuit_band"

    def test_wide_spread_at_buy_time_waits(self, db, monkeypatch):
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with(spread_pct=1.2)})
        approve_all(monkeypatch)
        d = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entry_details"][0]
        assert d["action"] == "WAIT" and d["gate_tag"] == "depth_at_entry" and d["reasoning"].startswith("DEPTH_AT_ENTRY")

    def test_depth_recheck_can_be_switched_off(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_DEPTH_RECHECK", False)
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with(spread_pct=1.2)})
        approve_all(monkeypatch)
        d = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entry_details"][0]
        assert d.get("gate_tag") != "depth_at_entry"

    def test_results_today_waits_and_a_failed_lookup_does_not(self, db, monkeypatch):
        from tz_utils import ist_now
        monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", True)
        event_risk.reset()
        today = ist_now().date().isoformat()
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"recent": [{"type": "earnings_date", "date": today}]})
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with()})
        approve_all(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        d = tally["entry_details"][0]
        assert tally["entered"] == 0 and d["action"] == "WAIT" and d["gate_tag"] == "earnings_day"
        assert d["reasoning"].startswith("EARNINGS_TODAY")

    def test_event_service_down_lets_the_candidate_through_this_gate(self, db, monkeypatch):
        monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", True)
        event_risk.reset()

        def down(s):
            raise TimeoutError("event service down")
        monkeypatch.setattr(event_risk, "_fetch", down)
        make_candidate(db)
        quotes(monkeypatch, {"TESTCO": _tick_with()})
        approve_all(monkeypatch)
        d = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entry_details"][0]
        assert d.get("gate_tag") != "earnings_day"
