"""tests/test_group466_close_all.py - offline tests for the close-all and disarm sequence (group 466).

Pure unit tests: the three steps (disarm, cancel BUYs, sell positions) and the two locks are replaced with fakes, so this checks
ORDER, the confirm gate, the locks-busy path and the never-raises contract without a database or a broker.
Run from services/real-trade-service:  python3 -m pytest tests/test_group466_close_all.py -q
"""
from __future__ import annotations

import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from execution import close_all as ca


@pytest.fixture()
def rig(monkeypatch):
    calls: list = []
    entry, exit_ = threading.Lock(), threading.Lock()
    fake_ap = types.ModuleType("execution.auto_pilot")
    fake_ap._get_lock = lambda mode: entry
    fake_ap._get_exit_lock = lambda mode: exit_
    monkeypatch.setitem(sys.modules, "execution.auto_pilot", fake_ap)
    monkeypatch.setattr(ca, "disarm_mode", lambda db, mode, actor, reason: calls.append("disarm") or True)
    monkeypatch.setattr(ca, "cancel_resting_buys", lambda db, mode: calls.append("cancel") or {"cancelled": 2, "failed": []})
    monkeypatch.setattr(ca, "sell_all_positions",
                        lambda db, mode, ticks=None: calls.append("sell") or
                        {"found": 3, "closed": 0, "sent": 3, "skipped_pending": [], "failed": []})
    monkeypatch.setattr(ca, "_audit", lambda db, actor, mode, r: None)
    return calls, entry, exit_


def test_happy_path_order_and_summary(rig):
    calls, entry, exit_ = rig
    r = ca.run_close_all(None, "REAL", actor="a", confirmed=True, lock_wait_s=0)
    assert calls == ["disarm", "cancel", "sell"]
    assert r["ok"] is True and r["buys_cancelled"] == 2 and r["sent"] == 3
    assert entry.locked() is False and exit_.locked() is False
    assert "resting BUY cancelled" in r["summary"] and "3 of 3" in r["summary"]


@pytest.mark.parametrize("v", ["CLOSE ALL", " close all ", "Close All"])
def test_confirm_ok_accepts(v):
    assert ca.confirm_ok(v)


@pytest.mark.parametrize("v", ["", "CLOSE", None, 1, "closeall"])
def test_confirm_ok_rejects(v):
    assert not ca.confirm_ok(v)


def test_locks_busy(rig):
    calls, entry, exit_ = rig
    assert entry.acquire(blocking=False)
    try:
        r = ca.run_close_all(None, "REAL", actor="a", confirmed=True, lock_wait_s=0)
    finally:
        entry.release()
    assert r["locks_busy"] is True and calls == ["disarm"]  # disarmed but nothing else
    assert exit_.locked() is False


def test_exit_lock_busy_releases_entry_lock(rig):
    calls, entry, exit_ = rig
    assert exit_.acquire(blocking=False)
    try:
        r = ca.run_close_all(None, "REAL", actor="a", confirmed=True, lock_wait_s=0)
    finally:
        exit_.release()
    assert r["locks_busy"] is True and calls == ["disarm"]
    assert entry.locked() is False   # must not leak the entry lock


def test_not_confirmed(rig):
    calls, entry, exit_ = rig
    r = ca.run_close_all(None, "REAL", actor="a", confirmed=False)
    assert r["ok"] is False and calls == []


def test_bad_mode(rig):
    assert ca.run_close_all(None, "XXX", actor="a", confirmed=True)["ok"] is False


def test_failures_make_ok_false_and_are_named(rig, monkeypatch):
    monkeypatch.setattr(ca, "sell_all_positions",
                        lambda db, mode, ticks=None: {"found": 2, "closed": 0, "sent": 1, "skipped_pending": ["ABC"], "failed": ["XYZ"]})
    r = ca.run_close_all(None, "REAL", actor="a", confirmed=True, lock_wait_s=0)
    assert r["ok"] is False and "FAILED: XYZ" in r["summary"] and "ABC" in r["summary"]
