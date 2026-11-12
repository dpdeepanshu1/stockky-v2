"""E481 (round 460): expire_stale_orders commits the EXPIRED status and its event BEFORE it releases the symbol lock, so a failed release
(which rolls the session back) can no longer discard the status and leave a PLACED order next to an EXPIRED event."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from entry_engine import entry
from execution import dhan_client
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401


def _stale(db, symbol, dhan_id):
    o = models.TradeOrder(mode="REAL", symbol=symbol, side="BUY", qty=5, order_type="LIMIT", status="PLACED",
                          dhan_order_id=dhan_id, filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _book(monkeypatch, ids):
    async def notify(t):
        return None
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [{"orderId": i, "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0} for i in ids])


def test_nothing_is_pending_in_the_session_when_the_lock_is_released(db, monkeypatch):
    _book(monkeypatch, ["D1"])
    o = _stale(db, "AAA", "D1")
    seen = {}

    def release(d, sym):
        seen["pending"] = bool(d.new or d.dirty or d.deleted)
        seen["status_in_db"] = d.query(models.TradeOrder.status).filter_by(id=o.id).scalar()
        seen["events"] = d.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="EXPIRED").count()
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    assert seen == {"pending": False, "status_in_db": "EXPIRED", "events": 1}


def test_a_release_that_raises_leaves_the_order_expired_with_its_event(db, monkeypatch):
    _book(monkeypatch, ["D1"])
    o = _stale(db, "AAA", "D1")

    def release(d, sym):
        d.rollback()                                   # what a failing release() does to the session
        raise RuntimeError("lock table unavailable")
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED"
    assert db.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="EXPIRED").count() == 1


def test_a_partly_filled_order_keeps_its_lock(db, monkeypatch):
    _book(monkeypatch, ["D1"])
    o = _stale(db, "AAA", "D1")
    o.filled_qty_so_far = 2
    o.status = "PARTIAL"
    db.commit()
    called = []
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda d, sym: called.append(sym))
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [{"orderId": "D1", "orderStatus": "CANCELLED", "filledQty": 2, "averageTradedPrice": 10.0}])
    run(entry.expire_stale_orders(db, "REAL"))
    assert called == []
