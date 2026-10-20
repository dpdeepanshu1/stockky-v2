"""group469: exit trigger price and exit slippage stored on each trade record (trade_records.py). Pure."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import trade_records as tr


def utc(h, m=0):
    return datetime(2026, 10, 9, h, m, tzinfo=timezone.utc)


def pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=utc(4), closed_at=utc(9, 30),
             broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
             is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None, current_stop=None)
    d.update(kw)
    return NS(**d)


def order(id, side, reason=None):
    return NS(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=reason, decision_id=None, watchlist_entry_id=None,
              execution_source="AUTO", created_at=utc(4) if side == "BUY" else utc(9, 30), updated_at=utc(9, 30), qty=10)


def fill(oid, price, at):
    return NS(order_id=oid, qty=10, price=price, filled_at=at)


def xd(action, ltp, at, id=1, position_id=1):
    return NS(id=id, position_id=position_id, action=action, ltp_at_decision=ltp, evaluated_at=at)


def rec(exit_price, reason, decisions=(), **pkw):
    orders = [order(1, "BUY"), order(2, "SELL", reason)]
    fills = [fill(1, 100.0, utc(4)), fill(2, exit_price, utc(9, 30))]
    return tr.build_records([pos(**pkw)], orders, fills, [], [], [], [], exit_decisions=list(decisions))[0]


def test_decision_ltp_is_the_trigger_and_slippage_is_fill_vs_it():
    r = rec(98.5, "eod_squareoff", [xd("FULL_EXIT", 99.0, utc(9, 29))])
    assert r["exit_trigger_price"] == 99.0 and r["exit_trigger_source"] == "decision_ltp"
    assert r["exit_slippage_pct"] == round((98.5 - 99.0) / 99.0 * 100.0, 3)     # -0.505: filled worse than the trigger


def test_hold_and_trail_rows_are_not_exits_and_the_latest_exit_decision_wins():
    ds = [xd("HOLD", 120.0, utc(9, 20), 1), xd("TRAIL_STOP", 118.0, utc(9, 21), 2),
          xd("FULL_EXIT", 97.0, utc(9, 25), 3), xd("EMERGENCY_EXIT", 96.0, utc(9, 28), 4)]
    r = rec(95.0, "stop_hit", ds)
    assert r["exit_trigger_price"] == 96.0 and r["exit_trigger_source"] == "decision_ltp"


def test_broker_stop_with_no_decision_uses_the_final_stop():
    r = rec(97.8, "broker_stop", [], current_stop=98.0)
    assert r["exit_trigger_source"] == "final_stop" and r["exit_trigger_price"] == 98.0
    assert r["exit_slippage_pct"] == round((97.8 - 98.0) / 98.0 * 100.0, 3)


def test_non_stop_exit_without_a_decision_has_no_trigger_and_nothing_is_guessed():
    r = rec(101.0, "manual", [], current_stop=98.0)
    assert (r["exit_trigger_price"], r["exit_trigger_source"], r["exit_slippage_pct"]) == (None, None, None)


def test_bad_rows_never_raise_and_summary_groups_by_reason():
    assert tr._exit_trigger(pos(), "stop_hit", [NS(action="FULL_EXIT", ltp_at_decision="x", evaluated_at=None)], 90.0)["exit_slippage_pct"] is None
    base = {"gross_pnl": 10.0, "net_pnl": 8.0, "charges": 2.0, "exit_day_ist": "2026-10-09"}
    recs = [{**base, "exit_reason": "stop_hit", "exit_slippage_pct": -0.2}, {**base, "exit_reason": "stop_hit", "exit_slippage_pct": -0.4},
            {**base, "exit_reason": "eod_squareoff", "exit_slippage_pct": 0.1}, {**base, "exit_reason": "manual", "exit_slippage_pct": None}]
    s = tr.exit_slippage_summary(recs)
    assert s["overall"]["n"] == 3 and s["by_exit_reason"]["stop_hit"] == {"n": 2, "mean": -0.3, "worst": -0.4, "best": -0.2}
    assert tr.exit_slippage_summary([]) == {}
    assert tr.expectancy_report(recs)["exit_slippage"]["overall"]["n"] == 3
