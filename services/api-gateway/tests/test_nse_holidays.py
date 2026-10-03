"""nse_holidays — closed-day calendar. Callers pass `.date()` values; a datetime is reduced to its IST date."""
from datetime import date, datetime, timedelta, timezone

import pytest

import nse_holidays as nh


class TestIsNseHoliday:
    @pytest.mark.parametrize("d", [
        date(2025, 2, 26), date(2025, 12, 25),
        date(2026, 1, 26), date(2026, 12, 25),
        date(2026, 9, 14),  # Ganesh Chaturthi — the live-incident date that motivated the 2026 audit fix
    ])
    def test_known_holidays(self, d):
        assert nh.is_nse_holiday(d) is True

    @pytest.mark.parametrize("d", [date(2026, 9, 15), date(2026, 9, 13), date(2024, 1, 1), date(2027, 1, 26)])
    def test_ordinary_days_are_open(self, d):
        assert nh.is_nse_holiday(d) is False

    def test_weekends_are_not_treated_as_holidays(self):
        # 2026-09-12 is a Saturday: weekend handling lives in the callers (weekday() < 5), not here.
        assert date(2026, 9, 12).weekday() == 5
        assert nh.is_nse_holiday(date(2026, 9, 12)) is False

    def test_naive_datetime_on_a_holiday_is_a_holiday(self):
        # Was pinned as False (a datetime never equals a date), which silently treated a closed day as
        # open for any caller that forgot .date(). A naive datetime is read as IST wall-clock time.
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 10, 0)) is True
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 10, 0).date()) is True

    def test_naive_datetime_on_an_ordinary_day_is_open(self):
        assert nh.is_nse_holiday(datetime(2026, 9, 15, 10, 0)) is False

    def test_datetime_midnight_boundaries(self):
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 0, 0)) is True
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 23, 59, 59)) is True
        assert nh.is_nse_holiday(datetime(2026, 9, 13, 23, 59, 59)) is False

    def test_aware_ist_datetime(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 9, 15, tzinfo=ist)) is True
        assert nh.is_nse_holiday(datetime(2026, 9, 15, 9, 15, tzinfo=ist)) is False

    def test_aware_utc_datetime_is_converted_to_the_ist_date(self):
        # 2026-09-13 20:00 UTC is 2026-09-14 01:30 IST -> the holiday.
        assert nh.is_nse_holiday(datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)) is True
        # 2026-09-14 20:00 UTC is 2026-09-15 01:30 IST -> an ordinary day, although the UTC date is the 14th.
        assert nh.is_nse_holiday(datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)) is False

    def test_date_input_is_unchanged(self):
        assert nh.is_nse_holiday(date(2026, 10, 2)) is True

    @pytest.mark.parametrize("bad", [None, "2026-09-14", 20260914])
    def test_non_date_values_are_still_not_holidays(self, bad):
        assert nh.is_nse_holiday(bad) is False


class TestHolidayName:
    def test_name_for_holiday(self):
        assert nh.holiday_name(date(2026, 9, 14)) == "NSE holiday"

    def test_none_for_open_day(self):
        assert nh.holiday_name(date(2026, 9, 15)) is None

    def test_name_for_a_datetime_on_a_holiday(self):
        assert nh.holiday_name(datetime(2026, 9, 14, 10, 0)) == "NSE holiday"
        assert nh.holiday_name(datetime(2026, 9, 15, 10, 0)) is None


class TestCalendarIntegrity:
    def test_all_entries_are_dates(self):
        assert all(type(d) is date for d in nh._NSE_HOLIDAYS)

    def test_2026_entries_are_present_and_sorted_unique(self):
        y26 = sorted(d for d in nh._NSE_HOLIDAYS if d.year == 2026)
        assert len(y26) == 16 and len(set(y26)) == 16

    def test_no_2026_holiday_falls_on_a_weekend_except_documented_ones(self):
        # Guards against a typo'd date silently becoming a no-op. Weekday-only exchange closures
        # are the norm; report any weekend entry so a human reviews it.
        weekend = [d.isoformat() for d in nh._NSE_HOLIDAYS if d.year == 2026 and d.weekday() >= 5]
        assert weekend == []
