"""Round 647 (E673): _entry_fill_from_orderbook no longer takes a lone order-book match that was created long before the position
opened (19 Nov 2026 SHRINGARMS id 547: priced from an earlier trade's entry order). Plain functions, no db fixture needed
(the order list is passed in); runs under pytest or `python3 <file>`."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import orders.reconcile as reconcile


def _pos(side="LONG"):
    return SimpleNamespace(id=1, symbol="T", dhan_security_id="1", entry_price=87.02, quantity=100, side=side,
                           opened_at=datetime(2026, 11, 19, 4, 48, 11, tzinfo=timezone.utc))


def _row(avg, ts=None, txn="BUY"):
    r = {"transactionType": txn, "securityId": "1", "orderStatus": "TRADED", "averageTradedPrice": avg, "filledQty": 100}
    if ts:
        r["createTime"] = ts
    return r


def _ts(p, **kw):
    return (p.opened_at.astimezone(reconcile.IST) + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def test_lone_match_from_an_old_trade_is_not_taken():
    p = _pos()
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, minutes=-25))]) is None


def test_lone_match_created_with_the_position_is_taken():
    p = _pos()
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, seconds=3))]) == 87.22


def test_lone_match_without_a_time_keeps_the_old_behaviour():
    p = _pos()
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22)]) == 87.22


def test_old_trade_plus_new_order_picks_the_new_one():
    p = _pos()
    rows = [_row(87.40, _ts(p, minutes=-25)), _row(87.22, _ts(p, seconds=4))]
    assert reconcile._entry_fill_from_orderbook(None, p, orders=rows) == 87.22


def test_lone_match_limit_is_180_seconds():
    p = _pos()
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, seconds=-179))]) == 87.22
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, seconds=-181))]) is None


def test_lone_match_nine_minutes_old_is_not_taken():
    # the case a plain 600 s limit would have let through
    p = _pos()
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, minutes=-9))]) is None


def test_short_position_uses_the_sell_order_and_the_same_limit():
    p = _pos("SHORT")
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, seconds=3), "SELL")]) == 87.22
    assert reconcile._entry_fill_from_orderbook(None, p, orders=[_row(87.22, _ts(p, minutes=-9), "SELL")]) is None


def test_several_matches_keep_the_ten_minute_window():
    p = _pos()
    rows = [_row(87.40, _ts(p, minutes=-25)), _row(87.22, _ts(p, minutes=-8))]
    assert reconcile._entry_fill_from_orderbook(None, p, orders=rows) == 87.22


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
