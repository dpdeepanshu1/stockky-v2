"""E198: shares that filled before the stale-order cancel landed must keep the symbol lock held, and a completely filled order must
stay FILLED. Decided by shares held (filled_qty_so_far), not by the status label."""
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

    async def _noop(*a, **k):
        return 0
    from execution import reconcile as R
    monkeypatch.setattr(R, "notify_async", _noop)
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
    o = models.TradeOrder(mode="REAL", decision_id=dec.id, symbol="ABC", side="BUY", qty=10, order_type="LIMIT",
                          status=status, dhan_order_id="D1", filled_qty_so_far=filled,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _locked(db):
    return db.query(models.SharedSymbolLock).filter_by(symbol="ABC").first() is not None


def _events(db, o):
    return [e.event_type for e in db.query(models.TradeOrderEvent).filter_by(order_id=o.id)]


def _book(monkeypatch, status, filled, price=100.0):
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": status, "orderType": "LIMIT",
         "averageTradedPrice": price, "filledQty": filled}])


def test_partial_late_fill_with_terminal_status_expires_but_keeps_lock(db, monkeypatch):
    o = _order(db)
    _book(monkeypatch, "CANCELLED", 3)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
    assert pos is not None and pos.qty_open == 3
    assert o.status == "EXPIRED" and o.filled_qty_so_far == 3
    assert _locked(db)                                            # real shares held -> lock stays until exit_engine closes them


def test_fully_filled_late_order_stays_filled_and_locked(db, monkeypatch):
    o = _order(db)
    _book(monkeypatch, "TRADED", 10)
    assert run(entry.expire_stale_orders(db, "REAL")) == 0        # not expired: nothing to retire
    db.refresh(o)
    pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
    assert pos is not None and pos.qty_open == 10
    assert o.status == "FILLED" and _locked(db)
    assert "EXPIRED" not in _events(db, o)


def test_placed_order_with_shares_already_booked_keeps_lock(db, monkeypatch):
    o = _order(db, status="PLACED", filled=3)                     # status never moved off PLACED, but 3 shares are held
    _book(monkeypatch, "CANCELLED", 3)                            # delta 0: nothing new to book
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and _locked(db)
    assert any("partially filled" in (e.detail or "") for e in db.query(models.TradeOrderEvent).filter_by(order_id=o.id))


def test_nothing_filled_still_expires_and_unlocks(db, monkeypatch):
    o = _order(db)
    _book(monkeypatch, "CANCELLED", 0, price=0.0)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and not _locked(db)
