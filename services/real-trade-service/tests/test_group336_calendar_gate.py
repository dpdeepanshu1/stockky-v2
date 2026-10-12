"""group336 - expiry / Friday / pre-holiday rules (entry_engine/calendar_gate.py) and their wiring into Gate 4d, sizing and the overnight carry cap.
Run from services/real-trade-service:  python3 -m pytest tests/test_group336_calendar_gate.py -q"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import calendar_gate as cg
from tz_utils import IST


def at(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(CALENDAR_RULES_ENABLED=True, CALENDAR_EXPIRY_WEEKDAY=1, CALENDAR_EXPIRY_ENTRY_CUTOFF_IST="14:30",
                     CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST="14:30", CALENDAR_FRIDAY_ENTRY_CUTOFF_IST="",
                     CALENDAR_EXPIRY_SIZE_MULT=0.8, CALENDAR_PRE_HOLIDAY_SIZE_MULT=0.8, CALENDAR_FRIDAY_SIZE_MULT=0.9,
                     CALENDAR_EXPIRY_CARRY_MAX=2, CALENDAR_PRE_HOLIDAY_CARRY_MAX=1, CALENDAR_FRIDAY_CARRY_MAX=2).items():
        monkeypatch.setattr(config, k, v, raising=False)


def test_expiry_days_and_holiday_roll_back():
    assert cg.is_expiry_day(date(2026, 10, 13)) == "weekly"
    assert cg.is_expiry_day(date(2026, 10, 27)) == "monthly"
    assert cg.is_expiry_day(date(2026, 10, 14)) is None
    assert cg.is_expiry_day(date(2026, 10, 20)) is None            # Dussehra holiday
    assert cg.is_expiry_day(date(2026, 10, 19)) == "weekly"        # expiry rolled back to Monday


def test_pre_holiday_needs_a_holiday_not_just_a_weekend():
    assert cg.is_pre_holiday(date(2026, 10, 19)) is True
    assert cg.is_pre_holiday(date(2026, 10, 1)) is True
    assert cg.is_pre_holiday(date(2026, 10, 16)) is False


def test_context_tags():
    assert cg.context(at(2026, 10, 16))["tags"] == ["friday"]
    assert cg.context(at(2026, 10, 19))["tags"] == ["expiry", "pre_holiday"]
    assert cg.context(at(2026, 10, 17))["tags"] == []


def test_cutoff_size_and_carry():
    assert cg.entry_block_reason(at(2026, 10, 19, 14, 29)) is None
    assert cg.entry_block_reason(at(2026, 10, 19, 14, 30)).startswith("CALENDAR_EXPIRY_CUTOFF")
    assert cg.entry_block_reason(at(2026, 10, 16, 15, 0)) is None
    assert cg.size_mult(at(2026, 10, 16)) == 0.9 and cg.size_mult(at(2026, 10, 19)) == 0.8 and cg.size_mult(at(2026, 10, 14)) == 1.0
    assert cg.carry_max_positions(3, at(2026, 10, 19)) == 1 and cg.carry_max_positions(3, at(2026, 10, 14)) == 3
    assert cg.carry_max_positions(1, at(2026, 10, 16)) == 1


def test_switch_off(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", False)
    assert cg.entry_block_reason(at(2026, 10, 19, 15)) is None and cg.size_mult(at(2026, 10, 19)) == 1.0
    assert cg.carry_max_positions(3, at(2026, 10, 19)) == 3


def test_config_blank_cutoff_means_none():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()
    assert "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST = _cal_str(" in src and "an explicitly blank variable means" in src
