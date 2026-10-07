"""Group 232: card P&L on the real BUY fill. When the super-order row has no usable entry price the
BUY fill is read from today's order book, live (_apply_entry_correction) and in the repair, and the
repair runs on a throttle by itself."""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import orders.reconcile as reconcile
import config
from execution import dhan_client
from tests.test_repair_closed_entry_prices import db, _pos, _ledger  # noqa: F401


def _buy(avg=87.22, qty=100, sid="1", status="TRADED", ts=None):
    r = {"transactionType": "BUY", "securityId": sid, "orderStatus": status,
         "averageTradedPrice": avg, "filledQty": qty}
    if ts:
        r["createTime"] = ts
    return r


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reconcile._order_list_cache = (0.0, [])
    reconcile._last_auto_repair_ts = 0.0


def _book(monkeypatch, rows, supers=None):
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: rows)
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: supers if supers is not None else [])


def test_single_buy_match_used(db):
    p = _pos(db, entry=87.02, exit_=88.0)
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[_buy()]) == 87.22


@pytest.mark.parametrize("row", [
    _buy(qty=50), _buy(sid="2"), _buy(status="REJECTED"), _buy(avg=99.0),
    {**_buy(), "transactionType": "SELL"},
])
def test_non_matching_rows_ignored(db, row):
    p = _pos(db, entry=87.02, exit_=88.0)
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[row]) is None


def test_no_security_id_or_entry_returns_none(db):
    p = _pos(db, entry=87.02)
    p.dhan_security_id = None
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[_buy()]) is None


def test_two_matches_without_times_is_ambiguous(db):
    p = _pos(db, entry=87.02)
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[_buy(87.2), _buy(87.4)]) is None


def test_two_matches_picked_by_time(db):
    p = _pos(db, entry=87.02)
    t0 = p.opened_at.astimezone(reconcile.IST)
    near = (t0 + timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    far = (t0 + timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S")
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[_buy(87.4, ts=far), _buy(87.2, ts=near)]) == 87.2


def test_two_matches_too_far_in_time_none(db):
    p = _pos(db, entry=87.02)
    t0 = p.opened_at.astimezone(reconcile.IST)
    far = (t0 + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    assert reconcile._entry_fill_from_orderbook(db, p, orders=[_buy(87.4, ts=far), _buy(87.2, ts=far)]) is None


def test_cached_order_list_one_call(db, monkeypatch):
    calls = []
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: calls.append(1) or [_buy()])
    assert reconcile._cached_order_list(db) and reconcile._cached_order_list(db)
    assert len(calls) == 1


def test_cached_order_list_failure_returns_empty(db, monkeypatch):
    def boom(d):
        raise RuntimeError("dhan down")
    monkeypatch.setattr(dhan_client, "get_order_list", boom)
    assert reconcile._cached_order_list(db) == []


def test_repair_uses_order_book_when_row_has_no_entry(db, monkeypatch):
    p = _pos(db, symbol="MASTERTR", entry=87.02, exit_=86.0, status="STOP_HIT", qty=100)
    led = _ledger(db)
    _book(monkeypatch, [_buy(87.22)], supers=[{"orderId": "S1", "orderStatus": "PENDING"}])
    r = reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p), db.refresh(led)
    assert len(r["changes"]) == 1
    assert p.entry_price == 87.22
    assert p.realized_pnl == pytest.approx((86.0 - 87.22) * 100)
    assert led.realized_pnl_today == pytest.approx(105.0 + (86.0 - 87.22) * 100 - (86.0 - 87.02) * 100)


def test_repair_uses_order_book_when_super_order_missing(db, monkeypatch):
    p = _pos(db, entry=87.02, exit_=88.0)
    _ledger(db)
    _book(monkeypatch, [_buy(87.22)], supers=[])
    reconcile.repair_closed_entry_prices(db, apply=True)
    db.refresh(p)
    assert p.entry_price == 87.22


def test_repair_still_skips_when_nothing_matches(db, monkeypatch):
    _pos(db)
    _book(monkeypatch, [], supers=[])
    r = reconcile.repair_closed_entry_prices(db)
    assert r["skipped"][0]["reason"] == "super order not in today's list"
    _book(monkeypatch, [], supers=[{"orderId": "S1", "orderStatus": "PENDING"}])
    r = reconcile.repair_closed_entry_prices(db)
    assert r["skipped"][0]["reason"] == "no real entry fill on the row"


def test_repair_order_book_fetch_error_reported(db, monkeypatch):
    _pos(db)
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [])

    def boom(d):
        raise RuntimeError("x")
    monkeypatch.setattr(dhan_client, "get_order_list", boom)
    r = reconcile.repair_closed_entry_prices(db)
    assert "order list fetch failed" in r["exit_error"]


def test_live_correction_falls_back_to_order_book(db, monkeypatch):
    p = _pos(db, entry=87.02, exit_=87.02, status="OPEN", qty=100)
    p.closed_at = None
    p.exit_price = None
    p.dhan_super_order_id = None   # skip the leg re-arm call
    db.commit()
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [_buy(87.22)])
    _ledger(db)
    assert reconcile._apply_entry_correction(db, p, {"orderId": "S1", "orderStatus": "PENDING"}) is True
    assert p.entry_price == 87.22 and p.capital_risked == pytest.approx(8722.0)


def test_live_correction_no_match_changes_nothing(db, monkeypatch):
    p = _pos(db, entry=87.02, status="OPEN")
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [])
    assert reconcile._apply_entry_correction(db, p, {"orderId": "S1"}) is False
    assert p.entry_price == 87.02


@pytest.mark.parametrize("row", [
    {"orderId": "S1", "orderStatus": "REJECTED"},
    {"orderId": "S1", "orderStatus": "PENDING", "legDetails": [{"legName": "ENTRY_LEG", "orderStatus": "CANCELLED"}]},
])
def test_rejected_entry_makes_no_order_book_call(db, monkeypatch, row):
    p = _pos(db, entry=87.02, status="OPEN")
    monkeypatch.setattr(dhan_client, "get_order_list",
                        lambda d: (_ for _ in ()).throw(AssertionError("order book fetched")))
    assert reconcile._apply_entry_correction(db, p, row) is False


def test_repair_rejected_entry_row_makes_no_order_book_call(db, monkeypatch):
    _pos(db)
    monkeypatch.setattr(dhan_client, "get_super_order_list",
                        lambda d: [{"orderId": "S1", "orderStatus": "REJECTED"}])
    monkeypatch.setattr(dhan_client, "get_order_list",
                        lambda d: (_ for _ in ()).throw(AssertionError("order book fetched")))
    r = reconcile.repair_closed_entry_prices(db)
    assert r["skipped"][0]["reason"] == "no real entry fill on the row"


def test_auto_repair_applies_and_throttles(db, monkeypatch):
    p = _pos(db, entry=87.02, exit_=88.0)
    _ledger(db)
    monkeypatch.setattr(config, "ENTRY_REPAIR_AUTO_INTERVAL_S", 300.0, raising=False)
    _book(monkeypatch, [_buy(87.22)], supers=[])
    assert reconcile.auto_repair_closed_entry_prices(db) == 1
    db.refresh(p)
    assert p.entry_price == 87.22
    q = _pos(db, symbol="OTHER", entry=87.02, exit_=88.0)
    assert reconcile.auto_repair_closed_entry_prices(db) == 0
    db.refresh(q)
    assert q.entry_price == 87.02


def test_auto_repair_off_and_never_raises(db, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_REPAIR_AUTO_INTERVAL_S", 0.0, raising=False)
    assert reconcile.auto_repair_closed_entry_prices(db) == 0
    monkeypatch.setattr(config, "ENTRY_REPAIR_AUTO_INTERVAL_S", 300.0, raising=False)
    monkeypatch.setattr(reconcile, "repair_closed_entry_prices",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert reconcile.auto_repair_closed_entry_prices(db) == 0
