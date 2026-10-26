"""E201: after a successful Dhan cancel, an order that is NOT in the order book that was just read is "cannot tell", not "nothing
filled". expire_stale_orders must leave it open for reconcile (same never-assume rule reconcile applies), not mark it EXPIRED."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from entry_engine import entry
from execution import dhan_client, shared_symbol_lock
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401


@pytest.fixture(autouse=True)
def rig(db, monkeypatch):
    sent = []

    async def notify(t):
        sent.append(t)
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
    return sent


def _order(db):
    shared_symbol_lock.try_claim(db, "ABC")
    o = models.TradeOrder(mode="REAL", symbol="ABC", side="BUY", qty=10, order_type="LIMIT", status="PLACED",
                          dhan_order_id="D1", filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _locked(db):
    return db.query(models.SharedSymbolLock).filter_by(symbol="ABC").first() is not None


@pytest.mark.parametrize("book", [[], None, [{"orderId": "OTHER", "orderStatus": "CANCELLED", "filledQty": 0}]],
                         ids=["empty", "none", "other-orders-only"])
def test_order_absent_from_the_book_stays_open_with_lock_held(db, monkeypatch, rig, book):
    o = _order(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: book)
    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED" and _locked(db)
    assert db.query(models.TradeOrderEvent).filter_by(order_id=o.id).count() == 0
    assert rig == []                                    # no alert spam for a transient read


def test_order_present_in_the_book_still_expires(db, monkeypatch):
    o = _order(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0}])
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and not _locked(db)


def test_one_missing_order_does_not_stop_the_others(db, monkeypatch):
    a = _order(db)
    shared_symbol_lock.try_claim(db, "BBB")
    b = models.TradeOrder(mode="REAL", symbol="BBB", side="BUY", qty=5, order_type="LIMIT", status="PLACED",
                          dhan_order_id="D2", filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(b)
    db.commit()
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D2", "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0}])
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(a)
    db.refresh(b)
    assert a.status == "PLACED" and b.status == "EXPIRED"
