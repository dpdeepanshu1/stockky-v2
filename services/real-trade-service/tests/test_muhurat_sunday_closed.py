"""group320: Diwali Muhurat trading (Sunday 2026-11-08) is deliberately NOT modelled as a trading session.

NSE announced a one-hour special evening session that day; the exact timing is notified later by circular. Stockky's
intraday logic (09:15-15:30 windows, 15:20 square-off, opening gate) has no meaning for it, so the safe behaviour is
the existing one: Sunday = closed, nothing runs. This pins that on purpose; if you ever add Muhurat support, change
this test deliberately rather than by accident.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import tz_utils

IST = ZoneInfo("Asia/Kolkata")


def test_muhurat_sunday_is_closed_all_day():
    for hh, mm in [(9, 30), (12, 0), (15, 0), (18, 15), (18, 45)]:
        assert tz_utils.is_market_open_ist(datetime(2026, 11, 8, hh, mm, tzinfo=IST)) is False


def test_the_regular_diwali_holidays_around_it_are_closed_too():
    assert tz_utils.is_market_open_ist(datetime(2026, 11, 10, 11, 0, tzinfo=IST)) is False   # Balipratipada
    assert tz_utils.is_market_open_ist(datetime(2026, 11, 9, 11, 0, tzinfo=IST)) is True     # ordinary Monday
