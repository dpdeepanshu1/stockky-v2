"""Round 648 (E674): _exit_fill_from_orderbook ignores an order created before the position opened (an earlier same-symbol,
same-size trade's exit; 19 Nov 2026 SHRINGARMS id 545 closed 04:44, id 547 opened 04:48). Plain functions, order list passed in;
runs under pytest or `python3 <file>`."""
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


def _row(avg, ts=None, txn="SELL", tag=None):
    r = {"transactionType": txn, "securityId": "1", "orderStatus": "TRADED", "averageTradedPrice": avg, "filledQty": 100}
    if ts:
        r["createTime"] = ts
    if tag:
        r["correlationId"] = tag
    return r


def _ts(p, **kw):
    return (p.opened_at.astimezone(reconcile.IST) + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _f(p, rows, ref=88.0):
    return reconcile._exit_fill_from_orderbook(None, p, ref, orders=rows, quiet=True)


def test_lone_exit_from_an_earlier_trade_is_not_taken():
    p = _pos()
    assert _f(p, [_row(88.4, _ts(p, minutes=-4))]) is None


def test_lone_exit_after_the_open_is_taken():
    p = _pos()
    assert _f(p, [_row(88.4, _ts(p, minutes=7))]) == 88.4


def test_earlier_trade_exit_plus_this_exit_picks_this_one():
    p = _pos()
    assert _f(p, [_row(88.9, _ts(p, minutes=-4)), _row(88.4, _ts(p, minutes=7))]) == 88.4


def test_no_create_time_keeps_the_old_behaviour():
    p = _pos()
    assert _f(p, [_row(88.4)]) == 88.4


def test_slack_is_sixty_seconds():
    p = _pos()
    assert _f(p, [_row(88.4, _ts(p, seconds=-59))]) == 88.4
    assert _f(p, [_row(88.4, _ts(p, seconds=-61))]) is None


def test_short_position_uses_the_buy_order_and_the_same_rule():
    p = _pos("SHORT")
    assert _f(p, [_row(86.0, _ts(p, minutes=-4), "BUY")], ref=86.0) is None
    assert _f(p, [_row(86.0, _ts(p, minutes=5), "BUY")], ref=86.0) == 86.0


def test_unreadable_time_is_not_a_reason_to_drop_the_row():
    p = _pos()
    assert _f(p, [_row(88.4, "not a time")]) == 88.4


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
