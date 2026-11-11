"""E447 (round 427): start-bulk-feed and refill-additional refuse a second start while a feed job is making progress (one shared guard,
gateway_support/feed_start_guard.py), a stalled job can be restarted, and a Stop that arrives while a structure refresh is still planning
is honoured instead of being wiped when the run begins."""
import asyncio
import threading
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks

from test_main_feed_control_audit import bulk, gw, refill, store, uni  # noqa: F401  (fixtures + gateway module)
from gateway_support import feed_start_guard as g


def _run(coro):
    return asyncio.run(coro)


def _iso(seconds_ago):
    return (gw.datetime.now(gw.IST) - timedelta(seconds=seconds_ago)).isoformat()


# ── bulk feed ─────────────────────────────────────────────────────────

def test_bulk_start_is_refused_while_a_fresh_job_is_running(store, bulk, uni):
    uni.symbols = ["TCS"]
    store.job_val = {"status": "running", "updated_at": _iso(20), "total": 500, "processed": 40}
    bt = BackgroundTasks()
    out = _run(gw.start_bulk_feed(background_tasks=bt))
    assert out["already_running"] is True and out["status"] == "running" and out["processed"] == 40
    assert bt.tasks == [] and store.set_jobs == []


def test_a_refused_bulk_start_does_not_clear_a_pending_stop(store, bulk, uni):
    store.job_val = {"status": "running", "updated_at": _iso(5)}
    _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
    assert bulk.stop_cleared == 0


def test_force_does_not_override_a_live_job(store, bulk, uni):
    uni.symbols = ["TCS"]
    store.job_val = {"status": "running", "updated_at": _iso(5)}
    assert _run(gw.start_bulk_feed(force=True, background_tasks=BackgroundTasks()))["already_running"] is True


def test_a_silent_running_row_is_stale_and_can_be_restarted(store, bulk, uni):
    uni.symbols = ["TCS"]
    store.job_val = {"status": "running", "updated_at": _iso(3600)}
    out = _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
    assert out["started"] is True and out["total"] == 1


def test_a_running_row_without_a_timestamp_never_locks_the_feed_out(store, bulk, uni):
    uni.symbols = ["TCS"]
    store.job_val = {"status": "running"}
    assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["started"] is True


@pytest.mark.parametrize("status", ["done", "stopped", "error", "idle"])
def test_a_finished_job_does_not_block(store, bulk, uni, status):
    uni.symbols = ["TCS"]
    store.job_val = {"status": status, "updated_at": _iso(1)}
    assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["started"] is True


def test_stale_window_is_an_env_switch(store, bulk, uni, monkeypatch):
    uni.symbols = ["TCS"]
    monkeypatch.setenv("BULK_FEED_STALE_AFTER_SEC", "60")
    store.job_val = {"status": "running", "updated_at": _iso(120)}
    assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["started"] is True


def test_an_unreadable_job_row_fails_open(store, bulk, uni, monkeypatch):
    uni.symbols = ["TCS"]
    monkeypatch.setattr(store, "job", lambda: (_ for _ in ()).throw(RuntimeError("store down")))
    assert _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))["started"] is True


def test_default_force_is_false_for_bulk(bulk):
    import inspect
    assert inspect.signature(gw.start_bulk_feed).parameters["force"].default is False


# ── double click while the symbols are still being resolved ───────────

def test_second_start_during_symbol_resolution_is_refused_and_the_claim_is_released(store, bulk, uni, monkeypatch):
    uni.symbols = ["TCS"]
    gate = threading.Event()
    real = uni.symbols

    def slow_build():
        gate.wait(5)
        return real

    monkeypatch.setattr(gw, "_build_scan_universe", slow_build)

    async def scenario():
        first = asyncio.create_task(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
        await asyncio.sleep(0.05)   # first is now inside to_thread(_build_scan_universe)
        second = await gw.start_bulk_feed(background_tasks=BackgroundTasks())
        gate.set()
        return await first, second

    first, second = _run(scenario())
    assert first["started"] is True
    assert second["already_running"] is True and "in progress" in second["message"]
    assert g._CLAIM["busy"] is False


def test_claim_is_released_when_the_start_raises(store, bulk, uni):
    store.symbols = []
    uni.symbols = []
    uni.nifty = []
    with pytest.raises(Exception):
        _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))   # no symbols anywhere -> 400
    assert g._CLAIM["busy"] is False


# ── refill ────────────────────────────────────────────────────────────

def test_refill_is_refused_while_a_bulk_job_is_running_on_the_shared_row(store, refill):
    store.symbols = ["AAA"]
    store.job_val = {"status": "running", "updated_at": _iso(10), "kind": "bulk"}
    bt = BackgroundTasks()
    out = _run(gw.data_feed_refill_additional(bt, force=True))
    assert out["already_running"] is True and bt.tasks == [] and refill.set_calls == []


def test_refill_with_a_stalled_local_job_restarts(store, refill, monkeypatch):
    store.symbols = ["AAA"]
    refill.job = {"status": "stalled"}
    assert _run(gw.data_feed_refill_additional(BackgroundTasks(), force=True))["total"] == 1


def test_bulk_is_refused_while_refill_is_running_locally(store, bulk, uni, refill):
    uni.symbols = ["TCS"]
    refill.job = {"status": "running", "processed": 3}
    out = _run(gw.start_bulk_feed(background_tasks=BackgroundTasks()))
    assert out["already_running"] is True and out["processed"] == 3


def test_both_routes_still_answer_over_http(store, bulk, uni, refill):
    from fastapi.testclient import TestClient
    tc = TestClient(gw.app, raise_server_exceptions=False)
    uni.symbols = ["TCS"]
    store.symbols = ["AAA"]
    for path in ("/data-feed/start-bulk-feed", "/api/data-feed/start-bulk-feed", "/api/feed/start-bulk-feed"):
        store.job_val = {}
        assert tc.post(path).status_code == 200


# ── structure refresh: a Stop during planning ─────────────────────────

class _FakeSR:
    """Just enough of structure_refresh for _start_structure_refresh_locked."""
    def __init__(self, stop_during_plan):
        self.stop_during_plan = stop_during_plan
        self._STOP = threading.Event()
        self.jobs = []
        self.run_calls = 0

    def enabled(self):
        return True

    def get_job(self):
        return {"status": "idle"}

    def _set_job(self, **kw):
        self.jobs.append(kw)

    def plan(self, store, force=False, limit=None):
        if self.stop_during_plan:
            self._STOP.set()
        return ["AAA", "BBB"], {"x": 1}, "2026-11-10"

    def make_fetcher(self, client):
        return None

    def run(self, *a, **k):
        self.run_calls += 1


def _start_sr(monkeypatch, sr):
    import sys
    import httpx
    monkeypatch.setitem(sys.modules, "structure_refresh", sr)
    started = []

    class _T:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(gw.threading, "Thread", _T)
    monkeypatch.setattr(gw, "_feed_store", lambda: object())
    out = gw._start_structure_refresh_locked(False, False, None)
    for t in started:
        t()
    return out


def test_stop_during_planning_is_honoured(monkeypatch):
    sr = _FakeSR(stop_during_plan=True)
    out = _start_sr(monkeypatch, sr)
    assert out["status"] == "running"
    assert sr.run_calls == 0
    assert sr.jobs[-1]["status"] == "stopped" and "planning" in sr.jobs[-1]["message"]


def test_a_stale_stop_flag_from_an_earlier_run_does_not_cancel_a_new_one(monkeypatch):
    sr = _FakeSR(stop_during_plan=False)
    sr._STOP.set()   # a Stop clicked while idle / left by the last run
    _start_sr(monkeypatch, sr)
    assert sr.run_calls == 1 and not any(j.get("status") == "stopped" for j in sr.jobs)


# ── bulk worker: the run's own verdict is kept ────────────────────────

@pytest.mark.parametrize("verdict,expected_status,stamps_meta", [
    ("success", "done", True), ("stopped", "stopped", True), ("error", "error", False), (None, "done", True)])
def test_bulk_worker_keeps_stopped_and_error_instead_of_writing_done(store, bulk, uni, verdict, expected_status, stamps_meta):
    uni.symbols = ["TCS"]
    bulk.result = {"tracked_stocks": 0 if verdict == "error" else 1, "message": "m", **({"status": verdict} if verdict else {})}
    bt = BackgroundTasks()
    _run(gw.start_bulk_feed(background_tasks=bt))
    for t in bt.tasks:
        t.func(*t.args, **t.kwargs)
    last = store.set_jobs[-1]
    assert last["status"] == expected_status and last["finished_at"].endswith("+05:30")
    assert bool(store.set_metas) is stamps_meta
