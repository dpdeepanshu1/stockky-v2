"""
tests/test_group301_md_poller.py - group301: scalper tick feed read from market-data /quotes/bulk.

Covers config (POSITION_FEED_SOURCE + poll knobs), ws_client._ingest_tick (the shared tick sink), feed/md_poller.py
(batching, row ingest, dedupe, staleness, poll loop, error back-off, off-hours idle) and the start()/ws_status() switch.

Run from services/position-stocks-service:  python3 -m pytest tests/test_group301_md_poller.py -q
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import feed.ws_client as wsc
from feed import md_poller as mp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    wsc._tick_buffers.clear()
    wsc._last_volume.clear()
    wsc._last_quote.clear()
    wsc._day_stats.clear()
    wsc._token_to_symbol.clear()
    wsc._on_tick_callbacks.clear()
    wsc._running = False
    wsc._connected = False
    wsc._reconnect_attempts = 0
    wsc._last_tick_at = None
    wsc._ws_task = None
    mp.reset()
    monkeypatch.setattr(config, "POSITION_FEED_SOURCE", "angelone_ws", raising=False)
    monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 500, raising=False)
    monkeypatch.setattr(config, "POSITION_BULK_POLL_S", 1.0, raising=False)
    monkeypatch.setattr(config, "POSITION_BULK_MAX_AGE_S", 30.0, raising=False)
    monkeypatch.setattr(config, "POSITION_BULK_TIMEOUT_S", 8.0, raising=False)
    yield
    wsc._tick_buffers.clear()
    wsc._on_tick_callbacks.clear()
    mp.reset()


def _iso(epoch: float) -> str:
    """naive UTC ISO string, the way market-data writes fetched_at"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()


def _row(sym="SBIN", price=100.0, at=None, **kw):
    r = {"symbol": sym, "price": price, "fetched_at": _iso(at if at is not None else time.time())}
    r.update(kw)
    return r


# ── config ────────────────────────────────────────────────────────────────────

def _reload_config(monkeypatch, **env):
    for k in ("POSITION_FEED_SOURCE", "POSITION_BULK_POLL_S", "POSITION_BULK_CHUNK",
              "POSITION_BULK_TIMEOUT_S", "POSITION_BULK_MAX_AGE_S"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


class TestConfig:
    def test_defaults(self, monkeypatch):
        c = _reload_config(monkeypatch)
        assert c.POSITION_FEED_SOURCE == "angelone_ws"
        assert c.POSITION_BULK_POLL_S == 1.0
        assert c.POSITION_BULK_CHUNK == 500
        assert c.POSITION_BULK_TIMEOUT_S == 8.0
        assert c.POSITION_BULK_MAX_AGE_S == 30.0

    def test_market_data_is_accepted_case_and_space_insensitive(self, monkeypatch):
        assert _reload_config(monkeypatch, POSITION_FEED_SOURCE="  Market_Data ").POSITION_FEED_SOURCE == "market_data"

    @pytest.mark.parametrize("raw", ["", "  ", "dhan", "ws", "1"])
    def test_blank_or_unknown_falls_back_to_the_websocket(self, monkeypatch, raw):
        assert _reload_config(monkeypatch, POSITION_FEED_SOURCE=raw).POSITION_FEED_SOURCE == "angelone_ws"

    def test_knobs_are_clamped(self, monkeypatch):
        c = _reload_config(monkeypatch, POSITION_BULK_POLL_S="0.01", POSITION_BULK_CHUNK="5000",
                           POSITION_BULK_TIMEOUT_S="0", POSITION_BULK_MAX_AGE_S="-4")
        assert c.POSITION_BULK_POLL_S == 0.2
        assert c.POSITION_BULK_CHUNK == 1000
        assert c.POSITION_BULK_TIMEOUT_S == 1.0
        assert c.POSITION_BULK_MAX_AGE_S == 1.0
        assert _reload_config(monkeypatch, POSITION_BULK_CHUNK="0").POSITION_BULK_CHUNK == 1

    def test_garbage_numbers_fall_back_to_defaults(self, monkeypatch):
        c = _reload_config(monkeypatch, POSITION_BULK_POLL_S="x", POSITION_BULK_CHUNK="y")
        assert (c.POSITION_BULK_POLL_S, c.POSITION_BULK_CHUNK) == (1.0, 500)


# ── ws_client._ingest_tick (shared sink) ──────────────────────────────────────

class TestIngestTick:
    def test_fills_buffer_volume_quote_stats_and_calls_callbacks(self):
        seen = []
        wsc.register_on_tick(lambda *a: seen.append(a))
        t = time.time()
        wsc._ingest_tick("SBIN", 100.0, t, 5000, 99.9, 100.1, (None, 101.0, 99.0, 98.0))
        assert wsc.get_tick_buffer("SBIN") == [(t, 100.0)]
        assert wsc.get_last_volume("SBIN") == 5000
        assert wsc.get_best_bid_ask("SBIN") == (99.9, 100.1)
        assert wsc.get_day_stats("SBIN") == (None, 101.0, 99.0, 98.0)
        assert seen == [("SBIN", 100.0, 5000, t)]

    def test_zero_volume_keeps_previous_and_no_depth_leaves_quote_unset(self):
        t = time.time()
        wsc._ingest_tick("SBIN", 100.0, t, 5000, None, None)
        wsc._ingest_tick("SBIN", 100.5, t + 1, 0, None, None, None)
        assert wsc.get_last_volume("SBIN") == 5000
        assert wsc.get_best_bid_ask("SBIN") is None
        assert wsc.get_day_stats("SBIN") is None

    def test_prunes_ticks_older_than_the_buffer_age(self):
        t = time.time()
        wsc._ingest_tick("SBIN", 100.0, t - wsc._MAX_BUFFER_AGE_S - 10, 0, None, None)
        wsc._ingest_tick("SBIN", 101.0, t, 0, None, None)
        assert wsc.get_tick_buffer("SBIN") == [(t, 101.0)]

    def test_a_raising_callback_does_not_stop_the_others(self):
        seen = []

        def bad(*a):
            raise RuntimeError("boom")
        wsc.register_on_tick(bad)
        wsc.register_on_tick(lambda *a: seen.append(a))
        wsc._ingest_tick("SBIN", 100.0, time.time(), 1, None, None)
        assert len(seen) == 1

    def test_implausible_day_stats_are_rejected_not_stored(self):
        wsc._ingest_tick("SBIN", 100.0, time.time(), 0, None, None, (None, 900.0, 800.0, 700.0))
        assert wsc.get_day_stats("SBIN") is None
        assert wsc.get_last_ltp("SBIN") == 100.0


# ── next_batch ────────────────────────────────────────────────────────────────

class TestNextBatch:
    def test_hot_first_then_universe_round_robin_wraps(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 3)
        mp._universe = ["A", "B", "C", "D", "E"]
        assert mp.next_batch(["X"]) == ["X", "A", "B"]
        assert mp.next_batch(["X"]) == ["X", "C", "D"]
        assert mp.next_batch(["X"]) == ["X", "E", "A"]      # wrapped

    def test_hot_name_in_the_universe_is_not_sent_twice(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 3)
        mp._universe = ["A", "B", "C", "D"]
        assert mp.next_batch(["B"]) == ["B", "A", "C"]

    def test_hot_list_longer_than_chunk_is_truncated_and_universe_not_touched(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 2)
        mp._universe = ["A", "B"]
        assert mp.next_batch(["X", "Y", "Z"]) == ["X", "Y"]
        assert mp._cursor == 0

    def test_small_universe_never_loops_forever(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 500)
        mp._universe = ["A", "B"]
        assert sorted(mp.next_batch(["A"])) == ["A", "B"]

    def test_empty_universe_sends_only_hot_names(self):
        assert mp.next_batch(["X"]) == ["X"]
        assert mp.next_batch([]) == []


# ── hot symbols ───────────────────────────────────────────────────────────────

class TestHotSymbols:
    def test_cleaned_deduped_and_blank_dropped(self):
        mp.set_hot_symbols_provider(lambda: ["sbin.ns", "SBIN", " ", None, "tcs"])
        assert mp._hot_symbols() == ["SBIN", "TCS"]

    def test_provider_failure_gives_empty_list(self):
        def boom():
            raise RuntimeError("db down")
        mp.set_hot_symbols_provider(boom)
        assert mp._hot_symbols() == []


# ── ingest_rows ───────────────────────────────────────────────────────────────

class TestIngestRows:
    def test_stores_a_fresh_row_with_volume_and_day_stats(self):
        now = time.time()
        n = mp.ingest_rows([_row("SBIN", 100.0, now - 1, volume=12345, day_high=101, day_low=99, previous_close=98)], now)
        assert n == 1
        assert wsc.get_last_ltp("SBIN") == 100.0
        assert wsc.get_last_volume("SBIN") == 12345
        assert wsc.get_day_stats("SBIN") == (None, 101.0, 99.0, 98.0)
        assert wsc.get_best_bid_ask("SBIN") is None
        assert wsc._last_tick_at == pytest.approx(now - 1, abs=1e-3)

    def test_unchanged_fetched_at_is_not_stored_twice_but_a_newer_one_is(self):
        now = time.time()
        assert mp.ingest_rows([_row("SBIN", 100.0, now - 5)], now) == 1
        assert mp.ingest_rows([_row("SBIN", 100.0, now - 5)], now) == 0
        assert mp._stats["unchanged_skipped"] == 1
        assert mp.ingest_rows([_row("SBIN", 100.2, now - 2)], now) == 1
        assert len(wsc.get_tick_buffer("SBIN")) == 2

    def test_rows_older_than_the_limit_are_dropped(self):
        now = time.time()
        assert mp.ingest_rows([_row("SBIN", 100.0, now - 31)], now) == 0
        assert mp._stats["stale_skipped"] == 1
        assert wsc.get_last_ltp("SBIN") is None
        assert mp.ingest_rows([_row("SBIN", 100.0, now - 29)], now) == 1

    def test_future_timestamp_is_clamped_to_now(self):
        now = time.time()
        assert mp.ingest_rows([_row("SBIN", 100.0, now + 3600)], now) == 1
        assert wsc.get_tick_buffer("SBIN")[0][0] == pytest.approx(now)

    def test_missing_or_bad_fetched_at_uses_now(self):
        now = time.time()
        assert mp.ingest_rows([{"symbol": "A", "price": 5}, {"symbol": "B", "price": 5, "fetched_at": "garbage"}], now) == 2

    def test_z_suffix_and_aware_timestamps_parse(self):
        now = time.time()
        iso = datetime.fromtimestamp(now - 2, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        assert mp.ingest_rows([{"symbol": "A", "price": 5, "fetched_at": iso}], now) == 1
        assert wsc.get_tick_buffer("A")[0][0] == pytest.approx(now - 2, abs=1e-3)

    @pytest.mark.parametrize("bad", [
        {"symbol": "A", "price": None}, {"symbol": "A", "price": 0}, {"symbol": "A", "price": -3},
        {"symbol": "A", "price": "abc"}, {"symbol": "A", "price": float("nan")}, {"price": 5},
        {"symbol": "", "price": 5}, "not a dict", None, 7,
    ])
    def test_unusable_rows_are_skipped(self, bad):
        assert mp.ingest_rows([bad], time.time()) == 0

    def test_symbol_suffix_and_case_are_normalised(self):
        now = time.time()
        mp.ingest_rows([_row("sbin.ns", 100.0, now - 1)], now)
        assert wsc.get_last_ltp("SBIN") == 100.0

    def test_bad_volume_becomes_zero_and_keeps_the_last_real_one(self):
        now = time.time()
        mp.ingest_rows([_row("A", 5.0, now - 3, volume=900)], now)
        mp.ingest_rows([_row("A", 5.1, now - 2, volume="n/a")], now)
        mp.ingest_rows([_row("A", 5.2, now - 1, volume=-4)], now)
        assert wsc.get_last_volume("A") == 900

    def test_no_day_fields_means_no_day_stats(self):
        now = time.time()
        mp.ingest_rows([_row("A", 5.0, now - 1)], now)
        assert wsc.get_day_stats("A") is None

    def test_on_tick_callbacks_fire_for_polled_rows(self):
        seen = []
        wsc.register_on_tick(lambda *a: seen.append(a))
        now = time.time()
        mp.ingest_rows([_row("A", 5.0, now - 1, volume=70)], now)
        assert seen and seen[0][:3] == ("A", 5.0, 70)

    def test_none_rows_argument_is_safe(self):
        assert mp.ingest_rows(None) == 0


# ── fetch / poll_once ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class _Client:
    def __init__(self, payload=None, status=200, exc=None):
        self.payload, self.status, self.exc, self.calls = payload, status, exc, []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return _Resp(self.payload, self.status)


class TestFetchAndPoll:
    def test_fetch_posts_symbols_to_quotes_bulk(self, monkeypatch):
        monkeypatch.setattr(config, "MARKET_DATA_URL", "http://md:8001", raising=False)
        c = _Client({"quotes": [{"symbol": "A"}]})
        assert asyncio.run(mp._fetch(c, ["A", "B"])) == [{"symbol": "A"}]
        assert c.calls == [("http://md:8001/quotes/bulk", {"symbols": ["A", "B"]})]

    @pytest.mark.parametrize("payload", [None, [], {"quotes": None}, {"quotes": "x"}, {}])
    def test_fetch_odd_payload_gives_empty_list(self, payload):
        assert asyncio.run(mp._fetch(_Client(payload), ["A"])) == []

    def test_fetch_http_error_raises(self):
        with pytest.raises(RuntimeError):
            asyncio.run(mp._fetch(_Client({}, status=503), ["A"]))

    def test_poll_once_sends_hot_first_and_stores_ticks(self, monkeypatch):
        mp._universe = ["A", "B", "C"]
        mp.set_hot_symbols_provider(lambda: ["HELD"])
        monkeypatch.setattr(config, "POSITION_BULK_CHUNK", 3)
        now = time.time()
        c = _Client({"quotes": [_row("HELD", 50.0, now - 1), _row("A", 10.0, now - 1)]})
        assert asyncio.run(mp.poll_once(c)) == 2
        assert c.calls[0][1]["symbols"] == ["HELD", "A", "B"]
        assert mp._stats["last_batch"] == 3 and mp._stats["last_rows"] == 2
        assert wsc.get_last_ltp("HELD") == 50.0

    def test_poll_once_with_nothing_to_poll_makes_no_request(self):
        mp.set_hot_symbols_provider(lambda: [])
        c = _Client({"quotes": []})
        assert asyncio.run(mp.poll_once(c)) == 0
        assert c.calls == []


class TestUniverse:
    def test_load_universe_drops_etfs_and_cleans_names(self, monkeypatch):
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {"SBIN": "1", "NIFTYBEES": "2", "tcs": "3"})
        monkeypatch.setattr(mp, "drop_etfs", lambda m: ({k: v for k, v in m.items() if k != "NIFTYBEES"}, 1))
        assert asyncio.run(mp._load_universe()) is True
        assert mp._universe == ["SBIN", "TCS"]
        assert mp._universe_loaded_at > 0

    def test_empty_scrip_master_is_false_and_leaves_universe_alone(self, monkeypatch):
        mp._universe = ["KEEP"]
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {})
        assert asyncio.run(mp._load_universe()) is False
        assert mp._universe == ["KEEP"]


# ── poll loop ─────────────────────────────────────────────────────────────────

class _ClientCtx:
    """stands in for httpx.AsyncClient(...) as an async context manager"""
    inst = None

    def __init__(self, *a, **k):
        type(self).inst = self
        self.timeout = k.get("timeout")
        self.client = _Client({"quotes": []})

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


def _drive_loop(monkeypatch, passes=1, idle=False, setup=None):
    """Run mp._poll_loop for `passes` sleeps, then stop it. Returns the list of sleep durations."""
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= passes:
            wsc._running = False

    monkeypatch.setattr(mp.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mp.httpx, "AsyncClient", _ClientCtx)
    monkeypatch.setattr(wsc, "_offhours_idle", lambda: idle)
    mp.set_hot_symbols_provider(lambda: [])
    if setup:
        setup()
    wsc._running = True
    asyncio.run(mp._poll_loop())
    return sleeps


class TestPollLoop:
    def test_successful_pass_marks_connected_and_paces_to_the_interval(self, monkeypatch):
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {"SBIN": "1"})
        monkeypatch.setattr(mp, "drop_etfs", lambda m: (m, 0))
        wsc._reconnect_attempts = 4
        sleeps = _drive_loop(monkeypatch)
        assert wsc._connected is True and wsc._reconnect_attempts == 0
        assert mp._stats["polls"] == 1 and mp._stats["errors"] == 0
        assert mp._stats["last_ok_at"] is not None
        assert 0.0 <= sleeps[0] <= 1.0
        assert _ClientCtx.inst.timeout == config.POSITION_BULK_TIMEOUT_S
        assert _ClientCtx.inst.client.calls[0][1] == {"symbols": ["SBIN"]}

    def test_empty_scrip_master_counts_as_a_failure_and_backs_off(self, monkeypatch):
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {})
        sleeps = _drive_loop(monkeypatch)
        assert mp._stats["errors"] == 1 and "scrip master" in mp._stats["last_error"]
        assert wsc._reconnect_attempts == 1
        assert sleeps == [2.0]                                  # interval * 2**1
        assert mp._stats["polls"] == 1                           # finally ran despite `continue`

    def test_three_failures_in_a_row_mark_the_feed_down_and_backoff_is_capped(self, monkeypatch):
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {})
        wsc._connected = True
        sleeps = _drive_loop(monkeypatch, passes=7)
        assert wsc._connected is False
        assert mp._stats["consecutive_errors"] == 7
        assert sleeps[:3] == [2.0, 4.0, 8.0] and max(sleeps) == mp._ERROR_BACKOFF_MAX_S

    def test_request_failure_then_recovery_resets_the_counters(self, monkeypatch):
        monkeypatch.setattr(mp, "get_all_nse_eq", lambda: {"SBIN": "1"})
        monkeypatch.setattr(mp, "drop_etfs", lambda m: (m, 0))
        state = {"n": 0}
        orig = _ClientCtx.__init__

        def init(self, *a, **k):
            orig(self, *a, **k)
            real_post = self.client.post

            async def post(url, json=None):
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("market-data down")
                return await real_post(url, json=json)
            self.client.post = post
        monkeypatch.setattr(_ClientCtx, "__init__", init)
        _drive_loop(monkeypatch, passes=2)
        assert mp._stats["errors"] == 1 and mp._stats["consecutive_errors"] == 0
        assert wsc._connected is True and mp._stats["polls"] == 2

    def test_off_hours_idles_without_polling(self, monkeypatch):
        wsc._connected = True
        sleeps = _drive_loop(monkeypatch, idle=True)
        assert wsc._connected is False
        assert sleeps == [wsc._OFFHOURS_RECHECK_S]
        assert mp._stats["polls"] == 0

    def test_cancellation_propagates(self, monkeypatch):
        async def boom(client):
            raise asyncio.CancelledError()
        monkeypatch.setattr(mp, "poll_once", boom)
        monkeypatch.setattr(mp, "_universe", ["A"])
        monkeypatch.setattr(mp, "_universe_loaded_at", time.time())
        monkeypatch.setattr(mp.httpx, "AsyncClient", _ClientCtx)
        monkeypatch.setattr(wsc, "_offhours_idle", lambda: False)
        wsc._running = True

        async def run():
            await mp._poll_loop()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())


# ── start() routing and ws_status() ──────────────────────────────────────────

class TestStartAndStatus:
    def test_start_uses_the_poller_when_selected_and_never_the_websocket(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_FEED_SOURCE", "market_data")
        called = {"ws": 0, "poll": 0}

        async def ws_loop():
            called["ws"] += 1

        async def poll_loop():
            called["poll"] += 1
            await asyncio.Event().wait()
        monkeypatch.setattr(wsc, "_ws_loop", ws_loop)
        monkeypatch.setattr(mp, "_poll_loop", poll_loop)

        async def run():
            await wsc.start()
            await asyncio.sleep(0)
            assert wsc._running is True and wsc._ws_task.get_name() == "position-stocks-md-poller"
            await wsc.stop()
        asyncio.run(run())
        assert called == {"ws": 0, "poll": 1}

    def test_start_uses_the_websocket_by_default(self, monkeypatch):
        called = {"ws": 0, "poll": 0}

        async def ws_loop():
            called["ws"] += 1

        async def poll_loop():
            called["poll"] += 1
        monkeypatch.setattr(wsc, "_ws_loop", ws_loop)
        monkeypatch.setattr(mp, "_poll_loop", poll_loop)

        async def run():
            await wsc.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await wsc.stop()
        asyncio.run(run())
        assert called == {"ws": 1, "poll": 0}

    def test_status_for_the_websocket_keeps_the_old_shape(self):
        s = wsc.ws_status()
        assert s["source"] == "angelone_ws" and "md_poller" not in s
        for k in ("running", "connected", "subscribed_symbols", "reconnect_attempts", "last_tick_at"):
            assert k in s

    def test_status_for_the_poller_adds_md_poller_and_uses_its_universe(self, monkeypatch):
        monkeypatch.setattr(config, "POSITION_FEED_SOURCE", "market_data")
        mp._universe = ["A", "B", "C"]
        wsc._connected = True
        wsc._last_tick_at = time.time()
        s = wsc.ws_status()
        assert s["source"] == "market_data"
        assert s["subscribed_symbols"] == 3 and s["connected"] is True
        assert s["last_tick_at"] is not None
        assert s["md_poller"]["universe"] == 3 and s["md_poller"]["chunk"] == 500
        assert s["md_poller"]["poll_interval_s"] == 1.0


class TestOpenPositionSymbols:
    def test_symbols_are_cleaned_and_blank_rows_dropped(self, monkeypatch):
        import db as _db

        class _Q:
            def filter(self, cond):
                self.cond = cond
                return self

            def all(self):
                return [("sbin.ns",), ("TCS",), (None,)]

        q = _Q()

        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def query(self, *cols):
                return q
        monkeypatch.setattr(_db, "get_session_factory", lambda: (lambda: _S()))
        assert mp.open_position_symbols() == ["SBIN", "TCS"]
