"""group 398: opened_at / closed_at / quantity on the per-trade record (trade_records.py), so scripts/opening_replay.py can replay a real-trade export. Pure."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_records as tr


def utc(h, m=0):
    return datetime(2026, 10, 9, h, m, tzinfo=timezone.utc)


def pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=utc(4, 0), closed_at=utc(5, 0),
             broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
             is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def order(id, side, qty=None):
    return NS(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=None, watchlist_entry_id=None,
              execution_source="AUTO", created_at=utc(4, 0), updated_at=utc(4, 0), qty=qty)


def fill(order_id, qty, price=100.0, at=None):
    return NS(order_id=order_id, qty=qty, price=price, filled_at=at or utc(4, 0))


def build(orders, fills):
    return tr.build_records([pos()], orders, fills, [], [], [], [])[0]


def test_quantity_is_the_filled_buy_quantity():
    r = build([order(1, "BUY", qty=50), order(2, "SELL", qty=50)], [fill(1, 30), fill(1, 15), fill(2, 45, at=utc(5, 0))])
    assert r["quantity"] == 45


def test_quantity_falls_back_to_the_buy_order_quantity():
    r = build([order(1, "BUY", qty=40), order(2, "SELL", qty=40)], [])
    assert r["quantity"] == 40


def test_quantity_is_none_when_nothing_matched():
    assert build([], [])["quantity"] is None


def test_two_buys_add_up_and_sells_are_ignored():
    r = build([order(1, "BUY", qty=10), order(3, "BUY", qty=5), order(2, "SELL", qty=15)], [fill(1, 10), fill(3, 5), fill(2, 15, at=utc(5, 0))])
    assert r["quantity"] == 15


def test_times_are_utc_iso_strings_the_replay_tool_can_parse():
    r = build([], [])
    assert r["opened_at"].startswith("2026-10-09T04:00:00") and r["closed_at"].startswith("2026-10-09T05:00:00")
    assert r["opened_at"].endswith(("Z", "+00:00"))


def test_entry_qty_helper_edge_cases():
    assert tr._entry_qty([], {}) is None
    assert tr._entry_qty([order(1, "BUY", qty="bad")], {}) is None
    assert tr._entry_qty([order(1, "BUY", qty=0)], {}) is None


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
