"""E274 round 47: swing picks with a typed positive event go first (swing/lane.order_event_first). Plain runner: python3 tests/test_e274_swing_event_first.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swing import lane

P = [{"symbol": "A", "rank": 1}, {"symbol": "B", "rank": 2}, {"symbol": "C", "rank": 3}, {"symbol": "D", "rank": 4}]


def test_backed_first_then_rank():
    out = lane.order_event_first(P, {"C": {"strength": 0.5}, "B": None})
    assert [t["symbol"] for t in out] == ["C", "A", "B", "D"]


def test_stronger_event_first_ties_by_rank():
    out = lane.order_event_first(P, {"D": {"strength": 0.9}, "B": {"strength": 0.9}, "C": {"strength": 0.5}})
    assert [t["symbol"] for t in out] == ["B", "D", "C", "A"]


def test_no_events_keeps_rank_order():
    assert [t["symbol"] for t in lane.order_event_first(P, {})] == ["A", "B", "C", "D"]


def test_bad_input_returns_picks():
    assert lane.order_event_first(P, None) == P


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
