"""
tests/test_market_hours.py — 100% coverage for market_hours.py

Pure stdlib (datetime, zoneinfo) — no network, no DB, runnable anywhere.

Run from services/market-data-service:
    python3 -m pytest tests/test_market_hours.py -v
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import market_hours as mh

IST = ZoneInfo("Asia/Kolkata")

def _ist(weekday: int, hour: int, minute: int) -> datetime:
    """Build an IST-aware datetime on the nearest weekday matching `weekday`
    (0=Mon … 6=Sun), at the given time."""
    # 2026-10-05 is a Monday (weekday=0). Group 139: the week must contain no NSE holiday -
    # the old base week (2026-09-28) had Fri 2026-10-02 (Gandhi Jayanti), now a closed day.
    base = datetime(2026, 10, 5, tzinfo=IST)
    delta = (weekday - base.weekday()) % 7
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=delta)


class TestIsFeedWindowIst:
    def test_during_market_hours_weekday_returns_true(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        now = _ist(0, 10, 0)   # Monday 10:00 IST — well within window
        assert mh.is_feed_window_ist(now) is True

    def test_saturday_returns_false(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        now = _ist(5, 10, 0)   # Saturday
        assert mh.is_feed_window_ist(now) is False

    def test_sunday_returns_false(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        now = _ist(6, 10, 0)   # Sunday
        assert mh.is_feed_window_ist(now) is False

    def test_before_pre_open_slack_returns_false(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        monkeypatch.setattr(mh, "_PRE_OPEN_SLACK_MIN", 10)
        # Market opens 09:15, slack = 10min → window opens at 09:05
        before_open = _ist(0, 9, 4)
        assert mh.is_feed_window_ist(before_open) is False

    def test_at_pre_open_slack_boundary_returns_true(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        monkeypatch.setattr(mh, "_PRE_OPEN_SLACK_MIN", 10)
        at_window_open = _ist(0, 9, 5)
        assert mh.is_feed_window_ist(at_window_open) is True

    def test_after_post_close_slack_returns_false(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        monkeypatch.setattr(mh, "_POST_CLOSE_SLACK_MIN", 5)
        # Market closes 15:30, slack = 5min → window closes at 15:35
        after_close = _ist(0, 15, 36)
        assert mh.is_feed_window_ist(after_close) is False

    def test_at_post_close_slack_boundary_returns_true(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        monkeypatch.setattr(mh, "_POST_CLOSE_SLACK_MIN", 5)
        at_window_close = _ist(0, 15, 35)
        assert mh.is_feed_window_ist(at_window_close) is True

    def test_always_on_overrides_weekend(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", True)
        saturday = _ist(5, 10, 0)
        assert mh.is_feed_window_ist(saturday) is True

    def test_always_on_overrides_outside_hours(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", True)
        midnight = _ist(0, 0, 0)
        assert mh.is_feed_window_ist(midnight) is True

    def test_none_uses_current_utc_time(self, monkeypatch):
        """Passing None should call datetime.now(utc) internally — just verify
        it doesn't raise (actual value depends on real wall time)."""
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        result = mh.is_feed_window_ist(None)
        assert isinstance(result, bool)

    def test_naive_utc_datetime_is_handled(self, monkeypatch):
        """A UTC-aware datetime (not IST) must be correctly converted."""
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        # Monday 04:30 UTC = 10:00 IST — should be True
        utc_now = datetime(2026, 9, 28, 4, 30, 0, tzinfo=timezone.utc)
        assert mh.is_feed_window_ist(utc_now) is True

    def test_friday_in_hours_returns_true(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", False)
        friday = _ist(4, 12, 0)   # Friday noon
        assert mh.is_feed_window_ist(friday) is True


class TestSecondsUntilNextWindow:
    def test_always_returns_60(self):
        assert mh.seconds_until_next_window() == 60.0

    def test_with_explicit_datetime_still_60(self):
        now = _ist(6, 0, 0)   # Sunday midnight
        assert mh.seconds_until_next_window(now) == 60.0
