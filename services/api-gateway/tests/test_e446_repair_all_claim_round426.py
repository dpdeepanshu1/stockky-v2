"""E446 (round 426): POST /repair-all claims the job before it responds (a double click / retry cannot start two jobs), and Refill All /
Repair Batch audit fresh (cache=False) instead of acting on the 20 s memo."""
import asyncio

from fastapi import BackgroundTasks

from test_main_repair_routes_stop_resume import audit, gw, job, patch1, sleeps  # noqa: F401  (fixtures)


def _run(coro):
    return asyncio.run(coro)


def test_route_marks_the_job_running_before_the_task_starts(job):
    bt = BackgroundTasks()
    out = _run(gw.repair_all_missing(bt, limit=7))
    assert out["started"] is True and len(bt.tasks) == 1
    assert job["status"] == "running" and job["message"] == "Refill All queued…" and job["started_at"].endswith("+05:30")


def test_a_second_click_before_the_task_runs_is_already_running_and_queues_nothing(job):
    first, second = BackgroundTasks(), BackgroundTasks()
    assert _run(gw.repair_all_missing(first))["started"] is True
    out = _run(gw.repair_all_missing(second))
    assert out["already_running"] is True and second.tasks == [] and len(first.tasks) == 1


def test_the_claim_resets_a_previous_runs_counters_and_cancel_flag(job):
    job.update({"status": "stopped", "total": 40, "processed": 12, "ok_count": 9, "last_symbol": "XYZ", "cancel_requested": True,
                "finished_at": "2026-11-10T10:00:00+05:30"})
    _run(gw.repair_all_missing(BackgroundTasks()))
    assert (job["total"], job["processed"], job["ok_count"], job["last_symbol"]) == (0, 0, 0, None)
    assert job["cancel_requested"] is False and job["finished_at"] is None


def test_queued_job_still_runs_to_done(job, patch1, audit, sleeps):
    audit.incomplete = ["A", "B"]
    bt = BackgroundTasks()
    _run(gw.repair_all_missing(bt))
    for t in bt.tasks:
        _run(t.func(*t.args, **t.kwargs))
    assert job["status"] == "done" and job["processed"] == 2


def test_refill_all_audits_fresh(job, patch1, sleeps, monkeypatch):
    seen = []

    async def spy(limit=500, cache=True):
        seen.append(cache)
        return {"incomplete_stocks": []}

    monkeypatch.setattr(gw, "audit_missing_feed_data", spy)
    _run(gw._run_refill_all_job(10))
    assert seen == [False]


def test_repair_batch_audits_fresh(patch1, sleeps, monkeypatch):
    seen = []

    async def spy(limit=500, cache=True):
        seen.append(cache)
        return {"incomplete_stocks": []}

    monkeypatch.setattr(gw, "audit_missing_feed_data", spy)
    _run(gw.repair_batch_missing(5))
    assert seen == [False]
