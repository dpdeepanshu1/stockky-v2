"""group320: api-gateway's holiday check also honours market-data's price-based 'no move 09:15-10:00 IST' verdict.

Only TODAY (IST) is ever judged by prices; any other date uses the calendar alone. Fail-open on every error.
"""
from datetime import date, datetime

import pytest

import nse_holidays as nh
import session_proof_client as spc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    spc.reset_state()
    monkeypatch.setattr(nh, "_today_ist", lambda: date(2026, 10, 12))   # Monday, not a listed holiday
    yield
    spc.reset_state()


def test_no_verdict_means_open():
    assert nh.is_nse_holiday(date(2026, 10, 12)) is False
    assert nh.holiday_name(date(2026, 10, 12)) is None


def test_closed_verdict_makes_today_a_holiday():
    spc.set_verdict("2026-10-12", True)
    assert nh.is_nse_holiday(date(2026, 10, 12)) is True
    assert nh.is_nse_holiday(datetime(2026, 10, 12, 11, 0)) is True
    assert "No price movement" in nh.holiday_name(date(2026, 10, 12))


def test_verdict_never_touches_other_dates():
    spc.set_verdict("2026-10-12", True)
    assert nh.is_nse_holiday(date(2026, 10, 13)) is False
    assert nh.is_nse_holiday(date(2026, 10, 9)) is False


def test_verdict_for_another_day_is_ignored():
    spc.set_verdict("2026-10-09", True)
    assert nh.is_nse_holiday(date(2026, 10, 12)) is False


def test_calendar_holiday_name_is_unchanged():
    assert nh.is_nse_holiday(date(2026, 10, 20)) is True
    assert nh.holiday_name(date(2026, 10, 20)) == "NSE holiday"


def test_disabled_client_is_ignored(monkeypatch):
    spc.set_verdict("2026-10-12", True)
    monkeypatch.setenv("SESSION_PROOF_CLIENT_ENABLED", "false")
    assert nh.is_nse_holiday(date(2026, 10, 12)) is False


def test_client_error_is_fail_open(monkeypatch):
    def boom(_day, refresh=True):
        raise RuntimeError("x")
    monkeypatch.setattr(spc, "closed_for_day", boom)
    assert nh.is_nse_holiday(date(2026, 10, 12)) is False
