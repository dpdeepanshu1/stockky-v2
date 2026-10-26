"""E207: the exit engine's held-days count (time stop, early warning, trail schedule) counts NSE trading sessions, not calendar days."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from exit_engine import exit as ex
from tests._tdays import opened_at_trading_days_ago

UTC = timezone.utc


def _d(y, m, d, h=6, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_same_day_is_zero():
    assert ex._held_trading_days(_d(2026, 10, 26, 4), _d(2026, 10, 26, 9)) == 0


def test_friday_entry_is_one_day_on_monday_not_three():
    assert ex._held_trading_days(_d(2026, 10, 23, 5), _d(2026, 10, 26, 5)) == 1


def test_weekend_is_skipped_across_a_full_week():
    assert ex._held_trading_days(_d(2026, 10, 26, 5), _d(2026, 11, 2, 5)) == 5


def test_exchange_holiday_is_skipped():
    # 2026-10-20 (Dussehra) is an NSE holiday: Mon 19th -> Wed 21st is one session, Mon 19th -> Fri 23rd is three.
    assert ex._held_trading_days(_d(2026, 10, 19, 5), _d(2026, 10, 21, 5)) == 1
    assert ex._held_trading_days(_d(2026, 10, 19, 5), _d(2026, 10, 23, 5)) == 3


def test_naive_opened_at_is_read_as_utc_and_ist_date_is_used():
    # 20:00 UTC on Fri 23rd is 01:30 IST Sat 24th: still one session by Monday.
    assert ex._held_trading_days(datetime(2026, 10, 23, 20, 0), _d(2026, 10, 26, 5)) == 1


def test_clock_going_backwards_or_bad_input_never_raises():
    assert ex._held_trading_days(_d(2026, 10, 27), _d(2026, 10, 26)) == 0
    assert ex._held_trading_days(None, _d(2026, 10, 26)) == 0


def test_very_old_position_falls_back_to_calendar_days():
    assert ex._held_trading_days(_d(2024, 1, 1), _d(2026, 10, 26)) == (_d(2026, 10, 26) - _d(2024, 1, 1)).days


@pytest.mark.parametrize("n", [0, 1, 2, 5, 6, 10, 11])
def test_test_helper_round_trips(n):
    now = datetime.now(UTC)
    assert ex._held_trading_days(opened_at_trading_days_ago(n, now), now) == n
