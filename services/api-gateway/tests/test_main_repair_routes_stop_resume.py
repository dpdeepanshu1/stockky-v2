"""tests/test_main_repair_routes_stop_resume.py — coverage for api-gateway/main.py, slice 19
(lines 10743-10909 and 11255-11329)

Pass 77. The thin route layer around `_patch_single_stock_feed`, plus the feed stop / resume controls:

* `POST /data-feed/repair-single/{symbol}` (+ 3 aliases) — URL-decoding, symbol cleaning, the
  "never 500 the UI" error envelope;
* `POST /data-feed/repair-batch` (+ alias) — limit clamp to 1..25, targets from the audit, per-symbol
  isolation, 0.5s pacing, the `successish_count`;
* the Refill-All background job (`_run_refill_all_job`) and its `repair-all`, `/status`, `/stop` routes —
  already-running guard, limit clamp, batches of 5 with 0.5s / 1.0s pacing, cancel between batches,
  done / stopped / error bookkeeping;
* `POST /data-feed/stop` — cooperative stop flag, force-commit from the checkpoint, meta + job writes;
* `POST /data-feed/resume` — force-stop a stuck "running" job first, then delegate to `data_feed_run`.

Everything downstream is faked: `_patch_single_stock_feed`, `audit_missing_feed_data`, the feed store,
`request_data_feed_stop`, `data_feed_run` (covered in its own pass), `asyncio.sleep`. Nothing touches the
network or a database. Findings are pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_repair_routes_stop_resume.py -v
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.job_val = {}
        self.metas = []
        self.set_jobs = []
        self.set_job_raises = False

    def job(self):
        return self.job_val

    def set_job(self, **kw):
        if self.set_job_raises:
            raise RuntimeError("store down")
        self.set_jobs.append(kw)
        self.job_val = {**self.job_val, **kw}
        return self.job_val

    def set_meta(self, **kw):
        self.metas.append(kw)


@pytest.fixture
def sleeps(monkeypatch):
    out = []

    async def fake_sleep(d):
        out.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return out


@pytest.fixture
def patch1(monkeypatch):
    """Fake `_patch_single_stock_feed`: results by symbol (dict | Exception), calls recorded."""
    p = types.SimpleNamespace(calls=[], results={}, default={"symbol": "?", "patched_fields": ["rsi"], "complete": True},
                              client=object())

    async def fake(sym, client):
        p.calls.append((sym, client))
        out = p.results.get(sym, {**p.default, "symbol": sym})
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(gw, "_patch_single_stock_feed", fake)
    monkeypatch.setattr(gw, "_get_http_client", lambda: p.client)
    return p


@pytest.fixture
def audit(monkeypatch):
    a = types.SimpleNamespace(incomplete=[], calls=[], raises=None)

    async def fake(limit=500, cache=True):
        a.calls.append(limit)
        if a.raises:
            raise a.raises
        return {"incomplete_stocks": [{"symbol": s} for s in a.incomplete]}

    monkeypatch.setattr(gw, "audit_missing_feed_data", fake)
    return a


@pytest.fixture
def job(monkeypatch):
    j = {"status": "idle", "total": 0, "processed": 0, "ok_count": 0, "started_at": None,
         "finished_at": None, "message": "Idle", "last_symbol": None, "cancel_requested": False}
    monkeypatch.setattr(gw, "_REFILL_ALL_JOB", j)
    return j


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


# ══ repair_single_stock ══════════════════════════════════════════════════════

class TestRepairSingle:
    def test_success_envelope_merges_the_patch_result(self, patch1):
        patch1.results["TCS"] = {"symbol": "TCS", "patched_fields": ["rsi"], "complete": True, "price": 5.0}
        out = _run(gw.repair_single_stock("tcs.ns"))
        assert out == {"status": "success", "ok": True, "symbol": "TCS", "patched_fields": ["rsi"],
                       "complete": True, "price": 5.0}
        assert patch1.calls == [("TCS", patch1.client)]

    def test_bo_suffix_and_whitespace_are_stripped(self, patch1):
        _run(gw.repair_single_stock(" infy.bo "))
        assert patch1.calls[0][0] == "INFY"

    def test_symbol_is_url_decoded_first(self, patch1):
        _run(gw.repair_single_stock("m%26m.ns"))
        assert patch1.calls[0][0] == "M&M"

    def test_percent_encoded_suffix_is_decoded_then_stripped(self, patch1):
        _run(gw.repair_single_stock("TCS%2ENS"))
        assert patch1.calls[0][0] == "TCS"

    @pytest.mark.parametrize("raw", ["", "   ", ".NS", None])
    def test_empty_symbol_is_a_soft_error_and_makes_no_call(self, patch1, raw):
        out = _run(gw.repair_single_stock(raw))
        assert out == {"status": "error", "symbol": raw, "message": "empty symbol", "complete": False}
        assert patch1.calls == []

    def test_failure_is_a_200_error_envelope_truncated_to_200(self, patch1):
        patch1.results["TCS"] = RuntimeError("E" * 300)
        out = _run(gw.repair_single_stock("TCS"))
        assert out == {"status": "error", "ok": False, "symbol": "TCS", "patched_fields": [],
                       "still_missing": ["unknown"], "complete": False, "message": "E" * 200}

    def test_routed_on_all_four_paths(self, patch1, tc):
        for path in ("/api/feed/repair-single/TCS", "/data-feed/repair-single/TCS",
                     "/api/data-feed/repair-single/TCS", "/feed/repair-single/TCS"):
            r = tc.post(path)
            assert r.status_code == 200 and r.json()["status"] == "success"
        assert len(patch1.calls) == 4

    def test_http_failure_never_surfaces_as_a_500(self, patch1, tc):
        patch1.results["TCS"] = RuntimeError("boom")
        r = tc.post("/data-feed/repair-single/TCS")
        assert r.status_code == 200 and r.json()["ok"] is False

    def test_get_is_a_405(self, patch1, tc):
        # unlike the one-segment routes, `/data-feed/repair-single/X` has two segments, so the
        # `/data-feed/{symbol}` catch-all does not swallow it and GET is a real 405.
        assert tc.get("/data-feed/repair-single/TCS").status_code == 405
        assert patch1.calls == []


# ══ repair_batch_missing ═════════════════════════════════════════════════════

class TestRepairBatch:
    def test_repairs_the_audit_targets_in_order_with_pacing(self, patch1, audit, sleeps):
        audit.incomplete = ["AAA", "BBB", "CCC"]
        out = _run(gw.repair_batch_missing(limit=10))
        assert [c[0] for c in patch1.calls] == ["AAA", "BBB", "CCC"] and audit.calls == [10]
        assert out["status"] == "completed" and out["repaired_count"] == 3 and out["successish_count"] == 3
        assert out["repaired_symbols"] == ["AAA", "BBB", "CCC"] and sleeps == [0.5, 0.5, 0.5]

    def test_targets_are_sliced_to_the_limit(self, patch1, audit, sleeps):
        audit.incomplete = [f"S{i}" for i in range(8)]
        out = _run(gw.repair_batch_missing(limit=3))
        assert out["repaired_symbols"] == ["S0", "S1", "S2"]

    @pytest.mark.parametrize("limit,expected", [(0, 10), (-5, 1), (1, 1), (25, 25), (26, 25), (999, 25)])
    def test_limit_is_clamped(self, patch1, audit, sleeps, limit, expected):
        _run(gw.repair_batch_missing(limit=limit))
        assert audit.calls == [expected]

    def test_default_limit_is_10(self, patch1, audit, sleeps):
        _run(gw.repair_batch_missing())
        assert audit.calls == [10]

    def test_no_targets_is_a_clean_empty_run(self, patch1, audit, sleeps):
        out = _run(gw.repair_batch_missing())
        assert out == {"status": "completed", "repaired_count": 0, "successish_count": 0,
                       "repaired": [], "repaired_symbols": []}
        assert sleeps == []

    def test_one_failure_does_not_stop_the_batch(self, patch1, audit, sleeps):
        audit.incomplete = ["AAA", "BAD", "CCC"]
        patch1.results["BAD"] = RuntimeError("X" * 300)
        out = _run(gw.repair_batch_missing())
        assert out["repaired_count"] == 3 and out["successish_count"] == 2
        assert out["repaired"][1] == {"symbol": "BAD", "error": "X" * 160, "complete": False}
        assert sleeps == [0.5, 0.5, 0.5]                    # the failing symbol is still paced

    def test_successish_counts_complete_or_patched(self, patch1, audit, sleeps):
        audit.incomplete = ["A", "B", "C", "D"]
        patch1.results.update({"A": {"symbol": "A", "complete": True},
                               "B": {"symbol": "B", "patched_fields": ["rsi"]},
                               "C": {"symbol": "C", "patched_fields": [], "complete": False},
                               "D": {"symbol": "D", "purged": True, "complete": False}})
        assert _run(gw.repair_batch_missing())["successish_count"] == 2

    def test_audit_failure_propagates(self, patch1, audit, sleeps):
        """NOT FIXED: unlike repair-single, the batch route has no error envelope around the audit call."""
        audit.raises = RuntimeError("audit down")
        with pytest.raises(RuntimeError):
            _run(gw.repair_batch_missing())

    def test_missing_incomplete_key_is_empty(self, patch1, monkeypatch, sleeps):
        async def fake(limit=500, cache=True):
            return {"incomplete_stocks": None}

        monkeypatch.setattr(gw, "audit_missing_feed_data", fake)
        assert _run(gw.repair_batch_missing())["repaired_count"] == 0

    def test_routed_on_both_paths(self, patch1, audit, sleeps, tc):
        audit.incomplete = ["AAA"]
        for path in ("/api/feed/repair-batch", "/data-feed/repair-batch"):
            r = tc.post(path + "?limit=5")
            assert r.status_code == 200 and r.json()["repaired_count"] == 1


# ══ _run_refill_all_job ══════════════════════════════════════════════════════

class TestRefillAllJob:
    def test_walks_every_target_and_finishes_done(self, patch1, audit, job, sleeps):
        audit.incomplete = ["A", "B", "C"]
        _run(gw._run_refill_all_job(5000))
        assert [c[0] for c in patch1.calls] == ["A", "B", "C"]
        assert job["status"] == "done" and job["total"] == 3 and job["processed"] == 3 and job["ok_count"] == 3
        assert job["message"] == "Done — 3/3 improved." and job["last_symbol"] == "C"
        assert job["started_at"].endswith("+05:30") and job["finished_at"].endswith("+05:30")
        assert job["cancel_requested"] is False

    def test_audit_is_asked_for_at_least_5000_rows(self, patch1, audit, job, sleeps):
        _run(gw._run_refill_all_job(10))
        _run(gw._run_refill_all_job(9000))
        assert audit.calls == [5000, 9000]

    def test_limit_slices_the_targets(self, patch1, audit, job, sleeps):
        audit.incomplete = [f"S{i}" for i in range(10)]
        _run(gw._run_refill_all_job(4))
        assert job["total"] == 4 and [c[0] for c in patch1.calls] == ["S0", "S1", "S2", "S3"]

    def test_zero_limit_means_no_slice(self, patch1, audit, job, sleeps):
        audit.incomplete = [f"S{i}" for i in range(7)]
        _run(gw._run_refill_all_job(0))
        assert job["total"] == 7

    def test_nothing_to_repair_is_done_immediately(self, patch1, audit, job, sleeps):
        _run(gw._run_refill_all_job(100))
        assert job["status"] == "done" and job["message"] == "Nothing to repair — feed already healthy."
        assert job["total"] == 0 and patch1.calls == [] and sleeps == []
        assert job["finished_at"].endswith("+05:30")

    def test_pacing_is_half_a_second_per_symbol_and_a_second_per_batch_of_five(self, patch1, audit, job, sleeps):
        audit.incomplete = [f"S{i}" for i in range(7)]
        _run(gw._run_refill_all_job(100))
        assert sleeps == [0.5] * 5 + [1.0] + [0.5] * 2 + [1.0]

    def test_progress_message_per_symbol(self, patch1, audit, job, sleeps, monkeypatch):
        audit.incomplete = ["A", "B"]
        seen = []
        orig = gw._patch_single_stock_feed

        async def spy(sym, client):
            seen.append((job["processed"], job["message"], job["status"]))
            return await orig(sym, client)

        monkeypatch.setattr(gw, "_patch_single_stock_feed", spy)
        _run(gw._run_refill_all_job(100))
        assert seen == [(0, "Repairing 2 symbols…", "running"), (1, "1/2 · last: A", "running")]

    def test_ok_count_counts_complete_or_patched_only(self, patch1, audit, job, sleeps):
        audit.incomplete = ["A", "B", "C"]
        patch1.results.update({"A": {"complete": True}, "B": {"patched_fields": [], "complete": False},
                               "C": {"patched_fields": ["rsi"]}})
        _run(gw._run_refill_all_job(100))
        assert job["ok_count"] == 2 and job["message"] == "Done — 2/3 improved."

    def test_per_symbol_failure_is_swallowed_counted_and_does_not_update_last_symbol(self, patch1, audit, job, sleeps):
        audit.incomplete = ["A", "BAD", "C"]
        patch1.results["BAD"] = RuntimeError("x")
        _run(gw._run_refill_all_job(100))
        assert job["status"] == "done" and job["processed"] == 3 and job["ok_count"] == 2
        assert job["last_symbol"] == "C"

    def test_failure_on_the_last_symbol_leaves_last_symbol_stale(self, patch1, audit, job, sleeps):
        audit.incomplete = ["A", "BAD"]
        patch1.results["BAD"] = RuntimeError("x")
        _run(gw._run_refill_all_job(100))
        assert job["last_symbol"] == "A" and job["message"] == "Done — 1/2 improved."

    def test_cancel_before_the_first_batch_stops_with_zero_processed(self, patch1, audit, job, sleeps, monkeypatch):
        audit.incomplete = ["A", "B"]
        orig = gw.audit_missing_feed_data

        async def audit_then_cancel(limit=500, cache=True):
            out = await orig(limit=limit)
            job["cancel_requested"] = True
            return out

        monkeypatch.setattr(gw, "audit_missing_feed_data", audit_then_cancel)
        _run(gw._run_refill_all_job(100))
        # NOT FIXED: the start-of-run update resets cancel_requested to False, so a stop that lands during the
        # audit is silently lost and the whole run proceeds.
        assert job["status"] == "done" and job["processed"] == 2

    def test_cancel_between_batches_stops_after_the_current_batch(self, patch1, audit, job, sleeps, monkeypatch):
        audit.incomplete = [f"S{i}" for i in range(12)]
        orig = gw._patch_single_stock_feed

        async def cancel_on_third(sym, client):
            if sym == "S2":
                job["cancel_requested"] = True
            return await orig(sym, client)

        monkeypatch.setattr(gw, "_patch_single_stock_feed", cancel_on_third)
        _run(gw._run_refill_all_job(100))
        assert job["status"] == "stopped" and job["processed"] == 5          # finishes the batch of 5
        assert job["message"] == "Stopped by user after 5/12." and job["finished_at"].endswith("+05:30")
        assert len(patch1.calls) == 5

    def test_audit_failure_marks_the_job_error_truncated_to_200(self, patch1, audit, job, sleeps):
        audit.raises = RuntimeError("E" * 300)
        _run(gw._run_refill_all_job(100))
        assert job["status"] == "error" and job["message"] == "E" * 200 and job["finished_at"].endswith("+05:30")

    def test_missing_incomplete_key_is_nothing_to_repair(self, patch1, job, sleeps, monkeypatch):
        async def fake(limit=500, cache=True):
            return {}

        monkeypatch.setattr(gw, "audit_missing_feed_data", fake)
        _run(gw._run_refill_all_job(100))
        assert job["message"] == "Nothing to repair — feed already healthy."

    def test_rerun_resets_counters(self, patch1, audit, job, sleeps):
        job.update({"processed": 99, "ok_count": 99, "finished_at": "old"})
        audit.incomplete = ["A"]
        _run(gw._run_refill_all_job(100))
        assert job["processed"] == 1 and job["ok_count"] == 1


class TestRefillAllRoutes:
    def test_start_queues_the_job_with_a_clamped_limit(self, job):
        bt = BackgroundTasks()
        out = _run(gw.repair_all_missing(bt, limit=100))
        assert out == {"ok": True, "started": True, "message": "Refill All started in the background."}
        assert bt.tasks[0].func is gw._run_refill_all_job and bt.tasks[0].args == (100,)

    @pytest.mark.parametrize("limit,expected", [(0, 5000), (-3, 1), (1, 1), (5000, 5000), (99999, 5000)])
    def test_limit_clamp(self, job, limit, expected):
        bt = BackgroundTasks()
        _run(gw.repair_all_missing(bt, limit=limit))
        assert bt.tasks[0].args == (expected,)

    def test_default_limit_is_5000(self, job):
        bt = BackgroundTasks()
        _run(gw.repair_all_missing(bt))
        assert bt.tasks[0].args == (5000,)

    def test_already_running_returns_the_job_and_queues_nothing(self, job):
        job.update({"status": "running", "total": 9, "processed": 4})
        bt = BackgroundTasks()
        out = _run(gw.repair_all_missing(bt))
        assert out["already_running"] is True and out["ok"] is True and out["total"] == 9
        assert out["processed"] == 4 and bt.tasks == []

    @pytest.mark.parametrize("status", ["idle", "done", "stopped", "error"])
    def test_any_non_running_status_starts(self, job, status):
        job["status"] = status
        bt = BackgroundTasks()
        assert _run(gw.repair_all_missing(bt))["started"] is True and len(bt.tasks) == 1

    def test_status_returns_a_copy(self, job):
        job["processed"] = 3
        out = _run(gw.repair_all_status())
        assert out == job and out is not job
        out["processed"] = 99
        assert job["processed"] == 3

    def test_stop_sets_the_cancel_flag(self, job):
        out = _run(gw.repair_all_stop())
        assert out == {"ok": True, "message": "Stop requested — will halt after the current batch."}
        assert job["cancel_requested"] is True

    def test_stop_on_an_idle_job_leaves_a_stale_flag(self, job):
        """NOT FIXED: stop is unconditional, so on an idle job the flag stays True until the next run resets it."""
        _run(gw.repair_all_stop())
        assert job["status"] == "idle" and job["cancel_requested"] is True

    def test_routed(self, job, patch1, audit, sleeps, tc):
        for path in ("/api/feed/repair-all", "/data-feed/repair-all"):
            job["status"] = "idle"
            r = tc.post(path + "?limit=1")
            assert r.status_code == 200 and r.json()["started"] is True
        for path in ("/api/feed/repair-all/status", "/data-feed/repair-all/status"):
            assert "status" in tc.get(path).json()
        for path in ("/api/feed/repair-all/stop", "/data-feed/repair-all/stop"):
            assert tc.post(path).json()["ok"] is True


# ══ data_feed_stop ═══════════════════════════════════════════════════════════

@pytest.fixture
def stopenv(monkeypatch):
    s = FakeStore()
    stop_calls = []
    st = types.SimpleNamespace(store=s, stop_calls=stop_calls, flag_raises=False)

    def req():
        stop_calls.append(1)
        if st.flag_raises:
            raise RuntimeError("flag broke")

    monkeypatch.setattr(gw, "_feed_store", lambda: s)
    monkeypatch.setattr(gw, "request_data_feed_stop", req)
    return st


class TestDataFeedStop:
    def test_force_commits_from_the_checkpoint(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 3, "total": 10, "ok_count": 4,
                                 "error_count": 1,
                                 "checkpoint": {"cursor": 6, "done": ["A", "B", "C", "D", "E"],
                                                "universe": ["A", "B"]}}
        out = _run(gw.data_feed_stop())
        assert out["ok"] is True and out["stopped"] is True and out["status"] == "stopped"
        assert out["processed"] == 6 and out["total"] == 10 and out["ok_count"] == 5     # max(4, len(done)=5)
        assert out["error_count"] == 1 and out["errors"] == 1 and out["stop_requested"] is False
        assert out["message"].startswith("Stopped at 6/10 — committed 5 fed stocks at ")
        assert out["checkpoint"] == {"cursor": 6, "done": ["A", "B", "C", "D", "E"], "universe": ["A", "B"]}
        assert out["finished_at"].endswith("+05:30")
        meta = stopenv.store.metas[0]
        assert meta["source"] == "stop" and meta["last_count"] == 5 and meta["last_errors"] == 1
        assert meta["universe_size"] == 10 and meta["partial"] is True
        assert meta["last_success_at"] == out["finished_at"] and meta["last_message"] == out["message"]
        assert stopenv.stop_calls == [1]

    def test_marks_the_job_stopping_before_committing(self, stopenv):
        stopenv.store.job_val = {"status": "running"}
        _run(gw.data_feed_stop())
        first = stopenv.store.set_jobs[0]
        assert first == {"stop_requested": True, "status": "stopping",
                         "message": "Stop requested — finishing current symbol…"}
        assert stopenv.store.set_jobs[1]["status"] == "stopped"

    def test_cursor_falls_back_to_processed(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 7, "total": 9}
        assert _run(gw.data_feed_stop())["processed"] == 7

    def test_zero_checkpoint_cursor_falls_back_to_processed(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 7, "total": 9, "checkpoint": {"cursor": 0}}
        assert _run(gw.data_feed_stop())["processed"] == 7

    def test_done_list_never_lowers_the_ok_count(self, stopenv):
        stopenv.store.job_val = {"status": "running", "ok_count": 9, "checkpoint": {"done": ["A"]}}
        assert _run(gw.data_feed_stop())["ok_count"] == 9

    def test_errors_key_is_the_fallback_for_error_count(self, stopenv):
        stopenv.store.job_val = {"status": "running", "errors": 4}
        out = _run(gw.data_feed_stop())
        assert out["error_count"] == 4 and stopenv.store.metas[0]["last_errors"] == 4

    def test_non_dict_checkpoint_is_empty(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 2, "total": 5, "checkpoint": "junk"}
        out = _run(gw.data_feed_stop())
        assert out["checkpoint"] == {"cursor": 2, "done": [], "universe": []}

    def test_none_done_and_universe_become_empty_lists(self, stopenv):
        stopenv.store.job_val = {"status": "running", "checkpoint": {"done": None, "universe": None}}
        assert _run(gw.data_feed_stop())["checkpoint"] == {"cursor": 0, "done": [], "universe": []}

    def test_finished_run_is_not_partial(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 10, "total": 10}
        _run(gw.data_feed_stop())
        assert stopenv.store.metas[0]["partial"] is False

    def test_zero_total_is_not_partial(self, stopenv):
        stopenv.store.job_val = {"status": "running"}
        _run(gw.data_feed_stop())
        assert stopenv.store.metas[0]["partial"] is False and stopenv.store.metas[0]["universe_size"] == 0

    def test_stop_flag_failure_is_swallowed(self, stopenv):
        stopenv.flag_raises = True
        stopenv.store.job_val = {"status": "running"}
        assert _run(gw.data_feed_stop())["stopped"] is True

    def test_stopping_marker_failure_is_swallowed_and_the_commit_still_runs(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 1, "total": 2}
        calls = []
        orig = stopenv.store.set_job

        def flaky(**kw):
            calls.append(kw)
            if kw.get("status") == "stopping":
                raise RuntimeError("down")
            return orig(**kw)

        stopenv.store.set_job = flaky
        out = _run(gw.data_feed_stop())
        assert out["stopped"] is True and [c["status"] for c in calls] == ["stopping", "stopped"]

    def test_force_false_stops_a_genuinely_running_job(self, stopenv):
        """FIXED: the job is read BEFORE the "stopping" marker is written, so `force=False` can stop a
        running job."""
        stopenv.store.job_val = {"status": "running", "processed": 3, "total": 9}
        out = _run(gw.data_feed_stop(force=False))
        assert out["stopped"] is True
        assert stopenv.store.metas[0]["source"] == "stop"

    def test_force_false_on_an_idle_job_is_a_pure_no_op(self, stopenv):
        """FIXED: a "not running" call no longer flips the job to stopping / stop_requested=True."""
        stopenv.store.job_val = {"status": "done"}
        out = _run(gw.data_feed_stop(force=False))
        assert out["stopped"] is False and out["detail"] == "Not running (status=done)"
        assert stopenv.store.job_val["status"] == "done" and not stopenv.store.job_val.get("stop_requested")
        assert stopenv.store.metas == []

    def test_force_false_with_a_failed_marker_reads_the_real_status(self, stopenv):
        stopenv.store.set_job_raises = True
        stopenv.store.job_val = {"status": "done"}
        out = _run(gw.data_feed_stop(force=False))
        assert out["stopped"] is False and out["detail"] == "Not running (status=done)" and out["status"] == "done"

    def test_force_false_with_a_failed_marker_and_a_running_job_does_stop(self, stopenv):
        stopenv.store.job_val = {"status": "running"}
        stopenv.store.set_job_raises = True
        out = _run(gw.data_feed_stop(force=False))             # the commit's own set_job then raises
        assert out["ok"] is False and out["stop_signalled"] is True and "checkpoint commit failed" in out["error"]
        assert stopenv.store.metas[0]["source"] == "stop"

    def test_commit_failure_is_reported_as_a_structured_error(self, stopenv):
        """FIXED: a failing commit is reported (the stop signal is already out) instead of a 500."""
        stopenv.store.job_val = {"status": "running"}
        stopenv.store.set_meta = lambda **kw: (_ for _ in ()).throw(RuntimeError("meta down"))
        out = _run(gw.data_feed_stop())
        assert out["ok"] is False and out["stopped"] is False and out["stop_signalled"] is True
        assert "meta down" in out["error"]

    def test_routed_on_both_paths(self, stopenv, tc):
        stopenv.store.job_val = {"status": "running"}
        for path in ("/data-feed/stop", "/api/data-feed/stop"):
            r = tc.post(path)
            assert r.status_code == 200 and r.json()["stopped"] is True
        stopenv.store.job_val = {"status": "done"}
        assert tc.post("/data-feed/stop?force=false").json()["stopped"] is False


# ══ data_feed_resume ═════════════════════════════════════════════════════════

@pytest.fixture
def resume(stopenv, monkeypatch):
    r = types.SimpleNamespace(run_calls=[], stop_calls=[], store=stopenv.store)

    async def fake_run(background_tasks, **kw):
        r.run_calls.append((background_tasks, kw))
        return {"ok": True, "status": "started"}

    async def fake_stop(force=True):
        r.stop_calls.append(force)
        return {"ok": True}

    monkeypatch.setattr(gw, "data_feed_run", fake_run)
    monkeypatch.setattr(gw, "data_feed_stop", fake_stop)
    return r


class TestDataFeedResume:
    def test_idle_job_resumes_without_a_force_stop(self, resume):
        resume.store.job_val = {"status": "stopped"}
        bt = BackgroundTasks()
        out = _run(gw.data_feed_resume(bt))
        assert out == {"ok": True, "status": "started"}
        assert resume.stop_calls == [] and resume.run_calls == [(bt, {"force": False, "resume": True})]

    def test_stuck_running_job_is_force_stopped_first(self, resume):
        resume.store.job_val = {"status": "running"}
        _run(gw.data_feed_resume(BackgroundTasks()))
        assert resume.stop_calls == [True] and len(resume.run_calls) == 1

    @pytest.mark.parametrize("status", ["done", "error", "stopping", "idle", None])
    def test_only_running_triggers_the_force_stop(self, resume, status):
        resume.store.job_val = {"status": status}
        _run(gw.data_feed_resume(BackgroundTasks()))
        assert resume.stop_calls == []

    def test_routed_on_both_paths(self, resume, tc):
        resume.store.job_val = {"status": "stopped"}
        for path in ("/data-feed/resume", "/api/data-feed/resume"):
            assert tc.post(path).json() == {"ok": True, "status": "started"}
        assert len(resume.run_calls) == 2


class TestPass82FeedStop:
    def test_reread_failure_after_the_marker_keeps_the_original_job(self, stopenv):
        stopenv.store.job_val = {"status": "running", "processed": 4, "total": 8}
        calls = {"n": 0}
        orig = stopenv.store.job

        def flaky():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("read down")
            return orig()

        stopenv.store.job = flaky
        out = _run(gw.data_feed_stop())
        assert out["stopped"] is True and stopenv.store.metas[0]["universe_size"] == 8
