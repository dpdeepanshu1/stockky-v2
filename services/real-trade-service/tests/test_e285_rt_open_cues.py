"""E285 round 102: overseas cues read at 08:30 IST size the morning's long entries down on a weak lead (entry_engine/open_cues.py). Never a refusal, never sizes up, fails open."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import open_cues

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m, d=2):                       # 2026-11-02 is a Monday; 2026-11-01 a Sunday
    return datetime(2026, 11, d, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    for k, v in (("OPEN_CUES_ENABLED", True), ("OPEN_CUES_TIME_IST", "08:30"), ("OPEN_CUES_UNTIL_IST", "10:30"), ("OPEN_CUES_RETRY_S", 300.0),
                 ("OPEN_CUES_SYMBOLS", "^N225,^HSI,ES=F,NQ=F"), ("OPEN_CUES_MAX_AGE_S", 7200.0), ("OPEN_CUES_WEAK_AVG_PCT", -0.9),
                 ("OPEN_CUES_WEAK_US_FUT_PCT", -1.2), ("OPEN_CUES_VERY_WEAK_FACTOR", 2.0), ("OPEN_CUES_WEAK_MUL", 0.7), ("OPEN_CUES_VERY_WEAK_MUL", 0.5),
                 ("GLOBAL_CUE_MIN_READINGS", 2), ("GLOBAL_CUE_US_FUTURES", "ES=F,NQ=F"), ("GLOBAL_CUE_MAX_AGE_S", 21600.0), ("GLOBAL_CUE_TIMEOUT_S", 12.0)):
        monkeypatch.setattr(config, k, v, raising=False)
    open_cues.reset()


class TestAssess:
    def test_fine_lead_or_too_few_readings_is_no_opinion(self):
        assert open_cues.assess({"^N225": 0.4, "ES=F": -0.2}) == (0, None)
        assert open_cues.assess({"ES=F": -3.0}) == (0, None)                    # one reading < GLOBAL_CUE_MIN_READINGS
        assert open_cues.assess(None) == (0, None) and open_cues.assess({}) == (0, None)

    def test_weak_average_is_level_one(self):
        lv, why = open_cues.assess({"^N225": -1.0, "^HSI": -1.2, "ES=F": -0.5, "NQ=F": -0.9})      # avg -0.9
        assert lv == 1 and why.startswith("OPEN_CUES_WEAK") and "average" in why

    def test_very_weak_average_is_level_two(self):
        assert open_cues.assess({"^N225": -2.0, "^HSI": -1.9})[0] == 2                              # avg -1.95 <= -1.8

    def test_one_weak_us_future_alone_is_enough(self):
        lv, why = open_cues.assess({"^N225": 0.3, "^HSI": 0.2, "ES=F": -1.3, "NQ=F": 0.1})
        assert lv == 1 and "ES=F" in why
        assert open_cues.assess({"^N225": 0.3, "^HSI": 0.2, "ES=F": -2.6, "NQ=F": 0.1})[0] == 2

    def test_a_zero_limit_disables_that_test(self, monkeypatch):
        monkeypatch.setattr(config, "OPEN_CUES_WEAK_AVG_PCT", 0.0)
        monkeypatch.setattr(config, "OPEN_CUES_WEAK_US_FUT_PCT", 0.0)
        assert open_cues.assess({"^N225": -5.0, "ES=F": -5.0}) == (0, None)


class TestParseBody:
    def test_unusable_rows_are_dropped(self):
        body = {"rows": {"^N225": {"day_change_pct": -1.1, "age_s": 600}, "^HSI": {"day_change_pct": -0.5, "age_s": 99999},
                         "ES=F": {"price": 99.0, "previous_close": 100.0}, "NQ=F": {"day_change_pct": 40.0}, "X": "junk"}}
        out = open_cues.parse_body(body)
        assert out["^N225"] == -1.1 and "^HSI" not in out and round(out["ES=F"], 2) == -1.0 and "NQ=F" not in out and "X" not in out
        assert open_cues.parse_body(None) == {} and open_cues.parse_body({"rows": 3}) == {}


class TestDue:
    def test_window_and_calendar(self):
        assert not open_cues.due(at(8, 29))
        assert open_cues.due(at(8, 30)) and open_cues.due(at(9, 40))
        assert not open_cues.due(at(10, 30))
        assert not open_cues.due(at(8, 45, d=1))                                # Sunday

    def test_off_switch(self, monkeypatch):
        monkeypatch.setattr(config, "OPEN_CUES_ENABLED", False)
        assert not open_cues.due(at(9, 0))

    def test_once_a_day_after_a_reading_and_retry_gap_after_an_empty_one(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {})
        open_cues.take(at(8, 31))
        assert not open_cues.due(at(8, 32))                                     # just tried, nothing read: wait OPEN_CUES_RETRY_S
        open_cues._state["tried_at"] = -1e9
        assert open_cues.due(at(8, 40))
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -1.0, "ES=F": -1.0})
        open_cues.take(at(8, 41))
        open_cues._state["tried_at"] = -1e9
        assert not open_cues.due(at(8, 50))                                     # a reading is stored for the day


class TestTakeAndMultiplier:
    def test_weak_lead_sizes_down_until_the_cutoff_then_stops(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -1.2, "^HSI": -1.0, "ES=F": -0.8, "NQ=F": -1.0})
        open_cues.take(at(8, 30))
        m, why = open_cues.multiplier(at(9, 30))
        assert m == 0.7 and why.startswith("OPEN_CUES_WEAK")
        assert open_cues.multiplier(at(10, 29))[0] == 0.7
        assert open_cues.multiplier(at(10, 30)) == (1.0, None)

    def test_very_weak_lead_uses_the_smaller_multiplier(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -2.2, "^HSI": -2.0})
        open_cues.take(at(8, 30))
        assert open_cues.multiplier(at(9, 20))[0] == 0.5

    def test_fine_lead_never_sizes_up(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": 2.2, "^HSI": 1.5, "ES=F": 1.0})
        open_cues.take(at(8, 30))
        assert open_cues.multiplier(at(9, 20)) == (1.0, None)

    def test_yesterdays_reading_does_not_carry_over(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -2.2, "^HSI": -2.0})
        open_cues.take(at(8, 30, d=2))
        assert open_cues.multiplier(at(9, 20, d=3)) == (1.0, None)

    def test_off_and_no_reading_are_neutral(self, monkeypatch):
        assert open_cues.multiplier(at(9, 20)) == (1.0, None)
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -2.2, "^HSI": -2.0})
        open_cues.take(at(8, 30))
        monkeypatch.setattr(config, "OPEN_CUES_ENABLED", False)
        assert open_cues.multiplier(at(9, 20)) == (1.0, None)

    def test_a_failed_fetch_never_raises_and_stores_nothing(self, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(open_cues, "_fetch", boom)
        assert open_cues.take(at(8, 30)) is None
        assert open_cues.status()["readings"] == {} and open_cues.multiplier(at(9, 20)) == (1.0, None)

    def test_status_shows_readings_level_and_reason(self, monkeypatch):
        monkeypatch.setattr(open_cues, "_fetch", lambda: {"^N225": -1.2, "^HSI": -1.0})
        open_cues.take(at(8, 30))
        st = open_cues.status()
        assert st["level"] == 1 and st["readings"]["^N225"] == -1.2 and st["day"] == "2026-11-02" and st["reason"]
