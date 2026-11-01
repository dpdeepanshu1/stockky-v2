"""E276 round 59: the Real Trade risk ladder (entry_engine/risk_stage.py): the decision tag, closed-position rows, the stage and the raised heat limit, DB stubbed.
Plain runner: python3 tests/test_e276_risk_ladder.py (pytest also collects it)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import ladder_stats, risk_ladder, risk_stage as rs


def test_tag_and_parse():
    t = rs.decision_tag(42)
    assert t == " [decision=42]" and rs.decision_tag(None) == "" and rs.decision_tag("x") == ""
    assert rs.parse_opened("10 @ 100.5, stop 98, target 104" + t) == (10, 42)
    assert rs.parse_opened("10 @ 100.5, stop 98, target 104 (broker-confirmed)" + t) == (10, 42)
    assert rs.parse_opened("10 @ 100.5, stop 98, target 104") is None and rs.parse_opened(None) is None


def _pos(i, pnl, entry=100.0, dist=2.0):
    return SimpleNamespace(id=i, net_realized_pnl=pnl, realized_pnl=pnl, avg_entry_price=entry, initial_stop_distance=dist,
                           opened_at=datetime(2026, 11, 1, 10, 0), max_price_seen=None, min_price_seen=None)


def test_rows_and_cells():
    stamp = json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930", "side": "LONG"}})
    pos = [_pos(i, 4.0 if i % 3 else -2.0) for i in range(1, 121)]                       # 2/3 win +4 (=+2R on a Rs20 risk? qty 10 x 2 = 20) ...
    opened = {p.id: f"10 @ 100, stop 98, target 104 [decision={p.id}]" for p in pos}
    decisions = {p.id: stamp for p in pos}
    rows = rs.build_rows(pos, opened, decisions)
    assert len(rows) == 120
    cells = ladder_stats.summarize(rows)
    c = cells["S1_CATALYST_UP|ALL"]
    assert c["n"] == 120 and c["r_n"] == 120 and c["lower_bound_r"] is not None
    assert rs.build_rows(pos[:3], {}, decisions) == []                                   # no tag = not counted


def test_stage_and_heat_limit():
    saved = rs._data
    try:
        good = {"r_n": 150, "lower_bound_r": 0.1}
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good}, "last": []}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 1 and rs.stage_for(None, "S9") == 0 and rs.stage_for(None, None) == 0
        assert rs.heat_limit_pct(None, "S1_CATALYST_UP", 5.0) == 6.0 and rs.heat_limit_pct(None, None, 5.0) == 5.0
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good}, "last": [-1.0] * 20}      # brake
        assert rs.stage_for(None, "S1_CATALYST_UP") == 0 and rs.heat_limit_pct(None, "S1_CATALYST_UP", 5.0) == 5.0
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": {"r_n": 400, "lower_bound_r": 0.3}}, "last": []}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 2 and rs.heat_limit_pct(None, "S1_CATALYST_UP", 5.0) == 7.0
        config.RISK_LADDER_ENABLED = False
        assert rs.stage_for(None, "S1_CATALYST_UP") == 0 and rs.heat_limit_pct(None, "S1_CATALYST_UP", 5.0) == 5.0
        config.RISK_LADDER_ENABLED = True
        def boom(db):
            raise RuntimeError("db down")
        rs._data = boom
        assert rs.stage_for(None, "S1_CATALYST_UP") == 0 and rs.heat_limit_pct(None, "S1_CATALYST_UP", 5.0) == 5.0
    finally:
        rs._data = saved
        config.RISK_LADDER_ENABLED = True


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
