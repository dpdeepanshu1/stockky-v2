"""E205: a stale order whose Dhan cancel keeps failing stays PLACED and is retried every pass; the operator is alerted once per
EXPIRE_CANCEL_FAIL_ALERT_S window per order, not once per pass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from entry_engine import entry
from execution import dhan_client
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401


@pytest.fixture(autouse=True)
def rig(db, monkeypatch):
    entry._cancel_fail_alerted.clear()
    monkeypatch.delenv("EXPIRE_CANCEL_FAIL_ALERT_S", raising=False)
    sent = []

    async def notify(t):
        sent.append(t)

    def cancel(db_, *, is_armed, dhan_order_id):
        raise RuntimeError("DH-906 gateway timeout")
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", cancel)
    yield sent
    entry._cancel_fail_alerted.clear()


def _stale(db, symbol="ABC", dhan_id="D1"):
    o = models.TradeOrder(mode="REAL", symbol=symbol, side="BUY", qty=5, order_type="LIMIT", status="PLACED",
                          dhan_order_id=dhan_id, filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def test_repeated_failures_alert_once(db, rig):
    o = _stale(db)
    for _ in range(4):
        assert run(entry.expire_stale_orders(db, "REAL")) == 0
    assert len(rig) == 1 and "cancel failed" in rig[0]
    db.refresh(o)
    assert o.status == "PLACED"                      # still retried / left for reconcile every pass


def test_each_order_gets_its_own_alert(db, rig):
    _stale(db, "AAA", "D1")
    _stale(db, "BBB", "D2")
    run(entry.expire_stale_orders(db, "REAL"))
    run(entry.expire_stale_orders(db, "REAL"))
    assert len(rig) == 2


def test_alert_repeats_after_the_window(db, rig):
    assert entry._cancel_fail_alert_due("D1", now=1000.0) is True
    assert entry._cancel_fail_alert_due("D1", now=1500.0) is False
    assert entry._cancel_fail_alert_due("D1", now=1900.0) is True      # 900 s after the first
    assert entry._cancel_fail_alert_due("D1", now=2000.0) is False


def test_zero_window_alerts_every_pass(db, rig, monkeypatch):
    monkeypatch.setenv("EXPIRE_CANCEL_FAIL_ALERT_S", "0")
    _stale(db)
    for _ in range(3):
        run(entry.expire_stale_orders(db, "REAL"))
    assert len(rig) == 3
