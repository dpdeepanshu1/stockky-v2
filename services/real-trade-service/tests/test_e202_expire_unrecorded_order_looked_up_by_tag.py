"""E202: a stale REAL entry order with no dhan_order_id must be looked up at Dhan by its correlation tag (stk<id>) before it is
declared dead and its symbol freed - the BUY may be live although the id was never recorded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from entry_engine import entry
from execution import dhan_client, shared_symbol_lock
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401


@pytest.fixture(autouse=True)
def rig(db, monkeypatch):
    r = {"sent": [], "cancels": [], "cancel_error": None, "tags": [], "lookup": (None, True)}

    async def notify(t):
        r["sent"].append(t)

    def cancel(db_, *, is_armed, dhan_order_id):
        r["cancels"].append(dhan_order_id)
        if r["cancel_error"]:
            raise r["cancel_error"]
        return {}

    def lookup(db_, tag):
        r["tags"].append(tag)
        return r["lookup"]
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", cancel)
    monkeypatch.setattr(dhan_client, "find_order_by_tag", lookup)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D9", "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0}])
    return r


def _order(db, mode="REAL"):
    if mode == "REAL":
        shared_symbol_lock.try_claim(db, "ABC")
    o = models.TradeOrder(mode=mode, symbol="ABC", side="BUY", qty=10, order_type="LIMIT", status="PLACED",
                          dhan_order_id=None, filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _locked(db):
    return db.query(models.SharedSymbolLock).filter_by(symbol="ABC").first() is not None


def test_live_order_found_by_tag_is_adopted_then_cancelled_and_expired(db, rig):
    o = _order(db)
    rig["lookup"] = ({"orderId": "D9", "orderStatus": "PENDING"}, True)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert rig["tags"] == [f"stk{o.id}"] and rig["cancels"] == ["D9"]
    assert o.dhan_order_id == "D9" and o.status == "EXPIRED"
    assert any("Unrecorded BUY" in m for m in rig["sent"])


def test_adopted_order_whose_cancel_fails_stays_open_with_its_id_for_reconcile(db, rig):
    o = _order(db)
    rig["lookup"] = ({"orderId": "D9", "orderStatus": "TRADED"}, True)
    rig["cancel_error"] = RuntimeError("already traded")
    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED" and o.dhan_order_id == "D9" and _locked(db)


def test_unreadable_book_leaves_the_order_open_and_the_lock_held(db, rig):
    o = _order(db)
    rig["lookup"] = (None, False)
    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED" and _locked(db) and rig["cancels"] == []
    assert db.query(models.TradeOrderEvent).filter_by(order_id=o.id).count() == 0


def test_order_confirmed_absent_expires_without_a_cancel(db, rig):
    o = _order(db)
    rig["lookup"] = (None, True)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and not _locked(db) and rig["cancels"] == []


def test_demo_orders_never_query_dhan(db, rig):
    o = _order(db, mode="DEMO")
    assert run(entry.expire_stale_orders(db, "DEMO")) == 1
    assert rig["tags"] == [] and rig["cancels"] == []
