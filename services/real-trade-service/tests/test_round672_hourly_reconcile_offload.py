"""round 672 (D7g): the hourly broker-vs-book pass (GET /reconcile-report/REAL?run=true and the auto-pilot tick) runs its blocking part
(two Dhan HTTP calls, the book / order queries, eDIS marker lookups) in a worker thread. The report it builds is unchanged."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from execution import hourly_reconcile as hr

_NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)


def _reset():
    hr._last_run_at = None
    hr._last_report = None
    hr._last_alert_sig = None
    hr._last_alert_at = None


async def _quiet_notify(_text):
    return None


def test_collect_runs_in_worker_thread_and_loop_stays_free(monkeypatch):
    monkeypatch.delenv("RECONCILE_OFF_LOOP", raising=False)
    _reset()
    seen = {}

    def slow_collect(db, now):
        seen["t"] = threading.current_thread().name
        time.sleep(0.4)
        return [{"kind": "missing_at_broker", "severity": "warn", "symbol": "ABC", "book_qty": 3, "broker_qty": 0}], 2

    monkeypatch.setattr(hr, "_collect", slow_collect)

    async def go():
        ticks = 0
        task = asyncio.create_task(hr.run_hourly_reconcile(object(), notify=_quiet_notify, now=_NOW))
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        return await task, ticks

    report, ticks = asyncio.run(go())
    assert ticks >= 10                       # the loop kept running while the pass waited
    assert seen["t"] != threading.main_thread().name
    assert report["warn_count"] == 1 and report["book_positions"] == 2 and report["error"] is None
    assert report["alerted"] is True         # alerting still happens on the loop, as before


def test_kill_switch_runs_inline(monkeypatch):
    monkeypatch.setenv("RECONCILE_OFF_LOOP", "0")
    _reset()
    seen = {}

    def collect(db, now):
        seen["t"] = threading.current_thread().name
        return [], 0

    monkeypatch.setattr(hr, "_collect", collect)
    report = asyncio.run(hr.run_hourly_reconcile(object(), notify=_quiet_notify, now=_NOW))
    assert seen["t"] == threading.current_thread().name
    assert report["drift"] == [] and report["warn_count"] == 0 and report["error"] is None


def test_collect_error_gives_an_error_report_and_never_raises(monkeypatch):
    monkeypatch.delenv("RECONCILE_OFF_LOOP", raising=False)
    _reset()

    def boom(db, now):
        raise RuntimeError("broker down")

    monkeypatch.setattr(hr, "_collect", boom)
    report = asyncio.run(hr.run_hourly_reconcile(object(), notify=_quiet_notify, now=_NOW))
    assert report["error"] == "RuntimeError: broker down"
    assert report["drift"] == [] and hr.last_report() is report
    assert "alerted" not in report


def test_run_has_no_blocking_call_left_on_the_loop():
    src = open(hr.__file__, encoding="utf-8").read()
    body = src[src.index("async def run_hourly_reconcile"):]
    assert "dhan_client." not in body and "db.query" not in body
    assert "await _off(_collect" in body
