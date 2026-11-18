"""Round 632 (E658): the swing scan claim is released when its thread cannot start.

scan_in_thread sets state "running" under the lock and only the scan thread clears it. If the thread could not be
started the claim stayed "running" for ever: every later start (the 16:00 tick and the manual route) answered
"already running" until the service restarted, so no evening scan ran.

Plain functions (pytest runs them unchanged; `python3 tests/test_round632_swing_scan_claim.py` runs them without pytest). No database, no network."""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SVC))
for p in (SVC, os.path.join(REPO, "scripts"), os.path.join(REPO, "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from swing import engine  # noqa: E402


def _reset():
    with engine._state_lock:
        engine._state.update(running=False, last_attempt=0.0, last_result=None)


class _Boom:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


def test_claim_is_released_when_the_thread_cannot_start():
    _reset()
    real = engine.threading.Thread
    engine.threading.Thread = _Boom
    try:
        started = engine.scan_in_thread(lambda: None)
    finally:
        engine.threading.Thread = real
    st = engine.runtime_state()
    assert started is False
    assert st["running"] is False, st
    assert st["last_result"]["status"] == "error" and "thread could not start" in st["last_result"]["detail"]


def test_a_start_after_a_failed_thread_start_is_accepted():
    _reset()
    real = engine.threading.Thread
    engine.threading.Thread = _Boom
    try:
        engine.scan_in_thread(lambda: None)
    finally:
        engine.threading.Thread = real
    release = threading.Event()
    ran = []
    orig_universe = engine.universe_symbols
    engine.universe_symbols = lambda held: (ran.append(1), release.wait(5), [])[2]
    try:
        assert engine.scan_in_thread(lambda: _FakeDb()) is True
        time.sleep(0.1)
        assert engine.scan_in_thread(lambda: _FakeDb()) is False         # the real claim still refuses a second start
        release.set()
        for _ in range(100):
            if not engine.runtime_state()["running"]:
                break
            time.sleep(0.05)
    finally:
        engine.universe_symbols = orig_universe
        release.set()
    assert ran == [1] and engine.runtime_state()["running"] is False


class _FakeDb:
    def close(self):
        pass


def test_simultaneous_starts_still_run_one_scan():
    _reset()
    release = threading.Event()
    ran = []
    orig = engine.universe_symbols
    engine.universe_symbols = lambda held: (ran.append(1), release.wait(5), [])[2]
    res, gate = [], threading.Barrier(8)

    def go():
        gate.wait(5)
        res.append(engine.scan_in_thread(lambda: _FakeDb()))

    try:
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        time.sleep(0.2)
        release.set()
        for _ in range(100):
            if not engine.runtime_state()["running"]:
                break
            time.sleep(0.05)
    finally:
        engine.universe_symbols = orig
        release.set()
    assert res.count(True) == 1 and len(ran) == 1, (res, ran)


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
