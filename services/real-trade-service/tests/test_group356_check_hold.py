"""group356 (14 Oct 10:38 IST restart log): the dynamic universe's /check warm-up must not start in the first minutes after a
restart or right after the 09:15 open - that is when position-stocks-service's quality gate needs analysis-intelligence-service."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du
from tz_utils import IST


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND", raising=False)
    monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_STARTUP_HOLD_S", raising=False)
    monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_OPEN_HOLD_MIN", raising=False)
    du._CHECK_PENDING[0] = False
    du._CHECK_RUNNING[0] = False
    du._last_run_ts = None
    yield
    du._CHECK_PENDING[0] = False


def _ist(h, m, s=0):
    return datetime(2026, 10, 14, h, m, s, tzinfo=IST)


class TestHoldRemaining:
    def test_fresh_process_is_held_for_the_startup_window(self):
        now = du._PROCESS_START + 60.0
        assert du._check_hold_remaining_s(now_mono=now, now_ist=_ist(11, 0)) == pytest.approx(240.0)

    def test_old_process_midday_is_not_held(self):
        now = du._PROCESS_START + 4000.0
        assert du._check_hold_remaining_s(now_mono=now, now_ist=_ist(11, 0)) == 0.0

    def test_first_ten_minutes_after_the_open_are_held(self):
        now = du._PROCESS_START + 4000.0
        assert du._check_hold_remaining_s(now_mono=now, now_ist=_ist(9, 20)) == pytest.approx(300.0)
        assert du._check_hold_remaining_s(now_mono=now, now_ist=_ist(9, 25)) == 0.0

    def test_before_the_open_is_not_a_hold(self):
        now = du._PROCESS_START + 4000.0
        assert du._check_hold_remaining_s(now_mono=now, now_ist=_ist(9, 0)) == 0.0

    def test_zero_turns_each_hold_off(self, monkeypatch):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_STARTUP_HOLD_S", "0")
        monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_OPEN_HOLD_MIN", "0")
        assert du._check_hold_remaining_s(now_mono=du._PROCESS_START + 1.0, now_ist=_ist(9, 16)) == 0.0

    @pytest.mark.parametrize("raw", ["", "abc", "-5"])
    def test_blank_or_bad_values_use_the_defaults(self, monkeypatch, raw):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_STARTUP_HOLD_S", raw)
        assert du._check_hold_remaining_s(now_mono=du._PROCESS_START + 60.0, now_ist=_ist(11, 0)) == pytest.approx(240.0)


def _refresh(desired=("INFY",), current=()):
    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=post_resp)
    with patch("watchlist_engine.dynamic_universe.is_market_open_ist", return_value=True), \
         patch("watchlist_engine.dynamic_universe._compute_desired_universe", new=AsyncMock(return_value=list(desired))), \
         patch("watchlist_engine.dynamic_universe._get_current_auto_subscriptions", new=AsyncMock(return_value=set(current))), \
         patch("watchlist_engine.dynamic_universe.httpx.AsyncClient", return_value=client):
        return asyncio.run(du.refresh_dynamic_universe())


class TestSyncWithHold:
    def test_subscriptions_sync_but_check_does_not_start_while_held(self):
        with patch.object(du, "_check_hold_remaining_s", return_value=120.0), \
             patch.object(du, "_start_check_background", return_value=True) as start:
            result = _refresh()
        assert result["added"] == ["INFY"]          # the subscription sync itself is not delayed
        start.assert_not_called()
        assert du._CHECK_PENDING[0] is True

    def test_held_check_starts_on_the_first_cycle_after_the_hold_even_if_not_due(self):
        du._CHECK_PENDING[0] = True
        du._last_run_ts = time.monotonic()           # just synced: _due() is False
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist", return_value=True), \
             patch.object(du, "_check_hold_remaining_s", return_value=0.0), \
             patch.object(du, "_start_check_background", return_value=True) as start:
            assert asyncio.run(du.refresh_dynamic_universe()) is None
        start.assert_called_once()
        assert du._CHECK_PENDING[0] is False

    def test_still_held_means_still_pending(self):
        du._CHECK_PENDING[0] = True
        du._last_run_ts = time.monotonic()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist", return_value=True), \
             patch.object(du, "_check_hold_remaining_s", return_value=30.0), \
             patch.object(du, "_start_check_background", return_value=True) as start:
            asyncio.run(du.refresh_dynamic_universe())
        start.assert_not_called()
        assert du._CHECK_PENDING[0] is True

    def test_no_hold_starts_the_check_immediately_as_before(self):
        with patch.object(du, "_check_hold_remaining_s", return_value=0.0), \
             patch.object(du, "_start_check_background", return_value=True) as start:
            _refresh()
        start.assert_called_once()
        assert du._CHECK_PENDING[0] is False
