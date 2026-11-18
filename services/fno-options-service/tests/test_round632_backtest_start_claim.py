"""Round 632 (E658): a backtest whose thread cannot start no longer stays "running" (and no longer blocks every later run with 409).

main.py needs fastapi / pydantic to import, so the real `backtest_run` function source is cut out of main.py and run
against small stand-ins (the same function body, not a copy). Plain functions; `python3 tests/test_round632_backtest_start_claim.py` runs them without pytest."""
from __future__ import annotations

import ast
import os
import threading
import time
import types

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(status_code, detail)
        self.status_code, self.detail = status_code, detail


def _load(thread_cls=None, run_job=None):
    src = open(MAIN, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "backtest_run")
    fn.decorator_list = []
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    jobs = {}
    th = types.SimpleNamespace(Thread=thread_cls or threading.Thread)
    g = {
        "bt_report": types.SimpleNamespace(request_problem=lambda *a, **k: None),
        "strategies": types.SimpleNamespace(ORDER=[]),
        "HTTPException": _HTTPException, "Depends": lambda x=None: None, "require_admin": None,
        "_jobs": jobs, "_jobs_lock": threading.Lock(), "time": time, "threading": th,
        "_run_job": run_job or (lambda job_id, body: None), "log": types.SimpleNamespace(warning=lambda *a, **k: None),
    }
    exec(compile(mod, MAIN, "exec"), g)
    return g, jobs


def _body(**kw):
    d = dict(kind="sim", underlyings=["nifty"], strategy="CORE", days=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


def test_a_failed_thread_start_marks_the_job_failed_and_answers_503():
    g, jobs = _load(thread_cls=_Boom)
    try:
        g["backtest_run"](_body(), None)
        raise AssertionError("expected a 503")
    except _HTTPException as e:
        assert e.status_code == 503
    (job,) = jobs.values()
    assert job["state"] == "failed" and "thread could not start" in job["error"]


def test_the_next_run_is_accepted_after_a_failed_thread_start():
    g, jobs = _load(thread_cls=_Boom)
    try:
        g["backtest_run"](_body(), None)
    except _HTTPException:
        pass
    g["threading"].Thread = threading.Thread
    time.sleep(1.01)                                    # the job id carries the second
    r = g["backtest_run"](_body(), None)
    assert r["job"] in jobs and jobs[r["job"]]["state"] == "running"


def test_a_running_job_still_refuses_a_second_start_with_409():
    release = threading.Event()
    g, jobs = _load(run_job=lambda job_id, body: release.wait(5))
    g["backtest_run"](_body(), None)
    try:
        g["backtest_run"](_body(), None)
        raise AssertionError("expected a 409")
    except _HTTPException as e:
        assert e.status_code == 409
    finally:
        release.set()


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
