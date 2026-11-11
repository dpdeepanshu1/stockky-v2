"""E444 (round 424): /data-feed/stop is a no-op on a job that is known to be idle / done / error (even with the default force=True), and
a junk stored counter, cursor or env value no longer turns Run / Stop into a 500 or a job stuck at "stopping"."""
import asyncio

import pytest

from test_main_data_feed_run import execute, fr, ok_routes, seq, start  # noqa: F401  (fr is a fixture)
from test_main_repair_routes_stop_resume import gw, stopenv  # noqa: F401  (stopenv is a fixture)


def _run(coro):
    return asyncio.run(coro)


# ── _safe_int ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val,default,expected", [
    (5, 0, 5), ("12", 0, 12), ("1,200", 0, 1200), (12.9, 0, 12), (" 7 ", 0, 7),
    (None, 0, 0), ("", 3, 3), ("abc", 9, 9), (float("nan"), 4, 4), (float("inf"), 4, 4), ("inf", 4, 4), ([], 2, 2), ({}, 2, 2),
])
def test_safe_int(val, default, expected):
    assert gw._safe_int(val, default) == expected


# ── stop on a finished / idle job ─────────────────────────────────────────────
@pytest.mark.parametrize("status", ["done", "idle", "error"])
def test_force_stop_on_a_finished_job_changes_nothing(stopenv, status):
    stopenv.store.job_val = {"status": status, "processed": 10, "total": 10, "ok_count": 10, "message": "all fed"}
    before = dict(stopenv.store.job_val)
    out = _run(gw.data_feed_stop())                       # default force=True
    assert out["ok"] is True and out["stopped"] is False and out["detail"] == f"Not running (status={status})"
    assert stopenv.store.job_val == before and stopenv.store.set_jobs == [] and stopenv.store.metas == []
    assert stopenv.stop_calls == []                       # no stale stop flag left behind


@pytest.mark.parametrize("status", ["running", "stopping", "stopped", None])
def test_force_stop_still_commits_a_running_stopping_stopped_or_unknown_job(stopenv, status):
    stopenv.store.job_val = {"status": status, "processed": 3, "total": 9} if status else {"processed": 3, "total": 9}
    out = _run(gw.data_feed_stop())
    assert out["stopped"] is True and out["processed"] == 3 and stopenv.store.metas[0]["source"] == "stop"


# ── stop with junk counters ───────────────────────────────────────────────────
def test_stop_survives_junk_counters_and_still_commits(stopenv):
    stopenv.store.job_val = {"status": "running", "processed": "n/a", "total": "??", "ok_count": None, "error_count": "x",
                             "checkpoint": {"cursor": "abc", "done": ["A", "B"], "universe": []}}
    out = _run(gw.data_feed_stop())
    assert out["ok"] is True and out["stopped"] is True and out["status"] == "stopped"
    assert out["processed"] == 0 and out["total"] == 0 and out["ok_count"] == 2 and out["error_count"] == 0


def test_stop_clamps_negative_counters(stopenv):
    stopenv.store.job_val = {"status": "running", "processed": -4, "total": -1, "ok_count": -2}
    out = _run(gw.data_feed_stop())
    assert out["processed"] == 0 and out["total"] == 0 and out["ok_count"] == 0


def test_stop_reads_numeric_strings(stopenv):
    stopenv.store.job_val = {"status": "running", "total": "10", "checkpoint": {"cursor": "6"}}
    assert _run(gw.data_feed_stop())["processed"] == 6


# ── run with junk stored state / env ──────────────────────────────────────────
def test_resume_with_a_junk_cursor_and_counters_starts_from_zero(fr):
    seq(fr)
    fr.store.job_val = {"status": "stopped", "processed": "x", "ok_count": "y", "error_count": [], "checkpoint": {"cursor": "bad"}}
    out, bt = start(fr, resume=True)
    assert out["started"] is True and out["resume_from"] == 0
    assert fr.store.job_val["ok_count"] == 0 and fr.store.job_val["error_count"] == 0


def test_junk_max_symbols_env_means_no_cap(fr):
    fr.mp.setenv("DATA_FEED_MAX_SYMBOLS", "lots")
    out, bt = start(fr)
    assert out["total"] == 3


@pytest.mark.parametrize("conc", ["many", "0", "-2", ""])
def test_junk_concurrency_env_does_not_break_the_worker(fr, conc):
    seq(fr, DATA_FEED_FUND_CONCURRENCY=conc)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    execute(bt)
    assert fr.store.job_val["status"] == "done" and [p[0] for p in fr.store.puts] == ["AAA", "BBB", "CCC"]


def test_junk_batch_size_env_does_not_break_the_worker(fr):
    seq(fr, DATA_FEED_BATCH_SIZE="big")
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    execute(bt)
    assert fr.store.job_val["status"] == "done"
