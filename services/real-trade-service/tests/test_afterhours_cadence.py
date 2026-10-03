"""
tests/test_afterhours_cadence.py

2026-10-04 (user request): the after-hours news scan loop used ONE fixed interval. It now
sleeps by time of day - every 6 h off-market, every 30 min in the 08:00-09:00 IST pre-open
ramp - and the long sleep is capped at the next boundary so the ramp / evening start are never
skipped. Also covers the finalize-reachability fix (the 15:45-08:45 window ends the same minute
finalize is due, so a scheduled tick at 08:45 used to be rejected as "outside_window").

Run from services/real-trade-service:
    python3 -m pytest tests/test_afterhours_cadence.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import auto_pilot as ap

IST = ZoneInfo("Asia/Kolkata")
_engine = create_engine("sqlite:///:memory:")


def at(h, m=0):
    return datetime(2026, 10, 5, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_START_IST", "15:45")
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_END_IST", "08:45")
    monkeypatch.setattr(config, "AFTERHOURS_FINALIZE_TIME_IST", "08:45")
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_RAMP_START_IST", "08:00")
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_RAMP_END_IST", "09:00")
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_RAMP_INTERVAL_SECONDS", 1800)
    monkeypatch.setattr(config, "AFTERHOURS_SCAN_OFFHOURS_INTERVAL_SECONDS", 21600)


class TestNextSleepSeconds:
    def test_ramp_runs_every_30_min_with_a_finalize_tick_at_0845(self):
        assert ap._afterhours_next_sleep_seconds(at(8, 0)) == 30 * 60
        assert ap._afterhours_next_sleep_seconds(at(8, 5)) == 30 * 60
        assert ap._afterhours_next_sleep_seconds(at(8, 30)) == 15 * 60      # lands exactly on 08:45 finalize
        assert ap._afterhours_next_sleep_seconds(at(8, 45)) == 15 * 60      # then 09:00 ramp end

    def test_off_hours_is_six_hours_when_no_boundary_is_closer(self):
        assert ap._afterhours_next_sleep_seconds(at(15, 45)) == 6 * 3600    # evening start -> 21:45
        assert ap._afterhours_next_sleep_seconds(at(21, 45)) == 6 * 3600    # -> 03:45
        assert ap._afterhours_next_sleep_seconds(at(2, 0)) == 6 * 3600      # -> 08:00 exactly

    def test_long_sleep_never_skips_the_morning_ramp(self):
        # 03:45 + 6h would be 09:45 - must wake at 08:00 instead
        assert ap._afterhours_next_sleep_seconds(at(3, 45)) == 4 * 3600 + 15 * 60

    def test_market_hours_sleep_straight_to_evening_start(self):
        assert ap._afterhours_next_sleep_seconds(at(9, 0)) == 6 * 3600 + 45 * 60   # -> 15:45
        assert ap._afterhours_next_sleep_seconds(at(12, 0)) == 3 * 3600 + 45 * 60
        assert ap._afterhours_next_sleep_seconds(at(15, 0)) == 45 * 60

    def test_never_below_30_seconds(self):
        assert ap._afterhours_next_sleep_seconds(datetime(2026, 10, 5, 8, 59, 50, tzinfo=IST)) == 30


def test_loop_sleeps_by_time_of_day(monkeypatch):
    sleeps = []

    class _Stop(Exception):
        pass

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:         # 1st = startup delay, 2nd = the loop's cadence sleep
            raise _Stop

    async def fake_to_thread(fn, *a, **kw):
        return None

    monkeypatch.setattr(ap.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ap.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(ap, "_afterhours_next_sleep_seconds", lambda now=None: 4321.0)
    with pytest.raises(_Stop):
        asyncio.run(ap._afterhours_scan_loop())
    assert sleeps[-1] == 4321.0


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    monkey_session = sessionmaker(bind=_engine)
    ap_get = ap.get_session_factory
    ap.get_session_factory = lambda: monkey_session
    yield s
    ap.get_session_factory = ap_get
    s.close()


class TestFinalizeReachable:
    def _arm(self, db, monkeypatch, hh, mm, window_active, finalize_last_run=None):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=True,
                                     afterhours_finalize_last_run=finalize_last_run))
        db.commit()
        fixed = at(hh, mm)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed)
        monkeypatch.setattr("tz_utils.ist_today_str", lambda now=None: "2026-10-05")
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: window_active)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-10-05")

    def test_0845_tick_outside_window_still_finalizes(self, db, monkeypatch):
        self._arm(db, monkeypatch, 8, 45, window_active=False)
        finalised = []

        async def fin(db_, mode, md):
            finalised.append((mode, md))
            return ["SYMA"]

        async def note(text):
            return True

        monkeypatch.setattr("watchlist_engine.afterhours_scan.finalize_nextday_watchlist", fin)
        monkeypatch.setattr(ap, "notify_async", note)
        res = asyncio.run(ap._afterhours_scan_body("REAL"))
        assert res["finalized"] is True and finalised == [("REAL", "2026-10-05")]

    def test_midday_tick_is_still_outside_window(self, db, monkeypatch):
        self._arm(db, monkeypatch, 12, 0, window_active=False)
        assert asyncio.run(ap._afterhours_scan_body("REAL"))["reason"] == "outside_window"

    def test_already_finalized_today_is_outside_window(self, db, monkeypatch):
        self._arm(db, monkeypatch, 8, 50, window_active=False, finalize_last_run="2026-10-05")
        assert asyncio.run(ap._afterhours_scan_body("REAL"))["reason"] == "outside_window"

    def test_finalize_only_tick_never_falls_through_to_a_scan(self, db, monkeypatch):
        # holiday-style case: market_date != today, so finalize is skipped; must NOT run a scan at 08:50
        self._arm(db, monkeypatch, 8, 50, window_active=False)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-10-06")
        called = []

        async def scan(*a, **kw):
            called.append(1)
            return 0

        monkeypatch.setattr("watchlist_engine.afterhours_scan.run_afterhours_scan", scan)
        res = asyncio.run(ap._afterhours_scan_body("REAL"))
        assert res["reason"] == "outside_window" and not called
