"""Round 634 (E660): the baseline job's "is one running? / now one is" check is one step.

Before, `precalculate_surprise_baselines` read `_job_lock` and set it in two separate statements with no lock around them, so two
callers arriving together (a cron dispatch and a click, or any caller that does not go through the route's own claim) could both
pass the check and both run the job. `_claim_job()` now does the check and the set under `_job_guard`. Plain functions; run with
pytest or by hand."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import surprise_premarket as sp


def _reset():
    sp._job_lock = False


def test_simultaneous_claims_give_exactly_one_winner():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(200):
            _reset()
            n, bar, wins = 8, threading.Barrier(8), []

            def go():
                bar.wait()
                if sp._claim_job():
                    wins.append(1)

            ts = [threading.Thread(target=go) for _ in range(n)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            assert len(wins) == 1, f"{len(wins)} callers claimed the job at once"
    finally:
        sys.setswitchinterval(old)
        _reset()


def test_second_caller_is_refused_while_the_first_runs_and_the_lock_is_freed_after():
    entered, release, results = threading.Event(), threading.Event(), []
    real = sp.ensure_schema

    def slow_schema():
        entered.set()
        release.wait(5)
        return False

    sp.ensure_schema = slow_schema
    try:
        _reset()
        first = threading.Thread(target=lambda: results.append(sp.precalculate_surprise_baselines(["RELIANCE"])))
        first.start()
        assert entered.wait(5)
        second = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert second["ok"] is False and second["error"] == "already_running"
        assert sp._job_lock is True, "the refused call must not free the running job's lock"
        release.set()
        first.join(5)
        assert results and results[0]["error"] == "schema_failed"
        assert sp._job_lock is False
        assert sp._claim_job() is True
    finally:
        sp.ensure_schema = real
        _reset()


def test_a_flag_set_directly_still_refuses():
    _reset()
    sp._job_lock = True
    try:
        r = sp.precalculate_surprise_baselines(["RELIANCE"])
        assert r["ok"] is False and r["error"] == "already_running"
        assert sp._job_lock is True
    finally:
        _reset()
