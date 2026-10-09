"""group 263: position-stocks-service stores the full charge split + net P&L on every scalp_charges_ledger row,
exposes the all-time/today net P&L block and per-trade rows, and books closed trades without a dashboard read."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from orders import charges_ledger as cl
from tz_utils import ist_today_str

NOW = datetime.now(timezone.utc) - timedelta(minutes=5)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    cl._last_sync_at = 0.0
    return s


def _pos(db, symbol, entry, exit_, qty, pnl, status="TARGET_HIT", closed=NOW):
    p = models.ScalpPosition(symbol=symbol, dhan_security_id="1", window_source="5m", target_price=entry * 1.02, stop_price=entry * 0.99, adaptive_target_pct=2.0, adaptive_stop_pct=1.0, capital_risked=entry * qty, status=status, quantity=qty, entry_price=entry, exit_price=exit_,
                             realized_pnl=pnl, opened_at=closed - timedelta(minutes=20), closed_at=closed)
    db.add(p)
    db.commit()
    return p


def test_booking_stores_split_and_net(db):
    _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    assert cl.sync_all(db) == 1
    r = db.query(models.ScalpChargesLedger).one()
    assert r.stt == pytest.approx(5100 * config.STT_INTRADAY_SELL_PCT / 100, abs=0.01)     # sell leg only
    assert r.stamp == pytest.approx(5000 * config.STAMP_DUTY_BUY_PCT_INTRADAY / 100, abs=0.01)
    assert r.total_charges == pytest.approx(r.brokerage + r.stt + r.exchange + r.sebi + r.gst + r.stamp, abs=0.02)
    assert r.net_pnl == pytest.approx(100.0 - r.total_charges, abs=0.01)


def test_restate_stored_fills_columns_of_rows_booked_before_this_group(db):
    db.add(models.ScalpChargesLedger(position_id=1, symbol="OLD", day=ist_today_str(), quantity=10, buy_value=1000.0,
                                     sell_value=1020.0, brokerage=0.0, gst_on_brokerage=0.0, total_charges=0.0,
                                     gross_pnl=20.0))
    db.commit()
    row = db.query(models.ScalpChargesLedger).one()
    assert row.stt is None and row.net_pnl is None
    assert cl.restate_stored(db, [row]) == 1
    db.expire_all()
    row = db.query(models.ScalpChargesLedger).one()
    assert row.stt > 0 and row.total_charges > 0 and row.net_pnl == pytest.approx(20.0 - row.total_charges, abs=0.01)
    assert cl.restate_stored(db, [row]) == 0                      # second pass: nothing left to change


def test_restate_stored_follows_a_rate_card_change(db, monkeypatch):
    _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    cl.sync_all(db)
    before = db.query(models.ScalpChargesLedger).one().total_charges
    monkeypatch.setattr(config, "STT_INTRADAY_SELL_PCT", config.STT_INTRADAY_SELL_PCT * 3)
    cl.cumulative(db)
    row = db.query(models.ScalpChargesLedger).one()
    assert row.total_charges > before
    assert row.stt == pytest.approx(5100 * config.STT_INTRADAY_SELL_PCT / 100, abs=0.01)


def test_cumulative_pnl_block_and_daily_net(db):
    _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    _pos(db, "BBB", 200.0, 198.0, 10, -20.0, status="STOP_HIT")
    rep = cl.cumulative(db)
    pnl = rep["pnl"]
    assert pnl["realized_gross_total"] == pytest.approx(80.0)
    assert pnl["charges_total"] == pytest.approx(rep["all_charges_total"], abs=0.01)
    assert pnl["net_realized_total"] == pytest.approx(80.0 - rep["all_charges_total"], abs=0.01)
    assert pnl["net_realized_today"] == pytest.approx(pnl["net_realized_total"], abs=0.01)   # both closed today
    day = rep["recent_days"][0]
    assert day["gross_pnl"] == pytest.approx(80.0) and day["net_pnl"] == pytest.approx(pnl["net_realized_total"], abs=0.01)


def test_trades_lists_stored_rows_for_a_day_with_split_and_net(db):
    _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    out = cl.trades(db)
    assert out["day"] == ist_today_str() and out["count"] == 1
    t = out["trades"][0]
    assert t["symbol"] == "AAA" and t["qty"] == 50 and t["buy_price"] == 100.0 and t["sell_price"] == 102.0
    assert t["total_charges"] == pytest.approx(t["brokerage"] + t["stt"] + t["exchange"] + t["sebi"] + t["gst"] + t["stamp"], abs=0.02)
    assert t["net_pnl"] == pytest.approx(100.0 - t["total_charges"], abs=0.01)
    assert cl.trades(db, day="2020-01-01")["count"] == 0


def test_trades_survive_the_retention_deleting_the_position(db):
    p = _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    cl.sync_all(db)
    db.delete(p)
    db.commit()
    assert cl.trades(db)["count"] == 1
    assert cl.cumulative(db)["pnl"]["realized_gross_total"] == pytest.approx(100.0)


def test_sync_throttled_books_once_per_interval_and_never_raises(db, monkeypatch):
    _pos(db, "AAA", 100.0, 102.0, 50, 100.0)
    assert cl.sync_throttled(db, 60.0) == 1
    _pos(db, "BBB", 100.0, 101.0, 10, 10.0)
    assert cl.sync_throttled(db, 60.0) == 0                     # throttled
    assert db.query(models.ScalpChargesLedger).count() == 1
    assert cl.sync_throttled(db, 0.0) == 1                      # interval elapsed
    monkeypatch.setattr(cl, "sync_all", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cl.sync_throttled(db, 0.0) == 0
