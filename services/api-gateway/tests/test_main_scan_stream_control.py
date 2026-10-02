"""tests/test_main_scan_stream_control.py — coverage for api-gateway/main.py, slice 7 (lines 5301-6040)

Pass 65. The streaming market scan, the Buy-Sniper route, the scan stop/cancel controls and the
watchlist-only scan:

* `GET /scan/stream` (`stream_market_scan`) — lite/full mode selection, `force_refresh`, the bulk feed
  preload + process-local warm, chunking + heartbeats, the `__ALL__` cancel flag, per-chunk live prices,
  the lite path, the full path (per-symbol timeout -> instant-score fallback), the chunk-level failure
  path, the price-resolution block and the closing `done` event;
* `POST /scan/find-buys` (`find_actionable_buys`);
* `POST /scan/cancel/{task_id}` (`cancel_scan`) and `POST /scan/stop-all` (`scan_stop_all`);
* `GET /scan/watchlist` (`scan_watchlist`) — empty watchlist, the five-way per-symbol enrichment fan-out,
  the deadline/partial path, top-pick / horizon-board fallbacks, market mood and the notification thread.

Everything downstream is faked: the feed preload (`data_feed.get_all_stock_feeds`), the async price fetch,
`_analyze_one_symbol_ultra`, `_lite_evaluate_from_feed`, `instant_scanner.compute_instant_scores`, the
sync `httpx.Client` the watchlist scan builds itself, the enrichment helpers, the kv cache and the
notification sender. Nothing touches the network or a database. Findings are pinned as current
behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_scan_stream_control.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
import instant_scanner
import price_resolver
from fastapi.testclient import TestClient


# ── shared fakes ─────────────────────────────────────────────────────────────

class KV:
    """Dict-backed stand-in for the gateway's `_redis_get` / `_redis_set`."""

    def __init__(self):
        self.store = {}
        self.sets = []          # (key, value, ttl)
        self.raise_on = None    # predicate(key) -> bool: raise RuntimeError on matching sets

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        if self.raise_on is not None and self.raise_on(key):
            raise RuntimeError("kv down")
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


@pytest.fixture
def client():
    return TestClient(gw.app, raise_server_exceptions=False)


# ═════════════════════════════════════════════════════════════════════════════
# GET /scan/stream
# ═════════════════════════════════════════════════════════════════════════════

class StreamEnv:
    """Scripted world for `stream_market_scan`."""

    def __init__(self):
        self.universe = []
        self.build_calls = 0
        self.prioritized = []
        self.prices = {}
        self.price_calls = []
        self.price_raises = None
        self.feeds = {}
        self.feeds_raises = None
        self.feed_store_raises = False
        self.paused_calls = []
        self.paused_raises = False
        self.redis_sets = []
        self.redis_set_raises = False
        self.ultra = None           # async (sym, kw) -> dict | raises
        self.ultra_calls = []
        self.lite = None            # (sym, fed, px) -> dict | raises
        self.lite_calls = []
        self.quote = lambda sym: None
        self.quote_calls = []
        self.instant = None         # (base, fed, tick) -> dict | raises
        self.instant_calls = []
        self.client = object()


class _RaisingDiscardSet(set):
    def discard(self, item):
        raise RuntimeError("discard broke")


@pytest.fixture
def senv(monkeypatch):
    env = StreamEnv()

    def build():
        env.build_calls += 1
        return list(env.universe)

    def prioritize(u):
        env.prioritized.append(list(u))
        return u

    async def prices(chunk, client):
        env.price_calls.append(list(chunk))
        if env.price_raises:
            raise env.price_raises
        return dict(env.prices)

    def redis_set(key, value, ttl=None):
        if env.redis_set_raises:
            raise RuntimeError("redis_set broke")
        env.redis_sets.append((key, value, ttl))

    def paused(flag):
        env.paused_calls.append(flag)
        if env.paused_raises:
            raise RuntimeError("pause broke")

    def get_feeds(bases):
        if env.feeds_raises:
            raise env.feeds_raises
        return env.feeds

    def feed_store():
        if env.feed_store_raises:
            raise RuntimeError("store broke")
        return SimpleNamespace(put_symbol=lambda *a, **k: None)

    async def ultra(sym, client, sem, **kw):
        env.ultra_calls.append((sym, kw))
        if env.ultra is not None:
            return await env.ultra(sym, kw)
        return {"symbol": str(sym).upper().replace(".NS", ""), "decision": "HOLD", "close": 100.0}

    def lite(sym, fed, px):
        env.lite_calls.append((sym, fed, px))
        if env.lite is not None:
            return env.lite(sym, fed, px)
        return {"symbol": str(sym).upper().replace(".NS", ""), "decision": "HOLD", "close": px or 100.0}

    def quote(sym):
        env.quote_calls.append(sym)
        return env.quote(sym)

    def instant(base, fed, tick):
        env.instant_calls.append((base, fed, tick))
        if env.instant is not None:
            return env.instant(base, fed, tick)
        return {"symbol": base, "decision": "HOLD", "combined_score": 50, "close": 10.0}

    monkeypatch.setattr(gw, "_build_scan_universe", build)
    monkeypatch.setattr(gw, "_prioritize_universe", prioritize)
    monkeypatch.setattr(gw, "_get_http_client", lambda: env.client)
    monkeypatch.setattr(gw, "_fetch_prices_bulk_async", prices)
    monkeypatch.setattr(gw, "_redis_set", redis_set)
    monkeypatch.setattr(gw, "set_activity_paused", paused)
    monkeypatch.setattr(gw, "_feed_store", feed_store)
    monkeypatch.setattr(gw, "_analyze_one_symbol_ultra", ultra)
    monkeypatch.setattr(gw, "_lite_evaluate_from_feed", lite)
    monkeypatch.setattr(gw, "_fetch_price_from_quote", quote)
    monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", False)
    monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: False)
    monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", set())
    monkeypatch.setattr(data_feed, "get_all_stock_feeds", get_feeds)
    monkeypatch.setattr(data_feed, "_LOCAL_SYMBOLS", {})
    monkeypatch.setattr(data_feed, "_LOCAL_INDEX", set())
    monkeypatch.setattr(instant_scanner, "compute_instant_scores", instant)
    monkeypatch.delenv("SCAN_STREAM_SYMBOL_TIMEOUT", raising=False)
    return env


def _stream(**kw):
    async def go():
        resp = await gw.stream_market_scan(**kw)
        lines = []
        async for chunk in resp.body_iterator:
            lines.append(json.loads(chunk))
        return resp, lines
    return asyncio.run(go())


def _events(lines, name=None):
    ev = [ln for ln in lines if ln.get("_meta")]
    return ev if name is None else [e for e in ev if e.get("event") == name]


def _rows(lines):
    return [ln for ln in lines if not ln.get("_meta")]


class _AsyncioProxy:
    """Stands in for `gw.asyncio`: everything real except `gather`."""

    def __init__(self, gather):
        self.gather = gather

    def __getattr__(self, name):
        return getattr(asyncio, name)


def _scripted_gather(results=None, raises=None):
    async def gather(*aws, return_exceptions=False):
        for a in aws:
            a.close()           # never-awaited coroutines would warn
        if raises is not None:
            raise raises
        return list(results)
    return gather


class _FakeTime:
    """Stands in for `gw.time`; `time()` raises on the listed call numbers."""

    def __init__(self, raise_on=()):
        self.n = 0
        self.raise_on = set(raise_on)

    def time(self):
        self.n += 1
        if self.n in self.raise_on:
            raise RuntimeError("clock broke")
        return 1000.0 + self.n

    def __getattr__(self, name):
        return getattr(time, name)


# ── mode selection, headers, universe ───────────────────────────────────────

class TestStreamModeAndUniverse:
    def test_response_is_ndjson_with_no_buffering_headers(self, senv):
        senv.universe = ["AAA"]
        resp, _ = _stream(lite=True)
        assert resp.media_type == "application/x-ndjson"
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"

    def test_default_is_full_mode(self, senv):
        senv.universe = ["AAA"]
        _, lines = _stream()
        assert _events(lines, "feed_bulk_loaded")[0]["lite"] is False
        assert len(senv.ultra_calls) == 1 and senv.lite_calls == []

    def test_lite_default_env_flag_turns_lite_on(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", True)
        senv.universe = ["AAA"]
        _, lines = _stream()
        assert _events(lines, "feed_bulk_loaded")[0]["lite"] is True
        assert len(senv.lite_calls) == 1 and senv.ultra_calls == []

    def test_open_circuit_forces_lite(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: True)
        senv.universe = ["AAA"]
        _, lines = _stream()
        assert _events(lines, "feed_bulk_loaded")[0]["lite"] is True

    def test_explicit_lite_false_beats_the_auto_switches(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_LITE_DEFAULT", True)
        monkeypatch.setattr(gw, "_should_force_lite_scan", lambda: True)
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        assert _events(lines, "feed_bulk_loaded")[0]["lite"] is False

    def test_explicit_lite_true(self, senv):
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert _events(lines, "feed_bulk_loaded")[0]["lite"] is True

    def test_universe_is_prioritised_and_total_reported(self, senv):
        senv.universe = ["AAA", "BBB", "CCC"]
        _, lines = _stream(lite=True)
        assert senv.build_calls == 1
        assert senv.prioritized == [["AAA", "BBB", "CCC"]]
        assert _events(lines, "feed_bulk_loaded")[0]["total"] == 3
        assert _events(lines, "done")[0]["total"] == 3

    def test_force_refresh_clears_universe_key_and_rebuilds(self, senv):
        senv.universe = ["AAA"]
        _stream(lite=True, force_refresh=True)
        assert senv.build_calls == 2
        assert (gw.SCAN_UNIVERSE_KEY, None, 1) in senv.redis_sets

    def test_no_force_refresh_builds_once(self, senv):
        senv.universe = ["AAA"]
        _stream(lite=True)
        assert senv.build_calls == 1
        assert senv.redis_sets == []

    def test_force_refresh_survives_a_failing_kv_clear(self, senv):
        senv.universe = ["AAA"]
        senv.redis_set_raises = True
        _, lines = _stream(lite=True, force_refresh=True)
        assert senv.build_calls == 2
        assert _events(lines, "done")[0]["processed"] == 1


class TestStreamStartupHygiene:
    def test_stale_power_off_state_is_cleared_before_streaming(self, senv, monkeypatch):
        flags = {"__ALL__", "task-1"}
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", flags)
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert "__ALL__" not in flags and "task-1" in flags
        assert senv.paused_calls == [False]
        assert _events(lines, "cancelled") == []
        assert _events(lines, "done")[0]["processed"] == 1

    def test_hygiene_failures_are_swallowed(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", _RaisingDiscardSet())
        senv.paused_raises = True
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert _events(lines, "done")[0]["processed"] == 1

    def test_empty_universe_emits_error_then_done_and_stops(self, senv):
        senv.universe = []
        _, lines = _stream(lite=True)
        assert lines == [
            {"_meta": True, "event": "error", "error": "empty_universe", "total": 0},
            {"_meta": True, "event": "done", "processed": 0, "total": 0, "elapsed": 0},
        ]
        assert senv.price_calls == [] and senv.lite_calls == [] and senv.ultra_calls == []


# ── bulk feed preload ───────────────────────────────────────────────────────

class TestStreamFeedPreload:
    def test_bases_are_normalised_and_hits_reported(self, senv, monkeypatch):
        seen = {}
        monkeypatch.setattr(data_feed, "get_all_stock_feeds",
                            lambda bases: seen.setdefault("bases", list(bases)) and senv.feeds)
        senv.feeds = {"AAA": {"close": 10}, "BBB": {"close": 20}}
        senv.universe = [" aaa.ns ", "BBB.BO", "ccc"]
        _, lines = _stream(lite=True)
        assert seen["bases"] == ["AAA", "BBB", "CCC"]
        ev = _events(lines, "feed_bulk_loaded")[0]
        assert ev["feed_hits"] == 2 and ev["workers"] == gw.MAX_PARALLEL_WORKERS

    def test_process_local_store_is_warmed(self, senv):
        senv.feeds = {"AAA": {"close": 10}, "BAD": "not-a-dict"}
        senv.universe = ["AAA"]
        _stream(lite=True)
        assert data_feed._LOCAL_SYMBOLS[data_feed.DATA_FEED_PREFIX + "AAA"]["close"] == 10
        assert data_feed._LOCAL_SYMBOLS[data_feed.FEED_ALIAS_PREFIX + "AAA"]["close"] == 10
        assert "AAA" in data_feed._LOCAL_INDEX
        # a non-dict row is skipped, never warmed
        assert "BAD" not in data_feed._LOCAL_INDEX
        assert data_feed.DATA_FEED_PREFIX + "BAD" not in data_feed._LOCAL_SYMBOLS

    def test_warm_failure_is_swallowed_and_event_still_emitted(self, senv):
        senv.feeds = {"AAA": {"close": 10}}
        senv.feed_store_raises = True
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert _events(lines, "feed_bulk_loaded")[0]["feed_hits"] == 1
        assert _events(lines, "feed_bulk_error") == []

    def test_none_from_preload_counts_as_zero_hits(self, senv):
        senv.feeds = None
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert _events(lines, "feed_bulk_loaded")[0]["feed_hits"] == 0

    def test_preload_failure_emits_error_event_and_scan_continues(self, senv):
        senv.feeds_raises = RuntimeError("x" * 500)
        senv.universe = ["AAA", "BBB"]
        _, lines = _stream(lite=True)
        err = _events(lines, "feed_bulk_error")[0]
        assert err["total"] == 2 and len(err["error"]) == 200
        assert _events(lines, "feed_bulk_loaded") == []
        assert _events(lines, "done")[0]["processed"] == 2
        # the failed preload leaves an empty feed for each symbol
        assert [c[1] for c in senv.lite_calls] == [{}, {}]


# ── chunking, heartbeats, cancel ────────────────────────────────────────────

class TestStreamChunking:
    def test_lite_uses_chunks_of_25(self, senv):
        senv.universe = [f"S{i}" for i in range(30)]
        _, lines = _stream(lite=True)
        assert [len(c) for c in senv.price_calls] == [25, 5]
        hb = _events(lines, "heartbeat")
        assert [h["chunk"] for h in hb] == [1, 2]
        assert [h["processed"] for h in hb] == [0, 25]
        assert all(h["total"] == 30 and "elapsed" in h for h in hb)

    def test_full_uses_chunks_of_at_least_10(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 8)
        senv.universe = [f"S{i}" for i in range(12)]
        _stream(lite=False)
        assert [len(c) for c in senv.price_calls] == [10, 2]

    def test_full_chunk_size_is_capped_at_15(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_BATCH_SIZE", 40)
        senv.universe = [f"S{i}" for i in range(20)]
        _stream(lite=False)
        assert [len(c) for c in senv.price_calls] == [15, 5]

    def test_heartbeat_clock_failure_is_swallowed(self, senv, monkeypatch):
        # time.time() calls: 1 = start, 2 = the heartbeat (raises), 3.. = progress / done
        monkeypatch.setattr(gw, "time", _FakeTime(raise_on={2}))
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert _events(lines, "heartbeat") == []
        assert len(_rows(lines)) == 1
        assert _events(lines, "done")[0]["processed"] == 1

    def test_cancel_flag_set_mid_stream_stops_before_next_chunk(self, senv, monkeypatch):
        flags = set()
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", flags)
        senv.universe = [f"S{i}" for i in range(30)]

        def lite(sym, fed, px):
            flags.add("__ALL__")         # Power-Off pressed while chunk 1 is being processed
            return {"symbol": sym, "decision": "HOLD", "close": 1.0}

        senv.lite = lite
        _, lines = _stream(lite=True)
        assert len(_rows(lines)) == 25
        cancelled = _events(lines, "cancelled")
        assert cancelled == [{"_meta": True, "event": "cancelled", "processed": 25, "total": 30}]
        done = _events(lines, "done")[0]
        assert done["processed"] == 25 and done["total"] == 30
        assert len(senv.price_calls) == 1          # chunk 2 never fetched
        # the closing event comes after the cancel event
        order = [ln["event"] for ln in _events(lines)]
        assert order.index("cancelled") < order.index("done")

    def test_task_specific_cancel_flag_does_not_stop_the_stream(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", {"some-task"})
        senv.universe = [f"S{i}" for i in range(30)]
        _, lines = _stream(lite=True)
        assert _events(lines, "cancelled") == []
        assert _events(lines, "done")[0]["processed"] == 30


# ── live prices ─────────────────────────────────────────────────────────────

class TestStreamChunkPrices:
    def test_price_fills_feed_row_without_overwriting_existing_values(self, senv):
        senv.feeds = {"AAA": {"close": 11.0}}
        senv.prices = {"AAA": 99.0, "BBB": 50.0}
        senv.universe = ["AAA", "BBB"]
        _stream(lite=True)
        by = {c[0]: c for c in senv.lite_calls}
        assert by["AAA"][1]["close"] == 11.0           # setdefault keeps the feed's own close
        assert by["AAA"][1]["price"] == 99.0           # ...but fills the missing price
        assert by["AAA"][2] == 99.0
        assert by["BBB"][1] == {"close": 50.0, "price": 50.0}
        assert by["BBB"][2] == 50.0

    def test_symbol_without_a_price_keeps_its_feed_row_and_gets_zero_px(self, senv):
        senv.feeds = {"AAA": {"close": 11.0}}
        senv.prices = {}
        senv.universe = ["AAA"]
        _stream(lite=True)
        assert senv.lite_calls == [("AAA", {"close": 11.0}, 0.0)]

    def test_price_fetch_failure_degrades_to_no_prices(self, senv):
        senv.price_raises = RuntimeError("quotes down")
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert senv.lite_calls == [("AAA", {}, 0.0)]
        assert _events(lines, "done")[0]["processed"] == 1

    def test_non_dict_prefetched_is_replaced_when_a_price_arrives(self, senv):
        # `get_all_stock_feeds` returning a truthy non-dict must not crash the price injection
        senv.feeds = ["oops"]
        senv.prices = {"AAA": 5.0}
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert senv.lite_calls == [("AAA", {"close": 5.0, "price": 5.0}, 5.0)]
        assert _events(lines, "done")[0]["processed"] == 1


# ── lite path ───────────────────────────────────────────────────────────────

class TestStreamLitePath:
    def test_rows_carry_progress(self, senv):
        senv.universe = ["AAA", "BBB"]
        _, lines = _stream(lite=True)
        rows = _rows(lines)
        assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
        assert [r["_progress"]["processed"] for r in rows] == [1, 2]
        assert all(r["_progress"]["total"] == 2 for r in rows)
        assert senv.ultra_calls == []

    def test_lite_evaluator_failure_becomes_an_error_row(self, senv):
        def lite(sym, fed, px):
            if sym == "BBB":
                raise ValueError("e" * 400)
            return {"symbol": sym, "decision": "BUY NOW", "close": px or 1.0}

        senv.lite = lite
        senv.universe = ["AAA", "BBB"]
        _, lines = _stream(lite=True)
        rows = {r["symbol"]: r for r in _rows(lines)}
        assert rows["AAA"]["decision"] == "BUY NOW"
        assert rows["BBB"]["decision"] == "ERROR" and len(rows["BBB"]["error"]) == 200
        assert _events(lines, "done")[0]["processed"] == 2


# ── full path ───────────────────────────────────────────────────────────────

class TestStreamFullPath:
    def test_worker_gets_feed_row_prefetched_map_and_skips_gemini(self, senv):
        senv.feeds = {"AAA": {"close": 10.0}}
        senv.prices = {"AAA": 12.0}
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        (sym, kw), = senv.ultra_calls
        assert sym == "AAA"
        assert kw["lite"] is False and kw["skip_gemini"] is True
        assert kw["feed_row"]["price"] == 12.0
        assert kw["prefetched_feeds"]["AAA"] is kw["feed_row"] or kw["prefetched_feeds"]["AAA"] == kw["feed_row"]
        row = _rows(lines)[0]
        assert row["symbol"] == "AAA" and row["_progress"] == {
            "processed": 1, "total": 1, "elapsed": row["_progress"]["elapsed"]}

    def test_slow_worker_falls_back_to_instant_scores(self, senv, monkeypatch):
        monkeypatch.setenv("SCAN_STREAM_SYMBOL_TIMEOUT", "0.05")

        async def slow(sym, kw):
            await asyncio.sleep(1)

        senv.ultra = slow
        senv.prices = {"AAA": 12.0}
        senv.feeds = {"AAA": {"close": 10.0}}
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["fallback_instant"] is True
        assert "fallback_reason" in row
        base, fed, tick = senv.instant_calls[0]
        assert base == "AAA" and fed["close"] == 10.0 and tick == {"price": 12.0}

    def test_fallback_without_a_price_passes_an_empty_tick(self, senv):
        async def boom(sym, kw):
            raise RuntimeError("decision down")

        senv.ultra = boom
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        assert senv.instant_calls == [("AAA", {}, {})]
        row = _rows(lines)[0]
        assert row["fallback_instant"] is True and row["fallback_reason"] == "decision down"

    def test_worker_and_instant_both_failing_gives_an_error_row(self, senv):
        async def boom(sym, kw):
            raise RuntimeError("decision down")

        def bad_instant(base, fed, tick):
            raise ValueError("i" * 400)

        senv.ultra = boom
        senv.instant = bad_instant
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["decision"] == "ERROR" and row["symbol"] == "AAA" and len(row["error"]) == 200

    def test_non_serialisable_values_are_stringified(self, senv):
        async def ok(sym, kw):
            return {"symbol": sym, "decision": "HOLD", "close": 5.0, "tags": {1, 2}}

        senv.ultra = ok
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        assert isinstance(_rows(lines)[0]["tags"], str)


class TestStreamBatchResultShapes:
    """`asyncio.gather(..., return_exceptions=True)` output handling (scripted gather)."""

    def test_exception_result_falls_back_to_instant_scores(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "asyncio", _AsyncioProxy(_scripted_gather([RuntimeError("r" * 300)])))
        senv.prices = {"AAA": 7.0}
        senv.feeds = {"AAA": {"close": 6.0}}
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["fallback_instant"] is True and len(row["fallback_reason"]) == 120
        assert senv.instant_calls[0][2] == {"price": 7.0}

    def test_exception_result_with_failing_instant_scorer_is_an_error_row(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "asyncio", _AsyncioProxy(_scripted_gather([RuntimeError("r" * 300)])))

        def bad(base, fed, tick):
            raise ValueError("nope")

        senv.instant = bad
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["symbol"] == "AAA" and row["decision"] == "ERROR" and len(row["error"]) == 200

    def test_dict_passes_through_and_junk_becomes_invalid(self, senv, monkeypatch):
        monkeypatch.setattr(
            gw, "asyncio",
            _AsyncioProxy(_scripted_gather([{"symbol": "AAA", "decision": "BUY NOW", "close": 3.0}, 42])),
        )
        senv.universe = ["AAA", "BBB"]
        _, lines = _stream(lite=False)
        rows = _rows(lines)
        assert rows[0]["decision"] == "BUY NOW"
        assert rows[1]["decision"] == "ERROR" and rows[1]["error"] == "invalid"
        assert rows[1]["symbol"] == "BBB"

    def test_chunk_level_failure_marks_every_symbol_and_continues(self, senv, monkeypatch):
        monkeypatch.setattr(gw, "asyncio", _AsyncioProxy(_scripted_gather(raises=RuntimeError("pool gone"))))
        senv.universe = ["AAA", "BBB", "CCC"]
        _, lines = _stream(lite=False)
        rows = _rows(lines)
        assert [r["symbol"] for r in rows] == ["AAA", "BBB", "CCC"]
        assert all(r["decision"] == "ERROR" and r["error"] == "pool gone" for r in rows)
        assert [r["_progress"]["processed"] for r in rows] == [1, 2, 3]
        done = _events(lines, "done")[0]
        assert done["processed"] == 3 and done["total"] == 3


# ── price-resolution block ──────────────────────────────────────────────────

class TestStreamPriceResolution:
    def test_feed_close_supplies_price_aliases_without_a_quote_call(self, senv):
        async def no_price(sym, kw):
            return {"symbol": "AAA", "decision": "HOLD"}

        senv.ultra = no_price
        senv.feeds = {"AAA": {"close": 55.5}}
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["close"] == 55.5 and row["price"] == 55.5 and row["ltp"] == 55.5
        assert senv.quote_calls == []

    def test_quote_lookup_fills_a_row_that_is_still_priceless(self, senv):
        async def no_price(sym, kw):
            return {"symbol": "AAA", "decision": "HOLD"}

        senv.ultra = no_price
        senv.quote = lambda sym: 42.0
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert senv.quote_calls == ["AAA"]
        assert row["close"] == 42.0 and row["current_price"] == 42.0

    def test_quote_lookup_returning_none_leaves_the_row_priceless(self, senv):
        async def no_price(sym, kw):
            return {"symbol": "AAA", "decision": "HOLD"}

        senv.ultra = no_price
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert senv.quote_calls == ["AAA"]
        assert "close" not in row and "price" not in row

    def test_quote_lookup_failure_is_swallowed(self, senv):
        async def no_price(sym, kw):
            return {"symbol": "AAA", "decision": "HOLD"}

        def boom(sym):
            raise RuntimeError("quote down")

        senv.ultra = no_price
        senv.quote = boom
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        assert _rows(lines)[0]["symbol"] == "AAA"
        assert _events(lines, "done")[0]["processed"] == 1

    def test_priced_row_does_not_trigger_a_quote_call(self, senv):
        senv.universe = ["AAA"]          # default worker returns close=100.0
        _, lines = _stream(lite=False)
        assert senv.quote_calls == []
        assert _rows(lines)[0]["close"] == 100.0

    def test_resolver_failure_is_swallowed_and_the_row_still_streams(self, senv, monkeypatch):
        def boom(row, feed=None, tick=None):
            raise RuntimeError("resolver broke")

        monkeypatch.setattr(price_resolver, "ensure_row_price", boom)
        senv.universe = ["AAA"]
        _, lines = _stream(lite=False)
        row = _rows(lines)[0]
        assert row["symbol"] == "AAA" and row["_progress"]["processed"] == 1


# ── wiring through the real routes ──────────────────────────────────────────

class TestStreamRoutes:
    @pytest.mark.parametrize("path", ["/scan/stream", "/api/scan/stream"])
    def test_both_paths_stream_ndjson(self, senv, client, path):
        senv.universe = ["AAA", "BBB"]
        r = client.get(path, params={"lite": "true"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        assert [x["symbol"] for x in _rows(lines)] == ["AAA", "BBB"]
        assert _events(lines, "done")[0]["total"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# POST /scan/find-buys
# ═════════════════════════════════════════════════════════════════════════════

class TestFindBuys:
    @pytest.mark.parametrize("path", ["/scan/find-buys", "/api/scan/find-buys"])
    def test_both_paths_work_and_always_return_200(self, client, path):
        r = client.post(path, json={"stocks": []})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["count"] == 0 and body["suggestions"] == []

    def test_payload_reaches_the_sniper(self, client, monkeypatch):
        import buy_sniper
        seen = {}

        def fake(payload):
            seen["payload"] = payload
            return {"ok": True, "count": 1, "suggestions": [{"symbol": "AAA"}]}

        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload", fake)
        r = client.post("/scan/find-buys", json={"stocks": [{"symbol": "AAA"}], "target_count": 2})
        assert r.json() == {"ok": True, "count": 1, "suggestions": [{"symbol": "AAA"}]}
        assert seen["payload"] == {"stocks": [{"symbol": "AAA"}], "target_count": 2}

    def test_list_body_is_wrapped_as_stocks(self, client, monkeypatch):
        import buy_sniper
        seen = {}
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload",
                            lambda p: seen.setdefault("p", p) and {"ok": True, "count": 0, "suggestions": []})
        client.post("/scan/find-buys", json=[{"symbol": "AAA"}])
        assert seen["p"] == {"stocks": [{"symbol": "AAA"}]}

    @pytest.mark.parametrize("raw", ['"just a string"', "42", "null"])
    def test_other_json_bodies_become_an_empty_stock_list(self, client, monkeypatch, raw):
        import buy_sniper
        seen = {}
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload",
                            lambda p: seen.setdefault("p", p) and {"ok": True, "count": 0, "suggestions": []})
        r = client.post("/scan/find-buys", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 200
        assert seen["p"] == {"stocks": []}

    def test_unparseable_body_becomes_an_empty_payload(self, client, monkeypatch):
        import buy_sniper
        seen = {}
        # `{}` is falsy, so the lambda's `and` would short-circuit: record explicitly
        def fake(p):
            seen["p"] = p
            return {"ok": True, "count": 0, "suggestions": []}

        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload", fake)
        r = client.post("/scan/find-buys", content="{not json", headers={"content-type": "application/json"})
        assert r.status_code == 200
        assert seen["p"] == {}

    def test_non_dict_sniper_result_is_replaced_by_an_empty_ok(self, client, monkeypatch):
        import buy_sniper
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload", lambda p: ["junk"])
        r = client.post("/scan/find-buys", json={})
        assert r.json() == {"ok": True, "count": 0, "suggestions": []}

    def test_missing_keys_are_defaulted(self, client, monkeypatch):
        import buy_sniper
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload",
                            lambda p: {"suggestions": [{"symbol": "A"}, {"symbol": "B"}], "extra": 1})
        body = client.post("/scan/find-buys", json={}).json()
        assert body == {"suggestions": [{"symbol": "A"}, {"symbol": "B"}], "extra": 1, "ok": True, "count": 2}

    def test_existing_keys_are_not_overwritten(self, client, monkeypatch):
        import buy_sniper
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload",
                            lambda p: {"ok": False, "count": 9, "suggestions": [{"symbol": "A"}]})
        body = client.post("/scan/find-buys", json={}).json()
        assert body == {"ok": False, "count": 9, "suggestions": [{"symbol": "A"}]}

    def test_none_suggestions_are_normalised_to_an_empty_list(self, client, monkeypatch):
        # FIXED: an explicit None is replaced by [] (setdefault alone did not).
        import buy_sniper
        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload", lambda p: {"suggestions": None})
        body = client.post("/scan/find-buys", json={}).json()
        assert body["count"] == 0 and body["suggestions"] == []

    def test_sniper_crash_returns_ok_false_with_a_truncated_error(self, client, monkeypatch):
        import buy_sniper

        def boom(p):
            raise RuntimeError("s" * 500)

        monkeypatch.setattr(buy_sniper, "suggestions_from_scan_payload", boom)
        r = client.post("/scan/find-buys", json={"stocks": []})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["count"] == 0 and body["suggestions"] == []
        assert len(body["error"]) == 300
        assert body["message"] == "Sniper could not evaluate candidates"

    def test_real_sniper_smoke_with_no_stocks(self, client):
        body = client.post("/scan/find-buys", json={"stocks": []}).json()
        assert body["ok"] is True and body["count"] == 0
        assert body["message"] == "No setups meet conviction / R:R / decision criteria"


# ═════════════════════════════════════════════════════════════════════════════
# POST /scan/cancel/{task_id}  and  POST /scan/stop-all
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def cancel_flags(monkeypatch):
    flags = set()
    monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", flags)
    return flags


class TestCancelScan:
    def test_unknown_task_still_gets_flag_durable_key_and_a_cancelled_record(self, kv, cancel_flags):
        out = gw.cancel_scan("t1")
        assert "t1" in cancel_flags
        assert (gw.SCAN_TASK_PREFIX + "t1:cancel", True, 3600) in kv.sets
        rec = kv.store[gw.SCAN_TASK_PREFIX + "t1"]
        assert rec == {
            "cancel_requested": True,
            "status": "cancelled",
            "partial": True,
            "message": "Stop requested — partial results committed",
        }
        assert kv.sets[-1][2] == 3600
        assert out == {
            "ok": True,
            "status": "cancel_requested",
            "processed_so_far": 0,
            "total": 0,
            "message": "Scan stop signalled — worker will commit partial and exit",
        }

    def test_running_task_is_marked_cancelled_and_progress_is_echoed(self, kv, cancel_flags):
        kv.store[gw.SCAN_TASK_PREFIX + "t2"] = {"status": "running", "processed": 40, "total": 300, "x": 1}
        out = gw.cancel_scan("t2")
        rec = kv.store[gw.SCAN_TASK_PREFIX + "t2"]
        assert rec["status"] == "cancelled" and rec["partial"] is True
        assert rec["cancel_requested"] is True and rec["processed"] == 40 and rec["x"] == 1
        assert out["processed_so_far"] == 40 and out["total"] == 300

    def test_task_without_a_status_is_cancelled_too(self, kv, cancel_flags):
        kv.store[gw.SCAN_TASK_PREFIX + "t3"] = {"processed": 1}
        gw.cancel_scan("t3")
        assert kv.store[gw.SCAN_TASK_PREFIX + "t3"]["status"] == "cancelled"

    @pytest.mark.parametrize("status", ["done", "error", "cancelled"])
    def test_finished_task_keeps_its_status(self, kv, cancel_flags, status):
        kv.store[gw.SCAN_TASK_PREFIX + "t4"] = {"status": status, "processed": 300, "total": 300}
        gw.cancel_scan("t4")
        rec = kv.store[gw.SCAN_TASK_PREFIX + "t4"]
        assert rec["status"] == status
        assert rec["cancel_requested"] is True and rec["partial"] is True

    def test_existing_message_is_preserved(self, kv, cancel_flags):
        kv.store[gw.SCAN_TASK_PREFIX + "t5"] = {"status": "running", "message": "Scanning 120/300"}
        gw.cancel_scan("t5")
        assert kv.store[gw.SCAN_TASK_PREFIX + "t5"]["message"] == "Scanning 120/300"

    def test_non_dict_record_is_treated_as_missing(self, kv, cancel_flags):
        kv.store[gw.SCAN_TASK_PREFIX + "t6"] = "garbage"
        out = gw.cancel_scan("t6")
        assert kv.store[gw.SCAN_TASK_PREFIX + "t6"]["status"] == "cancelled"
        assert out["processed_so_far"] == 0

    def test_failing_record_write_is_swallowed_but_the_flag_and_durable_key_stand(self, kv, cancel_flags):
        kv.store[gw.SCAN_TASK_PREFIX + "t7"] = {"status": "running", "processed": 5, "total": 9}
        kv.raise_on = lambda key: not key.endswith(":cancel")
        out = gw.cancel_scan("t7")
        assert "t7" in cancel_flags
        assert (gw.SCAN_TASK_PREFIX + "t7:cancel", True, 3600) in kv.sets
        # the record itself was never persisted...
        assert kv.store[gw.SCAN_TASK_PREFIX + "t7"]["status"] == "running"
        # ...but the response still reports the progress it read
        assert out["ok"] is True and out["processed_so_far"] == 5 and out["total"] == 9

    def test_route_is_registered(self, kv, cancel_flags, client):
        r = client.post("/scan/cancel/abc")
        assert r.status_code == 200 and r.json()["status"] == "cancel_requested"
        assert "abc" in cancel_flags

    def test_cancel_does_not_touch_the_global_stop_flag(self, kv, cancel_flags):
        gw.cancel_scan("t8")
        assert "__ALL__" not in cancel_flags


class TestScanStopAll:
    @pytest.fixture
    def mem(self, monkeypatch, kv):
        store = {}
        monkeypatch.setattr(gw, "_mem_kv", store)
        return store

    def test_sets_the_global_flag_even_with_nothing_running(self, cancel_flags, mem, kv):
        out = gw.scan_stop_all()
        assert "__ALL__" in cancel_flags
        assert out == {"ok": True, "stopped": 0, "message": "All scans stop-signalled"}
        assert kv.sets == []

    def test_only_running_task_records_are_stopped(self, cancel_flags, mem, kv):
        p = gw.SCAN_TASK_PREFIX
        mem[p + "a"] = {"status": "running", "processed": 3}
        mem[p + "b"] = {"status": "done"}
        mem[p + "c"] = {"status": "running"}
        mem[p + "c:cancel"] = True                       # durable cancel marker: skipped
        mem["other:key"] = {"status": "running"}         # not a scan task: skipped
        mem[p + "d"] = "not-a-dict"                      # skipped
        out = gw.scan_stop_all()
        assert out["stopped"] == 2
        for k in (p + "a", p + "c"):
            assert mem[k]["status"] == "cancelled"
            assert mem[k]["cancel_requested"] is True and mem[k]["partial"] is True
            assert (k, mem[k], 3600) in kv.sets
            assert (k + ":cancel", True, 3600) in kv.sets
        assert mem[p + "a"]["processed"] == 3
        assert mem[p + "b"] == {"status": "done"}
        assert mem["other:key"] == {"status": "running"}
        assert mem[p + "d"] == "not-a-dict"
        assert mem[p + "c:cancel"] is True

    def test_does_not_mutate_the_original_record_in_place(self, cancel_flags, mem, kv):
        p = gw.SCAN_TASK_PREFIX
        original = {"status": "running"}
        mem[p + "a"] = original
        gw.scan_stop_all()
        assert original == {"status": "running"}
        assert mem[p + "a"] is not original

    def test_kv_failure_does_not_abort_the_sweep(self, cancel_flags, mem, kv):
        # FIXED: one failing durable write no longer ends the loop; every running task is still
        # marked cancelled in memory and the failures are counted.
        p = gw.SCAN_TASK_PREFIX
        mem[p + "a"] = {"status": "running"}
        mem[p + "b"] = {"status": "running"}
        kv.raise_on = lambda key: True
        out = gw.scan_stop_all()
        assert out["ok"] is True and out["stopped"] == 0 and out["durable_write_failures"] == 2
        assert "__ALL__" in cancel_flags
        assert mem[p + "a"]["status"] == "cancelled"
        assert mem[p + "b"]["status"] == "cancelled"

    def test_route_is_registered(self, cancel_flags, mem, kv, client):
        r = client.post("/scan/stop-all")
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_stop_all_then_stream_clears_the_flag_for_the_next_run(self, senv, mem, kv):
        # the stream resets `__ALL__` on entry, so a Power-Off never poisons the next scan
        gw.scan_stop_all()
        assert "__ALL__" in gw._SCAN_CANCEL_FLAGS
        senv.universe = ["AAA"]
        _, lines = _stream(lite=True)
        assert "__ALL__" not in gw._SCAN_CANCEL_FLAGS
        assert _events(lines, "cancelled") == []


# ═════════════════════════════════════════════════════════════════════════════
# GET /scan/watchlist
# ═════════════════════════════════════════════════════════════════════════════

class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code = status
        self._data = {} if data is None else data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


class WLWorld:
    """Scripted world for `scan_watchlist`."""

    def __init__(self):
        self.watchlist = []
        self.decide = {}            # symbol -> FakeResp | Exception | callable(symbol)
        self.predict = {}           # symbol -> FakeResp | Exception   (missing -> 404)
        self.calls = []             # urls hit
        self.clients = []           # FakeSyncClient instances
        self.quote = lambda sym: None
        self.quote_calls = []
        self.fundamentals = None    # callable(normalized, symbol) | None
        self.news = lambda sym: None
        self.events = lambda sym: None
        self.outcomes = []
        self.notified = []
        self.notified_evt = threading.Event()
        self.cleanup_calls = []
        self.cleanup_evt = threading.Event()
        self.release = threading.Event()   # set at teardown so blocked workers can finish


class FakeSyncClient:
    world = None

    def __init__(self, *a, **kw):
        self.kw = kw
        self.closed = False
        FakeSyncClient.world.clients.append(self)

    def get(self, url, **kw):
        w = FakeSyncClient.world
        w.calls.append(url)
        if "/decide/" in url:
            sym = url.rsplit("/", 1)[1]
            out = w.decide.get(sym)
            if callable(out):
                out = out(sym)
            if isinstance(out, Exception):
                raise out
            return out if out is not None else FakeResp(404)
        if "/predict/" in url:
            sym = url.rsplit("/", 1)[1]
            out = w.predict.get(sym)
            if isinstance(out, Exception):
                raise out
            return out if out is not None else FakeResp(404)
        raise httpx.ConnectError("no route " + url)

    def close(self):
        self.closed = True


@pytest.fixture
def wl(monkeypatch):
    w = WLWorld()
    FakeSyncClient.world = w
    monkeypatch.setattr(httpx, "Client", FakeSyncClient)
    monkeypatch.setattr(gw, "_load_watchlist", lambda: list(w.watchlist))

    def quote(sym):
        w.quote_calls.append(sym)
        return w.quote(sym)

    def merge(normalized, sym):
        if w.fundamentals is not None:
            w.fundamentals(normalized, sym)

    def outcomes(results):
        w.outcomes.append(list(results))

    def notify(recs, verdict, scanned, universe):
        w.notified.append((recs, verdict, scanned, universe))
        w.notified_evt.set()

    def cleanup(pool, client, not_done, deadline):
        w.cleanup_calls.append((pool, client, list(not_done), deadline))
        w.cleanup_evt.set()

    monkeypatch.setattr(gw, "_fetch_price_from_quote", quote)
    monkeypatch.setattr(gw, "_merge_fundamentals", merge)
    monkeypatch.setattr(gw, "_fetch_news", lambda sym: w.news(sym))
    monkeypatch.setattr(gw, "_fetch_events", lambda sym: w.events(sym))
    monkeypatch.setattr(gw, "_record_symbol_outcomes", outcomes)
    monkeypatch.setattr(gw, "_send_scan_notification", notify)
    monkeypatch.setattr(gw, "_cleanup_scan_resources", cleanup)
    for name in ("WATCHLIST_SCAN_CONCURRENCY", "WATCHLIST_SCAN_HTTP_CONCURRENCY",
                 "WATCHLIST_SCAN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    yield w
    w.release.set()


def _decision(symbol, decision="HOLD", combined=50, **extra):
    """A decide payload that already carries price/news/event/prediction data, so only the
    fundamentals enrichment runs unless a test removes one of those keys."""
    d = {"symbol": symbol, "decision": decision, "combined_score": combined,
         "close": 100.0, "news_score": 50, "event_risk": True, "prediction_score": 50}
    d.update(extra)
    return d


def _scan(w, symbols, payloads):
    w.watchlist = list(symbols)
    for s, p in payloads.items():
        w.decide[s] = FakeResp(200, p)
    out = gw.scan_watchlist()
    assert w.notified_evt.wait(2), "notification thread never ran"
    return out


class TestWatchlistEmpty:
    def test_empty_watchlist_returns_the_empty_shape_without_any_http(self, wl):
        out = gw.scan_watchlist()
        assert out == {
            "scanned": 0,
            "universe_size": 0,
            "watchlist_size": 0,
            "recommendations": [],
            "watchlist_candidates": [],
            "verdict": "Watchlist is empty. Add some symbols first.",
            "market_mood": "Neutral",
            "market_stats": {"buy_signals": 0, "sell_signals": 0, "hold_signals": 0, "cautious": 0},
            "all_results": [],
            "errors": [],
        }
        assert wl.clients == [] and wl.calls == [] and wl.notified == []

    def test_route_is_registered(self, wl, client):
        r = client.get("/scan/watchlist")
        assert r.status_code == 200 and r.json()["market_mood"] == "Neutral"


class TestWatchlistBasics:
    def test_results_sorted_by_combined_score_and_counts_reported(self, wl):
        out = _scan(wl, ["AAA", "BBB", "CCC"], {
            "AAA": _decision("AAA", "HOLD", 40),
            "BBB": _decision("BBB", "BUY NOW", 80),
            "CCC": _decision("CCC", "PREPARE TO BUY", 60),
        })
        assert [r["symbol"] for r in out["all_results"]] == ["BBB", "CCC", "AAA"]
        assert out["scanned"] == 3 and out["universe_size"] == 3 and out["watchlist_size"] == 3
        assert out["errors"] == [] and out["partial"] is False
        assert out["watchlist_candidates"] == []

    def test_decide_calls_hit_the_decision_service_per_symbol(self, wl):
        _scan(wl, ["AAA", "BBB"], {"AAA": _decision("AAA"), "BBB": _decision("BBB")})
        decide_urls = sorted(u for u in wl.calls if "/decide/" in u)
        assert decide_urls == [f"{gw.DECISION_URL}/decide/AAA", f"{gw.DECISION_URL}/decide/BBB"]

    def test_client_is_closed_when_nothing_is_left_running(self, wl):
        _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        assert wl.clients[0].closed is True
        assert wl.cleanup_calls == []

    def test_client_is_built_with_a_30s_timeout_and_bounded_limits(self, wl):
        _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        kw = wl.clients[0].kw
        assert kw["timeout"] == 30
        assert kw["limits"].max_keepalive_connections == gw.MAX_PARALLEL_WORKERS
        assert kw["limits"].max_connections == gw.MAX_PARALLEL_WORKERS * 2

    def test_outcomes_are_recorded_for_the_universe_self_pruning(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "BUY NOW", 70)})
        assert wl.outcomes == [out["all_results"]]

    def test_zero_concurrency_env_values_are_floored_to_one(self, wl, monkeypatch):
        monkeypatch.setenv("WATCHLIST_SCAN_CONCURRENCY", "0")
        monkeypatch.setenv("WATCHLIST_SCAN_HTTP_CONCURRENCY", "0")
        out = _scan(wl, ["AAA", "BBB"], {"AAA": _decision("AAA"), "BBB": _decision("BBB")})
        assert out["scanned"] == 2 and out["errors"] == []

    def test_notification_runs_on_a_background_thread_with_the_summary(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "BUY NOW", 70)})
        recs, verdict, scanned, universe = wl.notified[0]
        assert recs == out["recommendations"]
        assert verdict == out["verdict"] and scanned == 1 and universe == 1


class TestWatchlistErrors:
    def test_http_status_error_is_recorded_and_the_rest_still_scan(self, wl):
        wl.watchlist = ["AAA", "BBB"]
        wl.decide["AAA"] = FakeResp(500)
        wl.decide["BBB"] = FakeResp(200, _decision("BBB", "BUY NOW", 70))
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert [r["symbol"] for r in out["all_results"]] == ["BBB"]
        assert [e["symbol"] for e in out["errors"]] == ["AAA"]
        assert out["errors"][0]["error"] == "boom"
        assert out["partial"] is False     # an HTTP error is not a deadline hit

    def test_transport_error_is_recorded(self, wl):
        wl.watchlist = ["AAA"]
        wl.decide["AAA"] = httpx.ConnectError("refused")
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert out["errors"] == [{"symbol": "AAA", "error": "refused"}]
        assert out["scanned"] == 0

    def test_non_http_failure_is_recorded(self, wl):
        wl.watchlist = ["AAA"]
        wl.decide["AAA"] = FakeResp(200, json_raises=True)
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert out["errors"] == [{"symbol": "AAA", "error": "bad json"}]

    def test_all_symbols_failing_yields_an_empty_but_well_formed_response(self, wl):
        wl.watchlist = ["AAA", "BBB"]
        wl.decide["AAA"] = FakeResp(500)
        wl.decide["BBB"] = FakeResp(503)
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert out["scanned"] == 0 and out["all_results"] == [] and len(out["errors"]) == 2
        assert out["recommendations"] == [] and out["final_verdict"]["best_short"] is None
        assert out["final_verdict"]["headline"] == "Short: 0 pick(s). Mid: 0, Long: 0."
        assert out["verdict"] == "No strong signals in your watchlist"
        assert out["market_mood"] == "Cautious"


class TestWatchlistDeadline:
    def test_straggler_is_reported_and_cleanup_is_handed_off(self, wl, monkeypatch):
        monkeypatch.setenv("WATCHLIST_SCAN_TIMEOUT_SECONDS", "0.3")
        monkeypatch.setenv("WATCHLIST_SCAN_CONCURRENCY", "2")
        wl.watchlist = ["FAST", "SLOW"]
        wl.decide["FAST"] = FakeResp(200, _decision("FAST", "BUY NOW", 70))

        def slow(sym):
            wl.release.wait(10)
            return FakeResp(200, _decision("SLOW"))

        wl.decide["SLOW"] = slow
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2) and wl.cleanup_evt.wait(2)
        assert [r["symbol"] for r in out["all_results"]] == ["FAST"]
        assert out["errors"] == [{"symbol": "SLOW", "error": "scan deadline (0.3s) exceeded, skipped this run"}]
        assert out["partial"] is True
        pool, client, not_done, deadline = wl.cleanup_calls[0]
        assert len(not_done) == 1 and deadline == 0.3
        assert wl.clients[0].closed is False       # left open for the background cleanup to close
        wl.release.set()
        pool.shutdown(wait=True)

    def test_deadline_message_keeps_a_sub_second_budget(self, wl, monkeypatch):
        # FIXED: `%g` / `:g` no longer round a sub-second budget to "0s" in the message.
        monkeypatch.setenv("WATCHLIST_SCAN_TIMEOUT_SECONDS", "0.2")
        wl.watchlist = ["SLOW"]

        def slow(sym):
            wl.release.wait(10)
            return FakeResp(200, _decision("SLOW"))

        wl.decide["SLOW"] = slow
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert "(0.2s)" in out["errors"][0]["error"]
        wl.release.set()
        wl.cleanup_calls[0][0].shutdown(wait=True)

    def test_partial_needs_both_a_shortfall_and_a_deadline_error(self, wl):
        # one HTTP failure: fewer results than entries, but no "deadline" error -> not partial
        wl.watchlist = ["AAA", "BBB"]
        wl.decide["AAA"] = FakeResp(500)
        wl.decide["BBB"] = FakeResp(200, _decision("BBB"))
        out = gw.scan_watchlist()
        assert wl.notified_evt.wait(2)
        assert len(out["all_results"]) < out["watchlist_size"] and out["partial"] is False


class TestWatchlistEnrichment:
    """The five per-symbol jobs: price / fundamentals / news / events / prediction."""

    def test_price_job_fills_close_and_default_support_resistance(self, wl):
        wl.quote = lambda sym: 200.0
        out = _scan(wl, ["AAA"], {"AAA": {"symbol": "AAA", "decision": "HOLD", "combined_score": 50,
                                          "news_score": 50, "event_risk": True, "prediction_score": 50}})
        r = out["all_results"][0]
        assert wl.quote_calls == ["AAA"]
        assert r["close"] == 200.0 and r["support"] == 190.0 and r["resistance"] == 210.0

    def test_price_job_keeps_existing_support_and_resistance(self, wl):
        wl.quote = lambda sym: 200.0
        out = _scan(wl, ["AAA"], {"AAA": {"symbol": "AAA", "decision": "HOLD", "combined_score": 50,
                                          "support": 150.0, "resistance": 250.0,
                                          "news_score": 50, "event_risk": True, "prediction_score": 50}})
        r = out["all_results"][0]
        assert r["support"] == 150.0 and r["resistance"] == 250.0 and r["close"] == 200.0

    def test_price_job_is_skipped_when_the_decision_already_has_a_close(self, wl):
        _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        assert wl.quote_calls == []

    def test_price_job_returning_none_leaves_close_unset(self, wl):
        wl.quote = lambda sym: None
        out = _scan(wl, ["AAA"], {"AAA": {"symbol": "AAA", "decision": "HOLD", "combined_score": 50,
                                          "news_score": 50, "event_risk": True, "prediction_score": 50}})
        assert out["all_results"][0]["close"] is None

    def test_fundamentals_job_mutates_the_normalized_row(self, wl):
        wl.fundamentals = lambda n, s: n.update(fundamental_score=77)
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        assert out["all_results"][0]["fundamental_score"] == 77

    def test_news_job_sets_score_and_reasons(self, wl):
        wl.news = lambda sym: {"news_score": 71, "reasons": ["good headline"]}
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", news_score=None)})
        r = out["all_results"][0]
        assert r["news_score"] == 71 and r["reasons"]["news"] == ["good headline"]
        assert r["reasons"]["technical"] == ["Data unavailable"]      # existing reasons kept

    def test_news_job_without_reasons_only_sets_the_score(self, wl):
        wl.news = lambda sym: {"news_score": 64, "reasons": []}
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", news_score=None)})
        r = out["all_results"][0]
        assert r["news_score"] == 64 and "news" not in r["reasons"]

    def test_news_job_is_skipped_when_a_score_is_already_present(self, wl):
        called = []
        wl.news = lambda sym: called.append(sym)
        _scan(wl, ["AAA"], {"AAA": _decision("AAA", news_score=10)})
        assert called == []

    def test_events_job_with_an_earnings_date_raises_event_risk(self, wl):
        wl.events = lambda sym: {"next_earnings_date": "2026-10-20", "other": 1}
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", event_risk=False)})
        r = out["all_results"][0]
        assert r["event_risk"] is True
        assert r["reasons"]["event"] == ["Earnings due: 2026-10-20"]
        assert r["event_data"] == {"next_earnings_date": "2026-10-20", "other": 1}

    def test_events_job_without_an_earnings_date_only_attaches_event_data(self, wl):
        wl.events = lambda sym: {"next_earnings_date": None, "note": "none"}
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", event_risk=False)})
        r = out["all_results"][0]
        assert r["event_risk"] is False and "event" not in r["reasons"]
        assert r["event_data"]["note"] == "none"

    def test_events_job_is_skipped_when_event_risk_is_already_set(self, wl):
        called = []
        wl.events = lambda sym: called.append(sym)
        _scan(wl, ["AAA"], {"AAA": _decision("AAA", event_risk=True)})
        assert called == []

    def test_events_job_is_skipped_when_an_event_reason_exists(self, wl):
        called = []
        wl.events = lambda sym: called.append(sym)
        _scan(wl, ["AAA"], {"AAA": _decision("AAA", event_risk=False, reasons={"event": ["x"]})})
        assert called == []

    def test_prediction_job_applies_a_loaded_model(self, wl):
        wl.predict["AAA"] = FakeResp(200, {"model_loaded": True, "prediction_score": 66, "note": "ok"})
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", prediction_score=None)})
        r = out["all_results"][0]
        assert r["prediction_score"] == 66 and r["prediction_note"] == "ok"
        assert any("/predict/AAA" in u for u in wl.calls)

    def test_prediction_job_ignores_a_model_that_is_not_loaded(self, wl):
        wl.predict["AAA"] = FakeResp(200, {"model_loaded": False, "prediction_score": 66})
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", prediction_score=None)})
        assert out["all_results"][0]["prediction_score"] is None

    def test_prediction_job_ignores_a_non_200(self, wl):
        wl.predict["AAA"] = FakeResp(503, {"model_loaded": True, "prediction_score": 66})
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", prediction_score=None)})
        assert out["all_results"][0]["prediction_score"] is None

    def test_prediction_job_failure_is_logged_and_ignored(self, wl, caplog):
        wl.predict["AAA"] = httpx.ReadTimeout("slow")
        with caplog.at_level("WARNING", logger=gw.logger.name):
            out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", prediction_score=None)})
        assert out["all_results"][0]["prediction_score"] is None
        assert "Prediction service lookup failed during watchlist scan for AAA" in caplog.text

    def test_prediction_job_is_skipped_when_a_score_is_already_present(self, wl):
        _scan(wl, ["AAA"], {"AAA": _decision("AAA", prediction_score=40)})
        assert not any("/predict/" in u for u in wl.calls)

    def test_a_failing_job_does_not_sink_the_symbol(self, wl, caplog):
        def boom(n, s):
            raise RuntimeError("fundamentals down")

        wl.fundamentals = boom
        with caplog.at_level("WARNING", logger=gw.logger.name):
            out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "BUY NOW", 70)})
        assert [r["symbol"] for r in out["all_results"]] == ["AAA"] and out["errors"] == []
        assert "Watchlist scan enrichment 'fundamentals' failed for AAA: fundamentals down" in caplog.text

    def test_a_failing_job_with_an_empty_message_logs_the_exception_type(self, wl, caplog):
        def boom(n, s):
            raise RuntimeError()

        wl.fundamentals = boom
        with caplog.at_level("WARNING", logger=gw.logger.name):
            _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        assert "failed for AAA: RuntimeError" in caplog.text

    def test_holding_period_estimate_and_summary_are_attached(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "BUY NOW", 70, target=110.0,
                                                   entry_range={"low": 100.0, "high": 102.0})})
        r = out["all_results"][0]
        est = r["holding_period_estimate"]
        assert est["min_days"] >= 3 and est["max_days"] > est["min_days"]
        assert isinstance(r["natural_language_summary"], str) and r["natural_language_summary"]

    def test_holding_period_estimate_is_none_without_a_target(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA")})
        assert out["all_results"][0]["holding_period_estimate"] is None


class TestWatchlistPicksAndMood:
    def test_buy_signals_become_top_picks_and_the_verdict_counts_them(self, wl):
        out = _scan(wl, ["AAA", "BBB", "CCC"], {
            "AAA": _decision("AAA", "BUY NOW", 80),
            "BBB": _decision("BBB", "PREPARE TO BUY", 70),
            "CCC": _decision("CCC", "HOLD", 90),
        })
        assert out["verdict"] == "2 opportunity(ies) found"
        assert out["market_mood"] == "Selective"
        assert out["market_stats"] == {"buy_signals": 2, "sell_signals": 0, "hold_signals": 1, "cautious": 0}
        assert out["recommendations"] == out["recommendations_short"]
        # the short board ranks by horizon score, so a 90-score HOLD outranks the two buys
        assert [r["symbol"] for r in out["recommendations"]] == ["CCC", "AAA", "BBB"]
        # ...while the verdict / mood only count the two actionable decisions

    def test_five_buys_is_bullish(self, wl):
        names = [f"S{i}" for i in range(5)]
        out = _scan(wl, names, {n: _decision(n, "BUY NOW", 60 + i) for i, n in enumerate(names)})
        assert out["market_mood"] == "Bullish"
        assert len(out["recommendations"]) == 5

    def test_more_sells_than_buys_is_bearish(self, wl):
        out = _scan(wl, ["AAA", "BBB", "CCC"], {
            "AAA": _decision("AAA", "BUY NOW", 60),
            "BBB": _decision("BBB", "SELL", 20),
            "CCC": _decision("CCC", "SELL", 10),
        })
        assert out["market_mood"] == "Bearish"
        assert out["market_stats"]["sell_signals"] == 2

    def test_equal_buys_and_sells_is_selective(self, wl):
        out = _scan(wl, ["AAA", "BBB"], {
            "AAA": _decision("AAA", "BUY NOW", 60),
            "BBB": _decision("BBB", "SELL", 20),
        })
        assert out["market_mood"] == "Selective"

    def test_no_buys_is_cautious_and_unknown_decisions_count_as_cautious_stat(self, wl):
        out = _scan(wl, ["AAA", "BBB", "CCC"], {
            "AAA": _decision("AAA", "HOLD", 40),
            "BBB": _decision("BBB", "DO NOT BUY", 10),
            "CCC": _decision("CCC", "WAIT", 30),
        })
        assert out["market_mood"] == "Cautious"
        assert out["verdict"] == "No strong signals in your watchlist"
        assert out["market_stats"] == {"buy_signals": 0, "sell_signals": 0, "hold_signals": 1, "cautious": 2}

    def test_horizon_boards_use_explicit_horizon_scores_and_decisions(self, wl):
        out = _scan(wl, ["AAA", "BBB"], {
            "AAA": _decision("AAA", "DO NOT BUY", 30, horizons={
                "short": {"score": 90, "decision": "BUY NOW"},
                "mid": {"score": 10, "decision": "DO NOT BUY"},
                "long": {"score": 10, "decision": "DO NOT BUY"}}),
            "BBB": _decision("BBB", "DO NOT BUY", 30, horizons={
                "short": {"score": 10, "decision": "DO NOT BUY"},
                "mid": {"score": 80, "decision": "PREPARE TO BUY"},
                "long": {"score": 85, "decision": "BUY NOW"}}),
        })
        assert [r["symbol"] for r in out["recommendations_short"]] == ["AAA"]
        assert [r["symbol"] for r in out["recommendations_mid"]] == ["BBB"]
        assert [r["symbol"] for r in out["recommendations_long"]] == ["BBB"]
        assert out["recommendations_short"][0]["horizon_focus"] == "short"
        assert out["recommendations_short"][0]["_hz_score"] == 90

    def test_horizon_fallback_scores_when_no_explicit_score(self, wl):
        # combined 60: short 60 >= 54 (kept); mid 60*0.95=57 >= 56 (kept);
        # long (fundamental 70)*0.9 + 60*0.1 = 69 >= 58 (kept)
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "HOLD", 60, fundamental_score=70)})
        assert out["recommendations_short"][0]["_hz_score"] == 60
        assert out["recommendations_mid"][0]["_hz_score"] == pytest.approx(57.0)
        assert out["recommendations_long"][0]["_hz_score"] == pytest.approx(69.0)

    def test_horizon_fallback_long_uses_combined_when_fundamental_is_zero(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "HOLD", 70, fundamental_score=0)})
        # (0 or 70)*0.9 + 70*0.1 = 70
        assert out["recommendations_long"][0]["_hz_score"] == pytest.approx(70.0)

    def test_score_below_the_horizon_floor_is_not_promoted(self, wl):
        # combined 55: short >= 54 kept; mid 52.25 < 56 dropped; long 0.9*50+5.5 = 50.5 < 58 dropped
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "HOLD", 55)})
        assert [r["symbol"] for r in out["recommendations_short"]] == ["AAA"]
        # empty boards fall back to the overall ranking, never to an empty list
        assert [r["symbol"] for r in out["recommendations_mid"]] == ["AAA"]
        assert [r["symbol"] for r in out["recommendations_long"]] == ["AAA"]

    def test_do_not_buy_above_the_floor_is_not_promoted(self, wl):
        out = _scan(wl, ["AAA"], {"AAA": _decision("AAA", "DO NOT BUY", 70)})
        row = out["recommendations_short"][0]
        # fallback row: true decision, not relabelled, not a horizon pick
        assert row["decision"] == "DO NOT BUY" and "promoted_from_score" not in row
        assert "horizon_focus" not in row
        assert out["all_results"][0]["decision"] == "DO NOT BUY"

    def test_error_rows_are_kept_out_of_the_boards(self, wl):
        out = _scan(wl, ["AAA", "BBB"], {
            "AAA": _decision("AAA", "ERROR", 99),
            "BBB": _decision("BBB", "HOLD", 60),
        })
        for key in ("recommendations_short", "recommendations_mid", "recommendations_long"):
            assert [r["symbol"] for r in out[key]] == ["BBB"]

    def test_empty_boards_fall_back_to_ranked_slices_when_there_are_many_rows(self, wl):
        names = [f"S{i:02d}" for i in range(16)]
        payloads = {n: _decision(n, "DO NOT BUY", i + 1) for i, n in enumerate(names)}   # scores 1..16
        out = _scan(wl, names, payloads)
        ranked = [f"S{i:02d}" for i in range(15, -1, -1)]      # best first
        assert [r["symbol"] for r in out["recommendations_short"]] == ranked[:5]
        assert [r["symbol"] for r in out["recommendations_mid"]] == ranked[5:10]
        assert [r["symbol"] for r in out["recommendations_long"]] == ranked[10:15]
        assert out["final_verdict"]["best_short"] == ranked[0]

    def test_final_verdict_summarises_board_sizes(self, wl):
        out = _scan(wl, ["AAA", "BBB"], {
            "AAA": _decision("AAA", "BUY NOW", 80),
            "BBB": _decision("BBB", "BUY NOW", 70),
        })
        fv = out["final_verdict"]
        assert fv["preferred_horizon"] == "short"
        assert fv["short_count"] == len(out["recommendations_short"])
        assert fv["mid_count"] == len(out["recommendations_mid"])
        assert fv["long_count"] == len(out["recommendations_long"])
        assert fv["headline"] == (
            f"Short: {fv['short_count']} pick(s). Mid: {fv['mid_count']}, Long: {fv['long_count']}.")
        assert fv["best_short"] == out["recommendations_short"][0]["symbol"]

    def test_short_board_falls_back_to_the_value_ranked_top_picks(self, wl):
        # every row is below the short floor (54) and none is actionable, so the short board is
        # empty and falls back to the plain score ranking instead of returning nothing
        out = _scan(wl, ["AAA", "BBB"], {
            "AAA": _decision("AAA", "HOLD", 20),
            "BBB": _decision("BBB", "HOLD", 30),
        })
        assert [r["symbol"] for r in out["recommendations_short"]] == ["BBB", "AAA"]
        assert out["recommendations"] == out["recommendations_short"]


class TestPass82StopAllOuterGuard:
    def test_an_unreadable_task_table_still_reports_ok(self, monkeypatch, kv):
        class BadMem(dict):
            def keys(self):
                raise RuntimeError("table gone")

        flags = set()
        monkeypatch.setattr(gw, "_SCAN_CANCEL_FLAGS", flags)
        monkeypatch.setattr(gw, "_mem_kv", BadMem())
        out = gw.scan_stop_all()
        assert out["ok"] is True and out["stopped"] == 0 and "__ALL__" in flags
