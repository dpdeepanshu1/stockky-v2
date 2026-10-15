"""group 396: stop_pct / stop_basis on the per-trade record and the by_stop_basis expectancy group (trade_records.py). Pure - SimpleNamespace rows."""
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


def order(id, side, decision_id=None):
    return NS(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=decision_id, watchlist_entry_id=None,
              execution_source="AUTO", created_at=utc(4, 0), updated_at=utc(4, 0))


def build_one(p, decision=None):
    orders = [order(1, "BUY", decision_id=decision.id if decision else None), order(2, "SELL")]
    return tr.build_records([p], orders, [], [decision] if decision else [], [], [], [])[0]


def dec(**kw):
    d = dict(id=7, candidate_id=None, proposed_price=100.0, proposed_stop=97.0, stop_basis="atr")
    d.update(kw)
    return NS(**d)


def test_stop_pct_comes_from_the_fixed_initial_distance():
    r = build_one(pos(initial_stop_distance=3.2), dec(stop_basis="flat"))
    assert r["stop_pct"] == 3.2 and r["stop_basis"] == "flat"


def test_stop_pct_falls_back_to_the_decisions_proposed_stop():
    r = build_one(pos(initial_stop_distance=None), dec(proposed_price=100.0, proposed_stop=96.5))
    assert r["stop_pct"] == 3.5 and r["stop_basis"] == "atr"


def test_a_position_without_the_attribute_uses_the_fallback_too():
    r = build_one(pos(), dec())                       # NS row has no initial_stop_distance at all
    assert r["stop_pct"] == 3.0


def test_rows_from_before_group_396_give_none_not_zero():
    r = build_one(pos(initial_stop_distance=None), None)
    assert r["stop_pct"] is None and r["stop_basis"] is None


def test_blank_or_non_string_basis_is_none():
    assert build_one(pos(initial_stop_distance=2.0), dec(stop_basis=""))["stop_basis"] is None
    assert build_one(pos(initial_stop_distance=2.0), dec(stop_basis=5))["stop_basis"] is None


def test_zero_or_inverted_levels_give_none():
    assert build_one(pos(), dec(proposed_stop=100.0))["stop_pct"] is None
    assert build_one(pos(), dec(proposed_stop=105.0))["stop_pct"] is None


def rec(pnl, basis=None):
    return {"gross_pnl": pnl, "net_pnl": None, "exit_reason": "stop_hit", "entry_hour_ist": "09", "tier_name": None, "catalyst_type": None,
            "source_tab": "watchlist", "exit_day_ist": "2026-10-09", "entry_slippage_pct": None, "held_minutes": 10.0, "charges": None, "stop_basis": basis}


def test_report_groups_by_stop_basis_with_unknown_bucket():
    out = tr.expectancy_report([rec(-10, "atr"), rec(30, "atr"), rec(-5, "flat"), rec(7)])
    g = out["by_stop_basis"]
    assert set(g) == {"atr", "flat", "unknown"}
    assert g["atr"]["trades"] == 2 and g["atr"]["expectancy"] == 10.0 and g["flat"]["trades"] == 1 and g["unknown"]["trades"] == 1


def test_empty_report_still_has_the_group_key():
    assert tr.expectancy_report([])["by_stop_basis"] == {}


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
