"""group302: event-loop stall watchdog (loop_watchdog.py). Stdlib only, so these run without the gateway app."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import loop_watchdog as lw  # noqa: E402


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _reset(hb=0.0):
    lw._state.update({"hb": hb, "loop_thread": threading.get_ident(), "started": False, "worst_stall_s": 0.0,
                      "worst_stall_at": None, "stall_count": 0, "last_stack": None})


def _capture():
    h = _Cap()
    lw.logger.addHandler(h)
    return h


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_LOOP_WATCHDOG", "0")
    _reset()
    assert lw.enabled() is False
    assert lw.start() is False
    assert lw._state["started"] is False


def test_no_heartbeat_yet_is_ignored():
    _reset(hb=0.0)
    assert lw.check_once(100.0, 3.0, 30.0, 0.0) == 0.0
    assert lw._state["stall_count"] == 0


def test_short_gap_is_not_a_stall():
    _reset(hb=100.0)
    assert lw.check_once(101.0, 3.0, 30.0, 0.0) == 0.0
    assert lw._state["stall_count"] == 0


def test_long_gap_logs_once_with_stack_and_records_worst():
    _reset(hb=100.0)
    h = _capture()
    try:
        t = lw.check_once(105.0, 3.0, 30.0, 0.0)
        assert t == 105.0
        # still stalled 2 s later: counted, but not logged again inside the quiet period
        t2 = lw.check_once(107.0, 3.0, 30.0, t)
        assert t2 == 105.0
    finally:
        lw.logger.removeHandler(h)
    assert len(h.msgs) == 1 and "event loop blocked for 5.0s" in h.msgs[0]
    assert lw._state["stall_count"] == 2
    assert lw._state["worst_stall_s"] == 7.0
    assert lw._state["worst_stall_at"]


def test_logs_again_after_the_quiet_period():
    _reset(hb=100.0)
    h = _capture()
    try:
        t = lw.check_once(105.0, 3.0, 30.0, 0.0)
        t = lw.check_once(140.0, 3.0, 30.0, t)
    finally:
        lw.logger.removeHandler(h)
    assert len(h.msgs) == 2 and t == 140.0


def test_loop_thread_stack_points_at_the_blocking_line():
    _reset()
    stack = lw._loop_thread_stack()
    assert stack and "test_loop_thread_stack_points_at_the_blocking_line" in stack


def test_snapshot_shape():
    _reset(hb=time.monotonic())
    snap = lw.snapshot()
    assert set(snap) == {"enabled", "running", "current_lag_s", "worst_stall_s", "worst_stall_at", "stall_count",
                         "last_stack"}


def test_heartbeat_task_stamps_the_loop():
    _reset()

    async def go():
        t = asyncio.ensure_future(lw._heartbeat())
        await asyncio.sleep(0.05)
        first = lw._state["hb"]
        t.cancel()
        return first

    assert asyncio.run(go()) > 0
    assert lw._state["loop_thread"] == threading.get_ident()
