"""E511: expire_stale_orders writes AND commits the EXPIRED status + its event BEFORE it releases the symbol lock.

shared_symbol_lock.release() rolls the session back when it fails. The status and the event used to be pending in the session at that moment, so
a failing release threw them away: the order looked live again and the audit row was lost. Now the expiry is committed first; a release that
fails afterwards is logged and does not undo it (the lock sweeper frees the stale lock)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import models
from entry_engine import entry
from execution import dhan_client
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401


def _book(monkeypatch, ids):
    async def notify(t):
        return None
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": i, "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0} for i in ids])


def _stale(db, symbol, dhan_id):
    o = models.TradeOrder(mode="REAL", symbol=symbol, side="BUY", qty=5, order_type="LIMIT", status="PLACED",
                          dhan_order_id=dhan_id, filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def test_nothing_is_pending_in_the_session_when_the_lock_is_released(db, monkeypatch):
    _book(monkeypatch, ["D1"])
    o = _stale(db, "AAA", "D1")
    seen = {}

    def release(d, sym):
        seen["pending"] = bool(d.new or d.dirty)
        seen["events"] = d.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="EXPIRED").count()
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    assert seen == {"pending": False, "events": 1}


def test_a_release_that_raises_leaves_the_order_expired_with_its_event(db, monkeypatch):
    _book(monkeypatch, ["D1"])
    o = _stale(db, "AAA", "D1")

    def release(d, sym):
        d.rollback()                                   # what a failing release() does to the session
        raise RuntimeError("lock table unavailable")
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.expire_all()
    assert db.query(models.TradeOrder).filter_by(id=o.id).one().status == "EXPIRED"
    assert db.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="EXPIRED").count() == 1


def test_a_failed_release_on_one_order_does_not_stop_the_next(db, monkeypatch):
    _book(monkeypatch, ["D1", "D2"])
    a, b = _stale(db, "AAA", "D1"), _stale(db, "BBB", "D2")
    released = []

    def release(d, sym):
        if sym == "AAA":
            d.rollback()
            raise RuntimeError("busy")
        released.append(sym)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)
    assert run(entry.expire_stale_orders(db, "REAL")) == 2
    assert released == ["BBB"]
    db.expire_all()
    assert {o.status for o in db.query(models.TradeOrder).all()} == {"EXPIRED"}
