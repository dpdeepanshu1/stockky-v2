"""E199: expire_stale_orders must not mark a REAL entry EXPIRED (or release the symbol lock) when the
post-cancel fill check could not be completed. reconcile_real_orders() only queries PLACED/PARTIAL
orders, so an EXPIRED label would hide a possible real fill from it for good."""
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


def _order(db, status="PLACED", filled=0):
    db.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0,
                               cash_available=100_000.0, broker_cash_available=100_000.0,
                               realized_pnl_today=0.0, realized_pnl_total=0.0))
    dec = models.TradeDecision(mode="REAL", symbol="ABC", decision_type="ENTRY", action="BUY",
                               proposed_stop=97.0, proposed_target=106.0)
    db.add(dec)
    db.commit()
    shared_symbol_lock.try_claim(db, "ABC")
    o = models.TradeOrder(mode="REAL", decision_id=dec.id, symbol="ABC", side="BUY", qty=10,
                          order_type="LIMIT", status=status, dhan_order_id="D1", filled_qty_so_far=filled,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _locked(db):
    return db.query(models.SharedSymbolLock).filter_by(symbol="ABC").first() is not None


def _events(db, o):
    return [e.event_type for e in db.query(models.TradeOrderEvent).filter_by(order_id=o.id).all()]


def test_order_list_read_failure_leaves_order_open_and_lock_held(db, monkeypatch, rig):
    o = _order(db)

    def boom(d):
        raise RuntimeError("DH-905 order book unavailable")
    monkeypatch.setattr(dhan_client, "get_order_list", boom)

    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED"
    assert "EXPIRED" not in _events(db, o)
    assert _locked(db)
    assert any("unresolved" in m for m in rig)


def test_fills_without_a_price_leave_order_open_and_lock_held(db, monkeypatch):
    o = _order(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": "CANCELLED", "averageTradedPrice": 0.0, "filledQty": 3}])

    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED" and _locked(db)
    assert db.query(models.TradePosition).filter_by(symbol="ABC").count() == 0


def test_booking_failure_is_rolled_back_and_order_stays_open(db, monkeypatch):
    from execution import reconcile as R
    o = _order(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": "CANCELLED", "averageTradedPrice": 100.0, "filledQty": 3}])

    async def half_booked(db_, order, price, delta, is_partial, cumulative_qty=None):
        order.filled_qty_so_far = (order.filled_qty_so_far or 0) + delta   # what the real one does first
        raise RuntimeError("position write failed")
    monkeypatch.setattr(R, "_book_fill_delta", half_booked)

    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED"
    assert (o.filled_qty_so_far or 0) == 0          # half-done bookkeeping discarded, reconcile sees the full delta
    assert _locked(db)


def test_reconcile_books_the_fill_after_an_unresolved_expire(db, monkeypatch):
    from execution import reconcile as R
    o = _order(db)
    state = {"fail": True}

    def book(d):
        if state["fail"]:
            raise RuntimeError("DH-905 order book unavailable")
        return [{"orderId": "D1", "orderStatus": "CANCELLED", "orderType": "LIMIT",
                 "averageTradedPrice": 100.0, "filledQty": 3}]
    monkeypatch.setattr(dhan_client, "get_order_list", book)

    async def _noop(*a, **k):
        return 0
    monkeypatch.setattr(R, "notify_async", _noop)
    monkeypatch.setattr(R, "import_broker_holdings", _noop)
    monkeypatch.setattr(R, "holdings_sync_reconcile", lambda d: {"closed": 0, "symbols": []})

    run(entry.expire_stale_orders(db, "REAL"))      # check fails -> order must stay visible to reconcile
    state["fail"] = False
    run(R.reconcile_real_orders(db))
    pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
    assert pos is not None and pos.qty_open == 3


def test_clean_check_with_no_fills_still_expires_and_unlocks(db, monkeypatch):
    o = _order(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": "CANCELLED", "averageTradedPrice": 0.0, "filledQty": 0}])

    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and not _locked(db)
