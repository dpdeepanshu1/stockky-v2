"""group185: the gateway warns at boot when the hand-kept NSE holiday set has run out.
Run: python3 -m pytest tests/test_group185_holiday_coverage_warning.py -q"""
from datetime import date, datetime, timezone

import nse_holidays as nh


def test_quiet_for_most_of_the_year():
    assert nh.holiday_coverage_warning(date(2026, 10, 6)) is None
    assert nh.holiday_coverage_warning(date(2026, 11, 14)) is None


def test_warns_about_next_year_from_15_nov():
    msg = nh.holiday_coverage_warning(date(2026, 11, 15))
    assert msg and "2027" in msg and "2026" not in msg
    assert "check_holiday_lists_sync.py" in msg
    assert nh.holiday_coverage_warning(date(2026, 12, 31)) == msg


def test_warns_about_the_current_year_when_it_has_no_dates():
    msg = nh.holiday_coverage_warning(date(2027, 1, 1))
    assert msg and "2027" in msg
    both = nh.holiday_coverage_warning(date(2027, 11, 20))
    assert "2027 and 2028" in both


def test_quiet_once_next_year_is_entered(monkeypatch):
    monkeypatch.setattr(nh, "_NSE_HOLIDAYS", set(nh._NSE_HOLIDAYS) | {date(2027, 1, 26)})
    assert nh.holiday_coverage_warning(date(2026, 12, 1)) is None
    assert nh.holiday_coverage_warning(date(2027, 1, 2)) is None


def test_datetime_is_reduced_to_the_ist_date():
    # 2026-11-14 20:00 UTC is already 15 Nov in India
    assert nh.holiday_coverage_warning(datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc))


def test_today_default_never_raises():
    assert nh.holiday_coverage_warning() is None or isinstance(nh.holiday_coverage_warning(), str)
