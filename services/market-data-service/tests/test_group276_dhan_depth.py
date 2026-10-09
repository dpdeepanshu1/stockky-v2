"""group276: best bid/ask + 5-level book summary from the Dhan market-quote `depth` block.
Run: python3 -m pytest tests/test_group276_dhan_depth.py -q"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dhan_data import quotes


def item(buy, sell, price=100.0):
    return {"last_price": price, "ohlc": {"close": 99.0}, "depth": {"buy": buy, "sell": sell}}


def lv(px, q):
    return {"price": px, "quantity": q, "orders": 1}


def test_spread_and_book_summary():
    r = quotes.map_item("X", item([lv(99.9, 100), lv(99.8, 50)], [lv(100.1, 80), lv(100.2, 10)]))
    assert r["best_bid"] == 99.9 and r["best_ask"] == 100.1
    assert r["spread_pct"] == pytest.approx(0.2, abs=0.001)
    assert r["bid_qty_5"] == 150 and r["ask_qty_5"] == 90
    assert r["book_value_5"] == pytest.approx(99.9 * 100 + 99.8 * 50 + 100.1 * 80 + 100.2 * 10)


def test_best_levels_are_picked_whatever_the_order():
    r = quotes.map_item("X", item([lv(99.5, 10), lv(99.9, 10)], [lv(100.4, 10), lv(100.1, 10)]))
    assert r["best_bid"] == 99.9 and r["best_ask"] == 100.1


@pytest.mark.parametrize("buy,sell", [
    ([], [lv(100.1, 1)]), ([lv(99.9, 1)], []),              # one side empty
    ([lv(101.0, 1)], [lv(100.0, 1)]),                       # crossed book
    ([lv(0, 5)], [lv(100.0, 5)]),                           # zero price
    ([lv(99.0, 0)], [lv(100.0, 5)]),                        # zero quantity
])
def test_unusable_depth_adds_no_fields(buy, sell):
    r = quotes.map_item("X", item(buy, sell))
    assert r is not None and "spread_pct" not in r and "best_bid" not in r


def test_missing_or_garbled_depth_never_breaks_the_quote():
    assert "spread_pct" not in quotes.map_item("X", {"last_price": 100})
    assert quotes.map_item("X", {"last_price": 100, "depth": "oops"})["price"] == 100
    assert quotes.map_item("X", {"last_price": 100, "depth": {"buy": [None], "sell": [{"price": "x"}]}})["price"] == 100


def test_depth_fields_survive_public_row():
    row = quotes.public_row(quotes.map_item("X", item([lv(99.9, 1)], [lv(100.1, 1)])))
    assert "spread_pct" in row and not any(k.startswith("_") for k in row)
