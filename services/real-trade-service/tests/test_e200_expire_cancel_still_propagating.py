"""E200: a successful Dhan cancel request is not proof the order is dead. If Dhan's book still shows the order live right after the
cancel, expire_stale_orders must leave it open (and book any shares seen as PARTIAL), not mark it EXPIRED and free the symbol."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from tests.account_seed import seed_account
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

    async def _noop(*a, **k):                      # booking a fill notifies through reconcile's own notify_async
        return 0
    from execution import reconcile as R
    monkeypatch.setattr(R, "notify_async", _noop)
    return sent


def _order(db):
    seed_account(db, "REAL", starting_capital=100_000.0, current_equity=100_000.0, cash_available=100_000.0, broker_cash_available=100_000.0, realized_pnl_today=0.0, realized_pnl_total=0.0)
    dec = models.TradeDecision(mode="REAL", symbol="ABC", decision_type="ENTRY", action="BUY",
                               proposed_stop=97.0, proposed_target=106.0)
    db.add(dec)
    db.commit()
    shared_symbol_lock.try_claim(db, "ABC")
    o = models.TradeOrder(mode="REAL", decision_id=dec.id, symbol="ABC", side="BUY", qty=10, order_type="LIMIT",
                          status="PLACED", dhan_order_id="D1", filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def _locked(db):
    return db.query(models.SharedSymbolLock).filter_by(symbol="ABC").first() is not None


def _book(monkeypatch, status, filled=0, price=0.0):
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": "D1", "orderStatus": status, "orderType": "LIMIT",
         "averageTradedPrice": price, "filledQty": filled}])


@pytest.mark.parametrize("status", ["PENDING", "TRANSIT", "CONFIRM", "PART_TRADED"])
def test_order_still_live_after_cancel_stays_open_quietly(db, monkeypatch, rig, status):
    o = _order(db)
    _book(monkeypatch, status)
    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    assert o.status == "PLACED" and _locked(db)
    assert "EXPIRED" not in [e.event_type for e in db.query(models.TradeOrderEvent).filter_by(order_id=o.id)]
    assert rig == []                      # transient state: no alert


def test_shares_seen_while_still_live_are_booked_and_order_stays_partial(db, monkeypatch):
    o = _order(db)
    _book(monkeypatch, "PART_TRADED", filled=3, price=100.0)
    assert run(entry.expire_stale_orders(db, "REAL")) == 0
    db.refresh(o)
    pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
    assert pos is not None and pos.qty_open == 3
    assert o.status == "PARTIAL" and o.filled_qty_so_far == 3     # still visible to reconcile, not FILLED / EXPIRED
    assert _locked(db)


def test_next_pass_books_the_rest_once_the_cancel_has_landed(db, monkeypatch):
    o = _order(db)
    _book(monkeypatch, "PART_TRADED", filled=3, price=100.0)
    run(entry.expire_stale_orders(db, "REAL"))
    _book(monkeypatch, "CANCELLED", filled=5, price=100.0)         # 2 more filled before the cancel took effect
    run(entry.expire_stale_orders(db, "REAL"))
    pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
    assert pos.qty_open == 5


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "REJECTED", "EXPIRED", ""])
def test_terminal_or_blank_status_with_no_fills_still_expires_and_unlocks(db, monkeypatch, status):
    o = _order(db)
    _book(monkeypatch, status)
    assert run(entry.expire_stale_orders(db, "REAL")) == 1
    db.refresh(o)
    assert o.status == "EXPIRED" and not _locked(db)
