"""group 390: the 09:10-09:30 fast cadence (start moved from 09:14 in group 399) of the real-trade schedule / full-cycle loops (execution/auto_pilot.py)."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import auto_pilot as ap
from tz_utils import IST

FRI = (2026, 10, 9)    # a Friday
SAT = (2026, 10, 10)


def at(h, m, s=0, day=FRI):
    return datetime(*day, h, m, s, tzinfo=IST)


def test_window_bounds():
    assert ap._opening_fast_window(at(9, 9, 59)) is False
    assert ap._opening_fast_window(at(9, 10)) is True
    assert ap._opening_fast_window(at(9, 29, 59)) is True
    assert ap._opening_fast_window(at(9, 30)) is False
    assert ap._opening_fast_window(at(9, 20, day=SAT)) is False


def test_cadence_inside_and_outside_the_window():
    assert ap._schedule_sleep_seconds(at(9, 20)) == config.OPENING_FAST_SCHEDULE_SECONDS == 5
    assert ap._full_cycle_sleep_seconds(at(9, 20)) == config.OPENING_FAST_CYCLE_SECONDS == 10
    assert ap._schedule_sleep_seconds(at(11, 0)) == config.SCHEDULE_CHECK_INTERVAL_SECONDS
    assert ap._full_cycle_sleep_seconds(at(11, 0)) == config.AUTO_PILOT_INTERVAL_SECONDS


def test_long_sleep_never_overshoots_the_window_start():
    assert ap._full_cycle_sleep_seconds(at(9, 8)) <= 121           # a 180 s sleep begun at 09:08 would skip past 09:10
    assert ap._schedule_sleep_seconds(at(9, 9, 50)) <= 11
    assert ap._full_cycle_sleep_seconds(at(7, 0)) == config.AUTO_PILOT_INTERVAL_SECONDS     # far from the window: unchanged
    assert ap._full_cycle_sleep_seconds(at(9, 8, day=SAT)) == config.AUTO_PILOT_INTERVAL_SECONDS


def test_switch_off(monkeypatch):
    monkeypatch.setattr(config, "OPENING_FAST_CYCLE_SECONDS", 0)
    assert ap._opening_fast_window(at(9, 20)) is False
    assert ap._schedule_sleep_seconds(at(9, 20)) == config.SCHEDULE_CHECK_INTERVAL_SECONDS
    assert ap._full_cycle_sleep_seconds(at(9, 12)) == config.AUTO_PILOT_INTERVAL_SECONDS


def test_bad_config_falls_back(monkeypatch):
    monkeypatch.setattr(config, "OPENING_FAST_START_IST", "garbage")
    assert ap._opening_fast_window(at(9, 10)) is True               # parse_hhmm default 09:10
