"""E203: one bad order must not abort check_pending_fills / expire_stale_orders for the others (and so must not abort the cycle
before the exit stage runs). The failing order is rolled back and retried next pass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from entry_engine import entry
from execution import dhan_client
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401
from tests.test_rt_entry_helpers import Tick, demo_order


def _demo_account(db):
    db.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0, current_equity=100_000.0,
                               cash_available=100_000.0, broker_cash_available=0.0,
                               realized_pnl_today=0.0, realized_pnl_total=0.0))
    db.commit()


def _quotes(monkeypatch, symbols, price=99.0):
    async def get_quotes(syms):
        return {s: Tick(s, price, datetime.now(timezone.utc), None, "t") for s in symbols}
    monkeypatch.setattr(entry, "get_quotes", get_quotes)


def test_a_failing_fill_does_not_stop_the_other_orders(db, monkeypatch):
    import portfolio.portfolio as P
    _demo_account(db)
    bad = demo_order(db, "AAA")
    good = demo_order(db, "BBB")
    _quotes(monkeypatch, ["AAA", "BBB"])
    real = P.try_fill_entry

    def flaky(d, o, t, stop, tgt):
        if o.symbol == "AAA":
            o.status = "FILLED"                       # half-written state, as a mid-way failure would leave
            raise RuntimeError("account write failed")
        return real(d, o, t, stop, tgt)
    monkeypatch.setattr(P, "try_fill_entry", flaky)

    assert run(entry.check_pending_fills(db, "DEMO")) == 1
    db.refresh(bad)
    db.refresh(good)
    assert good.status == "FILLED"
    assert bad.status == "PLACED"                     # the half-written FILLED was rolled back, retried next pass
    assert db.query(models.TradePosition).filter_by(symbol="AAA").count() == 0


def test_quote_fetch_failure_returns_zero_instead_of_raising(db, monkeypatch):
    _demo_account(db)
    o = demo_order(db)

    async def boom(syms):
        raise RuntimeError("feed down")
    monkeypatch.setattr(entry, "get_quotes", boom)
    assert run(entry.check_pending_fills(db, "DEMO")) == 0
    assert o.status == "PLACED"


def _stale(db, symbol, dhan_id):
    o = models.TradeOrder(mode="REAL", symbol=symbol, side="BUY", qty=5, order_type="LIMIT", status="PLACED",
                          dhan_order_id=dhan_id, filled_qty_so_far=0,
                          valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(o)
    db.commit()
    return o


def test_a_raising_order_does_not_stop_the_others_in_expire(db, monkeypatch):
    async def notify(t):
        return None
    monkeypatch.setattr(entry, "notify_async", notify)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
        {"orderId": i, "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0} for i in ("D1", "D2", "D3")])
    a = _stale(db, "AAA", "D1")
    b = _stale(db, "BBB", "D2")
    c = _stale(db, "CCC", "D3")
    real_release = entry.shared_symbol_lock.release

    def release(d, sym):
        if sym == "BBB":
            raise RuntimeError("lock table unavailable")
        return real_release(d, sym)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", release)

    assert run(entry.expire_stale_orders(db, "REAL")) == 2
    for o in (a, b, c):
        db.refresh(o)
    assert (a.status, b.status, c.status) == ("EXPIRED", "PLACED", "EXPIRED")   # a is kept, b rolled back, c still processed
