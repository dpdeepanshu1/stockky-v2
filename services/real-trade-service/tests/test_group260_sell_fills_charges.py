"""group 260: SELL fills are written to trade_fills, so the cumulative charges report counts sells.

Before this, only BUY fills (record_real_fill) wrote trade_fills rows; charges_ledger.report() prices orders
from trade_fills, so every SELL was skipped and STT / DP / sell-side charges read Rs 0 in "Charges since start".
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import charges_ledger as cl
import models
from execution import reconcile as R

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()

    async def notify(_text):
        return None

    monkeypatch.setattr(R, "notify_async", notify)
    s.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0,
                              cash_available=100_000.0, broker_cash_available=100_000.0,
                              realized_pnl_today=0.0, realized_pnl_total=0.0))
    s.commit()
    return s


def _order(db, side, symbol="ABC", qty=10, limit=None, product=None, filled=0, notional=None, status="FILLED"):
    o = models.TradeOrder(mode="REAL", symbol=symbol, side=side, qty=qty, limit_price=limit, status=status,
                          product_type=product, filled_qty_so_far=filled, broker_fill_notional=notional,
                          created_at=NOW, updated_at=NOW)
    db.add(o)
    db.commit()
    return o


def _position(db, symbol="ABC", qty=10, avg=100.0):
    p = models.TradePosition(mode="REAL", symbol=symbol, status="PENDING_EXIT", qty_open=qty,
                             avg_entry_price=avg, current_stop=90.0, current_target=120.0)
    db.add(p)
    db.commit()
    return p


def test_a_booked_sell_writes_a_trade_fill(db):
    _position(db)
    o = _order(db, "SELL", qty=10)
    asyncio.run(R._book_fill_delta(db, o, 105.0, 10, is_partial=False))
    fills = db.query(models.TradeFill).filter_by(order_id=o.id).all()
    assert [(f.qty, f.price) for f in fills] == [(10, 105.0)]


def test_a_sell_with_no_position_still_writes_its_fill(db):
    o = _order(db, "SELL", symbol="NOPOS", qty=4)
    asyncio.run(R._book_fill_delta(db, o, 50.0, 4, is_partial=False))
    assert [(f.qty, f.price) for f in db.query(models.TradeFill).filter_by(order_id=o.id)] == [(4, 50.0)]


def test_partial_sell_increments_each_get_their_own_fill_row(db):
    _position(db)
    o = _order(db, "SELL", qty=10)
    asyncio.run(R._book_fill_delta(db, o, 100.0, 4, is_partial=True))
    asyncio.run(R._book_fill_delta(db, o, 110.0, 6, is_partial=False))
    assert sorted((f.qty, f.price) for f in db.query(models.TradeFill).filter_by(order_id=o.id)) == [(4, 100.0), (6, 110.0)]


def test_report_counts_the_sell_side_charges(db):
    _position(db, qty=10)
    buy = _order(db, "BUY", product="CNC", qty=10)
    db.add(models.TradeFill(order_id=buy.id, qty=10, price=100.0, filled_at=NOW))
    sell = _order(db, "SELL", qty=10)
    asyncio.run(R._book_fill_delta(db, sell, 110.0, 10, is_partial=False))
    rep = cl.report(db, "REAL")
    comp = rep["total"]["components"]
    assert rep["orders"] == 2
    assert comp["stt"] == pytest.approx((1000 + 1100) * 0.001, abs=0.01)   # delivery STT, BUY and SELL (group 262)
    assert comp["dp"] == 0.0                                       # bought and sold the same day: nothing left demat


def test_report_prices_a_legacy_sell_from_the_broker_notional(db):
    s = _order(db, "SELL", filled=10, notional=1100.0)
    rep = cl.report(db, "REAL")
    assert rep["orders"] == 1 and rep["total"]["components"]["dp"] == pytest.approx(cl.dp_charge_rs())
    assert s.id  # legacy order, no fill rows


def test_report_prices_a_legacy_sell_from_limit_price_when_no_notional(db):
    _order(db, "SELL", filled=5, limit=200.0)
    assert cl.report(db, "REAL")["orders"] == 1


def test_report_skips_an_unpriceable_legacy_order(db):
    _order(db, "SELL", filled=5)                       # market sell, nothing to price it from
    _order(db, "SELL", filled=0, limit=100.0)          # nothing filled
    assert cl.report(db, "REAL")["orders"] == 0


def test_report_does_not_double_count_an_order_that_has_fills(db):
    o = _order(db, "BUY", filled=10, notional=1000.0, product="CNC")
    db.add(models.TradeFill(order_id=o.id, qty=10, price=100.0, filled_at=NOW))
    db.commit()
    assert cl.report(db, "REAL")["orders"] == 1


def test_order_charges_of_an_empty_order_is_all_zero():
    assert all(v == 0.0 for v in cl.order_charges(0, "SELL", "CNC").values())


def test_report_ignores_an_order_whose_fills_are_worth_nothing(db):
    o = _order(db, "BUY", product="CNC")
    db.add(models.TradeFill(order_id=o.id, qty=0, price=100.0, filled_at=NOW))
    db.commit()
    assert cl.report(db, "REAL")["orders"] == 0


def test_report_estimates_a_legacy_market_sell_from_the_earlier_buy(db):
    buy = _order(db, "BUY", product="CNC", qty=10)
    db.add(models.TradeFill(order_id=buy.id, qty=10, price=100.0, filled_at=NOW))
    db.commit()
    _order(db, "SELL", filled=10)                      # market sell: no fill row, no notional, no limit
    rep = cl.report(db, "REAL")
    assert rep["orders"] == 2 and rep["orders_estimated"] == 1
