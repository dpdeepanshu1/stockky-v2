"""
E337 - position-stocks short plan: size the short against the BID side of the 20-level book.

plan_short capped a short's quantity with depth_gate.max_qty_from_depth20, which always read the ASK side (`buy_qty_within_slip`) - the side a BUY walks into. A short
SELL walks into the bids, so a name with thin bids and fat asks was never capped, and one with thin asks and fat bids was capped for no reason. The call now passes
side="SHORT" and the cap reads `sell_qty_within_slip`.

Run from services/position-stocks-service:  python -m pytest tests/test_e337_short_depth_side.py -q
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_short_plan_asks_for_the_bid_side():
    s = _src("orders/short_entry.py")
    assert 'depth_gate.max_qty_from_depth20(sym, qty, "SHORT")' in s
    assert "depth_gate.max_qty_from_depth20(sym, qty)" not in s


def test_the_long_entry_still_asks_for_the_ask_side():
    s = _src("orders/entry.py")
    assert re.search(r"max_qty_from_depth20\(candidate\.symbol, quantity\)|max_qty_from_depth20\([^)]*\)", s)
    assert '"SHORT"' not in "".join(l for l in s.split("\n") if "max_qty_from_depth20" in l)


def test_the_cap_function_defaults_to_the_ask_side():
    import inspect
    from orders import depth_gate
    assert inspect.signature(depth_gate.max_qty_from_depth20).parameters["side"].default == "LONG"
