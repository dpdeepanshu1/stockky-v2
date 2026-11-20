"""round 668 (D7e): reconcile_real_orders runs its blocking steps (orphan repair, holdings sync, pending-orders query, Dhan order list,
two commits) in worker threads. Behaviour of the helper, its kill-switch, and that the call sites use it."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from execution import reconcile as R

_SRC = open(R.__file__, encoding="utf-8").read()


def test_off_runs_in_worker_thread_and_loop_stays_free(monkeypatch):
    monkeypatch.delenv("RECONCILE_OFF_LOOP", raising=False)
    seen = {}

    def slow():
        seen["t"] = threading.current_thread().name
        time.sleep(0.4)
        return 5

    async def go():
        ticks = 0
        task = asyncio.create_task(R._off(slow))
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        return await task, ticks

    val, ticks = asyncio.run(go())
    assert val == 5 and ticks >= 10
    assert seen["t"] != threading.main_thread().name


def test_kill_switch_runs_inline(monkeypatch):
    monkeypatch.setenv("RECONCILE_OFF_LOOP", "0")
    seen = {}

    def f(a, b=1):
        seen["t"] = threading.current_thread().name
        return a + b

    assert asyncio.run(R._off(f, 2, b=3)) == 5
    assert seen["t"] == threading.main_thread().name


def test_off_propagates_exceptions():
    def boom():
        raise ValueError("x")

    try:
        asyncio.run(R._off(boom))
        assert False
    except ValueError:
        pass


def test_call_sites_use_the_helper():
    for needle in ("await _off(_repair_orphaned_pending_exits", "await _off(holdings_sync_reconcile", "await _off(dhan_client.get_order_list",
                   "await _off(lambda: db.query(models.TradeOrder)"):
        assert needle in _SRC, needle
    assert _SRC.count("await _off(db.commit)") == 2
    # the original blocking forms are gone from the async function
    body = _SRC[_SRC.index("async def reconcile_real_orders"):]
    for bad in ("\n    _repair_orphaned_pending_exits(", "= holdings_sync_reconcile(db)", "= dhan_client.get_order_list(db)"):
        assert bad not in body, bad
