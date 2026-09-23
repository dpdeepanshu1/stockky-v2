"""
tests/test_watchlist_dynamic_universe.py  — session89, step 3
=============================================================
Coverage target:
  watchlist_engine/dynamic_universe.py   0% → 100%   (88 statements)

Strategy:
  - Reset module-level _last_run_ts before every test that cares about it.
  - Patch is_market_open_ist, httpx.AsyncClient, and
    candidate_engine.candidates._fetch_volume_shock_universe.
  - Each HTTP call path (subscribe / unsubscribe / /check) is exercised
    both in the success and exception branches.

Run from services/real-trade-service:
    python3 -m pytest tests/test_watchlist_dynamic_universe.py -v \
        --cov=watchlist_engine.dynamic_universe --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du


# ── helpers ──────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def _reset_timer():
    """Reset module-level _last_run_ts so _due() returns True."""
    du._last_run_ts = None


def _set_timer_just_ran():
    """Simulate that a run just happened (timer not yet due)."""
    du._last_run_ts = time.monotonic()


def _set_timer_overdue():
    """Simulate that the last run was >20 min ago."""
    du._last_run_ts = time.monotonic() - (du.REFRESH_INTERVAL_MIN * 60 + 1)


def _fake_client(get_side_effect=None, post_side_effect=None,
                 get_return=None, post_return=None):
    """Build a fake httpx.AsyncClient context-manager."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    elif get_return is not None:
        client.get = AsyncMock(return_value=get_return)
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    elif post_return is not None:
        client.post = AsyncMock(return_value=post_return)
    return client


def _ok_resp(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


# ══════════════════════════════════════════════════════════════════════════════
# _due()
# ══════════════════════════════════════════════════════════════════════════════

class TestDue:
    def test_none_ts_is_always_due(self):
        _reset_timer()
        assert du._due() is True

    def test_just_ran_is_not_due(self):
        _set_timer_just_ran()
        assert du._due() is False

    def test_overdue_returns_true(self):
        _set_timer_overdue()
        assert du._due() is True

    def test_exactly_on_boundary_is_due(self):
        du._last_run_ts = time.monotonic() - (du.REFRESH_INTERVAL_MIN * 60)
        # >= means it IS due at the exact boundary
        assert du._due() is True


# ══════════════════════════════════════════════════════════════════════════════
# _get_current_auto_subscriptions()
# ══════════════════════════════════════════════════════════════════════════════

class TestGetCurrentAutoSubscriptions:
    def test_returns_set_from_response(self):
        resp = _ok_resp({"subscriptions": ["INFY", "TCS", "RELIANCE"]})
        with patch("httpx.AsyncClient", return_value=_fake_client(get_return=resp)):
            result = run(du._get_current_auto_subscriptions())
        assert result == {"INFY", "TCS", "RELIANCE"}

    def test_empty_subscriptions_key(self):
        resp = _ok_resp({"subscriptions": []})
        with patch("httpx.AsyncClient", return_value=_fake_client(get_return=resp)):
            result = run(du._get_current_auto_subscriptions())
        assert result == set()

    def test_missing_subscriptions_key(self):
        resp = _ok_resp({})
        with patch("httpx.AsyncClient", return_value=_fake_client(get_return=resp)):
            result = run(du._get_current_auto_subscriptions())
        assert result == set()

    def test_raises_on_http_error(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=Exception("403 Forbidden"))
        with patch("httpx.AsyncClient", return_value=_fake_client(get_return=resp)):
            with pytest.raises(Exception, match="403"):
                run(du._get_current_auto_subscriptions())


# ══════════════════════════════════════════════════════════════════════════════
# _compute_desired_universe()
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeDesiredUniverse:
    def test_combines_vol_shock_and_momentum(self):
        movers_resp = _ok_resp({"symbols": ["HDFC", "ICICI"]})
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["INFY", "TCS"])), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=movers_resp)):
            result = run(du._compute_desired_universe())
        assert set(result) >= {"INFY", "TCS", "HDFC", "ICICI"}

    def test_deduplicates_preserving_order(self):
        movers_resp = _ok_resp({"symbols": ["TCS", "WIPRO"]})
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["TCS", "INFY"])), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=movers_resp)):
            result = run(du._compute_desired_universe())
        # TCS appears in both — must appear only once
        assert result.count("TCS") == 1

    def test_uppercases_all_symbols(self):
        movers_resp = _ok_resp({"symbols": ["hdfc"]})
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["reliance"])), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=movers_resp)):
            result = run(du._compute_desired_universe())
        assert "RELIANCE" in result
        assert "HDFC" in result

    def test_vol_shock_failure_still_returns_movers(self):
        movers_resp = _ok_resp({"symbols": ["AXIS"]})
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(side_effect=RuntimeError("yfinance down"))), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=movers_resp)):
            result = run(du._compute_desired_universe())
        assert "AXIS" in result

    def test_movers_failure_still_returns_vol_shock(self):
        err_resp = MagicMock()
        err_resp.raise_for_status = MagicMock(side_effect=Exception("gateway timeout"))
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["SBIN"])), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=err_resp)):
            result = run(du._compute_desired_universe())
        assert "SBIN" in result

    def test_both_sources_fail_returns_empty(self):
        err_resp = MagicMock()
        err_resp.raise_for_status = MagicMock(side_effect=Exception("error"))
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(side_effect=RuntimeError("fail"))), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=err_resp)):
            result = run(du._compute_desired_universe())
        assert result == []

    def test_momentum_movers_missing_symbols_key(self):
        movers_resp = _ok_resp({})  # no "symbols" key
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["COAL"])), \
             patch("httpx.AsyncClient", return_value=_fake_client(get_return=movers_resp)):
            result = run(du._compute_desired_universe())
        assert "COAL" in result


# ══════════════════════════════════════════════════════════════════════════════
# refresh_dynamic_universe() — main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshDynamicUniverseGates:
    """Market-hours gate and throttle gate."""

    def test_market_closed_returns_none(self):
        _reset_timer()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=False):
            result = run(du.refresh_dynamic_universe())
        assert result is None

    def test_not_due_returns_none(self):
        _set_timer_just_ran()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True):
            result = run(du.refresh_dynamic_universe())
        assert result is None

    def test_market_open_and_due_runs(self):
        _reset_timer()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe",
                   new=AsyncMock(return_value=[])):
            result = run(du.refresh_dynamic_universe())
        # Empty desired set → logs and returns None
        assert result is None

    def test_timer_is_set_after_run(self):
        _reset_timer()
        assert du._last_run_ts is None
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe",
                   new=AsyncMock(return_value=[])):
            run(du.refresh_dynamic_universe())
        assert du._last_run_ts is not None

    def test_compute_exception_returns_none(self):
        _reset_timer()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe",
                   new=AsyncMock(side_effect=RuntimeError("scan failed"))):
            result = run(du.refresh_dynamic_universe())
        assert result is None

    def test_get_subscriptions_exception_returns_none(self):
        _reset_timer()
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe",
                   new=AsyncMock(return_value=["INFY"])), \
             patch("watchlist_engine.dynamic_universe._get_current_auto_subscriptions",
                   new=AsyncMock(side_effect=RuntimeError("event service down"))):
            result = run(du.refresh_dynamic_universe())
        assert result is None


class TestRefreshDynamicUniverseSync:
    """Subscribe / unsubscribe / /check calls."""

    def _run_with_desired_and_current(self, desired, current,
                                       subscribe_ok=True, unsubscribe_ok=True,
                                       check_ok=True):
        """Helper: run refresh_dynamic_universe with controlled desired/current sets."""
        _reset_timer()

        sub_resp = MagicMock()
        sub_resp.raise_for_status = (MagicMock() if subscribe_ok
                                      else MagicMock(side_effect=Exception("subscribe failed")))
        unsub_resp = MagicMock()
        unsub_resp.raise_for_status = (MagicMock() if unsubscribe_ok
                                        else MagicMock(side_effect=Exception("unsub failed")))
        check_resp = MagicMock()
        check_resp.raise_for_status = (MagicMock() if check_ok
                                        else MagicMock(side_effect=Exception("check failed")))

        # /check is always a GET; subscribe/unsubscribe are POST
        call_state = {"posts": 0}

        async def fake_post(url, **kwargs):
            call_state["posts"] += 1
            if "unsubscribe" in url:
                return unsub_resp
            return sub_resp

        async def fake_get(url, **kwargs):
            return check_resp

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = fake_post
        client.get = fake_get

        with patch("watchlist_engine.dynamic_universe.is_market_open_ist",
                   return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe",
                   new=AsyncMock(return_value=desired)), \
             patch("watchlist_engine.dynamic_universe._get_current_auto_subscriptions",
                   new=AsyncMock(return_value=set(current))), \
             patch("httpx.AsyncClient", return_value=client):
            result = run(du.refresh_dynamic_universe())

        return result

    def test_new_symbols_are_subscribed(self):
        result = self._run_with_desired_and_current(
            desired=["INFY", "TCS"],
            current=[],
        )
        assert sorted(result["added"]) == ["INFY", "TCS"]
        assert result["removed"] == []
        assert result["kept"] == 0

    def test_stale_symbols_are_unsubscribed(self):
        result = self._run_with_desired_and_current(
            desired=[],
            current=["OLD1", "OLD2"],
        )
        assert result["added"] == []
        assert sorted(result["removed"]) == ["OLD1", "OLD2"]

    def test_overlap_counted_as_kept(self):
        result = self._run_with_desired_and_current(
            desired=["INFY", "HDFC"],
            current=["HDFC", "OLD"],
        )
        assert result["kept"] == 1
        assert "INFY" in result["added"]
        assert "OLD" in result["removed"]

    def test_no_diff_returns_result_with_empty_lists(self):
        result = self._run_with_desired_and_current(
            desired=["INFY"],
            current=["INFY"],
        )
        assert result["added"] == []
        assert result["removed"] == []
        assert result["kept"] == 1

    def test_max_auto_symbols_cap_applied(self):
        # Desired list longer than MAX_AUTO_SYMBOLS (60) — only first 60 considered
        desired = [f"SYM{i:03d}" for i in range(80)]
        result = self._run_with_desired_and_current(
            desired=desired,
            current=[],
        )
        # 80 desired but capped at 60 — added should be ≤ 60
        assert len(result["added"]) <= du.MAX_AUTO_SYMBOLS

    def test_subscribe_failure_logs_but_does_not_raise(self):
        result = self._run_with_desired_and_current(
            desired=["NEWCO"],
            current=[],
            subscribe_ok=False,
        )
        # subscribe failed → added stays empty, but result is still a dict
        assert isinstance(result, dict)
        assert result["added"] == []

    def test_unsubscribe_failure_logs_but_does_not_raise(self):
        result = self._run_with_desired_and_current(
            desired=[],
            current=["GONE"],
            unsubscribe_ok=False,
        )
        assert isinstance(result, dict)
        assert result["removed"] == []

    def test_check_failure_logs_but_does_not_raise(self):
        result = self._run_with_desired_and_current(
            desired=["INFY"],
            current=[],
            check_ok=False,
        )
        # /check failed but result is still returned
        assert "added" in result
        assert "INFY" in result["added"]

    def test_empty_desired_after_cap_does_not_subscribe(self):
        result = self._run_with_desired_and_current(
            desired=[],
            current=[],
        )
        # No desired, no current → None (empty desired returns early)
        assert result is None

    def test_result_has_all_expected_keys(self):
        result = self._run_with_desired_and_current(
            desired=["A", "B"],
            current=["B", "C"],
        )
        assert "added" in result
        assert "removed" in result
        assert "kept" in result
