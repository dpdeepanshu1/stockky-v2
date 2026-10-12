"""group336 - expiry / Friday / pre-holiday rules (orders/calendar_gate.py), the no-news big-move check (orders/catalyst_gate.py) and the
losing-window auto-disable + stop/target advice (orders/window_health.py).
Run from services/position-stocks-service:  python3 -m pytest tests/test_group336_calendar_catalyst_window.py -q"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import calendar_gate as cg, catalyst_gate, window_health as wh
from tz_utils import IST


def at(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(CALENDAR_RULES_ENABLED=True, CALENDAR_EXPIRY_WEEKDAY=1, CALENDAR_EXPIRY_ENTRY_CUTOFF_IST="14:30",
                     CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST="14:30", CALENDAR_FRIDAY_ENTRY_CUTOFF_IST="",
                     CALENDAR_EXPIRY_SIZE_MULT=0.8, CALENDAR_PRE_HOLIDAY_SIZE_MULT=0.8, CALENDAR_FRIDAY_SIZE_MULT=0.9,
                     CALENDAR_EXPIRY_CARRY_MAX=2, CALENDAR_PRE_HOLIDAY_CARRY_MAX=1, CALENDAR_FRIDAY_CARRY_MAX=2,
                     CATALYST_NO_NEWS_ACTION="size", CATALYST_MOVE_PCT=4.0, CATALYST_NO_NEWS_SIZE_MULT=0.6,
                     WINDOW_AUTODISABLE_ENABLED=True, WINDOW_AUTODISABLE_DAYS=7, WINDOW_AUTODISABLE_MIN_TRADES=8,
                     WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT=35.0, WINDOW_TUNING_MIN_TRADES=20).items():
        monkeypatch.setattr(config, k, v, raising=False)
    wh.reset()
    yield
    wh.reset()


class TestCalendar:
    def test_expiry_days(self):
        assert cg.is_expiry_day(date(2026, 10, 13)) == "weekly"
        assert cg.is_expiry_day(date(2026, 10, 27)) == "monthly"          # last Tuesday of October
        assert cg.is_expiry_day(date(2026, 10, 14)) is None

    def test_expiry_rolls_back_over_a_holiday(self):
        # Tue 2026-10-20 is Dussehra -> expiry moves to Mon 19th
        assert cg.is_expiry_day(date(2026, 10, 20)) is None
        assert cg.is_expiry_day(date(2026, 10, 19)) == "weekly"

    def test_pre_holiday_needs_a_real_holiday_not_a_weekend(self):
        assert cg.is_pre_holiday(date(2026, 10, 19)) is True            # Tue 20th is a holiday
        assert cg.is_pre_holiday(date(2026, 10, 1)) is True             # Fri 2nd Gandhi Jayanti
        assert cg.is_pre_holiday(date(2026, 10, 16)) is False           # ordinary Friday before a plain weekend

    def test_context_tags(self):
        assert cg.context(at(2026, 10, 16))["tags"] == ["friday"]
        assert cg.context(at(2026, 10, 19))["tags"] == ["expiry", "pre_holiday"]
        assert cg.context(at(2026, 10, 17))["tags"] == []               # Saturday
        assert cg.context(at(2026, 10, 14))["tags"] == []

    def test_entry_cutoff(self):
        assert cg.entry_block_reason(at(2026, 10, 19, 14, 29)) is None
        r = cg.entry_block_reason(at(2026, 10, 19, 14, 30))
        assert r and r.startswith("CALENDAR_EXPIRY_CUTOFF")
        assert cg.entry_block_reason(at(2026, 10, 14, 15, 0)) is None   # ordinary day
        assert cg.entry_block_reason(at(2026, 10, 16, 15, 0)) is None   # Friday cut-off blank by default

    def test_friday_cutoff_when_set(self, monkeypatch):
        monkeypatch.setattr(config, "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "15:00")
        assert cg.entry_block_reason(at(2026, 10, 16, 15, 0)).startswith("CALENDAR_FRIDAY_CUTOFF")

    def test_size_and_carry(self):
        assert cg.size_mult(at(2026, 10, 14)) == 1.0
        assert cg.size_mult(at(2026, 10, 16)) == 0.9
        assert cg.size_mult(at(2026, 10, 19)) == 0.8
        assert cg.carry_max_positions(3, at(2026, 10, 19)) == 1
        assert cg.carry_max_positions(3, at(2026, 10, 14)) == 3
        assert cg.carry_max_positions(1, at(2026, 10, 16)) == 1         # never raised above the normal cap

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", False)
        assert cg.entry_block_reason(at(2026, 10, 19, 15)) is None
        assert cg.size_mult(at(2026, 10, 19)) == 1.0
        assert cg.carry_max_positions(3, at(2026, 10, 19)) == 3


class TestCatalyst:
    def test_sizes_down_only_when_big_move_and_confirmed_no_news(self):
        assert catalyst_gate.decide(5.0, False) == (None, 0.6)
        assert catalyst_gate.decide(5.0, True) == (None, 1.0)
        assert catalyst_gate.decide(5.0, None) == (None, 1.0)           # unknown is never "no news"
        assert catalyst_gate.decide(5.0, False, True) == (None, 1.0)    # bulk deal counts as news
        assert catalyst_gate.decide(3.9, False) == (None, 1.0)
        assert catalyst_gate.decide("bad", False) == (None, 1.0)

    def test_block_and_off(self, monkeypatch):
        monkeypatch.setattr(config, "CATALYST_NO_NEWS_ACTION", "block")
        reason, mult = catalyst_gate.decide(5.0, False)
        assert reason.startswith("CATALYST_NONE") and mult == 1.0
        monkeypatch.setattr(config, "CATALYST_NO_NEWS_ACTION", "off")
        assert catalyst_gate.decide(5.0, False) == (None, 1.0)


def R(window, pnl, gain=2.0, dd=-1.0):
    from orders import excursion  # noqa: F401
    return NS(status="TARGET_HIT" if pnl > 0 else "STOP_HIT", realized_pnl=pnl, opened_at=datetime(2026, 10, 12, 10, 0), window_source=window,
              entry_price=100.0, max_price_seen=100.0 * (1 + gain / 100), min_price_seen=100.0 * (1 + dd / 100),
              adaptive_target_pct=3.0, adaptive_stop_pct=2.0, error_message=None)


class TestWindowHealth:
    def test_losing_window_is_paused_others_not(self):
        rows = [R("5m", -10)] * 9 + [R("5m", 5)] + [R("15m", 20)] * 5 + [R("manual", -50)] * 20
        ev = wh.evaluate(rows)
        assert ev["5m"]["disabled"] is True and ev["15m"]["disabled"] is False and "manual" not in ev

    def test_too_few_trades_or_good_win_rate_never_pauses(self):
        assert wh.evaluate([R("5m", -10)] * 7)["5m"]["disabled"] is False
        assert wh.evaluate([R("5m", -10)] * 6 + [R("5m", 30)] * 4)["5m"]["disabled"] is False      # 40 % wins, net positive

    def test_best_window_is_always_spared(self):
        ev = wh.evaluate([R("5m", -10)] * 10 + [R("1m", -5)] * 9)
        assert sum(v["disabled"] for v in ev.values()) == 1 and ev["1m"]["disabled"] is False

    def test_filter_candidates(self, monkeypatch):
        wh.reset()          # the module caches the paused set for 5 minutes; start clean
        # a profitable 15m window exists, so the losing 5m is not the "one window that always trades"
        monkeypatch.setattr(wh, "_load_rows", lambda db: [R("5m", -10)] * 10 + [R("15m", 20)] * 5)
        c = [NS(symbol="A", window_label="5m"), NS(symbol="B", window_label="15m")]
        db = NS(rollback=lambda: None)
        kept, dropped = wh.filter_candidates(db, c)
        assert [x.symbol for x in kept] == ["B"] and dropped == ["A:5m"]

    def test_switch_off_and_db_error_fail_open(self, monkeypatch):
        c = [NS(symbol="A", window_label="5m")]
        monkeypatch.setattr(config, "WINDOW_AUTODISABLE_ENABLED", False)
        assert wh.filter_candidates(NS(rollback=lambda: None), c) == (c, [])
        monkeypatch.setattr(config, "WINDOW_AUTODISABLE_ENABLED", True)
        wh.reset()
        monkeypatch.setattr(wh, "_load_rows", lambda db: (_ for _ in ()).throw(RuntimeError("db down")))
        assert wh.filter_candidates(NS(rollback=lambda: None), c) == (c, [])

    def test_suggest_needs_enough_trades_then_states_numbers(self):
        assert "note" in wh.suggest([R("5m", 5)] * 3)
        s = wh.suggest([R("5m", 5)] * 25 + [R("5m", -5, gain=1.8)] * 5)
        assert s["applied_automatically"] is False
        assert s["suggested_target_pct"] == 2.0 and s["suggested_stop_pct"] == 1.0
        assert s["losers"] == 5 and s["losers_that_got_halfway_to_target"] == 5
