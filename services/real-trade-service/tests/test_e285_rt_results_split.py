"""E285 round 101: Real Trade's results-day split for the entry check (entry_engine/event_risk.py), the rule Position Stocks has had since E272:
results out before 09:00 = a known event (not blocked); results out later in the session = blocked until EARNINGS_REACTION_WAIT_MIN minutes after publication;
unknown publication time = blocked. The overnight-carry check is not split."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import event_risk

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)          # a Monday, 10:00 IST


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", True)
    monkeypatch.setattr(config, "EARNINGS_CARRY_BLOCK", True)
    monkeypatch.setattr(config, "EARNINGS_SPLIT_ENABLED", True)
    monkeypatch.setattr(config, "EARNINGS_KNOWN_BEFORE_IST", "09:00")
    monkeypatch.setattr(config, "EARNINGS_REACTION_WAIT_MIN", 5.0)
    event_risk.reset()


def iso(h, m):
    return datetime(2026, 11, 2, h, m, tzinfo=IST).astimezone(timezone.utc).isoformat()


class TestSplitReason:
    def test_before_nine_is_a_known_event(self):
        assert event_risk.split_reason(iso(8, 30), NOW) is None
        assert event_risk.split_reason(iso(8, 59), NOW) is None

    def test_in_session_waits_five_minutes_after_publication(self):
        r = event_risk.split_reason(iso(9, 58), NOW)                       # published 2 min ago
        assert r and "waiting 3.0 more min" in r
        assert event_risk.split_reason(iso(9, 55), NOW) is None            # exactly 5 min ago
        assert event_risk.split_reason(iso(9, 0), NOW) is None             # long ago

    def test_unknown_or_old_publication_stays_blocked(self):
        assert "publication time unknown" in event_risk.split_reason(None, NOW)
        assert "publication time unknown" in event_risk.split_reason("junk", NOW)
        old = datetime(2026, 10, 30, 8, 0, tzinfo=IST).isoformat()
        assert "not from today" in event_risk.split_reason(old, NOW)

    def test_split_off_blocks_every_results_day(self, monkeypatch):
        monkeypatch.setattr(config, "EARNINGS_SPLIT_ENABLED", False)
        assert event_risk.split_reason(iso(8, 0), NOW)


class TestResultOutFrom:
    def test_earliest_results_event_only(self):
        body = {"events": [{"type": "results", "published": iso(9, 40)}, {"type": "results", "published": iso(8, 10)}, {"type": "order_win", "published": iso(7, 0)}]}
        assert event_risk.result_out_from(body) == datetime(2026, 11, 2, 8, 10, tzinfo=IST).astimezone(timezone.utc).isoformat()
        assert event_risk.result_out_from({"events": [{"type": "order_win", "published": iso(7, 0)}]}) is None
        assert event_risk.result_out_from(None) is None and event_risk.result_out_from({"events": [{"type": "results", "published": "x"}]}) is None


class TestRejectReason:
    def test_known_event_is_not_blocked_but_carry_still_is(self, monkeypatch):
        d = datetime.now(IST).date()
        monkeypatch.setattr(event_risk, "lookup", lambda s: d)
        monkeypatch.setattr(event_risk, "result_out_at", lambda s: datetime(d.year, d.month, d.day, 7, 0, tzinfo=IST).isoformat())
        assert event_risk.reject_reason("ABC") is None
        assert event_risk.reject_reason("ABC", carry=True).startswith("EARNINGS_TODAY")

    def test_unknown_timing_stays_blocked_with_the_reason_text(self, monkeypatch):
        d = datetime.now(IST).date()
        monkeypatch.setattr(event_risk, "lookup", lambda s: d)
        monkeypatch.setattr(event_risk, "result_out_at", lambda s: None)
        r = event_risk.reject_reason("ABC")
        assert r.startswith("EARNINGS_TODAY") and "publication time unknown" in r

    def test_other_dates_and_split_off_are_unchanged(self, monkeypatch):
        d = datetime.now(IST).date()
        monkeypatch.setattr(event_risk, "lookup", lambda s: d + timedelta(days=3))
        assert event_risk.reject_reason("ABC") is None
        monkeypatch.setattr(event_risk, "lookup", lambda s: d)
        monkeypatch.setattr(config, "EARNINGS_SPLIT_ENABLED", False)
        monkeypatch.setattr(event_risk, "result_out_at", lambda s: (_ for _ in ()).throw(AssertionError("must not be asked")))
        assert event_risk.reject_reason("ABC").startswith("EARNINGS_TODAY")


class TestResultOutAt:
    def test_found_time_is_cached_for_the_day_and_none_is_retried_later(self, monkeypatch):
        calls = []
        bodies = [{"events": []}, {"events": [{"type": "results", "published": iso(9, 30)}]}]
        monkeypatch.setattr(event_risk, "_fetch_active", lambda s: calls.append(s) or bodies[min(len(calls) - 1, 1)])
        assert event_risk.result_out_at("ABC") is None
        assert event_risk.result_out_at("ABC") is None and len(calls) == 1          # None cached for RESULT_OUT_RETRY_S
        event_risk._out_cache["ABC"] = (event_risk._out_cache["ABC"][0], None, -1e9)    # the retry time has passed
        assert event_risk.result_out_at("ABC") is not None and len(calls) == 2
        assert event_risk.result_out_at("ABC") is not None and len(calls) == 2          # a found time is final for the day

    def test_a_failed_fetch_never_raises(self, monkeypatch):
        def boom(s):
            raise RuntimeError("down")
        monkeypatch.setattr(event_risk, "_fetch_active", boom)
        assert event_risk.result_out_at("ABC") is None
