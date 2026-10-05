"""Group 164/165: repair_closed_entry_prices() - today's closed rows with a stale entry or exit price."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import orders.reconcile as reconcile
from capital import ledger
from execution import dhan_client

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _pos(db, *, symbol="UNITEDPOLY", entry=44.58, exit_=48.0, qty=100, status="TARGET_HIT",
         so="S1", closed=None, **kw):
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id="1", window_source="5m", status=status, entry_price=entry,
        exit_price=exit_, quantity=qty, realized_pnl=(exit_ - entry) * qty,
        realized_pnl_pct=(exit_ - entry) / entry * 100, target_price=50.0, stop_price=40.0,
        adaptive_target_pct=5.0, adaptive_stop_pct=3.0, capital_risked=entry * qty,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        closed_at=closed or datetime.now(timezone.utc), dhan_super_order_id=so, **kw)
    db.add(p)
    db.commit()
    return p


def _row(oid="S1", avg=48.14, status="TRADED"):
    return {"orderId": oid, "orderStatus": status, "averageTradedPrice": avg, "filledQty": 100}


def _ledger(db):
    row = ledger._get_or_create(db)
    row.available_capital, row.realized_pnl_today, row.realized_pnl_total = 1000.0, 105.0, 500.0
    db.commit()
    return row


def _patch(monkeypatch, rows):
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: rows)


def test_no_rows_no_fetch(db, monkeypatch):
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: (_ for _ in ()).throw(AssertionError("fetched")))
    r = reconcile.repair_closed_entry_prices(db)
    assert r["changes"] == [] and r["unchanged"] == 0


def test_dry_run_reports_but_changes_nothing(db, monkeypatch):
    p = _pos(db)
    led = _ledger(db)
    _patch(monkeypatch, [_row()])
    r = reconcile.repair_closed_entry_prices(db)
    assert r["applied"] is False and len(r["changes"]) == 1
    c = r["changes"][0]
    assert c["old_entry"] == 44.58 and c["real_entry"] == 48.14
    assert c["delta"] == pytest.approx(-356.0)
    assert r["total_pnl_delta"] == pytest.approx(-356.0)
    db.refresh(p), db.refresh(led)
    assert p.entry_price == 44.58 and led.realized_pnl_today == 105.0


def test_apply_fixes_row_and_ledger(db, monkeypatch):
    p = _pos(db)
    led = _ledger(db)
    _patch(monkeypatch, [_row()])
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p), db.refresh(led)
    assert p.entry_price == 48.14 and p.capital_risked == pytest.approx(4814.0)
    assert p.realized_pnl == pytest.approx((48.0 - 48.14) * 100)
    assert p.realized_pnl_pct == pytest.approx((48.0 - 48.14) / 48.14 * 100)
    assert led.realized_pnl_today == pytest.approx(105.0 - 356.0)
    assert led.realized_pnl_total == pytest.approx(500.0 - 356.0)
    assert led.available_capital == pytest.approx(1000.0 - 356.0)
    assert r["applied"] is True


def test_idempotent(db, monkeypatch):
    _pos(db)
    _ledger(db)
    _patch(monkeypatch, [_row()])
    reconcile.repair_closed_entry_prices(db, apply=True)
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    assert r["changes"] == [] and r["unchanged"] == 1


def test_positive_correction(db, monkeypatch):
    p = _pos(db, entry=50.0, exit_=52.0)
    led = _ledger(db)
    _patch(monkeypatch, [_row(avg=48.0)])
    reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p), db.refresh(led)
    assert p.realized_pnl == pytest.approx(400.0)
    assert led.realized_pnl_today == pytest.approx(105.0 + 200.0)


def test_skips(db, monkeypatch):
    _pos(db, symbol="A", so="S1")                                   # not in list
    _pos(db, symbol="B", so="S2")                                   # no fill
    _pos(db, symbol="C", so="S3")                                   # >25% drift
    _pos(db, symbol="D", so="S4", overnight_stop_prior_qty=10)      # partials
    _pos(db, symbol="E", so="S5", status="OPEN")                    # still open: not a candidate
    _pos(db, symbol="F", so="S6", closed=datetime.now(timezone.utc) - timedelta(days=2))  # not today
    _pos(db, symbol="G", so="S7", status="ERROR")                   # error: not a candidate
    _patch(monkeypatch, [{"orderId": "S2", "orderStatus": "PENDING"}, _row("S3", avg=80.0),
                         _row("S4"), _row("S5"), _row("S6"), _row("S7")])
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    reasons = {s["symbol"]: s["reason"] for s in r["skipped"]}
    assert set(reasons) == {"A", "B", "C", "D"}
    assert "not in today" in reasons["A"] and "no real entry fill" in reasons["B"]
    assert "differs" in reasons["C"] and "overnight" in reasons["D"]
    assert r["changes"] == []


def test_fetch_failure_reported(db, monkeypatch):
    _pos(db)
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    assert "boom" in r["error"] and r["changes"] == []


def test_ledger_zero_delta_is_noop(db):
    led = _ledger(db)
    ledger.adjust_closed_pnl_today(db, delta=0.0)
    db.refresh(led)
    assert led.realized_pnl_today == 105.0


# -- group 165: exit price from the order book for TARGET_HIT / STOP_HIT ----------------
def _sell(avg, qty=100, sec="1", status="TRADED"):
    return {"transactionType": "SELL", "securityId": sec, "orderStatus": status,
            "filledQty": qty, "averageTradedPrice": avg}


def _patch_book(monkeypatch, rows, book):
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: rows)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: book)


def test_exit_repaired_from_order_book(db, monkeypatch):
    p = _pos(db, entry=48.14, exit_=50.0, status="TARGET_HIT")      # entry already right, exit = trigger
    led = _ledger(db)
    _patch_book(monkeypatch, [_row(avg=48.14)], [_sell(49.6)])
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p), db.refresh(led)
    c = r["changes"][0]
    assert c["old_exit"] == 50.0 and c["real_exit"] == 49.6
    assert p.exit_price == 49.6 and p.realized_pnl == pytest.approx((49.6 - 48.14) * 100)
    assert led.realized_pnl_today == pytest.approx(105.0 - 40.0)


def test_entry_and_exit_both_repaired(db, monkeypatch):
    p = _pos(db, entry=44.58, exit_=50.0, status="STOP_HIT")
    _ledger(db)
    _patch_book(monkeypatch, [_row(avg=48.14)], [_sell(49.6)])
    reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p)
    assert (p.entry_price, p.exit_price) == (48.14, 49.6)
    assert p.realized_pnl == pytest.approx((49.6 - 48.14) * 100)


@pytest.mark.parametrize("book", [
    [_sell(49.6), _sell(49.7)],                 # ambiguous: two matches
    [_sell(49.6, qty=50)],                      # wrong quantity
    [_sell(49.6, sec="2")],                     # other security
    [_sell(49.6, status="PENDING")],            # not filled
    [_sell(70.0)],                              # >10% from booked exit
    [],                                         # empty book
])
def test_exit_left_alone_when_not_unique_match(db, monkeypatch, book):
    p = _pos(db, entry=48.14, exit_=50.0, status="TARGET_HIT")
    _ledger(db)
    _patch_book(monkeypatch, [_row(avg=48.14)], book)
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p)
    assert r["changes"] == [] and r["unchanged"] == 1 and p.exit_price == 50.0


def test_flat_sell_statuses_do_not_read_order_book(db, monkeypatch):
    _pos(db, entry=48.14, exit_=50.0, status="STAGNATION_EXIT")
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [_row(avg=48.14)])
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: (_ for _ in ()).throw(AssertionError("read")))
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    assert r["changes"] == [] and r["unchanged"] == 1


def test_order_book_failure_still_repairs_entry(db, monkeypatch):
    p = _pos(db, entry=44.58, exit_=48.0, status="TARGET_HIT")
    _ledger(db)
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [_row(avg=48.14)])
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: (_ for _ in ()).throw(RuntimeError("down")))
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p)
    assert "down" in r["exit_error"] and p.entry_price == 48.14 and p.exit_price == 48.0


def test_exit_lookup_reuses_one_order_book_fetch(db, monkeypatch):
    _pos(db, symbol="A", so="S1", entry=48.14, exit_=50.0)
    _pos(db, symbol="B", so="S2", entry=48.14, exit_=50.0)
    calls = []
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [_row("S1", 48.14), _row("S2", 48.14)])
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: calls.append(1) or [])
    reconcile.repair_closed_entry_prices(db)
    assert len(calls) == 1


# -- group 166: daily-loss-limit report (report only) ----------------------------------
def test_loss_limit_report_after_apply(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)
    _pos(db)
    led = _ledger(db)
    led.total_allocated_capital = 5000.0
    led.realized_pnl_today = -100.0
    db.commit()
    _patch(monkeypatch, [_row()])
    r = reconcile.repair_closed_entry_prices(db, apply=True)     # -100 - 356 = -456 -> 9.12% of 5000
    db.refresh(led)
    assert r["daily_loss_pct"] == pytest.approx(9.12)
    assert r["daily_loss_limit_pct"] == 4.0 and r["exceeds_daily_loss_limit"] is True
    assert r["kill_switch_tripped"] is False and led.daily_loss_kill_switch_tripped is False   # reported, not tripped


def test_loss_limit_report_within_limit(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)
    led = _ledger(db)
    led.total_allocated_capital = 5000.0
    db.commit()
    _pos(db)
    _patch(monkeypatch, [_row(avg=44.58)])    # already correct -> no change
    r = reconcile.repair_closed_entry_prices(db)
    assert r["exceeds_daily_loss_limit"] is False and r["daily_loss_pct"] == 0.0


def test_loss_limit_report_failure_is_swallowed(db, monkeypatch):
    _pos(db)
    _patch(monkeypatch, [_row(avg=44.58)])
    monkeypatch.setattr(ledger, "_get_or_create", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    r = reconcile.repair_closed_entry_prices(db)
    assert "daily_loss_pct" not in r and r["unchanged"] == 1
