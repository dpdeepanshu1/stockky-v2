"""E285 round 100: the automatic long entry is a marketable LIMIT (execution/dhan_client.long_entry_order) and a limit BUY that never fills is cancelled
(orders/stale_entry.py, called from orders/reconcile.run_exit_reconciliation). Manual BUY and the plain-order fallback keep MARKET."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import dhan_client
from orders import stale_entry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "LONG_ENTRY_MARKETABLE_LIMIT", True)
    monkeypatch.setattr(config, "LONG_ENTRY_CUSHION_PCT", 0.3)
    monkeypatch.setattr(config, "LONG_ENTRY_STALE_S", 15.0)
    stale_entry.reset()


class TestLongEntryOrder:
    @pytest.mark.parametrize("live", [100.0, 250.35, 1234.5, 5000.0])
    def test_limit_is_above_live_by_at_least_the_cushion_and_on_a_tick(self, live):
        t, p = dhan_client.long_entry_order(live, live * 1.04, live * 0.99)
        raw = live * 1.003
        assert t == "LIMIT" and dhan_client.is_valid_tick_price(p)
        assert raw - 1e-9 <= p <= raw + dhan_client.tick_size_for_price(raw) + 1e-9

    def test_wrong_side_legs_zero_price_or_switch_off_give_market(self, monkeypatch):
        assert dhan_client.long_entry_order(100.0, 100.1, 99.0)[0] == "MARKET"
        assert dhan_client.long_entry_order(100.0, 104.0, 100.5)[0] == "MARKET"
        assert dhan_client.long_entry_order(0.0, 104.0, 99.0) == ("MARKET", 0.0)
        monkeypatch.setattr(config, "LONG_ENTRY_MARKETABLE_LIMIT", False)
        assert dhan_client.long_entry_order(100.0, 104.0, 99.0) == ("MARKET", 100.0)

    def test_cushion_is_the_slippage_cap(self, monkeypatch):
        monkeypatch.setattr(config, "LONG_ENTRY_CUSHION_PCT", 1.0)
        assert dhan_client.long_entry_order(100.0, 104.0, 99.0) == ("LIMIT", 101.0)


def _pos(age_s, side="LONG", status="OPEN", oid="S1"):
    opened = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None)
    return SimpleNamespace(side=side, status=status, dhan_super_order_id=oid, symbol="ABC", id=1, opened_at=opened)


def _row(st="PENDING", filled=0):
    return {"orderStatus": st, "filledQty": filled, "legDetails": [{"legName": "ENTRY_LEG", "orderStatus": st}]}


class TestStaleEntry:
    def test_only_a_wholly_unfilled_waiting_long_older_than_the_limit_is_stale(self):
        assert not stale_entry.is_stale(_pos(5), _row())
        assert stale_entry.is_stale(_pos(20), _row())
        assert stale_entry.is_stale(_pos(20), _row("TRANSIT"))
        assert not stale_entry.is_stale(_pos(20), _row("TRADED"))
        assert not stale_entry.is_stale(_pos(20), _row("PENDING", filled=3))
        assert not stale_entry.is_stale(_pos(20), _row("CANCELLED"))
        assert not stale_entry.is_stale(_pos(20, side="SHORT"), _row())
        assert not stale_entry.is_stale(_pos(20, status="STOP_HIT"), _row())

    def test_switch_off_means_never_stale(self, monkeypatch):
        monkeypatch.setattr(config, "LONG_ENTRY_MARKETABLE_LIMIT", False)
        assert not stale_entry.is_stale(_pos(60), _row())

    def test_cancels_the_entry_leg_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dhan_client, "cancel_super_order", lambda db, order_id, order_leg: calls.append((order_id, order_leg)))
        assert stale_entry.cancel_if_stale(None, _pos(30), _row()) is True
        assert stale_entry.cancel_if_stale(None, _pos(30), _row()) is False
        assert calls == [("S1", "ENTRY_LEG")]

    def test_a_failed_cancel_never_raises_and_is_not_retried(self, monkeypatch):
        def boom(db, order_id, order_leg):
            raise RuntimeError("dhan down")
        monkeypatch.setattr(dhan_client, "cancel_super_order", boom)
        assert stale_entry.cancel_if_stale(None, _pos(30, oid="S2"), _row()) is False
        assert stale_entry.cancel_if_stale(None, _pos(30, oid="S2"), _row()) is False


def test_wiring_auto_entry_uses_the_limit_and_manual_buy_keeps_market():
    src = open(os.path.join(ROOT, "orders", "entry.py"), encoding="utf-8").read()
    assert src.count("dhan_client.long_entry_order(") == 1
    assert src.count("order_type=_e_type") == 1 and src.count("price=_e_px") == 1
    assert src.count('tag="MANUAL"') == 2 and 'order_type="MARKET",\n                price=current_ltp' in src       # the manual BUY is unchanged
    rec = open(os.path.join(ROOT, "orders", "reconcile.py"), encoding="utf-8").read()
    assert "stale_entry.cancel_if_stale(db, pos, row)" in rec
