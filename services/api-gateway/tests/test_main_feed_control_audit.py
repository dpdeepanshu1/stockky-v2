"""tests/test_main_feed_control_audit.py — coverage for api-gateway/main.py, slice 16 (lines 9461-10045)

Pass 74. The data-feed control + health-audit block that follows the ops/hard-reset tail:

* `POST /data-feed/start-bulk-feed` (+ 2 aliases) — symbol resolution cascade (universe -> feed index ->
  nifty list[:150]), cleaning / de-dupe, `running` job marker, BackgroundTasks vs daemon-thread fallback and
  the background worker's success / failure bookkeeping;
* `POST refresh-prepare-to-buy` — candidate band, per-symbol quote walk (key priority, v>0 gate, error list
  capped at 20, 0.3s pacing);
* `refill-additional` status / trigger, `/data-feed/meta`, `/data-feed/status` (stale / stop auto-heal);
* the audit memo (`_audit_cache_get/_put`), `audit-missing` route and `_compute_feed_audit`;
* `purge-over-cap`.

Everything downstream is faked: the feed store, `data_feed` / `refill_additional` helpers, the market-data
httpx client, `asyncio.sleep`, the kv index and the clock. Nothing touches the network or a database.
Findings are pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_feed_control_audit.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
import kv_cache
import refill_additional
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.symbols = []
        self.rows = {}
        self.list_raises = False
        self.row_raises = set()
        self.delete_raises = set()
        self.deleted = []
        self.set_jobs = []
        self.set_metas = []
        self.job_val = {}
        self.meta_val = {}
        self.count = 0
        self.meta_raises = False
        self.job_raises = False

    def list_symbols(self):
        if self.list_raises:
            raise RuntimeError("index down")
        return self.symbols

    def get_symbol(self, sym):
        if sym in self.row_raises:
            raise RuntimeError("row read failed")
        return self.rows.get(sym)

    def delete_symbol(self, sym):
        if sym in self.delete_raises:
            raise RuntimeError("delete failed")
        self.deleted.append(sym)

    def set_job(self, **kw):
        if self.job_raises:
            raise RuntimeError("store down")
        self.set_jobs.append(kw)
        self.job_val = {**self.job_val, **kw}
        return self.job_val

    def set_meta(self, **kw):
        if self.meta_raises:
            raise RuntimeError("meta down")
        self.set_metas.append(kw)
        self.meta_val = {**self.meta_val, **kw}
        return self.meta_val

    def job(self):
        return self.job_val

    def meta(self):
        return self.meta_val

    def count_symbols(self):
        if isinstance(self.count, Exception):
            raise self.count
        return self.count


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(gw, "_feed_store", lambda: s)
    return s


@pytest.fixture
def bulk(monkeypatch):
    b = types.SimpleNamespace(bulk_calls=[], result={"tracked_stocks": 3, "message": "bulk ok"},
                              result_raises=False, exc=None, stop_cleared=0, stop_raises=False)

    def run_bulk(syms, merge_existing=False):
        b.bulk_calls.append((list(syms), merge_existing))
        if b.result_raises:
            raise b.exc or RuntimeError("yahoo down")
        return b.result

    def clear_stop():
        b.stop_cleared += 1
        if b.stop_raises:
            raise RuntimeError("redis down")

    monkeypatch.setattr(data_feed, "run_bulk_yahoo_price_feed", run_bulk)
    monkeypatch.setattr(data_feed, "clear_data_feed_stop", clear_stop)
    return b


@pytest.fixture
def uni(monkeypatch):
    u = types.SimpleNamespace(symbols=[], raises=False, nifty=[], nifty_raises=False, calls=0)

    def build():
        u.calls += 1
        if u.raises:
            raise RuntimeError("universe down")
        return u.symbols

    def nifty():
        if u.nifty_raises:
            raise RuntimeError("nifty down")
        return u.nifty

    monkeypatch.setattr(gw, "_build_scan_universe", build)
    monkeypatch.setattr(gw, "_get_nifty_indices", nifty)
    return u


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


def _run_tasks(bt):
    for t in bt.tasks:
        t.func(*t.args, **t.kwargs)


# ══ start_bulk_feed ══════════════════════════════════════════════════════════

class TestStartBulkFeed:
    def test_universe_symbols_are_cleaned_and_deduped(self, store, bulk, uni):
        uni.symbols = ["tcs.ns", "INFY.BO", " tcs ", "", None, "Reliance"]
        bt = BackgroundTasks()
        out = _run(gw.start_bulk_feed(background_tasks=bt))
        assert out["ok"] is True and out["status"] == "started" and out["started"] is True
        assert out["total"] == 3 and "3 symbols" in out["message"]
        job = store.set_jobs[0]
        assert job["status"] == "running" and job["total"] == 3 and job["processed"] == 0
        assert job["ok_count"] == 0 and job["error_count"] == 0 and job["stop_requested"] is False
        assert "3 symbols" in job["message"] and job["started_at"].endswith("+05:30")
        assert bt.tasks[0].args == (["TCS", "INFY", "RELIANCE"],)
        assert bulk.stop_cleared == 1

    def test_use_universe_false_skips_universe_and_uses_feed_index(self, store, bulk, uni):
        store.symbols = ["AAA", "BBB"]
        uni.symbols = ["ZZZ"]
        bt = BackgroundTasks()
        out = _run(gw.start_bulk_feed(use_universe=False, background_tasks=bt))
        assert uni.calls == 0 and out["total"] == 2
        assert bt.tasks[0].args == (["AAA", "BBB"],)

    def test_universe_failure_falls_back_to_feed_index(self, store, bulk, uni):
        uni.raises = True
        store.symbols = ["AAA"]
        bt = BackgroundTasks()
        assert _run(gw.start_bulk_feed(background_tasks=bt))["total"] == 1

    def test_empty_universe_falls_back_to_feed_index(self, store, bulk, uni):
        uni.symbols = []
        store.symbols = ["AAA", "BBB"]
        assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["total"] == 2

    def test_feed_index_failure_falls_back_to_nifty_capped_at_150(self, store, bulk, uni):
        store.list_raises = True
        uni.nifty = [f"S{i}" for i in range(200)]
        bt = BackgroundTasks()
        out = _run(gw.start_bulk_feed(background_tasks=bt))
        assert out["total"] == 150 and bt.tasks[0].args[0][-1] == "S149"

    def test_empty_feed_index_falls_back_to_nifty(self, store, bulk, uni):
        uni.nifty = ["N1", "N2"]
        assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["total"] == 2

    def test_nothing_anywhere_is_a_400(self, store, bulk, uni):
        uni.nifty_raises = True
        with pytest.raises(HTTPException) as ei:
            _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
        assert ei.value.status_code == 400 and ei.value.detail == "No symbols available for bulk feed"

    def test_only_blank_symbols_is_a_400(self, store, bulk, uni):
        uni.symbols = ["", ".NS", "  "]
        store.symbols = []
        uni.nifty = []
        with pytest.raises(HTTPException) as ei:
            _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
        assert ei.value.status_code == 400

    def test_clear_stop_failure_is_swallowed(self, store, bulk, uni):
        bulk.stop_raises = True
        uni.symbols = ["AAA"]
        assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["ok"] is True

    def test_running_marker_failure_is_swallowed(self, store, bulk, uni):
        store.job_raises = True
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        assert _run(gw.start_bulk_feed(background_tasks=bt))["ok"] is True
        assert len(bt.tasks) == 1

    def test_worker_success_writes_done_job_and_meta(self, store, bulk, uni):
        uni.symbols = ["AAA", "BBB"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        _run_tasks(bt)
        assert bulk.bulk_calls == [(["AAA", "BBB"], True)]
        job = store.set_jobs[0]
        assert job["status"] == "done" and job["message"] == "bulk ok"
        assert job["processed"] == 3 and job["ok_count"] == 3 and job["total"] == 2
        assert job["error_count"] == 0 and job["finished_at"].endswith("+05:30")
        meta = store.set_metas[0]
        assert meta["last_count"] == 3 and meta["last_message"] == "bulk ok"
        assert meta["source"] == "yfinance_bulk_bg" and meta["last_success_at"].endswith("+05:30")

    def test_worker_message_falls_back_when_result_has_none(self, store, bulk, uni):
        bulk.result = {"tracked_stocks": 7}
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        _run_tasks(bt)
        assert store.set_jobs[0]["message"] == "Bulk feed done: 7"
        assert store.set_metas[0]["last_message"] is None

    def test_falsy_result_counts_zero(self, store, bulk, uni):
        bulk.result = None
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        _run_tasks(bt)
        assert store.set_jobs[0]["processed"] == 0 and store.set_jobs[0]["message"] == "Bulk feed done: 0"

    def test_worker_failure_marks_job_error_with_200_char_truncation(self, store, bulk, uni):
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        bulk.result_raises = True
        _run_tasks(bt)
        job = store.set_jobs[0]
        assert job["status"] == "error" and job["message"] == "yahoo down" and "finished_at" in job
        assert store.set_metas == []

    def test_worker_error_message_is_truncated_to_200(self, store, bulk, uni):
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        bulk.result_raises, bulk.exc = True, RuntimeError("L" * 400)
        _run_tasks(bt)
        assert store.set_jobs[0]["message"] == "L" * 200

    def test_failure_marker_failure_is_swallowed(self, store, bulk, uni):
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        bulk.result_raises = True
        store.job_raises = True
        _run_tasks(bt)                                   # must not raise

    def test_store_failure_on_success_path_falls_into_the_error_handler(self, store, bulk, uni):
        uni.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.start_bulk_feed(background_tasks=bt))
        store.set_jobs.clear()
        store.meta_raises = True                          # set_job(done) ok, set_meta blows up
        _run_tasks(bt)
        assert [j["status"] for j in store.set_jobs] == ["done", "error"]
        assert store.set_jobs[-1]["message"] == "meta down"

    def test_direct_call_without_background_tasks_uses_a_daemon_thread(self, store, bulk, uni, monkeypatch):
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target, self.args, self.daemon = target, args, daemon

            def start(self):
                started.append(self)

        # use_universe=False keeps the route off asyncio.to_thread, whose executor also builds
        # threading.Thread objects and would hang forever against this fake.
        store.symbols = ["TCS", "INFY"]
        with monkeypatch.context() as m:
            m.setattr(threading, "Thread", FakeThread)
            out = _run(gw.start_bulk_feed(use_universe=False))
        assert out["ok"] is True and len(started) == 1
        t = started[0]
        assert t.daemon is True and t.args == (["TCS", "INFY"],)
        t.target(*t.args)                                  # the worker is the real _bulk_worker
        assert bulk.bulk_calls == [(["TCS", "INFY"], True)]

    def test_routed_on_all_three_paths_and_query_flags(self, store, bulk, uni, tc):
        uni.symbols = ["AAA", "BBB"]
        for path in ("/data-feed/start-bulk-feed", "/api/data-feed/start-bulk-feed", "/api/feed/start-bulk-feed"):
            r = tc.post(path)
            assert r.status_code == 200 and r.json()["total"] == 2
        assert len(bulk.bulk_calls) == 3                   # TestClient runs BackgroundTasks after the response
        store.symbols = ["ONLY"]
        r = tc.post("/data-feed/start-bulk-feed?force=false&use_universe=false")
        assert r.status_code == 200 and r.json()["total"] == 1

    def test_get_is_not_a_405(self, store, bulk, uni, tc):
        n = len(bulk.bulk_calls)
        # NOT FIXED: GET is not a 405 — `/data-feed/{symbol}` captures it. Nothing is started.
        assert tc.get("/data-feed/start-bulk-feed").status_code != 405
        assert len(bulk.bulk_calls) == n


# ══ refresh_prepare_to_buy ═══════════════════════════════════════════════════

class FakeQuoteResp:
    def __init__(self, status=200, body=None, content=True):
        self.status_code = status
        self._body = body
        self.content = b"x" if content else b""

    def json(self):
        return self._body


class FakeAsyncClient:
    routes = {}
    calls = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        FakeAsyncClient.calls.append((url, timeout))
        out = FakeAsyncClient.routes.get(url.rsplit("/", 1)[-1])
        if isinstance(out, Exception):
            raise out
        return out if out is not None else FakeQuoteResp(404)


@pytest.fixture
def ptb(monkeypatch):
    p = types.SimpleNamespace(candidates=[], cand_raises=None, cand_kw=None, patched=[], patch_ok=True, sleeps=[])
    FakeAsyncClient.routes = {}
    FakeAsyncClient.calls = []

    def find(min_score=0, max_score=0):
        p.cand_kw = (min_score, max_score)
        if p.cand_raises:
            raise p.cand_raises
        return p.candidates

    def patch(symbol, price):
        p.patched.append((symbol, price))
        return p.patch_ok

    async def fake_sleep(d):
        p.sleeps.append(d)

    monkeypatch.setattr(data_feed, "find_prepare_to_buy_candidates", find)
    monkeypatch.setattr(data_feed, "patch_feed_price", patch)
    monkeypatch.setattr(gw.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.test/")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    p.routes = FakeAsyncClient.routes
    return p


class TestPrepareToBuy:
    def test_candidate_lookup_failure_is_a_500_truncated_to_200(self, ptb):
        ptb.cand_raises = RuntimeError("E" * 300)
        with pytest.raises(HTTPException) as ei:
            _run(gw.refresh_prepare_to_buy())
        assert ei.value.status_code == 500 and ei.value.detail == "E" * 200

    def test_no_candidates_returns_empty_envelope(self, ptb):
        out = _run(gw.refresh_prepare_to_buy(min_score=50, max_score=60))
        assert out == {"status": "success", "refreshed_count": 0, "symbols": [],
                       "message": "No Prepare-to-Buy candidates in score band",
                       "min_score": 50, "max_score": 60}
        assert ptb.cand_kw == (50, 60)
        assert FakeAsyncClient.calls == []

    def test_default_band_is_58_to_68(self, ptb):
        _run(gw.refresh_prepare_to_buy())
        assert ptb.cand_kw == (58.0, 68.0)

    def test_quote_walk_updates_and_paces(self, ptb):
        ptb.candidates = ["AAA", "BBB"]
        ptb.routes["AAA"] = FakeQuoteResp(200, {"cmp": 101.5})
        ptb.routes["BBB"] = FakeQuoteResp(200, {"ltp": 55})
        out = _run(gw.refresh_prepare_to_buy())
        assert out["status"] == "success" and out["refreshed_count"] == 2
        assert out["symbols"] == ["AAA", "BBB"] and out["updated"] == ["AAA", "BBB"] and out["errors"] == []
        assert out["message"] == "Refreshed 2/2 Prepare-to-Buy quotes"
        assert ptb.patched == [("AAA", 101.5), ("BBB", 55.0)]
        assert ptb.sleeps == [0.3, 0.3]
        assert FakeAsyncClient.calls[0] == ("http://md.test/quote/AAA", 3.0)

    def test_base_url_trailing_slash_is_stripped(self, ptb):
        ptb.candidates = ["AAA"]
        _run(gw.refresh_prepare_to_buy())
        assert FakeAsyncClient.calls[0][0] == "http://md.test/quote/AAA"

    def test_key_priority_is_cmp_first(self, ptb):
        ptb.candidates = ["AAA"]
        ptb.routes["AAA"] = FakeQuoteResp(200, {"price": 9, "cmp": 7})
        _run(gw.refresh_prepare_to_buy())
        assert ptb.patched == [("AAA", 7.0)]

    def test_zero_and_unparseable_values_fall_through_to_the_next_key(self, ptb):
        ptb.candidates = ["AAA"]
        ptb.routes["AAA"] = FakeQuoteResp(200, {"cmp": 0, "price": "abc", "ltp": None, "close": 12.5})
        _run(gw.refresh_prepare_to_buy())
        assert ptb.patched == [("AAA", 12.5)]

    def test_zero_price_is_not_patched(self, ptb):
        ptb.candidates = ["AAA"]
        ptb.routes["AAA"] = FakeQuoteResp(200, {"cmp": 0, "price": 0})
        out = _run(gw.refresh_prepare_to_buy())
        assert ptb.patched == [] and out["updated"] == [] and out["errors"] == []

    def test_non_dict_and_empty_bodies_are_ignored(self, ptb):
        ptb.candidates = ["AAA", "BBB"]
        ptb.routes["AAA"] = FakeQuoteResp(200, [1, 2])
        ptb.routes["BBB"] = FakeQuoteResp(200, None, content=False)
        out = _run(gw.refresh_prepare_to_buy())
        assert ptb.patched == [] and out["updated"] == []

    def test_patch_returning_false_is_not_counted(self, ptb):
        ptb.candidates = ["AAA"]
        ptb.patch_ok = False
        ptb.routes["AAA"] = FakeQuoteResp(200, {"cmp": 5})
        out = _run(gw.refresh_prepare_to_buy())
        assert ptb.patched == [("AAA", 5.0)] and out["refreshed_count"] == 0
        assert out["symbols"] == ["AAA"]

    def test_non_200_is_recorded_as_a_status_error(self, ptb):
        ptb.candidates = ["AAA"]
        ptb.routes["AAA"] = FakeQuoteResp(503)
        out = _run(gw.refresh_prepare_to_buy())
        assert out["errors"] == [{"symbol": "AAA", "status": 503}]

    def test_exception_is_recorded_truncated_to_120_and_walk_continues(self, ptb):
        ptb.candidates = ["AAA", "BBB"]
        ptb.routes["AAA"] = RuntimeError("X" * 200)
        ptb.routes["BBB"] = FakeQuoteResp(200, {"cmp": 3})
        out = _run(gw.refresh_prepare_to_buy())
        assert out["errors"] == [{"symbol": "AAA", "error": "X" * 120}]
        assert out["updated"] == ["BBB"] and ptb.sleeps == [0.3, 0.3]

    def test_error_list_is_capped_at_20(self, ptb):
        ptb.candidates = [f"S{i}" for i in range(25)]
        out = _run(gw.refresh_prepare_to_buy())            # every quote 404s
        assert len(out["errors"]) == 20 and out["refreshed_count"] == 0
        assert len(out["symbols"]) == 25

    def test_routed_on_all_three_paths_post_only(self, ptb, tc):
        ptb.candidates = ["AAA"]
        ptb.routes["AAA"] = FakeQuoteResp(200, {"cmp": 1})
        for path in ("/data-feed/refresh-prepare-to-buy", "/api/data-feed/refresh-prepare-to-buy",
                     "/api/feed/refresh-prepare-to-buy"):
            r = tc.post(path + "?min_score=10&max_score=20")
            assert r.status_code == 200 and r.json()["min_score"] == 10
        assert ptb.cand_kw == (10.0, 20.0)


# ══ refill-additional ════════════════════════════════════════════════════════

@pytest.fixture
def refill(monkeypatch):
    r = types.SimpleNamespace(job={"status": "idle"}, job_raises=False, set_calls=[], set_raises=False,
                              run_calls=[], run_raises=None)

    def get_job():
        if r.job_raises:
            raise RuntimeError("J" * 300)
        return dict(r.job)

    def set_job(**kw):
        r.set_calls.append(kw)
        if r.set_raises:
            raise RuntimeError("set failed")
        return kw

    def run(syms):
        r.run_calls.append(list(syms))
        if r.run_raises:
            raise r.run_raises
        return {}

    monkeypatch.setattr(refill_additional, "get_refill_job", get_job)
    monkeypatch.setattr(refill_additional, "_set_job", set_job)
    monkeypatch.setattr(refill_additional, "run_refill_additional", run)
    return r


class TestRefillStatus:
    def test_ok_envelope_merges_the_job(self, refill):
        refill.job = {"status": "running", "processed": 4}
        assert gw.data_feed_refill_status() == {"ok": True, "status": "running", "processed": 4}

    def test_failure_returns_idle_envelope_truncated_to_200(self, refill):
        refill.job_raises = True
        out = gw.data_feed_refill_status()
        assert out == {"ok": False, "error": "J" * 200, "status": "idle"}

    def test_routed_on_both_paths(self, refill, tc):
        for path in ("/data-feed/refill-additional/status", "/api/data-feed/refill-additional/status"):
            assert tc.get(path).json()["ok"] is True


class TestRefillTrigger:
    def test_already_running_without_force_short_circuits(self, store, refill):
        refill.job = {"status": "running", "total": 9}
        out = _run(gw.data_feed_refill_additional(BackgroundTasks(), force=False))
        assert out == {"ok": True, "already_running": True, "status": "running", "total": 9}
        assert refill.set_calls == []

    def test_running_with_force_starts_anyway(self, store, refill):
        refill.job = {"status": "running"}
        store.symbols = ["AAA"]
        out = _run(gw.data_feed_refill_additional(BackgroundTasks(), force=True))
        assert out["status"] == "running" and out["total"] == 1

    def test_idle_job_starts_regardless_of_force_flag(self, store, refill):
        store.symbols = ["AAA"]
        assert _run(gw.data_feed_refill_additional(BackgroundTasks(), force=False))["total"] == 1

    def test_symbols_from_store_are_normalised_and_worker_runs_them(self, store, refill):
        store.symbols = ["tcs.ns", "INFY.BO", "", None]
        bt = BackgroundTasks()
        out = _run(gw.data_feed_refill_additional(bt))
        assert out == {"ok": True, "status": "running", "total": 2,
                       "message": "Refill Additional Data started for 2 symbols"}
        call = refill.set_calls[0]
        assert call["status"] == "running" and call["total"] == 2 and call["processed"] == 0
        assert call["ok_count"] == 0 and call["error_count"] == 0 and "2 symbols" in call["message"]
        _run_tasks(bt)
        assert refill.run_calls == [["TCS", "INFY"]]

    def test_store_failure_falls_back_to_universe(self, store, refill, uni):
        store.list_raises = True
        uni.symbols = ["aaa.ns", "BBB"]
        out = _run(gw.data_feed_refill_additional(BackgroundTasks()))
        assert out["total"] == 2

    def test_empty_store_falls_back_to_universe(self, store, refill, uni):
        uni.symbols = ["AAA"]
        assert _run(gw.data_feed_refill_additional(BackgroundTasks()))["total"] == 1

    def test_universe_failure_means_zero_symbols_not_an_error(self, store, refill, uni):
        uni.raises = True
        out = _run(gw.data_feed_refill_additional(BackgroundTasks()))
        assert out["ok"] is True and out["total"] == 0

    def test_none_universe_is_zero_symbols(self, store, refill, uni):
        uni.symbols = None
        assert _run(gw.data_feed_refill_additional(BackgroundTasks()))["total"] == 0

    def test_worker_failure_marks_job_error_truncated_to_240(self, store, refill):
        store.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.data_feed_refill_additional(bt))
        refill.set_calls.clear()
        refill.run_raises = RuntimeError("W" * 400)
        _run_tasks(bt)
        assert refill.set_calls == [{"status": "error", "message": "W" * 240}]

    def test_worker_failure_marker_failure_is_swallowed(self, store, refill):
        store.symbols = ["AAA"]
        bt = BackgroundTasks()
        _run(gw.data_feed_refill_additional(bt))
        refill.run_raises = RuntimeError("x")
        refill.set_raises = True
        _run_tasks(bt)                                     # must not raise

    def test_setup_failure_is_a_500_truncated_to_240(self, store, refill):
        refill.job_raises = True
        with pytest.raises(HTTPException) as ei:
            _run(gw.data_feed_refill_additional(BackgroundTasks()))
        assert ei.value.status_code == 500 and ei.value.detail == "J" * 240

    def test_routed_on_both_paths_post_only(self, store, refill, tc):
        store.symbols = ["AAA"]
        for path in ("/data-feed/refill-additional", "/api/data-feed/refill-additional"):
            r = tc.post(path)
            assert r.status_code == 200 and r.json()["total"] == 1
        assert len(refill.run_calls) == 2
        # NOT FIXED: GET is not a 405 — `/data-feed/{symbol}` captures it. Nothing is started.
        assert tc.get("/data-feed/refill-additional").status_code != 405
        assert len(refill.run_calls) == 2


# ══ /data-feed/meta ══════════════════════════════════════════════════════════

class TestFeedMeta:
    def test_returns_meta_and_job(self, store):
        store.meta_val = {"last_count": 5}
        store.job_val = {"status": "done"}
        assert gw.data_feed_meta() == {"ok": True, "meta": {"last_count": 5}, "job": {"status": "done"}}

    def test_routed_on_both_paths(self, store, tc):
        for path in ("/data-feed/meta", "/api/data-feed/meta"):
            assert tc.get(path).json()["ok"] is True


# ══ /data-feed/status ════════════════════════════════════════════════════════

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=gw.IST)


class FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(gw, "datetime", FrozenDT)
    monkeypatch.delenv("DATA_FEED_STALE_SEC", raising=False)


def _ago(sec):
    return (NOW - timedelta(seconds=sec)).isoformat()


class TestFeedStatus:
    def test_idle_job_is_passed_through_untouched(self, store, clock):
        store.job_val = {"status": "done", "finished_at": "F"}
        store.meta_val = {"last_success_at": "M"}
        store.count = 11
        out = gw.data_feed_status()
        assert out["ok"] is True and out["status"] == "done" and out["stocks_in_feed"] == 11
        assert out["last_count"] == 11 and out["last_success"] == "M" and out["last_success_at"] == "M"
        assert out["meta"] == {"last_success_at": "M"}
        assert store.set_jobs == [] and store.set_metas == []

    def test_last_success_falls_back_to_job_finished_at(self, store, clock):
        store.job_val = {"status": "done", "finished_at": "F"}
        assert gw.data_feed_status()["last_success"] == "F"

    def test_count_failure_falls_back_to_meta_last_count(self, store, clock):
        store.count = RuntimeError("down")
        store.meta_val = {"last_count": 8}
        store.job_val = {"ok_count": 2}
        assert gw.data_feed_status()["stocks_in_feed"] == 8

    def test_zero_count_falls_back_to_meta_then_job(self, store, clock):
        store.count = 0
        store.meta_val = {"last_count": 0}
        store.job_val = {"ok_count": 2}
        assert gw.data_feed_status()["stocks_in_feed"] == 2

    def test_positive_count_is_never_overridden(self, store, clock):
        store.count = 3
        store.meta_val = {"last_count": 99}
        assert gw.data_feed_status()["last_count"] == 3

    def test_fresh_running_job_is_left_alone(self, store, clock):
        store.job_val = {"status": "running", "updated_at": _ago(10)}
        assert gw.data_feed_status()["status"] == "running"
        assert store.set_jobs == []

    def test_stale_running_job_is_auto_stopped(self, store, clock):
        store.job_val = {"status": "running", "updated_at": _ago(901), "processed": 40, "total": 100,
                         "ok_count": 38, "error_count": 2,
                         "checkpoint": {"cursor": 42, "done": ["A", "B"], "universe": ["A", "B", "C"]}}
        out = gw.data_feed_status()
        assert out["status"] == "stopped" and out["processed"] == 42 and out["stop_requested"] is False
        assert out["message"].startswith("Auto-stopped (stale/sleep) at 42/100 — committed 38 fed stocks at ")
        assert out["checkpoint"] == {"cursor": 42, "done": ["A", "B"], "universe": ["A", "B", "C"]}
        assert out["errors"] == 2 and out["error_count"] == 2 and out["finished_at"] == NOW.isoformat()
        meta = store.set_metas[0]
        assert meta["source"] == "stop_or_stale" and meta["universe_size"] == 100
        assert meta["partial"] is True and meta["last_count"] == 38 and meta["last_errors"] == 2
        assert out["meta"]["last_success_at"] == NOW.isoformat()

    def test_stale_threshold_is_strictly_greater_than(self, store, clock, monkeypatch):
        monkeypatch.setenv("DATA_FEED_STALE_SEC", "100")
        store.job_val = {"status": "running", "updated_at": _ago(100)}
        assert gw.data_feed_status()["status"] == "running"
        store.job_val = {"status": "running", "updated_at": _ago(101)}
        assert gw.data_feed_status()["status"] == "stopped"

    def test_default_threshold_is_900_seconds(self, store, clock):
        store.job_val = {"status": "running", "updated_at": _ago(900)}
        assert gw.data_feed_status()["status"] == "running"
        store.job_val = {"status": "running", "updated_at": _ago(901)}
        assert gw.data_feed_status()["status"] == "stopped"

    def test_stop_requested_commits_a_stopped_checkpoint_even_when_fresh(self, store, clock):
        store.job_val = {"status": "running", "updated_at": _ago(1), "stop_requested": True,
                         "processed": 10, "total": 10, "ok_count": 10}
        out = gw.data_feed_status()
        assert out["status"] == "stopped"
        assert out["message"].startswith("Stopped at 10/10 — committed 10 fed stocks at ")
        assert store.set_metas[0]["partial"] is False

    def test_stop_requested_without_any_timestamp(self, store, clock):
        store.job_val = {"status": "running", "stop_requested": True, "processed": 3, "total": 9}
        out = gw.data_feed_status()
        assert out["status"] == "stopped" and out["processed"] == 3 and store.set_metas[0]["partial"] is True

    def test_running_without_timestamps_or_stop_is_left_alone(self, store, clock):
        store.job_val = {"status": "running", "elapsed_sec": 5000}
        assert gw.data_feed_status()["status"] == "running"

    def test_updated_at_wins_over_started_at(self, store, clock):
        store.job_val = {"status": "running", "updated_at": _ago(5000), "started_at": _ago(1)}
        assert gw.data_feed_status()["status"] == "stopped"

    def test_resumed_at_wins_over_started_at(self, store, clock):
        store.job_val = {"status": "running", "resumed_at": _ago(1), "started_at": _ago(5000)}
        assert gw.data_feed_status()["status"] == "running"

    def test_unparseable_timestamp_falls_through_to_the_next_key(self, store, clock):
        store.job_val = {"status": "running", "updated_at": "not-a-date", "started_at": _ago(5000)}
        assert gw.data_feed_status()["status"] == "stopped"

    def test_z_suffix_is_parsed_as_utc(self, store, clock):
        z = (NOW - timedelta(seconds=5000)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.job_val = {"status": "running", "updated_at": z}
        assert gw.data_feed_status()["status"] == "stopped"

    def test_naive_timestamp_is_read_as_ist(self, store, clock):
        naive = (NOW - timedelta(seconds=1000)).replace(tzinfo=None).isoformat()
        store.job_val = {"status": "running", "updated_at": naive}
        # as IST that's 1000s old (> 900 -> stopped); read as UTC it would be in the future (age < 0).
        assert gw.data_feed_status()["status"] == "stopped"

    def test_cursor_prefers_checkpoint_then_processed(self, store, clock):
        store.job_val = {"status": "running", "stop_requested": True, "processed": 7, "total": 20,
                         "checkpoint": {"cursor": 12}}
        assert gw.data_feed_status()["processed"] == 12
        store.job_val = {"status": "running", "stop_requested": True, "processed": 7, "total": 20,
                         "checkpoint": {"cursor": 0}}
        assert gw.data_feed_status()["processed"] == 7

    def test_non_dict_checkpoint_is_treated_as_empty(self, store, clock):
        store.job_val = {"status": "running", "stop_requested": True, "processed": 4, "total": 8,
                         "checkpoint": "garbage"}
        out = gw.data_feed_status()
        assert out["checkpoint"] == {"cursor": 4, "done": [], "universe": []}

    def test_ok_and_error_counts_fall_back(self, store, clock):
        store.meta_val = {"last_count": 6}
        store.job_val = {"status": "running", "stop_requested": True, "processed": 1, "total": 2,
                         "errors": 4}
        out = gw.data_feed_status()
        assert out["ok_count"] == 6 and out["error_count"] == 4 and out["errors"] == 4

    def test_heal_failure_is_swallowed_and_job_returned_as_is(self, store, clock):
        store.job_val = {"status": "running", "stop_requested": True, "total": 5}
        store.meta_raises = True
        out = gw.data_feed_status()
        assert out["ok"] is True and out["status"] == "running"

    def test_routed_on_both_paths(self, store, clock, tc):
        for path in ("/data-feed/status", "/api/data-feed/status"):
            assert tc.get(path).json()["ok"] is True


# ══ audit memo ═══════════════════════════════════════════════════════════════

@pytest.fixture
def memo(monkeypatch):
    monkeypatch.setattr(gw, "_AUDIT_MEMO", {})
    monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 20.0)
    monkeypatch.setattr(gw, "time", types.SimpleNamespace(time=lambda: 1000.0))
    return gw._AUDIT_MEMO


class TestAuditMemo:
    def test_absent_key_is_a_miss(self, memo):
        assert gw._audit_cache_get("k") is None

    def test_put_returns_fresh_flag_and_get_returns_cached_with_age(self, memo):
        assert gw._audit_cache_put("k", {"a": 1}) == {"a": 1, "cached": False}
        memo["k"] = (1000.0 - 5.26, {"a": 1})
        assert gw._audit_cache_get("k") == {"a": 1, "cached": True, "cache_age_sec": 5.3}

    def test_non_dict_payloads_pass_straight_through(self, memo):
        assert gw._audit_cache_put("k", [1, 2]) == [1, 2]
        assert gw._audit_cache_get("k") == [1, 2]

    def test_entry_exactly_at_ttl_is_stale(self, memo):
        memo["k"] = (1000.0 - 20.0, {"a": 1})
        assert gw._audit_cache_get("k") is None
        memo["k"] = (1000.0 - 19.9, {"a": 1})
        assert gw._audit_cache_get("k")["cached"] is True

    def test_ttl_of_zero_disables_the_memo(self, memo, monkeypatch):
        monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 0.0)
        memo["k"] = (1000.0, {"a": 1})
        assert gw._audit_cache_get("k") is None
        monkeypatch.setattr(gw, "AUDIT_TTL_SEC", -5.0)
        assert gw._audit_cache_get("k") is None


# ══ audit-missing route + _compute_feed_audit ════════════════════════════════

def _good(price=100):
    return {"price": price, "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0}


@pytest.fixture
def audit(store, monkeypatch):
    monkeypatch.setattr(gw, "_AUDIT_MEMO", {})
    monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 20.0)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
    kv = types.SimpleNamespace(idx=None, raises=False)

    def kv_get(key):
        if kv.raises:
            raise RuntimeError("kv down")
        assert key == "stockky:data_feed:index"
        return kv.idx

    monkeypatch.setattr(kv_cache, "kv_get", kv_get)
    return kv


class TestComputeFeedAudit:
    def test_empty_everywhere(self, audit, store):
        out = gw._compute_feed_audit(500)
        assert out["total_universe"] == 0 and out["health_score"] == 0.0 and out["fully_populated"] == 0
        assert out["message"] == "No feed symbols tracked yet — run Data Feed first."
        assert out["required_fields"] == ["price", "rsi", "pe_ratio", "roce", "sentiment_score"]
        assert out["incomplete_stocks"] == [] and out["over_cap_stocks"] == [] and out["cached"] is False

    def test_kv_index_dict_fallback(self, audit, store):
        audit.idx = {"symbols": ["aaa", "BBB.NS", ""]}
        store.rows = {"AAA": _good(), "BBB": _good()}
        out = gw._compute_feed_audit(500)
        assert out["total_universe"] == 2 and out["fully_populated"] == 2

    def test_kv_index_list_fallback(self, audit, store):
        audit.idx = ["aaa", "bbb", None]
        store.rows = {"AAA": _good(), "BBB": _good()}
        assert gw._compute_feed_audit(500)["total_universe"] == 2

    def test_kv_index_of_unexpected_shape_is_ignored(self, audit, store):
        audit.idx = "garbage"
        assert gw._compute_feed_audit(500)["total_universe"] == 0

    def test_kv_failure_is_swallowed(self, audit, store):
        audit.raises = True
        assert gw._compute_feed_audit(500)["total_universe"] == 0

    def test_store_listing_failure_falls_back_to_kv(self, audit, store):
        store.list_raises = True
        audit.idx = {"symbols": ["AAA"]}
        store.rows = {"AAA": _good()}
        assert gw._compute_feed_audit(500)["total_universe"] == 1

    def test_store_symbols_win_over_kv_index(self, audit, store):
        store.symbols = ["AAA"]
        audit.idx = {"symbols": ["X", "Y", "Z"]}
        assert gw._compute_feed_audit(500)["total_universe"] == 1

    def test_symbols_are_cleaned_deduped_and_system_keys_dropped(self, audit, store):
        store.symbols = ["tcs.ns", "TCS", "INFY.BO", "SYSTEM:meta", "system:x", "", None, " hdfc "]
        out = gw._compute_feed_audit(500)
        assert out["total_universe"] == 3
        assert sorted(s["symbol"] for s in out["incomplete_stocks"]) == ["HDFC", "INFY", "TCS"]

    def test_complete_and_incomplete_rows_and_health(self, audit, store):
        store.symbols = ["AAA", "BBB", "CCC"]
        store.rows = {"AAA": _good(), "BBB": {"price": 10}, "CCC": {}}
        out = gw._compute_feed_audit(500)
        assert out["fully_populated"] == 1 and out["incomplete_count"] == 2 and out["total_universe"] == 3
        assert out["health_score"] == 33.3
        assert out["message"] == "Health 33.3% · 1/3 complete"
        assert out["over_cap_count"] == 0
        assert [s["symbol"] for s in out["incomplete_stocks"]] == ["CCC", "BBB"]   # most missing first
        assert out["incomplete_stocks"][1]["missing_fields"] == ["rsi", "pe_ratio", "roce", "sentiment_score"]
        assert out["incomplete_stocks"][1]["current_price"] == 10.0

    def test_equal_missing_counts_sort_by_symbol(self, audit, store):
        store.symbols = ["ZZZ", "AAA", "MMM"]
        store.rows = {}
        out = gw._compute_feed_audit(500)
        assert [s["symbol"] for s in out["incomplete_stocks"]] == ["AAA", "MMM", "ZZZ"]

    def test_all_complete_is_100_percent(self, audit, store):
        store.symbols = ["AAA"]
        store.rows = {"AAA": _good()}
        out = gw._compute_feed_audit(500)
        assert out["health_score"] == 100.0 and out["message"] == "Health 100.0% · 1/1 complete"

    def test_limit_slices_incomplete_but_count_is_the_full_total(self, audit, store):
        store.symbols = [f"S{i:02d}" for i in range(5)]
        out = gw._compute_feed_audit(2)
        assert len(out["incomplete_stocks"]) == 2 and out["incomplete_count"] == 5

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_means_no_slice(self, audit, store, limit):
        store.symbols = [f"S{i}" for i in range(4)]
        out = gw._compute_feed_audit(limit)
        assert len(out["incomplete_stocks"]) == 4

    def test_row_read_failure_and_non_dict_rows_count_as_empty(self, audit, store):
        store.symbols = ["AAA", "BBB", "CCC"]
        store.rows = {"AAA": _good(), "BBB": ["not", "a", "dict"], "CCC": None}
        store.row_raises.add("AAA")
        out = gw._compute_feed_audit(500)
        assert out["fully_populated"] == 0 and out["incomplete_count"] == 3
        assert all(len(s["missing_fields"]) == 5 for s in out["incomplete_stocks"])

    def test_updated_at_chain(self, audit, store):
        store.symbols = ["AAA", "BBB", "CCC", "DDD"]
        store.rows = {
            "AAA": {"updated_at": "U", "repair_updated_at": "R", "fed_at": "F"},
            "BBB": {"repair_updated_at": "R", "fed_at": "F"},
            "CCC": {"fed_at": "F"},
            "DDD": {},
        }
        out = {s["symbol"]: s["updated_at"] for s in gw._compute_feed_audit(500)["incomplete_stocks"]}
        assert out == {"AAA": "U", "BBB": "R", "CCC": "F", "DDD": ""}

    def test_over_cap_rows_are_split_out_of_incomplete(self, audit, store, monkeypatch):
        monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
        store.symbols = ["CHEAP", "PRICEY", "BOSCHLTD"]
        store.rows = {"CHEAP": _good(100), "PRICEY": _good(6000), "BOSCHLTD": {}}
        out = gw._compute_feed_audit(500)
        assert out["over_cap_count"] == 2 and out["fully_populated"] == 1 and out["incomplete_count"] == 0
        assert out["over_cap_stocks"] == [{"symbol": "PRICEY", "current_price": 6000.0},
                                          {"symbol": "BOSCHLTD", "current_price": 0.0}]
        assert out["message"] == "Health 33.3% · 1/3 complete · 2 over ₹5000 cap (use Purge)"

    def test_over_cap_list_is_capped_at_200_but_count_is_not(self, audit, store, monkeypatch):
        monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
        store.symbols = [f"S{i}" for i in range(250)]
        store.rows = {s: _good(9000) for s in store.symbols}
        out = gw._compute_feed_audit(500)
        assert out["over_cap_count"] == 250 and len(out["over_cap_stocks"]) == 200

    def test_result_is_memoised_under_a_per_limit_key(self, audit, store):
        store.symbols = ["AAA"]
        gw._compute_feed_audit(7)
        assert "feed_audit:7" in gw._AUDIT_MEMO and "feed_audit:500" not in gw._AUDIT_MEMO


class TestAuditRoute:
    def test_miss_computes_then_second_call_is_cached(self, audit, store, tc):
        store.symbols = ["AAA"]
        store.rows = {"AAA": _good()}
        first = tc.get("/data-feed/audit-missing").json()
        assert first["cached"] is False and first["total_universe"] == 1
        store.symbols = ["AAA", "BBB"]                     # would change the answer if recomputed
        second = tc.get("/data-feed/audit-missing").json()
        assert second["cached"] is True and second["total_universe"] == 1 and "cache_age_sec" in second

    def test_cache_false_forces_a_recount(self, audit, store, tc):
        store.symbols = ["AAA"]
        tc.get("/api/feed/audit-missing")
        store.symbols = ["AAA", "BBB"]
        out = tc.get("/api/data-feed/audit-missing?cache=false").json()
        assert out["cached"] is False and out["total_universe"] == 2

    def test_limit_is_part_of_the_cache_key(self, audit, store, tc):
        store.symbols = [f"S{i}" for i in range(5)]
        a = tc.get("/data-feed/audit-missing?limit=2").json()
        b = tc.get("/data-feed/audit-missing?limit=4").json()
        assert len(a["incomplete_stocks"]) == 2 and len(b["incomplete_stocks"]) == 4 and b["cached"] is False

    def test_direct_call_runs_the_walk_in_the_threadpool(self, audit, store):
        store.symbols = ["AAA"]
        out = _run(gw.audit_missing_feed_data(limit=10, cache=False))
        assert out["total_universe"] == 1 and out["cached"] is False

    def test_defaults_are_limit_500_and_cache_on(self, audit, store):
        store.symbols = ["AAA"]
        _run(gw.audit_missing_feed_data())
        assert "feed_audit:500" in gw._AUDIT_MEMO
        store.symbols = ["AAA", "BBB"]
        assert _run(gw.audit_missing_feed_data())["cached"] is True


# ══ purge-over-cap ═══════════════════════════════════════════════════════════

@pytest.fixture
def purge(store, monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
    return store


class TestPurgeOverCap:
    def test_deletes_only_over_cap_rows(self, purge):
        purge.symbols = ["BIG1", "SMALL", "BIG2"]
        purge.rows = {"BIG1": {"price": 9000}, "SMALL": {"price": 100}, "BIG2": {"close": "6,500"}}
        out = _run(gw.purge_over_cap_feed_symbols())
        assert out["ok"] is True and out["purged_count"] == 2 and out["purged_symbols"] == ["BIG1", "BIG2"]
        assert purge.deleted == ["BIG1", "BIG2"]
        assert out["message"] == "Removed 2 symbol(s) above ₹5000 from the feed."

    def test_known_expensive_symbol_is_purged_by_name_even_with_no_price(self, purge):
        purge.symbols = ["MRF"]
        purge.rows = {"MRF": {}}
        assert _run(gw.purge_over_cap_feed_symbols())["purged_symbols"] == ["MRF"]

    def test_no_cap_configured_purges_nothing(self, purge, monkeypatch):
        monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        purge.symbols = ["MRF"]
        purge.rows = {"MRF": {"price": 99999}}
        out = _run(gw.purge_over_cap_feed_symbols())
        assert out["purged_count"] == 0 and purge.deleted == []
        assert out["message"] == "Removed 0 symbol(s) above ₹0 from the feed."

    def test_listing_failure_purges_nothing(self, purge):
        purge.list_raises = True
        out = _run(gw.purge_over_cap_feed_symbols())
        assert out["purged_count"] == 0 and out["purged_symbols"] == []

    def test_row_fetch_failure_still_reaches_the_cap_check_with_an_empty_row(self, purge):
        """NOT FIXED: a failed `get_symbol` is swallowed as `{}` and the symbol is still judged by the cap
        gate, so a transient read error can delete a row that the by-name denylist flags."""
        purge.symbols = ["MRF", "SMALL"]
        purge.rows = {"MRF": {"price": 1}, "SMALL": {"price": 1}}
        purge.row_raises.update({"MRF", "SMALL"})
        out = _run(gw.purge_over_cap_feed_symbols())
        assert out["purged_symbols"] == ["MRF"]

    def test_missing_rows_become_empty_dicts(self, purge):
        purge.symbols = ["GHOST", "MRF"]
        purge.rows = {}
        assert _run(gw.purge_over_cap_feed_symbols())["purged_symbols"] == ["MRF"]

    def test_non_dict_rows_are_never_purged(self, purge):
        purge.symbols = ["MRF"]
        purge.rows = {"MRF": ["not", "a", "dict"]}
        assert _run(gw.purge_over_cap_feed_symbols())["purged_count"] == 0

    def test_delete_failure_is_not_counted_and_the_sweep_continues(self, purge):
        purge.symbols = ["BIG1", "BIG2"]
        purge.rows = {"BIG1": {"price": 9000}, "BIG2": {"price": 9000}}
        purge.delete_raises.add("BIG1")
        out = _run(gw.purge_over_cap_feed_symbols())
        assert out["purged_symbols"] == ["BIG2"] and purge.deleted == ["BIG2"]

    def test_routed_on_both_paths_post_only(self, purge, tc):
        purge.symbols = ["BIG"]
        purge.rows = {"BIG": {"price": 9000}}
        for path in ("/api/feed/purge-over-cap", "/data-feed/purge-over-cap"):
            assert tc.post(path).status_code == 200
        purge.deleted.clear()
        # NOT FIXED: GET is not a 405 — `/data-feed/{symbol}` captures it. Nothing is purged.
        assert tc.get("/data-feed/purge-over-cap").status_code != 405
        assert purge.deleted == []
