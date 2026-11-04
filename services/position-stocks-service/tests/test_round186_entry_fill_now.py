"""Round 186 (issue #6): the real entry fill is read right after the BUY is placed (reconcile.correct_entry_now), not 10-13 s later."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config
import orders.reconcile as reconcile
from execution import dhan_client
from tests.test_repair_closed_entry_prices import db, _pos, _ledger  # noqa: F401


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reconcile._order_list_cache = (0.0, [])
    reconcile._rearm_state.clear()
    monkeypatch.setattr(reconcile.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True, raising=False)
    monkeypatch.setattr(reconcile, "_entry_fill_from_order_events", lambda pos: None)
    monkeypatch.setattr(reconcile, "_rearm_legs", lambda d, p: True)


def _setup(monkeypatch, supers, book=None):
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d, release_db=False: supers)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d, release_db=False: book or [])


def _open(db, entry=705.90):
    p = _pos(db, entry=entry)
    p.status, p.dhan_super_order_id, p.quantity = "OPEN", "SO1", 100
    p.dhan_security_id, p.adaptive_target_pct, p.adaptive_stop_pct = "1", 2.0, 1.0
    db.commit()
    return p


def test_fill_from_super_order_row_corrects_entry(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "TRADED", "averageTradedPrice": 707.0, "filledQty": 100}])
    assert reconcile.correct_entry_now(db, p, wait_s=2, poll_s=0.2) is True
    assert p.entry_price == 707.0


def test_row_without_price_falls_back_to_order_book(db, monkeypatch):
    p = _open(db)
    book = [{"transactionType": "BUY", "securityId": "1", "orderStatus": "TRADED", "averageTradedPrice": 707.0, "filledQty": 100}]
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "PENDING", "legDetails": []}], book)
    assert reconcile.correct_entry_now(db, p, wait_s=2, poll_s=0.2) is True
    assert p.entry_price == 707.0


def test_websocket_price_used_first(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "PENDING"}])
    monkeypatch.setattr(reconcile, "_entry_fill_from_order_events", lambda pos: 706.5)
    assert reconcile.correct_entry_now(db, p, wait_s=2, poll_s=0.2) is True
    assert p.entry_price == 706.5


def test_websocket_price_outside_5pct_band_ignored(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "PENDING"}])
    monkeypatch.setattr(reconcile, "_entry_fill_from_order_events", lambda pos: 900.0)
    assert reconcile.correct_entry_now(db, p, wait_s=0.5, poll_s=0.2) is False
    assert p.entry_price == 705.90


def test_dead_entry_stops_without_touching_price(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "REJECTED"}])
    assert reconcile.correct_entry_now(db, p, wait_s=2, poll_s=0.2) is False
    assert p.entry_price == 705.90


def test_no_price_within_wait_returns_false_and_keeps_estimate(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "PENDING"}])
    assert reconcile.correct_entry_now(db, p, wait_s=1, poll_s=0.2) is False
    assert p.entry_price == 705.90


def test_polls_until_fill_appears(db, monkeypatch):
    p = _open(db)
    calls = {"n": 0}
    def supers(d, release_db=False):
        calls["n"] += 1
        return [{"orderId": "SO1", "orderStatus": "TRADED", "averageTradedPrice": 707.0, "filledQty": 100}] if calls["n"] >= 3 else [{"orderId": "SO1", "orderStatus": "PENDING"}]
    monkeypatch.setattr(dhan_client, "get_super_order_list", supers)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d, release_db=False: [])
    # sleep is stubbed, so the deadline is reached by the poll count: give a generous wait
    t = {"v": 0.0}
    monkeypatch.setattr(reconcile.time, "monotonic", lambda: t.__setitem__("v", t["v"] + 0.1) or t["v"])
    assert reconcile.correct_entry_now(db, p, wait_s=3, poll_s=0.2) is True
    assert calls["n"] >= 3 and p.entry_price == 707.0


def test_disabled_when_wait_is_zero(db, monkeypatch):
    p = _open(db)
    _setup(monkeypatch, [{"orderId": "SO1", "orderStatus": "TRADED", "averageTradedPrice": 707.0, "filledQty": 100}])
    assert reconcile.correct_entry_now(db, p, wait_s=0) is False
    assert p.entry_price == 705.90


def test_never_raises_when_dhan_errors(db, monkeypatch):
    p = _open(db)
    def boom(*a, **k):
        raise RuntimeError("dhan down")
    monkeypatch.setattr(dhan_client, "get_super_order_list", boom)
    monkeypatch.setattr(dhan_client, "get_order_list", boom)
    assert reconcile.correct_entry_now(db, p, wait_s=0.5, poll_s=0.2) is False
