"""Round 638 (E664): loop_watchdog.start() that fails part-way frees its claim instead of staying "started" for ever.

`start` set `_state["started"] = True` and then asked for the running loop, created the heartbeat task and started the
watchdog thread. A failure in any of those (no running loop, "can't start new thread") left the flag True: no later call
tried again, and /ops/loop-lag reported running with no watchdog. Stdlib only.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import loop_watchdog as lw  # noqa: E402


def _reset():
    lw._state.update({"hb": 0.0, "loop_thread": None, "started": False, "worst_stall_s": 0.0,
                      "worst_stall_at": None, "stall_count": 0, "last_stack": None})


class _FailThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class _NoopThread:
    started = 0

    def __init__(self, *a, **k):
        pass

    def start(self):
        _NoopThread.started += 1


def test_no_running_loop_frees_the_claim():
    _reset()
    os.environ.pop("GATEWAY_LOOP_WATCHDOG", None)
    assert lw.start() is False            # no loop is running here: get_running_loop raises
    assert lw._state["started"] is False
    assert lw.snapshot()["running"] is False


def test_failed_thread_start_frees_the_claim_and_cancels_the_heartbeat():
    _reset()
    os.environ.pop("GATEWAY_LOOP_WATCHDOG", None)
    real = threading.Thread
    out = {}

    async def go():
        threading.Thread = _FailThread
        try:
            out["first"] = lw.start()
        finally:
            threading.Thread = real
        out["claim"] = lw._state["started"]
        await asyncio.sleep(0.05)          # let the cancelled heartbeat settle
        _NoopThread.started = 0
        threading.Thread = _NoopThread
        try:
            out["second"] = lw.start()
        finally:
            threading.Thread = real
        out["claim2"] = lw._state["started"]
        out["threads"] = _NoopThread.started
        out["again"] = lw.start()
        out["threads_after"] = _NoopThread.started
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
    asyncio.run(go())
    assert out["first"] is False and out["claim"] is False
    assert out["second"] is True and out["claim2"] is True
    assert out["threads"] == 1
    assert out["again"] is True and out["threads_after"] == 1      # idempotent once started


def test_disabled_still_returns_false_without_claim():
    _reset()
    os.environ["GATEWAY_LOOP_WATCHDOG"] = "0"
    try:
        assert lw.start() is False
        assert lw._state["started"] is False
    finally:
        os.environ.pop("GATEWAY_LOOP_WATCHDOG", None)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
