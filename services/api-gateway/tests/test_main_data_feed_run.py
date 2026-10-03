"""tests/test_main_data_feed_run.py — coverage for api-gateway/main.py, slice 20 (lines 10914-11245)

Pass 78. `POST /data-feed/run` (+ alias) — the full-universe feed launcher and its background `_run` worker:

* the already-running guard (force / resume bypass it);
* universe resolution: `_build_scan_universe` -> nifty list[:150] fallback, `DATA_FEED_MAX_SYMBOLS` cap,
  `.NS`/`.BO` cleaning, `only_new` filtering (and its "nothing new" envelope);
* the three modes — `start`, `refresh` (force), `resume` (cursor from checkpoint / processed, clamp, finished-run
  reset) — and the job rows each one writes;
* `_run`: PHASE 0 chunked bulk quotes (threshold, env switches, failure), the `DATA_FEED_SKIP_SEQUENTIAL_FUND`
  early finish, PHASE 1 per-symbol fund + events fill (skip-cached, success / error counting, batch warm-up and
  pacing), the cooperative stop triggers (process flag, job flag, job status), and the final done / meta rows.

Everything downstream is faked: the feed store, the scan-universe builders, the bulk Yahoo feed, the shared httpx
client, `extract_feed_payload`, `_warm_upstream_services`, the stop flag and `asyncio.sleep`. Nothing touches the
network or a database. Findings are pinned as current behaviour and marked ``NOT FIXED``; the ones fixed afterwards (empty-universe 400,
resume skipping the remaining symbols) now pin the fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_data_feed_run.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
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
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

_ENV_KEYS = ("DATA_FEED_MAX_SYMBOLS", "DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", "DATA_FEED_SKIP_SEQUENTIAL_FUND",
             "DATA_FEED_FUND_CONCURRENCY", "DATA_FEED_BATCH_SIZE")


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.job_val = {}
        self.set_jobs = []
        self.metas = []
        self.rows = {}
        self.get_raises = set()
        self.puts = []
        self.put_raises = set()

    def job(self):
        return self.job_val

    def set_job(self, **kw):
        self.set_jobs.append(kw)
        self.job_val = {**self.job_val, **kw}
        return self.job_val

    def set_meta(self, **kw):
        self.metas.append(kw)

    def get_symbol(self, s):
        if s in self.get_raises:
            raise RuntimeError("read failed")
        return self.rows.get(s)

    def put_symbol(self, sym, row, ttl=None):
        if sym in self.put_raises:
            raise RuntimeError("put failed")
        self.puts.append((sym, row, ttl))


class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code, self._data, self._raises = status, data, json_raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._data


class FakeClient:
    def __init__(self):
        self.routes = {}
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        for frag, out in self.routes.items():
            if frag in url:
                if isinstance(out, Exception):
                    raise out
                return out
        return FakeResp(404)

    def urls(self):
        return [u for u, _ in self.calls]


@pytest.fixture
def fr(monkeypatch):
    f = types.SimpleNamespace(
        store=FakeStore(), universe=["AAA", "BBB", "CCC"], nifty=[], universe_raises=False, universe_calls=0,
        bulk_result={"tracked_stocks": 0, "symbols": []}, bulk_raises=None, bulk_calls=[],
        client=FakeClient(), warm_calls=[], warm_raises=False, on_warm=None, sleeps=[],
        stop_flag_after=None, stop_checks=0, clear_calls=0, extract_calls=[], extract_raises=False,
    )

    def build():
        f.universe_calls += 1
        if f.universe_raises:
            raise RuntimeError("universe down")
        return list(f.universe)

    def bulk(universe, flag):
        f.bulk_calls.append((list(universe), flag, threading.current_thread() is threading.main_thread()))
        if f.bulk_raises:
            raise f.bulk_raises
        return f.bulk_result

    async def warm(client):
        f.warm_calls.append(client)
        if f.on_warm:
            f.on_warm()
        if f.warm_raises:
            raise RuntimeError("warm failed")

    async def fake_sleep(d):
        f.sleeps.append(d)

    def stop_requested():
        f.stop_checks += 1
        return f.stop_flag_after is not None and f.stop_checks > f.stop_flag_after

    def clear():
        f.clear_calls += 1

    def extract(base, fund, events):
        f.extract_calls.append((base, fund, events))
        if f.extract_raises:
            raise RuntimeError("extract boom")
        return {"symbol": base, "f": fund, "e": events}

    monkeypatch.setattr(gw, "_feed_store", lambda: f.store)
    monkeypatch.setattr(gw, "_build_scan_universe", build)
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: f.nifty)
    monkeypatch.setattr(data_feed, "run_bulk_yahoo_price_feed_cached", bulk)
    monkeypatch.setattr(gw, "_get_http_client", lambda: f.client)
    monkeypatch.setattr(gw, "_warm_upstream_services", warm)
    monkeypatch.setattr(gw, "FUNDAMENTAL_URL", "http://fund.t")
    monkeypatch.setattr(gw, "EVENT_URL", "http://ev.t")
    monkeypatch.setattr(gw, "extract_feed_payload", extract)
    monkeypatch.setattr(gw, "data_feed_stop_requested", stop_requested)
    monkeypatch.setattr(gw, "clear_data_feed_stop", clear)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    f.mp = monkeypatch
    return f


def seq(fr, **extra):
    """Env that lets `_run` reach the per-symbol loop (no early bulk-complete / skip-sequential finish)."""
    fr.mp.setenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", "0")
    fr.mp.setenv("DATA_FEED_SKIP_SEQUENTIAL_FUND", "0")
    for k, v in extra.items():
        fr.mp.setenv(k, str(v))


def start(fr, **kw):
    bt = BackgroundTasks()
    out = _run(gw.data_feed_run(bt, **kw))
    return out, bt


def execute(bt):
    for t in bt.tasks:
        _run(t.func(*t.args, **t.kwargs))


def go(fr, **kw):
    out, bt = start(fr, **kw)
    execute(bt)
    return out


def ok_routes(fr, *symbols):
    for s in symbols:
        fr.client.routes[f"/analyze/{s}"] = FakeResp(200, {"fund": s})
        fr.client.routes[f"/events/{s}"] = FakeResp(200, {"ev": s})


# ══ launcher: guard + universe ═══════════════════════════════════════════════

class TestLauncherGuard:
    def test_running_job_short_circuits(self, fr):
        fr.store.job_val = {"status": "running", "processed": 4, "total": 9}
        out, bt = start(fr)
        assert out == {"ok": True, "already_running": True, "status": "running", "processed": 4, "total": 9}
        assert bt.tasks == [] and fr.universe_calls == 0 and fr.store.set_jobs == []

    def test_force_bypasses_the_guard(self, fr):
        fr.store.job_val = {"status": "running"}
        out, bt = start(fr, force=True)
        assert out["started"] is True and out["mode"] == "refresh" and len(bt.tasks) == 1

    def test_resume_bypasses_the_guard(self, fr):
        fr.store.job_val = {"status": "running"}
        out, _ = start(fr, resume=True)
        assert out["mode"] == "resume"

    @pytest.mark.parametrize("status", ["idle", "done", "stopped", "stopping", "error", None])
    def test_non_running_status_starts(self, fr, status):
        fr.store.job_val = {"status": status}
        assert start(fr)[0]["started"] is True


class TestUniverse:
    def test_scan_universe_is_cleaned_and_uppercased(self, fr):
        fr.universe = ["tcs.ns", "INFY.BO", "Reliance"]
        out, bt = start(fr)
        assert out["total"] == 3
        assert fr.store.set_jobs[0]["checkpoint"]["universe"] == ["TCS", "INFY", "RELIANCE"]

    def test_universe_is_built_off_the_event_loop_thread(self, fr, monkeypatch):
        seen = []

        def build():
            seen.append(threading.current_thread() is threading.main_thread())
            return ["AAA"]

        monkeypatch.setattr(gw, "_build_scan_universe", build)
        start(fr)
        assert seen == [False]

    def test_empty_universe_falls_back_to_nifty_capped_at_150(self, fr):
        fr.universe = []
        fr.nifty = [f"S{i}" for i in range(200)]
        out, _ = start(fr)
        assert out["total"] == 150
        assert fr.store.set_jobs[0]["checkpoint"]["universe"][-1] == "S149"

    @pytest.mark.parametrize("nifty", [None, []])
    def test_empty_everywhere_is_a_400_and_starts_nothing(self, fr, nifty):
        """FIXED: there was no "no symbols" guard (unlike start-bulk-feed's 400), so an empty universe reported
        `started: True`, wrote a running job with total 0 and queued a worker. It is now a 400 before any
        job is written or task queued (a nifty fallback of None counts as empty too)."""
        fr.universe, fr.nifty = [], nifty
        bt = BackgroundTasks()
        with pytest.raises(HTTPException) as ei:
            _run(gw.data_feed_run(bt))
        assert ei.value.status_code == 400 and ei.value.detail == "No symbols available for data feed"
        assert bt.tasks == [] and fr.store.set_jobs == []

    @pytest.mark.parametrize("kw", [{"force": True}, {"resume": True}, {"only_new": True}])
    def test_the_empty_guard_applies_to_every_mode(self, fr, kw):
        fr.universe, fr.nifty = [], []
        with pytest.raises(HTTPException) as ei:
            _run(gw.data_feed_run(BackgroundTasks(), **kw))
        assert ei.value.status_code == 400 and fr.store.set_jobs == []

    def test_a_universe_that_only_normalises_to_something_still_starts(self, fr):
        fr.universe = ["tcs.ns"]
        assert start(fr)[0]["total"] == 1

    def test_max_symbols_env_truncates(self, fr):
        fr.mp.setenv("DATA_FEED_MAX_SYMBOLS", "2")
        assert start(fr)[0]["total"] == 2

    @pytest.mark.parametrize("val", ["0", "", "-5"])
    def test_non_positive_max_symbols_means_no_cap(self, fr, val):
        fr.mp.setenv("DATA_FEED_MAX_SYMBOLS", val)
        assert start(fr)[0]["total"] == 3

    def test_max_symbols_applies_to_the_nifty_fallback_too(self, fr):
        fr.universe = []
        fr.nifty = ["A", "B", "C", "D"]
        fr.mp.setenv("DATA_FEED_MAX_SYMBOLS", "3")
        assert start(fr)[0]["total"] == 3


class TestOnlyNew:
    def test_only_symbols_without_a_feed_entry_are_kept(self, fr):
        fr.store.rows = {"AAA": {"x": 1}}
        out, _ = start(fr, only_new=True)
        assert out["total"] == 2 and fr.store.set_jobs[0]["checkpoint"]["universe"] == ["BBB", "CCC"]

    def test_empty_entry_counts_as_new(self, fr):
        fr.store.rows = {"AAA": {}}
        assert start(fr, only_new=True)[0]["total"] == 3

    def test_read_failure_counts_as_new(self, fr):
        fr.store.rows = {"AAA": {"x": 1}, "BBB": {"x": 1}, "CCC": {"x": 1}}
        fr.store.get_raises.add("BBB")
        out, _ = start(fr, only_new=True)
        assert fr.store.set_jobs[0]["checkpoint"]["universe"] == ["BBB"]

    def test_nothing_new_returns_a_not_started_envelope(self, fr):
        fr.store.rows = {s: {"x": 1} for s in fr.universe}
        out, bt = start(fr, only_new=True)
        assert out == {"ok": True, "started": False, "mode": "only_new", "total": 0,
                       "message": "No new symbols — all universe members already have feed data. "
                                  "Use full feed only if you need a refresh."}
        assert bt.tasks == [] and fr.store.set_jobs == []

    def test_only_new_uses_the_cleaned_symbol_for_the_lookup(self, fr):
        fr.universe = ["aaa.ns"]
        fr.store.rows = {"AAA": {"x": 1}}
        assert start(fr, only_new=True)[0]["started"] is False


# ══ launcher: modes ══════════════════════════════════════════════════════════

class TestModes:
    def test_default_is_a_fresh_start(self, fr):
        out, bt = start(fr)
        assert out == {"ok": True, "started": True, "mode": "start", "resume_from": 0, "total": 3,
                       "message": "Data feed started for 3 scan-universe stocks"}
        job = fr.store.set_jobs[0]
        assert job["status"] == "running" and job["processed"] == 0 and job["total"] == 3
        assert job["message"] == "Feeding 3 symbols…" and job["elapsed_sec"] == 0
        assert job["estimated_remaining_sec"] is None and job["errors"] == 0 and job["ok_count"] == 0
        assert job["error_count"] == 0 and job["stop_requested"] is False
        assert job["checkpoint"] == {"cursor": 0, "done": [], "universe": ["AAA", "BBB", "CCC"]}
        assert job["started_at"].endswith("+05:30")
        assert bt.tasks[0].args == (0, 0, 0, set())

    def test_force_is_mode_refresh_and_ignores_old_progress(self, fr):
        fr.store.job_val = {"status": "done", "ok_count": 9, "error_count": 2,
                            "checkpoint": {"cursor": 2, "done": ["AAA"]}}
        out, bt = start(fr, force=True)
        assert out["mode"] == "refresh" and out["resume_from"] == 0
        assert bt.tasks[0].args == (0, 0, 0, set())
        assert fr.store.set_jobs[0]["checkpoint"]["done"] == []

    def test_force_wins_over_resume(self, fr):
        fr.store.job_val = {"checkpoint": {"cursor": 2}}
        out, bt = start(fr, force=True, resume=True)
        assert out["mode"] == "refresh" and bt.tasks[0].args[0] == 0

    def test_non_resume_ignores_a_stored_checkpoint(self, fr):
        fr.store.job_val = {"status": "stopped", "checkpoint": {"cursor": 2, "done": ["AAA"]}}
        out, bt = start(fr)
        assert out["resume_from"] == 0 and bt.tasks[0].args == (0, 0, 0, set())

    def test_resume_uses_the_checkpoint_cursor_and_carries_counts(self, fr):
        fr.store.job_val = {"status": "stopped", "started_at": "S", "ok_count": 4, "error_count": 1,
                            "checkpoint": {"cursor": 2, "done": ["AAA", "BBB"]}}
        out, bt = start(fr, resume=True)
        assert out == {"ok": True, "started": True, "mode": "resume", "resume_from": 2, "total": 3,
                       "message": "Data feed resumed from 2/3"}
        job = fr.store.set_jobs[0]
        assert job["processed"] == 2 and job["message"] == "Resuming from 2/3…" and job["started_at"] == "S"
        assert job["resumed_at"].endswith("+05:30") and job["ok_count"] == 4 and job["error_count"] == 1
        assert job["errors"] == 1 and job["stop_requested"] is False
        assert job["checkpoint"]["cursor"] == 2 and sorted(job["checkpoint"]["done"]) == ["AAA", "BBB"]
        assert bt.tasks[0].args == (2, 4, 1, {"AAA", "BBB"})

    def test_resume_without_started_at_stamps_a_new_one(self, fr):
        fr.store.job_val = {"checkpoint": {"cursor": 1}}
        start(fr, resume=True)
        assert fr.store.set_jobs[0]["started_at"].endswith("+05:30")

    def test_cursor_falls_back_to_processed(self, fr):
        fr.store.job_val = {"status": "stopped", "processed": 1}
        assert start(fr, resume=True)[0]["resume_from"] == 1

    def test_checkpoint_cursor_wins_over_processed_when_both_are_set(self, fr):
        fr.store.job_val = {"status": "stopped", "processed": 1, "checkpoint": {"cursor": 2}}
        assert start(fr, resume=True)[0]["resume_from"] == 2

    def test_zero_checkpoint_cursor_falls_back_to_processed(self, fr):
        fr.store.job_val = {"status": "stopped", "processed": 2, "checkpoint": {"cursor": 0}}
        assert start(fr, resume=True)[0]["resume_from"] == 2

    def test_cursor_is_clamped_into_the_universe(self, fr):
        fr.store.job_val = {"status": "running", "processed": 99}
        assert start(fr, resume=True)[0]["resume_from"] == 3
        fr.store.job_val = {"status": "running", "processed": -4}
        assert start(fr, resume=True)[0]["resume_from"] == 0

    @pytest.mark.parametrize("status", ["stopped", "error", "idle", "done"])
    def test_finished_run_with_a_full_cursor_restarts_from_zero(self, fr, status):
        fr.store.job_val = {"status": status, "ok_count": 5, "error_count": 1,
                            "checkpoint": {"cursor": 3, "done": ["AAA", "BBB", "CCC"]}}
        out, bt = start(fr, resume=True)
        assert out["resume_from"] == 0 and out["mode"] == "resume"
        assert bt.tasks[0].args == (0, 0, 0, set())
        assert fr.store.set_jobs[0]["checkpoint"]["done"] == [] and fr.store.set_jobs[0]["message"] == "Resuming from 0/3…"

    def test_running_job_with_a_full_cursor_is_not_reset(self, fr):
        fr.store.job_val = {"status": "running", "ok_count": 5, "checkpoint": {"cursor": 3, "done": ["AAA"]}}
        out, bt = start(fr, resume=True)
        assert out["resume_from"] == 3 and bt.tasks[0].args == (3, 5, 0, {"AAA"})

    def test_cursor_beyond_a_shrunk_universe_clamps_then_resets(self, fr):
        fr.store.job_val = {"status": "stopped", "checkpoint": {"cursor": 50, "done": ["X"]}}
        assert start(fr, resume=True)[0]["resume_from"] == 0

    def test_non_dict_checkpoint_is_empty(self, fr):
        fr.store.job_val = {"status": "stopped", "processed": 1, "checkpoint": "junk"}
        out, bt = start(fr, resume=True)
        assert out["resume_from"] == 1 and bt.tasks[0].args[3] == set()

    def test_routed_on_both_paths_with_query_flags(self, fr):
        client = TestClient(gw.app, raise_server_exceptions=False)
        for path in ("/data-feed/run", "/api/data-feed/run"):
            fr.store.job_val = {}
            r = client.post(path + "?force=true")
            assert r.status_code == 200 and r.json()["mode"] == "refresh"
        fr.store.rows = {s: {"x": 1} for s in fr.universe}
        assert client.post("/data-feed/run?only_new=true").json()["started"] is False


# ══ _run PHASE 0: bulk seed ══════════════════════════════════════════════════

class TestBulkPhase:
    def test_bulk_runs_off_thread_with_the_whole_universe_and_merge_flag(self, fr):
        seq(fr)
        fr.universe = ["tcs.ns", "INFY"]
        fr.bulk_result = {"tracked_stocks": 0, "symbols": []}
        go(fr)
        assert fr.bulk_calls == [(["TCS", "INFY"], True, False)]
        assert fr.clear_calls == 1

    def test_progress_rows_around_the_bulk_call(self, fr):
        seq(fr)
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["aaa.ns", "BBB.BO"]}
        go(fr)
        pre, post = fr.store.set_jobs[1], fr.store.set_jobs[2]
        assert pre["message"] == "Bulk Yahoo quotes for 3 symbols (chunks of 50)…" and pre["processed"] == 0
        assert pre["updated_at"].endswith("+05:30")
        assert post["message"] == "Bulk 5-field seed done: 2/3 (price+RSI local; PE/ROCE/sentiment baseline)"
        assert post["processed"] == 2 and post["ok_count"] == 2
        assert post["checkpoint"]["cursor"] == 0 and sorted(post["checkpoint"]["done"]) == ["AAA", "BBB"]

    def test_bulk_covered_symbols_are_skipped_by_the_loop(self, fr):
        seq(fr)
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["AAA", "BBB"]}
        ok_routes(fr, "CCC")
        go(fr)
        assert fr.client.urls() == ["http://fund.t/analyze/CCC", "http://ev.t/events/CCC"]
        assert [m["message"] for m in fr.store.set_jobs if m.get("message", "").startswith("Skip cached")] == [
            "Skip cached AAA (1/3)", "Skip cached BBB (2/3)"]

    def test_ok_count_is_the_max_of_prior_and_bulk(self, fr):
        seq(fr)
        fr.bulk_result = {"tracked_stocks": 1, "symbols": []}
        fr.store.job_val = {"status": "done"}
        bt = BackgroundTasks()
        _run(gw.data_feed_run(bt, force=True))
        t = bt.tasks[0]
        _run(t.func(0, 7, 0, set()))                                 # ok0=7 > bulk's 1
        assert fr.store.set_jobs[2]["ok_count"] == 7

    def test_none_bulk_result_means_nothing_saved(self, fr):
        seq(fr)
        fr.bulk_result = None
        go(fr)
        assert fr.store.set_jobs[2]["processed"] == 0

    def test_bulk_failure_is_swallowed_and_the_loop_still_runs(self, fr):
        seq(fr)
        fr.bulk_raises = RuntimeError("yahoo down")
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        assert len(fr.store.puts) == 3 and fr.store.job_val["status"] == "done"

    def test_symbols_key_missing_is_fine(self, fr):
        seq(fr)
        fr.bulk_result = {"tracked_stocks": 1}
        go(fr)
        assert fr.store.set_jobs[2]["checkpoint"]["done"] == []

    @pytest.mark.parametrize("start_at,done", [(1, set()), (0, {"AAA"})])
    def test_phase_is_skipped_when_resuming_with_progress(self, fr, start_at, done):
        seq(fr)
        _run(self._launch_and_run(fr, start_at, done))
        assert fr.bulk_calls == []

    @staticmethod
    async def _launch_and_run(fr, start_at, done):
        bt = BackgroundTasks()
        await gw.data_feed_run(bt, force=True)
        await bt.tasks[0].func(start_at, 0, 0, done)

    # -- early "bulk-complete" finish -----------------------------------------
    def test_enough_coverage_finishes_the_run_after_the_bulk_phase(self, fr):
        fr.universe = [f"S{i}" for i in range(10)]
        fr.bulk_result = {"tracked_stocks": 3, "symbols": ["S0", "S1", "S2"]}      # 3 >= int(0.35*10)
        go(fr)
        meta = fr.store.metas[0]
        assert meta["source"] == "bulk_5field" and meta["last_count"] == 3 and meta["last_errors"] == 0
        assert meta["universe_size"] == 10 and meta["partial"] is True
        assert meta["last_message"].startswith("Data feed bulk-complete for 3 stocks at ")
        assert meta["last_message"].endswith("(local RSI + baseline PE/ROCE/sentiment; use Repair for real fundamentals)")
        job = fr.store.job_val
        assert job["status"] == "done" and job["processed"] == 10 and job["ok_count"] == 3 and job["error_count"] == 0
        assert job["checkpoint"]["cursor"] == 10 and sorted(job["checkpoint"]["done"]) == ["S0", "S1", "S2"]
        assert job["message"] == meta["last_message"] and job["finished_at"] == meta["last_success_at"]
        assert fr.client.calls == []

    def test_full_coverage_is_not_partial(self, fr):
        fr.bulk_result = {"tracked_stocks": 3, "symbols": ["AAA", "BBB", "CCC"]}
        go(fr)
        assert fr.store.metas[0]["partial"] is False

    def test_below_the_35_percent_threshold_it_does_not_bulk_complete(self, fr):
        fr.universe = [f"S{i}" for i in range(10)]
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["S0", "S1"]}
        go(fr)
        assert all(m.get("source") != "bulk_5field" for m in fr.store.metas)

    def test_threshold_is_at_least_one_symbol(self, fr):
        fr.universe = ["ONLY"]
        fr.bulk_result = {"tracked_stocks": 0, "symbols": []}
        go(fr)
        assert fr.store.metas[0].get("source") == "manual_or_api_or_scheduler"   # 0 < max(1, 0) -> no early finish

    @pytest.mark.parametrize("val", ["1", "true", "YES", " On "])
    def test_truthy_skip_values(self, fr, val):
        fr.mp.setenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", val)
        fr.bulk_result = {"tracked_stocks": 3, "symbols": []}
        go(fr)
        assert fr.store.metas[0]["source"] == "bulk_5field"

    @pytest.mark.parametrize("val", ["", "   "])
    def test_blank_skip_value_means_unset_so_the_default_early_finish_applies(self, fr, val):
        # group 75: `DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK=` in a .env used to read as "off"
        fr.mp.setenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", val)
        fr.bulk_result = {"tracked_stocks": 3, "symbols": []}
        go(fr)
        assert fr.store.metas[0]["source"] == "bulk_5field"

    @pytest.mark.parametrize("val", ["0", "no", "off", "false", " 0 "])
    def test_falsy_skip_values_disable_the_early_finish(self, fr, val):
        fr.mp.setenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", val)
        fr.mp.setenv("DATA_FEED_SKIP_SEQUENTIAL_FUND", "0")
        fr.bulk_result = {"tracked_stocks": 3, "symbols": []}
        go(fr)
        assert all(m["source"] != "bulk_5field" for m in fr.store.metas)


# ══ _run: skip-sequential early finish ═══════════════════════════════════════

class TestSkipSequential:
    def test_default_stops_after_the_bulk_seed_when_anything_was_saved(self, fr):
        fr.universe = [f"S{i}" for i in range(10)]
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["S0", "S1"]}        # below 35% -> no bulk-complete
        go(fr)
        job = fr.store.job_val
        assert job["status"] == "done" and job["processed"] == 10 and job["ok_count"] == 2
        assert job["message"] == "Data feed stopped after bulk seed (2 rows) — sequential fund skipped"
        meta = fr.store.metas[0]
        assert meta["last_count"] == 2 and meta["partial"] is True and meta["universe_size"] == 10
        assert job["finished_at"] == meta["last_success_at"] and fr.client.calls == []

    def test_nothing_saved_falls_through_to_the_loop(self, fr):
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        assert len(fr.store.puts) == 3 and fr.store.metas[-1]["source"] == "manual_or_api_or_scheduler"

    def test_flag_off_runs_the_loop_even_with_a_bulk_seed(self, fr):
        fr.mp.setenv("DATA_FEED_SKIP_SEQUENTIAL_FUND", "0")
        fr.universe = [f"S{i}" for i in range(10)]
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["S0", "S1"]}
        ok_routes(fr, *[f"S{i}" for i in range(2, 10)])
        go(fr)
        assert len(fr.store.puts) == 8

    def test_resuming_with_progress_feeds_the_remaining_symbols(self, fr):
        """FIXED: with the default env, a resumed run whose carried `ok_count` was > 0 hit the skip-sequential
        branch immediately and declared the job done, so the remaining symbols were never fed. The early
        finish now applies only to a run that did the bulk seed itself; a resume continues the sequential
        fill from its cursor."""
        fr.store.job_val = {"status": "stopped", "ok_count": 2,
                            "checkpoint": {"cursor": 2, "done": ["AAA", "BBB"]}}
        ok_routes(fr, "CCC")
        out = go(fr, resume=True)
        assert out["resume_from"] == 2
        assert [p[0] for p in fr.store.puts] == ["CCC"]
        job = fr.store.job_val
        assert job["status"] == "done" and job["ok_count"] == 3 and job["checkpoint"]["cursor"] == 3
        assert sorted(job["checkpoint"]["done"]) == ["AAA", "BBB", "CCC"]
        assert job["message"].startswith("Data feed successfully for 3 stocks at ")
        assert fr.store.metas[-1]["partial"] is False

    def test_resuming_never_reruns_the_bulk_seed_or_the_early_finish_message(self, fr):
        fr.store.job_val = {"status": "stopped", "ok_count": 1,
                            "checkpoint": {"cursor": 1, "done": ["AAA"]}}
        ok_routes(fr, "BBB", "CCC")
        go(fr, resume=True)
        assert fr.bulk_calls == []                          # the bulk phase is skipped on a mid-run resume
        assert [p[0] for p in fr.store.puts] == ["BBB", "CCC"]
        assert "sequential fund skipped" not in fr.store.job_val["message"]

    def test_a_resume_that_restarts_from_zero_still_does_the_bulk_seed_and_early_finish(self, fr):
        # a finished job (cursor at the end) resumed again resets to 0 -> the bulk seed runs again and the
        # early finish applies exactly as for a fresh run
        fr.universe = [f"S{i}" for i in range(10)]
        fr.bulk_result = {"tracked_stocks": 2, "symbols": ["S0", "S1"]}
        fr.store.job_val = {"status": "done", "ok_count": 5,
                            "checkpoint": {"cursor": 10, "done": ["S0"]}}
        go(fr, resume=True)
        assert fr.store.job_val["message"] == "Data feed stopped after bulk seed (2 rows) — sequential fund skipped"


# ══ _run PHASE 1: per-symbol fill ════════════════════════════════════════════

class TestSequentialLoop:
    def test_every_symbol_is_fetched_extracted_and_stored(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        assert fr.client.calls[0] == ("http://fund.t/analyze/AAA", 25)
        assert fr.client.calls[1] == ("http://ev.t/events/AAA", 12)
        assert fr.extract_calls[0] == ("AAA", {"fund": "AAA"}, {"ev": "AAA"})
        assert [p[0] for p in fr.store.puts] == ["AAA", "BBB", "CCC"]
        assert all(p[2] == gw.DATA_FEED_TTL for p in fr.store.puts)
        job = fr.store.job_val
        assert job["status"] == "done" and job["ok_count"] == 3 and job["error_count"] == 0
        assert job["message"].startswith("Data feed successfully for 3 stocks at ")
        assert job["checkpoint"]["cursor"] == 3 and sorted(job["checkpoint"]["done"]) == ["AAA", "BBB", "CCC"]
        assert job["stop_requested"] is False and job["finished_at"].endswith("+05:30")

    def test_final_meta_row(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        meta = fr.store.metas[-1]
        assert meta["source"] == "manual_or_api_or_scheduler" and meta["partial"] is False
        assert meta["last_count"] == 3 and meta["last_errors"] == 0 and meta["universe_size"] == 3
        assert meta["last_message"] == fr.store.job_val["message"]

    def test_per_symbol_progress_row(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        rows = [j for j in fr.store.set_jobs if j.get("message", "").startswith("Fed ")]
        assert [r["message"] for r in rows] == ["Fed 1/3 (AAA)", "Fed 2/3 (BBB)", "Fed 3/3 (CCC)"]
        assert rows[0]["processed"] == 1 and rows[0]["checkpoint"]["cursor"] == 1
        assert rows[0]["updated_at"].endswith("+05:30")

    def test_fund_only_is_enough(self, fr):
        seq(fr)
        fr.client.routes["/analyze/AAA"] = FakeResp(200, {"fund": 1})
        fr.universe = ["AAA"]
        go(fr)
        assert fr.extract_calls == [("AAA", {"fund": 1}, None)] and len(fr.store.puts) == 1

    def test_events_only_is_enough(self, fr):
        seq(fr)
        fr.client.routes["/events/AAA"] = FakeResp(200, {"ev": 1})
        fr.universe = ["AAA"]
        go(fr)
        assert fr.extract_calls == [("AAA", None, {"ev": 1})]

    def test_neither_upstream_counts_as_an_error(self, fr):
        seq(fr)
        fr.universe = ["AAA", "BBB"]
        fr.client.routes["/analyze/BBB"] = FakeResp(200, {"fund": 1})
        go(fr)
        assert fr.store.job_val["error_count"] == 1 and fr.store.job_val["ok_count"] == 1
        assert [p[0] for p in fr.store.puts] == ["BBB"]

    @pytest.mark.parametrize("fund,events", [(FakeResp(500), FakeResp(404)),
                                             (RuntimeError("x"), RuntimeError("y")),
                                             (FakeResp(200, json_raises=True), FakeResp(200, json_raises=True))])
    def test_upstream_failures_are_swallowed_per_call(self, fr, fund, events):
        seq(fr)
        fr.universe = ["AAA"]
        fr.client.routes["/analyze/AAA"] = fund
        fr.client.routes["/events/AAA"] = events
        go(fr)
        assert fr.store.job_val["status"] == "done" and fr.store.job_val["error_count"] == 1

    def test_error_status_bodies_are_never_used_as_data(self, fr):
        seq(fr)
        fr.universe = ["AAA"]
        fr.client.routes["/analyze/AAA"] = FakeResp(404, {"detail": "not found"})
        fr.client.routes["/events/AAA"] = FakeResp(500, {"detail": "boom"})
        go(fr)
        assert fr.store.puts == [] and fr.store.job_val["error_count"] == 1

    def test_events_still_fetched_when_the_fund_call_raises(self, fr):
        seq(fr)
        fr.universe = ["AAA"]
        fr.client.routes["/analyze/AAA"] = RuntimeError("x")
        fr.client.routes["/events/AAA"] = FakeResp(200, {"ev": 1})
        go(fr)
        assert len(fr.store.puts) == 1

    def test_empty_json_bodies_are_falsy(self, fr):
        seq(fr)
        fr.universe = ["AAA"]
        fr.client.routes["/analyze/AAA"] = FakeResp(200, {})
        fr.client.routes["/events/AAA"] = FakeResp(200, None)
        go(fr)
        assert fr.store.job_val["error_count"] == 1 and fr.store.puts == []

    def test_extract_failure_is_an_error_and_the_run_continues(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        fr.extract_raises = True
        go(fr)
        assert fr.store.job_val["status"] == "done" and fr.store.job_val["error_count"] == 3
        assert fr.store.job_val["ok_count"] == 0

    def test_store_write_failure_is_an_error_and_not_marked_done(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        fr.store.put_raises.add("BBB")
        go(fr)
        assert fr.store.job_val["error_count"] == 1 and fr.store.job_val["ok_count"] == 2
        assert "BBB" not in fr.store.job_val["checkpoint"]["done"]

    def test_done_symbols_are_skipped_without_any_fetch(self, fr):
        seq(fr)
        bt = BackgroundTasks()
        _run(gw.data_feed_run(bt, force=True))
        _run(bt.tasks[0].func(1, 0, 0, {"BBB"}))                       # resume at 1, BBB already done
        assert "Skip cached BBB (2/3)" in [j.get("message") for j in fr.store.set_jobs]
        assert not any("BBB" in u for u in fr.client.urls())
        assert any("CCC" in u for u in fr.client.urls())

    def test_resume_loop_starts_at_the_cursor(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        fr.store.job_val = {"status": "stopped", "ok_count": 1, "checkpoint": {"cursor": 1, "done": ["AAA"]}}
        go(fr, resume=True)
        assert [p[0] for p in fr.store.puts] == ["BBB", "CCC"]
        assert fr.store.job_val["ok_count"] == 3                       # 1 carried + 2 new

    # -- pacing ---------------------------------------------------------------
    def test_pacing_default_batch_of_20_and_quarter_second_every_5(self, fr):
        seq(fr)
        fr.universe = [f"S{i}" for i in range(20)]
        ok_routes(fr, *fr.universe)
        go(fr)
        assert fr.sleeps == [0.25, 0.25, 0.25, 0.5] and len(fr.warm_calls) == 1

    def test_batch_size_env_is_floored_at_5(self, fr):
        seq(fr, DATA_FEED_BATCH_SIZE=2)
        fr.universe = [f"S{i}" for i in range(10)]
        ok_routes(fr, *fr.universe)
        go(fr)
        assert fr.sleeps == [0.5, 0.5] and len(fr.warm_calls) == 2

    def test_batch_size_env_is_honoured_above_the_floor(self, fr):
        seq(fr, DATA_FEED_BATCH_SIZE=7)
        fr.universe = [f"S{i}" for i in range(14)]
        ok_routes(fr, *fr.universe)
        go(fr)
        assert len(fr.warm_calls) == 2 and fr.sleeps.count(0.5) == 2

    def test_batch_boundary_writes_a_warming_row(self, fr):
        seq(fr, DATA_FEED_BATCH_SIZE=5)
        fr.universe = [f"S{i}" for i in range(5)]
        ok_routes(fr, *fr.universe)
        go(fr)
        assert any(j.get("message") == "Batch done 5/5 — warming services…" for j in fr.store.set_jobs)
        assert fr.warm_calls == [fr.client]

    def test_warm_failure_is_swallowed(self, fr):
        seq(fr, DATA_FEED_BATCH_SIZE=5)
        fr.warm_raises = True
        fr.universe = [f"S{i}" for i in range(5)]
        ok_routes(fr, *fr.universe)
        go(fr)
        assert fr.store.job_val["status"] == "done" and 0.5 in fr.sleeps

    def test_fewer_than_five_symbols_never_sleep(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        go(fr)
        assert fr.sleeps == []

    def test_failed_symbols_are_paced_like_successes(self, fr):
        seq(fr)
        fr.universe = [f"S{i}" for i in range(5)]
        go(fr)                                                        # every symbol fails
        assert fr.sleeps == [0.25] and fr.store.job_val["error_count"] == 5


# ══ _run: cooperative stop ═══════════════════════════════════════════════════

class TestStop:
    def _assert_stopped_at(self, fr, i, ok):
        job = fr.store.job_val
        assert job["status"] == "stopped" and job["processed"] == i and job["ok_count"] == ok
        assert job["message"].startswith(f"Stopped at {i}/{len(fr.universe)} — committed {ok} fed stocks at ")
        assert job["stop_requested"] is False and job["checkpoint"]["cursor"] == i
        meta = fr.store.metas[-1]
        assert meta["source"] == "stop" and meta["partial"] is True and meta["last_count"] == ok
        assert meta["universe_size"] == len(fr.universe) and meta["last_message"] == job["message"]
        assert job["finished_at"] == meta["last_success_at"]

    def test_process_flag_stops_before_the_next_symbol(self, fr):
        seq(fr)
        ok_routes(fr, "AAA", "BBB", "CCC")
        fr.stop_flag_after = 1                                        # 1st check passes, 2nd trips
        go(fr)
        self._assert_stopped_at(fr, 1, 1)
        assert [p[0] for p in fr.store.puts] == ["AAA"] and fr.store.job_val["checkpoint"]["done"] == ["AAA"]

    def test_flag_set_before_the_first_symbol_stops_immediately(self, fr):
        seq(fr)
        fr.stop_flag_after = 0
        go(fr)
        self._assert_stopped_at(fr, 0, 0)
        assert fr.store.puts == [] and fr.client.calls == []

    def test_job_stop_requested_stops_the_run(self, fr):
        seq(fr)
        out, bt = start(fr)
        fr.store.job_val["stop_requested"] = True                      # set after the launcher's row, like /stop does
        execute(bt)
        self._assert_stopped_at(fr, 0, 0)

    @pytest.mark.parametrize("status", ["stopped", "stopping"])
    def test_job_status_stopped_or_stopping_stops_the_run(self, fr, status):
        seq(fr, DATA_FEED_BATCH_SIZE=5)
        fr.universe = [f"S{i}" for i in range(6)]
        ok_routes(fr, *fr.universe)
        fr.on_warm = lambda: fr.store.job_val.update(status=status)    # lands after the last in-loop set_job
        go(fr)
        self._assert_stopped_at(fr, 5, 5)
        assert len(fr.store.puts) == 5

    def test_stop_keeps_the_error_count(self, fr):
        seq(fr)
        fr.universe = ["AAA", "BBB", "CCC"]
        fr.client.routes["/analyze/AAA"] = FakeResp(200, {"fund": 1})
        fr.stop_flag_after = 2
        go(fr)                                                        # AAA ok, BBB fails, stop at CCC
        assert fr.store.metas[-1]["last_errors"] == 1 and fr.store.job_val["error_count"] == 1

    def test_stop_clears_the_process_flag_at_start_of_run_only(self, fr):
        seq(fr)
        fr.stop_flag_after = 0
        go(fr)
        assert fr.clear_calls == 1
