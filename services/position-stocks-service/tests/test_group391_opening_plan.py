"""group 391: 09:10 opening plan (screening/opening_plan.py) and its hook in orders/adaptive.py."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import adaptive
from screening import opening_plan, prev_day

IST = timezone(timedelta(hours=5, minutes=30))
MON = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=IST)     # a Monday trading day
SAT = lambda h, m: datetime(2026, 10, 10, h, m, tzinfo=IST)


def pd_of(atr_pct=3.0, avg_range=None, close=100.0):
    return prev_day.PrevDay(high=close * 1.02, low=close * 0.98, close=close,
                            atr=(atr_pct / 100.0 * close) if atr_pct else None, date="2026-10-09", avg_range_pct=avg_range)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    opening_plan.reset()
    prev_day.reset()
    monkeypatch.setattr(config, "OPENING_PLAN_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_PLAN_BAR_FRACTION", 0.25)
    monkeypatch.setattr(config, "OPENING_PLAN_TIME_IST", "09:10")
    monkeypatch.setattr(config, "OPENING_PLAN_USE_UNTIL_IST", "10:00")
    yield
    opening_plan.reset()
    prev_day.reset()


def seed(sym, pd):
    prev_day._cache[sym] = (opening_plan.ist_today_str(), pd)


class TestExpectedBar:
    def test_atr_scaled_by_fraction(self):
        assert opening_plan.expected_bar_pct(pd_of(4.0)) == pytest.approx(1.0)

    def test_falls_back_to_avg_range_when_no_atr(self):
        assert opening_plan.expected_bar_pct(pd_of(None, avg_range=2.0)) == pytest.approx(0.5)

    def test_none_without_any_daily_number(self):
        assert opening_plan.expected_bar_pct(pd_of(None)) is None

    def test_bad_fraction_gives_none(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_PLAN_BAR_FRACTION", 0.0)
        assert opening_plan.expected_bar_pct(pd_of(4.0)) is None


class TestWindow:
    def test_due_between_plan_time_and_cutoff(self):
        assert opening_plan.due(MON(9, 10)) and opening_plan.due(MON(9, 40))
        assert not opening_plan.due(MON(9, 9)) and not opening_plan.due(MON(10, 0))

    def test_not_due_weekend_or_disabled_or_built(self, monkeypatch):
        assert not opening_plan.due(SAT(9, 30))
        opening_plan.build([], now=MON(9, 10))
        assert not opening_plan.due(MON(9, 11))
        opening_plan.reset()
        monkeypatch.setattr(config, "OPENING_PLAN_ENABLED", False)
        assert not opening_plan.due(MON(9, 30)) and not opening_plan.window_open(MON(9, 30))


class TestBuildAndLookup:
    def test_build_plans_cached_symbols_only(self):
        seed("AAA", pd_of(4.0))
        st = opening_plan.build(["AAA.NS", "BBB"], now=MON(9, 10))
        assert st["planned"] == 1 and st["universe"] == 2
        assert opening_plan.bar_atr_pct("AAA", now=MON(9, 15)) == pytest.approx(1.0)
        assert opening_plan.bar_atr_pct("BBB", now=MON(9, 15)) is None

    def test_late_cached_symbol_read_from_prev_day_peek(self):
        opening_plan.build(["CCC"], now=MON(9, 10))
        seed("CCC", pd_of(2.0))
        assert opening_plan.bar_atr_pct("CCC", now=MON(9, 16)) == pytest.approx(0.5)

    def test_none_outside_window(self):
        seed("AAA", pd_of(4.0))
        opening_plan.build(["AAA"], now=MON(9, 10))
        assert opening_plan.bar_atr_pct("AAA", now=MON(10, 5)) is None
        assert opening_plan.bar_atr_pct("AAA", now=SAT(9, 20)) is None

    def test_never_raises_on_junk(self):
        assert opening_plan.bar_atr_pct(None, now=MON(9, 20)) is None
        assert opening_plan.build(None, now=MON(9, 10))["planned"] == 0


class TestAdaptiveHook:
    @pytest.fixture(autouse=True)
    def bar_mode(self, monkeypatch):
        monkeypatch.setattr(config, "ADAPTIVE_BAR_ATR_ENABLED", True)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_PCT", 0.0)
        monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: None)
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: None)      # no live bars at the open

    def at_open(self, monkeypatch, hh=9, mm=16):
        monkeypatch.setattr(opening_plan, "window_open", lambda now=None: True)

    def test_plan_replaces_the_flat_two_percent_stop(self, monkeypatch):
        seed("AAA", pd_of(3.0))                                    # bar ~0.75% -> stop 0.75*1.3 = 0.975%
        opening_plan.build(["AAA"], now=MON(9, 10))
        self.at_open(monkeypatch)
        monkeypatch.setattr(config, "ADAPTIVE_BAR_STOP_MIN_PCT", 0.8)   # E251 raised the production floor to 1.2; this test pins the plan -> stop mapping, not the floor
        lv = adaptive.compute(0.0, 100.0, symbol="AAA")
        assert lv.stop_pct == pytest.approx(0.975, abs=0.01)
        assert lv.stop_pct < config.MIN_STOP_PCT
        assert lv.target_pct == pytest.approx(lv.stop_pct * config.ADAPTIVE_BAR_TARGET_RR, abs=0.01)

    def test_stop_stays_inside_bar_clamps(self, monkeypatch):
        seed("HOT", pd_of(12.0))
        seed("DUL", pd_of(0.5))
        opening_plan.build(["HOT", "DUL"], now=MON(9, 10))
        self.at_open(monkeypatch)
        assert adaptive.compute(0.0, 100.0, symbol="HOT").stop_pct == pytest.approx(config.ADAPTIVE_BAR_STOP_MAX_PCT)
        assert adaptive.compute(0.0, 100.0, symbol="DUL").stop_pct == pytest.approx(config.ADAPTIVE_BAR_STOP_MIN_PCT)

    def test_no_plan_keeps_legacy_levels(self, monkeypatch):
        self.at_open(monkeypatch)
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: None)
        lv = adaptive.compute(0.0, 100.0, symbol="ZZZ")
        assert lv.stop_pct == pytest.approx(config.MIN_STOP_PCT)

    def test_outside_window_keeps_legacy_levels(self, monkeypatch):
        seed("AAA", pd_of(3.0))
        opening_plan.build(["AAA"], now=MON(9, 10))
        monkeypatch.setattr(opening_plan, "window_open", lambda now=None: False)
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: None)
        assert adaptive.compute(0.0, 100.0, symbol="AAA").stop_pct == pytest.approx(config.MIN_STOP_PCT)

    def test_live_bars_win_over_the_plan(self, monkeypatch):
        seed("AAA", pd_of(3.0))
        opening_plan.build(["AAA"], now=MON(9, 10))
        self.at_open(monkeypatch)
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: 1.2)       # live bars say 1.2% -> stop 1.56%
        assert adaptive.compute(0.0, 100.0, symbol="AAA").stop_pct == pytest.approx(1.56, abs=0.01)

    def test_plan_error_falls_back_silently(self, monkeypatch):
        def boom(symbol, now=None):
            raise RuntimeError("x")
        monkeypatch.setattr(opening_plan, "bar_atr_pct", boom)
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: None)
        assert adaptive.compute(0.0, 100.0, symbol="AAA").stop_pct == pytest.approx(config.MIN_STOP_PCT)
