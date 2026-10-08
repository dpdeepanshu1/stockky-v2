"""group253: the dynamic universe's /check warm-up runs on its own thread, one at a time, and no longer holds the cycle."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND", raising=False)
    monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S", raising=False)
    du._CHECK_RUNNING[0] = False
    du._CHECK_LAST.update({"started": 0, "finished": 0, "ok": None, "elapsed_s": None, "skipped_busy": 0})
    du._last_run_ts = None
    yield
    # let any worker a test left behind finish before the next test
    deadline = time.time() + 3
    while du._CHECK_RUNNING[0] and time.time() < deadline:
        time.sleep(0.01)


def _wait_done(n=1, timeout=3.0):
    deadline = time.time() + timeout
    while du.check_status()["finished"] < n and time.time() < deadline:
        time.sleep(0.01)
    assert du.check_status()["finished"] >= n


class _SyncClient:
    """Stand-in for httpx.Client: records the timeout, blocks on `gate` if given, raises `exc` if given."""
    seen_timeouts: list = []
    gate: threading.Event | None = None
    exc: BaseException | None = None
    urls: list = []

    def __init__(self, timeout=None, **kw):
        _SyncClient.seen_timeouts.append(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        _SyncClient.urls.append(url)
        if _SyncClient.gate is not None:
            _SyncClient.gate.wait(5)
        if _SyncClient.exc is not None:
            raise _SyncClient.exc
        r = MagicMock()
        r.raise_for_status = MagicMock()
        return r


@pytest.fixture(autouse=True)
def _reset_client():
    _SyncClient.seen_timeouts = []
    _SyncClient.gate = None
    _SyncClient.exc = None
    _SyncClient.urls = []
    yield


def _refresh(desired=("INFY",), current=()):
    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=post_resp)
    client.get = AsyncMock(side_effect=AssertionError("background mode must not await /check on the async client"))
    with patch("watchlist_engine.dynamic_universe.is_market_open_ist", return_value=True), \
         patch("watchlist_engine.dynamic_universe._compute_desired_universe", new=AsyncMock(return_value=list(desired))), \
         patch("watchlist_engine.dynamic_universe._get_current_auto_subscriptions", new=AsyncMock(return_value=set(current))), \
         patch("watchlist_engine.dynamic_universe.httpx.AsyncClient", return_value=client), \
         patch("watchlist_engine.dynamic_universe.httpx.Client", _SyncClient):
        du._last_run_ts = None
        return asyncio.run(du.refresh_dynamic_universe())


class TestEnvHelpers:
    @pytest.mark.parametrize("raw,exp", [(None, True), ("", True), ("1", True), ("0", False), ("false", False),
                                         ("OFF", False), (" no ", False)])
    def test_background_flag(self, monkeypatch, raw, exp):
        if raw is None:
            monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND", raising=False)
        else:
            monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND", raw)
        assert du._check_background_enabled() is exp

    @pytest.mark.parametrize("raw,exp", [(None, 600.0), ("", 600.0), ("abc", 600.0), ("0", 600.0), ("-5", 600.0),
                                         ("nan", 600.0), ("120", 120.0)])
    def test_timeout(self, monkeypatch, raw, exp):
        if raw is None:
            monkeypatch.delenv("DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S", raising=False)
        else:
            monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S", raw)
        assert du._check_background_timeout_s() == exp


class TestBackgroundCheck:
    def test_refresh_returns_without_waiting_for_a_slow_check(self):
        gate = threading.Event()
        _SyncClient.gate = gate
        t0 = time.monotonic()
        result = _refresh()
        assert time.monotonic() - t0 < 2.0
        assert result["added"] == ["INFY"]
        assert du.check_status()["running"] is True
        gate.set()
        _wait_done()
        st = du.check_status()
        assert st["ok"] is True and st["running"] is False

    def test_check_hits_the_event_check_url_with_the_background_timeout(self, monkeypatch):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S", "300")
        _refresh()
        _wait_done()
        assert _SyncClient.urls and _SyncClient.urls[0].endswith("/check")
        assert _SyncClient.seen_timeouts == [300.0]

    def test_second_sync_while_one_runs_does_not_start_another(self, caplog):
        gate = threading.Event()
        _SyncClient.gate = gate
        _refresh()
        with caplog.at_level("INFO"):
            _refresh()
        st = du.check_status()
        assert st["started"] == 1 and st["skipped_busy"] == 1
        assert any("still running" in r.getMessage() for r in caplog.records)
        gate.set()
        _wait_done()
        assert len(_SyncClient.urls) == 1

    def test_a_new_check_can_start_after_the_last_one_finished(self):
        _refresh()
        _wait_done(1)
        _refresh()
        _wait_done(2)
        assert du.check_status()["started"] == 2

    def test_failure_is_logged_with_type_and_elapsed_and_clears_the_flag(self, caplog):
        _SyncClient.exc = httpx.ReadTimeout("")
        with caplog.at_level("WARNING"):
            _refresh()
            _wait_done()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("/check trigger failed (ReadTimeout) after " in m for m in msgs)
        st = du.check_status()
        assert st["ok"] is False and st["running"] is False

    def test_success_is_logged_with_elapsed(self, caplog):
        with caplog.at_level("INFO"):
            _refresh()
            _wait_done()
        assert any("/check finished in " in r.getMessage() for r in caplog.records)

    def test_failed_check_does_not_block_the_next_one(self):
        _SyncClient.exc = RuntimeError("boom")
        _refresh()
        _wait_done(1)
        _SyncClient.exc = None
        _refresh()
        _wait_done(2)
        assert du.check_status()["ok"] is True

    def test_thread_that_cannot_start_clears_the_flag(self, caplog):
        with patch("watchlist_engine.dynamic_universe.threading.Thread", side_effect=RuntimeError("no threads")):
            with caplog.at_level("WARNING"):
                assert du._start_check_background(10.0) is False
        assert du.check_status()["running"] is False
        assert any("could not start the /check thread" in r.getMessage() for r in caplog.records)

    def test_refresh_still_returns_its_result_when_the_thread_cannot_start(self):
        with patch("watchlist_engine.dynamic_universe.threading.Thread", side_effect=RuntimeError("no threads")):
            result = _refresh(desired=("TCS",))
        assert result["added"] == ["TCS"]


class TestInlineModeUnchanged:
    def test_flag_off_waits_on_the_async_client_like_before(self, monkeypatch):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND", "0")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(return_value=resp)
        client.get = AsyncMock(return_value=resp)
        with patch("watchlist_engine.dynamic_universe.is_market_open_ist", return_value=True), \
             patch("watchlist_engine.dynamic_universe._compute_desired_universe", new=AsyncMock(return_value=["INFY"])), \
             patch("watchlist_engine.dynamic_universe._get_current_auto_subscriptions", new=AsyncMock(return_value=set())), \
             patch("watchlist_engine.dynamic_universe.httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.dynamic_universe.httpx.Client", _SyncClient):
            du._last_run_ts = None
            asyncio.run(du.refresh_dynamic_universe())
        assert client.get.await_count == 1
        assert _SyncClient.urls == []
        assert du.check_status()["started"] == 0
