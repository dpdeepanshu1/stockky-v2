"""Round 631 (E657): the overnight orchestrator, the weekend hydrator and the symbol-master sync claim their
"running" state in the same locked step as the "already running" check.

Before, each start function read the job state, released the lock, spawned a thread and only that thread set
"running". Two starts arriving together both read "idle" and both spawned a worker.

Plain functions (pytest runs them unchanged; `python3 tests/test_round631_start_claims.py` runs them without pytest).
No network, no clock, no real files: the worker bodies are replaced by stand-ins."""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys
import tempfile
import threading
import time
import types

try:
    import httpx  # noqa: F401
except ImportError:                      # a bare interpreter without the service requirements
    sys.modules["httpx"] = types.ModuleType("httpx")

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler")
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
_n = itertools.count()


def _load(name):
    os.environ.setdefault("SCHEDULER_STATE_DIR", tempfile.mkdtemp())
    spec = importlib.util.spec_from_file_location(f"{name}_r631_{next(_n)}", os.path.join(_DIR, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _hammer(fn, threads=8):
    """Call fn from `threads` threads released together; return the list of results."""
    out, errs = [], []
    gate = threading.Barrier(threads)

    def go():
        try:
            gate.wait(5)
            out.append(fn())
        except Exception as e:  # pragma: no cover - a failure here fails the test below
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(threads)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not errs, errs
    return out


# ---------------------------------------------------------------- overnight orchestrator
def _orch():
    m = _load("overnight_orchestrator")
    runs, release = [], threading.Event()

    def fake_phase(phase):
        runs.append(phase)
        release.wait(5)
        m._set_job(status="done", message="ok")

    m._run_phase = fake_phase
    return m, runs, release


def test_overnight_simultaneous_starts_run_one_phase():
    m, runs, release = _orch()
    res = _hammer(lambda: m.start_phase_background("datafeed"))
    time.sleep(0.2)
    release.set()
    assert len(runs) == 1, runs
    assert sum(1 for r in res if r.get("started")) == 1
    assert sum(1 for r in res if r.get("already_running")) == len(res) - 1


def test_overnight_second_phase_is_refused_while_the_first_runs():
    m, runs, release = _orch()
    a = m.start_phase_background("datafeed")
    b = m.start_phase_background("premarket")
    time.sleep(0.1)
    release.set()
    assert a.get("started") and b.get("already_running")
    assert runs == ["datafeed"]


def test_overnight_a_later_start_works_once_the_job_ended():
    m, runs, release = _orch()
    release.set()
    m.start_phase_background("datafeed")
    for _ in range(50):
        if m.get_status().get("status") == "done":
            break
        time.sleep(0.05)
    assert m.get_status()["status"] == "done"
    assert m.start_phase_background("premarket").get("started")


def test_overnight_claim_is_released_when_the_thread_cannot_start():
    m, runs, release = _orch()
    real = m.threading.Thread

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    m.threading.Thread = Boom
    try:
        r = m.start_phase_background("datafeed")
    finally:
        m.threading.Thread = real
    assert r.get("ok") is False
    assert m.get_status()["status"] == "error"
    release.set()
    assert m.start_phase_background("datafeed").get("started")   # not stuck on "running"


# ---------------------------------------------------------------- weekend hydrator
def _hyd():
    m = _load("weekend_hydrator")
    runs, release = [], threading.Event()

    def fake_job(hour_idx, full):
        runs.append((hour_idx, full))
        release.wait(5)
        m._set_job(status="done", message="ok")

    m._run_job = fake_job
    return m, runs, release


def test_hydrator_simultaneous_starts_run_one_job():
    m, runs, release = _hyd()
    res = _hammer(lambda: m.start_hydrate_background(hour_idx=3))
    time.sleep(0.2)
    release.set()
    assert len(runs) == 1, runs
    assert sum(1 for r in res if r.get("started")) == 1
    assert sum(1 for r in res if r.get("already_running")) == len(res) - 1


def test_hydrator_stalled_job_can_still_be_restarted():
    m, runs, release = _hyd()
    release.set()
    m._set_job(status="running", message="old")
    m._HYDRATE_JOB["updated_epoch"] = time.time() - (m._STALE_AFTER_SEC + 60)   # no progress for longer than the stale limit
    r = m.start_hydrate_background()
    assert r.get("started"), r


def test_hydrator_claim_is_released_when_the_thread_cannot_start():
    m, runs, release = _hyd()
    real = m.threading.Thread

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    m.threading.Thread = Boom
    try:
        r = m.start_hydrate_background()
    finally:
        m.threading.Thread = real
    assert r.get("ok") is False
    assert m.get_hydrate_job()["status"] == "error"
    release.set()
    assert m.start_hydrate_background().get("started")


# ---------------------------------------------------------------- symbol master sync
def _sync():
    m = _load("symbol_master_sync")
    runs, release = [], threading.Event()

    def fake_run_sync():
        runs.append(1)
        release.wait(5)
        return {"ok": True}

    m.run_sync = fake_run_sync
    return m, runs, release


def test_sync_simultaneous_starts_run_one_sync():
    m, runs, release = _sync()
    res = _hammer(m.start_sync_background)
    time.sleep(0.2)
    release.set()
    for _ in range(50):
        if m.get_sync_job()["status"] == "done":
            break
        time.sleep(0.05)
    assert len(runs) == 1, runs
    assert sum(1 for r in res if r.get("status") == "started") == 1
    assert sum(1 for r in res if r.get("status") == "already_running") == len(res) - 1
    assert m.get_sync_job()["status"] == "done"
    assert m.get_sync_job()["result"] == {"ok": True}


def test_sync_claim_is_released_when_the_thread_cannot_start():
    m, runs, release = _sync()
    real = m.threading.Thread

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    m.threading.Thread = Boom
    try:
        r = m.start_sync_background()
    finally:
        m.threading.Thread = real
    assert r.get("status") == "error"
    assert m.get_sync_job()["status"] == "error"
    release.set()
    assert m.start_sync_background().get("status") == "started"


if __name__ == "__main__":
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
