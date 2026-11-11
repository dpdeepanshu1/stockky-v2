"""E365: observation.due() no longer evaluates a row whose observation is so old that the 120-day daily fetch cannot reach the sessions right after it."""
from __future__ import annotations

from stockky_ms import observation as ob

DAY = 86400.0
NOW = 1_800_000_000.0


def _row(age_days):
    return {"obs_ts": NOW - age_days * DAY, "complete": False}


def test_recent_row_is_due_and_lookback_covers_it():
    d = ob.due(_row(30), NOW)
    assert d["due"] and d["lookback_days"] == 40


def test_row_at_the_edge_is_still_due():
    d = ob.due(_row(110), NOW)
    assert d["due"] and d["lookback_days"] == 120


def test_row_past_the_edge_is_not_evaluated_on_wrong_bars():
    d = ob.due(_row(111), NOW)
    assert not d["due"] and d["reason"] == "too_old" and d["lookback_days"] == 0


def test_same_day_and_complete_unchanged():
    assert ob.due(_row(0), NOW)["reason"] == "same_day"
    assert ob.due({"obs_ts": NOW - 5 * DAY, "complete": True}, NOW)["reason"] == "complete"
