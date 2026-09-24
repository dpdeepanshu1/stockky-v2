"""
tests/test_tz_utils.py — direct unit tests for tz_utils.py (coverage plan,
position-stocks-service round (session112 round 9): was 71%, missing lines
87-88, 99-104, 134-135, 142, 147).

Every function here is a pure stdlib datetime helper (no DB, no network) —
same pattern as session94's event_depth_local.py — but unlike that module,
NOTHING in the repo calls it directly under test today: every call site
(main.py's _check_and_expire_gates, entry_engine, manual_engine,
auth/dhan_credentials_ro.py) monkeypatches tz_utils functions away rather
than exercising the real ones, so this file is this module's first direct
coverage. Ported from real-trade-service's tests/test_tz_utils.py
(session106) — the two tz_utils.py copies are identical apart from
docstrings, so the same test bodies apply unchanged.

Pure stdlib (datetime, zoneinfo) — runs with plain python3, no
sqlalchemy/pytest required to hand-verify the logic.

Run from services/position-stocks-service:
    python -m pytest tests/test_tz_utils.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time as dtime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import tz_utils


def _ist(y, mo, d, h, mi, s=0):
    """Build a tz-aware datetime directly in IST for readable fixtures."""
    return datetime(y, mo, d, h, mi, s, tzinfo=tz_utils.IST)


# ── is_nse_holiday ───────────────────────────────────────────────────────

def test_is_nse_holiday_true_on_listed_holiday():
    # 2026-09-14 is Ganesh Chaturthi, in the module's own holiday set.
    assert tz_utils.is_nse_holiday(_ist(2026, 9, 14, 12, 0)) is True


def test_is_nse_holiday_false_on_ordinary_trading_day():
    # 2026-09-15 is a Tuesday, not in the holiday set.
    assert tz_utils.is_nse_holiday(_ist(2026, 9, 15, 12, 0)) is False


def test_is_nse_holiday_defaults_to_now_when_no_arg_given():
    # Just needs to run the "now" branch without raising.
    result = tz_utils.is_nse_holiday()
    assert isinstance(result, bool)


# ── is_market_open_ist ───────────────────────────────────────────────────

def test_market_open_true_during_trading_hours_on_a_weekday():
    # 2026-09-15 is a Tuesday, 11:00 IST — well inside 09:15-15:30.
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 15, 11, 0)) is True


def test_market_closed_on_saturday():
    # 2026-09-19 is a Saturday.
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 19, 11, 0)) is False


def test_market_closed_on_sunday():
    # 2026-09-20 is a Sunday.
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 20, 11, 0)) is False


def test_market_closed_on_an_nse_holiday_even_during_hours():
    # 2026-09-14 (Ganesh Chaturthi) is a Monday, 11:00 IST would otherwise
    # be well inside market hours — this is exactly the incident the
    # AUDIT FIX comment in the module describes.
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 14, 11, 0)) is False


def test_market_closed_before_open_on_a_weekday():
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 15, 9, 0)) is False


def test_market_closed_after_close_on_a_weekday():
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 15, 15, 31)) is False


def test_market_open_at_exact_open_and_close_boundaries():
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 15, 9, 15)) is True
    assert tz_utils.is_market_open_ist(_ist(2026, 9, 15, 15, 30)) is True


# ── as_aware ──────────────────────────────────────────────────────────────

def test_as_aware_none_passes_through():
    assert tz_utils.as_aware(None) is None


def test_as_aware_attaches_utc_to_naive_datetime():
    naive = datetime(2026, 9, 15, 4, 0, 0)
    result = tz_utils.as_aware(naive)
    assert result.tzinfo is timezone.utc
    assert result.replace(tzinfo=None) == naive


def test_as_aware_leaves_already_aware_datetime_unchanged():
    aware = datetime(2026, 9, 15, 4, 0, 0, tzinfo=timezone.utc)
    assert tz_utils.as_aware(aware) is aware


# ── ist_now / ist_today_str ──────────────────────────────────────────────

def test_ist_now_converts_utc_to_ist_offset():
    utc_dt = datetime(2026, 9, 15, 4, 0, 0, tzinfo=timezone.utc)
    result = tz_utils.ist_now(utc_dt)
    # IST is UTC+5:30
    assert result.hour == 9 and result.minute == 30


def test_ist_today_str_formats_as_yyyy_mm_dd():
    utc_dt = datetime(2026, 9, 15, 20, 0, 0, tzinfo=timezone.utc)  # 01:30 IST next day
    assert tz_utils.ist_today_str(utc_dt) == "2026-09-16"


# ── parse_hhmm ────────────────────────────────────────────────────────────

def test_parse_hhmm_valid_string():
    assert tz_utils.parse_hhmm("09:15", 0, 0) == dtime(9, 15)


def test_parse_hhmm_falls_back_to_default_on_garbage_input():
    assert tz_utils.parse_hhmm("not-a-time", 9, 15) == dtime(9, 15)


def test_parse_hhmm_falls_back_to_default_on_none():
    assert tz_utils.parse_hhmm(None, 15, 30) == dtime(15, 30)


def test_parse_hhmm_falls_back_when_only_one_part_given():
    assert tz_utils.parse_hhmm("0915", 9, 15) == dtime(9, 15)


# ── ist_time_at_or_after ─────────────────────────────────────────────────

def test_ist_time_at_or_after_true_when_past_target():
    now = _ist(2026, 9, 15, 16, 0)
    assert tz_utils.ist_time_at_or_after(dtime(15, 30), now) is True


def test_ist_time_at_or_after_false_when_before_target():
    now = _ist(2026, 9, 15, 9, 0)
    assert tz_utils.ist_time_at_or_after(dtime(15, 30), now) is False


def test_ist_time_at_or_after_true_exactly_at_target():
    now = _ist(2026, 9, 15, 15, 30, 0)
    assert tz_utils.ist_time_at_or_after(dtime(15, 30), now) is True


# ── is_ist_weekday ────────────────────────────────────────────────────────

def test_is_ist_weekday_true_on_tuesday():
    assert tz_utils.is_ist_weekday(_ist(2026, 9, 15, 12, 0)) is True


def test_is_ist_weekday_false_on_saturday():
    assert tz_utils.is_ist_weekday(_ist(2026, 9, 19, 12, 0)) is False


def test_is_ist_weekday_false_on_sunday():
    assert tz_utils.is_ist_weekday(_ist(2026, 9, 20, 12, 0)) is False


def test_is_ist_weekday_does_not_consult_the_holiday_list():
    # Docstring is explicit that this function has no holiday awareness,
    # unlike is_market_open_ist — 2026-09-14 is a holiday but also a
    # Monday, so this must still be True.
    assert tz_utils.is_ist_weekday(_ist(2026, 9, 14, 12, 0)) is True


# ── iso_utc ───────────────────────────────────────────────────────────────

def test_iso_utc_none_passes_through():
    assert tz_utils.iso_utc(None) is None


def test_iso_utc_adds_offset_to_naive_datetime():
    naive = datetime(2026, 8, 28, 4, 7, 0)
    result = tz_utils.iso_utc(naive)
    assert result == "2026-08-28T04:07:00+00:00"


def test_iso_utc_preserves_already_aware_datetime():
    aware = datetime(2026, 8, 28, 4, 7, 0, tzinfo=timezone.utc)
    assert tz_utils.iso_utc(aware) == aware.isoformat()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
