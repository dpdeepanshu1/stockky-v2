"""E445 (round 425): an unexpected exception inside the background feed worker marks the job "error" (checkpoint kept) instead of leaving it
"running" for ever; a worker that a newer run replaced does not touch the row; Resume continues from the checkpoint after a crash."""
from test_main_data_feed_run import execute, fr, ok_routes, seq, start  # noqa: F401  (fr is a fixture)


def _crash_when_progress_reaches(fr, n, *, also_fail_error_write=False, supersede_first=False):
    real = fr.store.set_job

    def set_job(**kw):
        if kw.get("status") == "running" and kw.get("processed") == n:
            if supersede_first:
                fr.store.job_val["run_id"] = "newer-run"
            raise RuntimeError("boom")
        if also_fail_error_write and kw.get("status") == "error":
            raise RuntimeError("store down")
        return real(**kw)

    fr.store.set_job = set_job


def test_crash_mid_loop_marks_the_job_error_and_keeps_the_checkpoint(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    _crash_when_progress_reaches(fr, 2)
    out, bt = start(fr)
    execute(bt)                                              # must not raise
    job = fr.store.job_val
    assert job["status"] == "error" and job["message"].startswith("Data feed crashed: boom")
    assert job["stop_requested"] is False and job.get("finished_at")
    assert job["checkpoint"]["cursor"] == 1 and job["checkpoint"]["done"] == ["AAA"] and job["ok_count"] == 1
    assert [p[0] for p in fr.store.puts] == ["AAA", "BBB"]


def test_resume_after_a_crash_continues_from_the_checkpoint(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    _crash_when_progress_reaches(fr, 2)
    out, bt = start(fr)
    execute(bt)
    fr.store.set_job = type(fr.store).set_job.__get__(fr.store)      # store healthy again
    out2, bt2 = start(fr, resume=True)
    assert out2["started"] is True and out2["mode"] == "resume" and out2["resume_from"] == 1


def test_a_replaced_worker_that_crashes_does_not_overwrite_the_newer_run(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    _crash_when_progress_reaches(fr, 2, supersede_first=True)
    out, bt = start(fr)
    execute(bt)
    assert fr.store.job_val["status"] == "running" and fr.store.job_val["run_id"] == "newer-run"


def test_a_failing_error_write_is_swallowed(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    _crash_when_progress_reaches(fr, 2, also_fail_error_write=True)
    out, bt = start(fr)
    execute(bt)                                              # still must not raise
    assert fr.store.job_val["status"] == "running"           # nothing could be written; the crash is only logged


def test_a_clean_run_is_unchanged(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    execute(bt)
    assert fr.store.job_val["status"] == "done"
