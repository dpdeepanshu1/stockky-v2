"""Round 284: entry / square-off times that cannot work together refuse the entry with the reason (nothing is changed silently). Pure, no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fno import gates
from fno.params import Params

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 11, 10, 30, tzinfo=IST)        # a Wednesday


def _why(**kw):
    return gates.param_problems(Params().with_(**kw))


def test_the_shipped_times_are_fine():
    assert gates.param_problems(Params()) == []


def test_a_last_entry_at_or_after_the_square_off_is_a_problem():
    assert any("must be before the square-off" in w for w in _why(entry_end="15:15"))
    assert any("must be before the square-off" in w for w in _why(entry_end="15:20"))
    assert _why(entry_end="15:14") == []


def test_a_window_that_closes_before_it_opens_is_a_problem():
    assert any("after it closes" in w for w in _why(entry_start="14:40", entry_end="14:30"))
    assert _why(entry_start="09:30", entry_end="09:30", entry_settle_min=0) == []        # round 497: a one-minute window cannot also hold a settle period


def test_expiry_day_times_must_fit_too():
    assert any("expiry-day last entry" in w for w in _why(expiry_entry_end="14:45"))
    assert _why(expiry_entry_end="14:50", expiry_squareoff_at="15:00") == []             # before both square-off times: fine
    assert any("expiry-day last entry" in w for w in _why(expiry_entry_end="15:00", expiry_squareoff_at="15:30", squareoff_at="14:55"))   # the earlier normal square-off applies on expiry day too
    assert _why(expiry_entry_end="13:30", expiry_squareoff_at="14:45") == []


def test_an_unreadable_time_falls_back_to_the_default_and_is_not_a_problem():
    assert _why(entry_end="soon") == []


def test_entry_gates_refuses_with_the_reason():
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 5.0, gates.DayState(), Params().with_(entry_end="15:20"))
    assert not ok and any("must be before the square-off" in w for w in why)
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 5.0, gates.DayState(), Params())
    assert ok, why
