"""group 263: real-trade-service STORES every executed order's charges (trade_charges_ledger) and each day's gross /
charges / net P&L (trade_pnl_daily), instead of only recomputing them on read."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import charges_ledger as cl
import config
import models
from tz_utils import ist_today_str

NOW = datetime.now(timezone.utc) - timedelta(minutes=2)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0,
                              cash_available=100_000.0, broker_cash_available=100_000.0,
                              realized_pnl_today=-50.0, realized_pnl_total=-500.0,
                              pnl_last_reset_date=ist_today_str()))
    s.commit()
    cl._last_sync_at.clear()
    return s


def _filled(db, side, qty, price, symbol="ABC", product="CNC", minutes=0):
    t = NOW + timedelta(minutes=minutes)
    o = models.TradeOrder(mode="REAL", symbol=symbol, side=side, qty=qty, status="FILLED", product_type=product,
                          filled_qty_so_far=qty, created_at=t, updated_at=t)
    db.add(o)
    db.commit()
    db.add(models.TradeFill(order_id=o.id, qty=qty, price=price, filled_at=t))
    db.commit()
    return o


def test_report_is_read_only_by_default(db):
    _filled(db, "BUY", 10, 100.0)
    cl.report(db, "REAL")
    assert db.query(models.TradeChargesLedger).count() == 0
    assert db.query(models.TradePnlDaily).count() == 0


def test_persist_stores_one_row_per_order_with_full_split(db):
    buy = _filled(db, "BUY", 10, 100.0)
    sell = _filled(db, "SELL", 10, 110.0, minutes=5)
    rep = cl.report(db, "REAL", 14, True, -50.0)
    rows = {r.order_id: r for r in db.query(models.TradeChargesLedger).all()}
    assert set(rows) == {buy.id, sell.id}
    b = rows[buy.id]
    assert b.mode == "REAL" and b.side == "BUY" and b.product == "CNC" and b.order_value == pytest.approx(1000.0)
    assert b.stt == pytest.approx(1000 * config.STT_DELIVERY_PCT_PER_LEG / 100, abs=1e-3)   # delivery STT on the buy
    assert b.stamp > 0 and b.dp == 0.0
    assert b.total_charges == pytest.approx(b.brokerage + b.stt + b.exchange + b.sebi + b.gst + b.stamp + b.dp, abs=1e-3)
    s = rows[sell.id]
    assert s.stamp == 0.0 and s.stt > 0
    # what is stored adds up to what the report shows
    assert sum(r.total_charges for r in rows.values()) == pytest.approx(rep["all_charges_total"], abs=0.01)


def test_persist_is_idempotent_and_restates_when_the_rate_card_changes(db, monkeypatch):
    _filled(db, "BUY", 10, 100.0)
    cl.report(db, "REAL", 14, True)
    first = db.query(models.TradeChargesLedger).one().total_charges
    assert cl.persist_rows(db, "REAL", []) == 0
    cl.report(db, "REAL", 14, True)                                   # unchanged -> nothing rewritten
    assert db.query(models.TradeChargesLedger).count() == 1
    monkeypatch.setattr(config, "STT_DELIVERY_PCT_PER_LEG", config.STT_DELIVERY_PCT_PER_LEG * 2)
    cl.report(db, "REAL", 14, True)                                   # rate card changed -> stored row restated
    row = db.query(models.TradeChargesLedger).one()
    assert row.total_charges > first
    assert row.stt == pytest.approx(1000 * config.STT_DELIVERY_PCT_PER_LEG / 100, abs=1e-3)


def test_daily_row_holds_charges_and_todays_gross_net(db):
    _filled(db, "BUY", 10, 100.0)
    rep = cl.report(db, "REAL", 14, True, -50.0)
    d = db.query(models.TradePnlDaily).filter_by(mode="REAL", day=ist_today_str()).one()
    assert d.realized_gross == -50.0 and d.orders == 1
    assert d.charges == pytest.approx(rep["all_charges_total"], abs=0.01)
    assert d.net_realized == pytest.approx(-50.0 - d.charges, abs=0.01)
    hist = cl.daily_history(db, "REAL")
    assert hist[0]["day"] == ist_today_str() and hist[0]["net_realized"] == d.net_realized


def test_a_later_sync_without_gross_keeps_the_stored_gross(db):
    _filled(db, "BUY", 10, 100.0)
    cl.report(db, "REAL", 14, True, -50.0)
    cl.report(db, "REAL", 14, True, None)
    assert db.query(models.TradePnlDaily).one().realized_gross == -50.0


def test_gross_is_stored_even_on_a_day_with_no_orders(db):
    cl.report(db, "REAL", 14, True, -12.0)
    d = db.query(models.TradePnlDaily).one()
    assert d.realized_gross == -12.0 and d.charges == 0.0 and d.net_realized == -12.0


def test_freeze_day_gross_survives_the_rollover(db):
    cl.freeze_day_gross(db, "REAL", "2026-10-07", -80.0)
    cl.freeze_day_gross(db, "REAL", None, 5.0)              # no previous day -> ignored
    row = db.query(models.TradePnlDaily).one()
    assert (row.day, row.realized_gross, row.net_realized) == ("2026-10-07", -80.0, -80.0)


def test_daily_pnl_reset_stores_the_finished_days_gross_first(db):
    from portfolio import portfolio as pf
    acct = db.query(models.TradeAccount).one()
    acct.pnl_last_reset_date = "2026-10-01"
    acct.realized_pnl_today = -33.0
    db.commit()
    pf._maybe_reset_daily_pnl(db, acct)
    assert acct.realized_pnl_today == 0.0
    d = db.query(models.TradePnlDaily).filter_by(day="2026-10-01").one()
    assert d.realized_gross == -33.0


def test_sync_throttled_stores_once_per_interval_and_never_raises(db, monkeypatch):
    _filled(db, "BUY", 10, 100.0)
    assert cl.sync_throttled(db, "REAL", 60.0) == 1
    assert db.query(models.TradeChargesLedger).count() == 1
    _filled(db, "BUY", 5, 100.0, symbol="XYZ", minutes=1)
    assert cl.sync_throttled(db, "REAL", 60.0) == 0           # throttled
    assert db.query(models.TradeChargesLedger).count() == 1
    monkeypatch.setattr(cl, "report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cl.sync_throttled(db, "REAL", 0.0) == 0            # swallowed


def test_storage_failure_never_breaks_the_report(db, monkeypatch):
    _filled(db, "BUY", 10, 100.0)
    monkeypatch.setattr(models, "TradeChargesLedger", None)   # any access raises inside persist_rows
    rep = cl.report(db, "REAL", 14, True)
    assert rep["orders"] == 1 and rep["all_charges_total"] > 0
