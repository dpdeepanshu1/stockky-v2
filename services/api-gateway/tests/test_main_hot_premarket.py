"""tests/test_main_hot_premarket.py — coverage for api-gateway/main.py, slice 21 (lines 11335-11529)

Pass 79. The Hot Picks read routes and the Premarket bulk pre-feed:

* `GET /stockky-hot/status`, `/stockky-hot/result`, `/stockky-hot/premarket/status` — job merge, `has_result`,
  result-key precedence, the "no result yet" envelope;
* `POST /stockky-hot/premarket` — already-running guard, universe build failure / empty, running job row, queued
  worker, response envelope;
* `_run_premarket` (the background closure): bhavcopy baseline first, then the AngelOne LTP overlay only in a live
  session (`preopen` / `open` / `post`) — 50-symbol chunks, price-key priority, merge-into-existing write, rate
  pacing, per-chunk / per-write failure isolation — the final done message and the error row.

Everything downstream is faked: the redis get/set shims, the hot job helpers, `_build_scan_universe`,
`data_feed.run_bulk_yahoo_price_feed`, `httpx.post`, the feed store, the session-phase clock and `time.sleep`.
Nothing touches the network or a database. Findings are pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_hot_premarket.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
import httpx
from fastapi import BackgroundTasks


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code, self._data, self._raises = status, data, json_raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._data


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.puts = []
        self.put_raises = set()

    def get_symbol(self, s):
        return self.rows.get(s)

    def put_symbol(self, sym, row, ttl=None):
        if sym in self.put_raises:
            raise RuntimeError("write failed")
        self.puts.append((sym, row, ttl))


@pytest.fixture
def hp(monkeypatch):
    h = types.SimpleNamespace(
        redis={}, hot_job={"status": "idle"}, pm_job={"status": "idle"}, pm_sets=[],
        universe=["AAA", "BBB"], universe_raises=None, phase="closed",
        bulk_result={"tracked_stocks": 2, "message": "bulk ok"}, bulk_raises=None, bulk_calls=[],
        post_calls=[], post_handler=lambda url, json, timeout: FakeResp(200, {"quotes": []}),
        sleeps=[], store=FakeStore(), store_raises=False, order=[],
    )

    def universe():
        if h.universe_raises:
            raise h.universe_raises
        return list(h.universe)

    def bulk(syms, merge_existing=False):
        h.order.append("bulk")
        h.bulk_calls.append((list(syms), merge_existing))
        if h.bulk_raises:
            raise h.bulk_raises
        return h.bulk_result

    def post(url, json=None, timeout=None):
        h.order.append("post")
        h.post_calls.append((url, json, timeout))
        return h.post_handler(url, json, timeout)

    def feed_store():
        if h.store_raises:
            raise RuntimeError("store down")
        return h.store

    def pm_set(set_fn, get_fn, **kw):
        h.pm_sets.append(kw)
        h.pm_job = {**h.pm_job, **kw}

    monkeypatch.setattr(gw, "_redis_get", lambda k: h.redis.get(k))
    monkeypatch.setattr(gw, "hot_job_get", lambda g: dict(h.hot_job))
    monkeypatch.setattr(gw, "hot_premarket_job_get", lambda g: dict(h.pm_job))
    monkeypatch.setattr(gw, "hot_premarket_job_set", pm_set)
    monkeypatch.setattr(gw, "_build_scan_universe", universe)
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: h.phase)
    monkeypatch.setattr(gw, "_feed_store", feed_store)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.t/")
    monkeypatch.setattr(data_feed, "run_bulk_yahoo_price_feed", bulk)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(time, "sleep", lambda d: h.sleeps.append(d))
    return h


def worker(hp):
    """Launch the route and return (response, the queued _run_premarket callable + args)."""
    bt = BackgroundTasks()
    out = _run(gw.stockky_hot_premarket(bt))
    return out, bt


def run_worker(hp, **setup):
    for k, v in setup.items():
        setattr(hp, k, v)
    out, bt = worker(hp)
    hp.pm_sets.clear()
    t = bt.tasks[0]
    t.func(*t.args, **t.kwargs)
    return out


def final(hp):
    return hp.pm_sets[-1]


def quote(sym, **kw):
    return {"symbol": sym, "price": 100.0, **kw}


# ══ status / result routes ═══════════════════════════════════════════════════

class TestHotStatus:
    def test_merges_the_job_and_reports_no_result(self, hp):
        hp.hot_job = {"status": "running", "processed": 3}
        out = gw.stockky_hot_status()
        assert out == {"ok": True, "status": "running", "processed": 3, "has_result": False,
                       "result_generated_at": None}

    def test_result_key_supplies_has_result_and_generated_at(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"generated_at": "G", "picks": []}
        out = gw.stockky_hot_status()
        assert out["has_result"] is True and out["result_generated_at"] == "G"

    def test_result_key_wins_over_the_stocks_cache(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"generated_at": "NEW"}
        hp.redis[gw.HOT_STOCKS_CACHE_KEY] = {"generated_at": "OLD"}
        assert gw.stockky_hot_status()["result_generated_at"] == "NEW"

    def test_falls_back_to_the_stocks_cache(self, hp):
        hp.redis[gw.HOT_STOCKS_CACHE_KEY] = {"generated_at": "OLD"}
        out = gw.stockky_hot_status()
        assert out["has_result"] is True and out["result_generated_at"] == "OLD"

    def test_non_dict_cache_has_a_result_but_no_timestamp(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = ["x"]
        out = gw.stockky_hot_status()
        assert out["has_result"] is True and out["result_generated_at"] is None

    def test_missing_generated_at_is_none(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"picks": []}
        assert gw.stockky_hot_status()["result_generated_at"] is None

    def test_job_keys_can_override_ok(self, hp):
        hp.hot_job = {"ok": False}
        assert gw.stockky_hot_status()["ok"] is False                # NOT FIXED: the job dict is spread after ok=True


class TestHotResult:
    def test_no_cache_is_a_soft_failure(self, hp):
        assert gw.stockky_hot_result() == {
            "ok": False, "detail": "No Hot Picks result yet — run Search Hot Picks Stocks"}

    def test_cached_result_is_marked_ok_and_cached(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"picks": [1], "generated_at": "G"}
        assert gw.stockky_hot_result() == {"picks": [1], "generated_at": "G", "ok": True, "cached": True}

    def test_stored_ok_false_is_overridden(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"ok": False, "picks": []}
        assert gw.stockky_hot_result()["ok"] is True

    def test_result_key_wins_over_the_stocks_cache(self, hp):
        hp.redis[gw.HOT_RESULT_KEY] = {"v": "new"}
        hp.redis[gw.HOT_STOCKS_CACHE_KEY] = {"v": "old"}
        assert gw.stockky_hot_result()["v"] == "new"

    def test_falls_back_to_the_stocks_cache(self, hp):
        hp.redis[gw.HOT_STOCKS_CACHE_KEY] = {"v": "old"}
        assert gw.stockky_hot_result()["v"] == "old"

    def test_non_dict_cache_crashes_the_route(self, hp):
        """NOT FIXED: `{**cached, ...}` assumes a dict; a truthy non-dict value in redis is an unhandled TypeError."""
        hp.redis[gw.HOT_RESULT_KEY] = ["x"]
        with pytest.raises(TypeError):
            gw.stockky_hot_result()


class TestPremarketStatus:
    def test_merges_the_job(self, hp):
        hp.pm_job = {"status": "done", "processed": 5}
        assert gw.stockky_hot_premarket_status() == {"ok": True, "status": "done", "processed": 5}


# ══ POST /stockky-hot/premarket ══════════════════════════════════════════════

class TestPremarketRoute:
    def test_running_job_short_circuits(self, hp):
        hp.pm_job = {"status": "running", "processed": 4}
        out, bt = worker(hp)
        assert out == {"ok": True, "already_running": True, "status": "running", "processed": 4}
        assert bt.tasks == [] and hp.pm_sets == []

    @pytest.mark.parametrize("status", ["idle", "done", "error", None])
    def test_non_running_status_starts(self, hp, status):
        hp.pm_job = {"status": status}
        assert worker(hp)[0]["started"] is True

    def test_universe_failure_is_a_soft_error_truncated_to_160(self, hp):
        hp.universe_raises = RuntimeError("U" * 300)
        out, bt = worker(hp)
        assert out == {"ok": False, "error": "could not build universe: " + "U" * 160}
        assert bt.tasks == [] and hp.pm_sets == []

    @pytest.mark.parametrize("empty", [[], None])
    def test_empty_universe_is_a_soft_error(self, hp, empty):
        hp.universe = empty
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(gw, "_build_scan_universe", lambda: empty)
            out = _run(gw.stockky_hot_premarket(BackgroundTasks()))
        finally:
            monkey.undo()
        assert out == {"ok": False, "error": "scan universe is empty — nothing to pre-feed"}
        assert hp.pm_sets == []

    def test_start_writes_a_running_job_and_queues_the_worker(self, hp):
        hp.universe = ["AAA", "BBB", "CCC"]
        out, bt = worker(hp)
        assert out == {"ok": True, "started": True, "total": 3,
                       "message": "Premarket bulk pre-feed started for 3 eligible stocks"}
        job = hp.pm_sets[0]
        assert job["status"] == "running" and job["processed"] == 0 and job["total"] == 3
        assert job["message"] == "Pre-feeding 3 eligible stocks (bulk)…" and job["finished_at"] is None
        assert job["started_at"].endswith("+05:30")
        assert bt.tasks[0].args == (["AAA", "BBB", "CCC"],)

    def test_universe_is_built_off_the_event_loop_thread(self, hp, monkeypatch):
        import threading
        seen = []
        monkeypatch.setattr(gw, "_build_scan_universe",
                            lambda: seen.append(threading.current_thread() is threading.main_thread()) or ["A"])
        worker(hp)
        assert seen == [False]


# ══ _run_premarket: closed market ════════════════════════════════════════════

class TestPremarketWorkerClosed:
    def test_bhavcopy_only_with_merge_existing(self, hp):
        run_worker(hp)
        assert hp.bulk_calls == [(["AAA", "BBB"], True)] and hp.post_calls == [] and hp.sleeps == []
        assert final(hp) == {"status": "done", "processed": 2, "total": 2, "message": "bulk ok",
                             "finished_at": final(hp)["finished_at"]}
        assert final(hp)["finished_at"].endswith("+05:30")

    def test_message_falls_back_when_the_bulk_result_has_none(self, hp):
        run_worker(hp, bulk_result={"tracked_stocks": 5})
        assert final(hp)["message"] == "Pre-fed 5/2 stocks" and final(hp)["processed"] == 5

    def test_none_result_counts_zero(self, hp):
        run_worker(hp, bulk_result=None)
        assert final(hp)["processed"] == 0 and final(hp)["message"] == "Pre-fed 0/2 stocks"

    @pytest.mark.parametrize("phase", ["closed", "weekend", "holiday", "", None])
    def test_non_session_phases_skip_the_angelone_sweep(self, hp, phase):
        run_worker(hp, phase=phase)
        assert hp.post_calls == []

    def test_bulk_failure_marks_the_job_error_truncated_to_200(self, hp):
        run_worker(hp, bulk_raises=RuntimeError("E" * 300))
        job = final(hp)
        assert job["status"] == "error" and job["message"] == "E" * 200 and job["finished_at"].endswith("+05:30")
        assert hp.post_calls == []


# ══ _run_premarket: live session overlay ═════════════════════════════════════

class TestPremarketWorkerLive:
    @pytest.mark.parametrize("phase", ["preopen", "open", "post"])
    def test_live_phases_run_the_overlay(self, hp, phase):
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("AAA")]})
        run_worker(hp, phase=phase)
        assert len(hp.post_calls) == 1

    def test_bhavcopy_runs_before_the_angelone_sweep(self, hp):
        run_worker(hp, phase="open")
        assert hp.order == ["bulk", "post"]

    def test_chunks_of_50_with_pacing_after_each(self, hp):
        syms = [f"S{i}" for i in range(120)]
        run_worker(hp, phase="open", universe=syms)
        assert [len(c[1]["symbols"]) for c in hp.post_calls] == [50, 50, 20]
        assert hp.post_calls[0][1]["symbols"][0] == "S0" and hp.post_calls[2][1]["symbols"][-1] == "S119"
        assert hp.sleeps == [0.3, 0.3, 0.3]

    def test_request_shape(self, hp):
        run_worker(hp, phase="open")
        url, body, timeout = hp.post_calls[0]
        assert url == "http://md.t/quotes/bulk" and body == {"symbols": ["AAA", "BBB"]} and timeout == 12.0

    def test_live_ltp_is_merged_over_the_existing_row(self, hp):
        hp.store.rows["AAA"] = {"symbol": "AAA", "price": 90, "rsi": 55, "ltp": 90, "close": 90}
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [
            quote("AAA", price=101.5, source="angel", fetched_at="T1")]})
        run_worker(hp, phase="open")
        sym, row, ttl = hp.store.puts[0]
        assert sym == "AAA" and ttl == gw.DATA_FEED_TTL
        assert row == {"symbol": "AAA", "price": 101.5, "ltp": 101.5, "close": 101.5, "rsi": 55,
                       "source": "angel", "fetched_at": "T1", "price_refreshed_at": "T1"}

    def test_defaults_for_missing_source_and_timestamps(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("AAA")]})
        run_worker(hp, phase="open")
        row = hp.store.puts[0][1]
        assert row["source"] == "angelone_rest" and row["fetched_at"] == "" and row["price_refreshed_at"] == ""

    def test_new_symbol_is_created_from_an_empty_row(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("ZZZ")]})
        run_worker(hp, phase="open")
        assert hp.store.puts[0][1]["symbol"] == "ZZZ"

    def test_price_key_priority_and_fallthrough(self, hp):
        qs = [{"symbol": "A", "price": 0, "ltp": None, "close": "abc", "cmp": "7.5", "last_price": 9},
              {"symbol": "B", "price": 3, "ltp": 4},
              {"symbol": "C", "last_price": 6}]
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": qs})
        run_worker(hp, phase="open")
        assert {p[0]: p[1]["price"] for p in hp.store.puts} == {"A": 7.5, "B": 3.0, "C": 6.0}

    def test_unusable_quotes_are_skipped(self, hp):
        qs = [{"price": 5}, {"symbol": "", "price": 5}, {"symbol": "A"}, {"symbol": "B", "price": -1},
              {"symbol": "C", "price": "x"}]
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": qs})
        run_worker(hp, phase="open")
        assert hp.store.puts == [] and "0/2 live" in hp.pm_sets[1]["message"]

    def test_messages_and_hit_count(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("AAA"), quote("BBB")]})
        run_worker(hp, phase="open")
        assert hp.pm_sets[0] == {"message": "AngelOne LTP sweep: overlaying live prices on 2 bhavcopy-seeded symbols…"}
        assert hp.pm_sets[1] == {"message": "AngelOne overlay done (2/2 live prices written over bhavcopy baseline)."}
        assert final(hp)["message"] == "bulk ok · 2 live LTPs overlaid from AngelOne"

    def test_no_hits_means_no_suffix(self, hp):
        run_worker(hp, phase="open")
        assert final(hp)["message"] == "bulk ok"

    def test_non_200_chunk_is_skipped_and_paced(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(503)
        run_worker(hp, phase="open")
        assert hp.store.puts == [] and hp.sleeps == [0.3] and final(hp)["status"] == "done"

    def test_chunk_exception_is_isolated_and_paced(self, hp):
        calls = []

        def handler(u, j, t):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("net")
            return FakeResp(200, {"quotes": [quote("S60")]})

        run_worker(hp, phase="open", universe=[f"S{i}" for i in range(70)], post_handler=handler)
        assert [p[0] for p in hp.store.puts] == ["S60"] and hp.sleeps == [0.3, 0.3]

    def test_bad_json_chunk_is_isolated(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(200, json_raises=True)
        run_worker(hp, phase="open")
        assert final(hp)["status"] == "done"

    def test_missing_quotes_key_is_empty(self, hp):
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": None})
        run_worker(hp, phase="open")
        assert hp.store.puts == []

    def test_write_failure_is_swallowed_but_the_hit_is_still_counted(self, hp):
        """NOT FIXED: `angelone_hits` is incremented before the store write, so a failed write is reported as a
        live price "written over the baseline"."""
        hp.store.put_raises.add("AAA")
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("AAA"), quote("BBB")]})
        run_worker(hp, phase="open")
        assert [p[0] for p in hp.store.puts] == ["BBB"]
        assert final(hp)["message"] == "bulk ok · 2 live LTPs overlaid from AngelOne"

    def test_symbols_are_written_under_the_raw_quote_key(self, hp):
        """NOT FIXED: the overlay never strips `.NS` / upper-cases, so a quote keyed "tcs.ns" creates a second feed
        row instead of updating "TCS"."""
        hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [quote("tcs.ns")]})
        run_worker(hp, phase="open")
        assert hp.store.puts[0][0] == "tcs.ns"

    def test_sweep_setup_failure_is_swallowed_and_the_job_still_finishes(self, hp):
        hp.store_raises = True
        run_worker(hp, phase="open")
        assert hp.post_calls == [] and final(hp)["status"] == "done" and final(hp)["message"] == "bulk ok"

    def test_trailing_slash_on_the_market_url_is_stripped(self, hp, monkeypatch):
        monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://x.t///")
        run_worker(hp, phase="open")
        assert hp.post_calls[0][0].startswith("http://x.t") and "//quotes" not in hp.post_calls[0][0]

    def test_bulk_failure_skips_the_overlay_entirely(self, hp):
        run_worker(hp, phase="open", bulk_raises=RuntimeError("x"))
        assert hp.post_calls == [] and final(hp)["status"] == "error"
