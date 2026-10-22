"""A1b (2026-10-22): find_order_by_tag + the uncertain-BUY guard used by entry_engine/entry.py.

The full entry loop is covered by the existing entry suites; this file pins the two new building blocks.
Run from services/real-trade-service:  python3 -m pytest tests/test_a1b_order_recovery.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from entry_engine import entry
from execution import dhan_client


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch):
    # conftest stubs find_order_by_tag for every test; this file tests the real function, so undo the stub.
    import importlib
    real = importlib.import_module("execution.dhan_client")
    monkeypatch.setattr(real, "find_order_by_tag", _REAL_FIND)


from execution import dhan_client as _dc_mod
_REAL_FIND = _dc_mod.find_order_by_tag


def test_finds_order_by_correlation_id(monkeypatch):
    rows = [{"orderId": "1", "correlationId": "stk7"}, {"orderId": "2", "correlationId": "stk8"}]
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db: rows)
    row, ok = dhan_client.find_order_by_tag(None, "stk8")
    assert ok is True and row["orderId"] == "2"


def test_not_found_is_checked_ok(monkeypatch):
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db: [{"orderId": "1", "correlationId": "other"}])
    assert dhan_client.find_order_by_tag(None, "stk9") == (None, True)


def test_unreadable_order_book_is_unknown_not_absent(monkeypatch):
    def boom(db):
        raise RuntimeError("timeout")
    monkeypatch.setattr(dhan_client, "get_order_list", boom)
    assert dhan_client.find_order_by_tag(None, "stk9") == (None, False)


def test_uncertain_buy_blocks_same_symbol_today_only(monkeypatch):
    entry._UNCERTAIN_BUYS.clear()
    assert entry._uncertain_buy_active("REAL", "ABC") is False
    entry._mark_uncertain_buy("REAL", "ABC")
    assert entry._uncertain_buy_active("REAL", "ABC") is True
    assert entry._uncertain_buy_active("REAL", "XYZ") is False
    assert entry._uncertain_buy_active("DEMO", "ABC") is False
    entry._UNCERTAIN_BUYS[("REAL", "ABC")] = "2000-01-01"      # an earlier day no longer blocks
    assert entry._uncertain_buy_active("REAL", "ABC") is False
    entry._UNCERTAIN_BUYS.clear()


def test_rejected_or_cancelled_orders_do_not_count_as_live(monkeypatch):
    rows = [{"orderId": "1", "correlationId": "stk5", "orderStatus": "REJECTED"},
            {"orderId": "2", "correlationId": "stk5", "orderStatus": "CANCELLED"}]
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db: rows)
    assert dhan_client.find_order_by_tag(None, "stk5") == (None, True)


def test_traded_and_pending_orders_count_as_live(monkeypatch):
    for st in ("TRADED", "PENDING", "TRANSIT", "PART_TRADED"):
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db, st=st: [{"orderId": "9", "correlationId": "t", "orderStatus": st}])
        row, ok = dhan_client.find_order_by_tag(None, "t")
        assert ok and row["orderId"] == "9"


@pytest.mark.parametrize("msg,expected", [
    ("Dhan API error: RMS:1102:You have insufficient funds", True),
    ("EXCH:16283:The order price is not multiple of tick size", True),
    ("Dhan API error: Validate Qty from CDSL", True),
    ("ReadTimeout: timed out", False),
    ("Connection reset by peer", False),
    ("", False),
])
def test_clear_broker_rejection_classifier(msg, expected):
    assert dhan_client.is_clear_broker_rejection(msg) is expected
