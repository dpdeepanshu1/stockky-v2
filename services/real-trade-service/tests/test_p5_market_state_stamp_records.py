"""P5: market_state / profile_version on the per-trade record, the by_market_state expectancy group and the export/report script. Pure."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))

import trade_records as tr


def utc(h, m=0):
    return datetime(2026, 10, 9, h, m, tzinfo=timezone.utc)


def pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=utc(4), closed_at=utc(5),
             broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
             is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def order(id, side, **kw):
    d = dict(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=None, watchlist_entry_id=None,
             execution_source="AUTO", created_at=utc(4), updated_at=utc(4))
    d.update(kw)
    return NS(**d)


def rec(p, buy_kw=None):
    return tr.build_records([p], [order(1, "BUY", **(buy_kw or {})), order(2, "SELL")], [], [], [], [], [])[0]


def test_record_carries_the_position_stamp():
    r = rec(pos(market_state="stressed", profile_version="v7"))
    assert r["market_state"] == "stressed" and r["profile_version"] == "v7"


def test_falls_back_to_the_entry_order_when_the_position_has_none():
    r = rec(pos(market_state=None, profile_version=None), {"market_state": "normal", "profile_version": "v3"})
    assert r["market_state"] == "normal" and r["profile_version"] == "v3"


def test_position_stamp_wins_over_the_order():
    r = rec(pos(market_state="stressed", profile_version="v7"), {"market_state": "normal", "profile_version": "v3"})
    assert r["market_state"] == "stressed" and r["profile_version"] == "v7"


def test_nothing_recorded_is_none_never_a_default():
    r = rec(pos())                                    # row has no attribute at all (pre-stamp history)
    assert r["market_state"] is None and r["profile_version"] is None
    r = rec(pos(market_state="", profile_version="  "))
    assert r["market_state"] is None and r["profile_version"] is None


def test_expectancy_report_has_by_market_state_and_none_bucket():
    rows = [rec(pos(id=1, market_state="stressed", profile_version="v7", realized_pnl=-30.0)),
            rec(pos(id=2, market_state="normal", profile_version="v7", realized_pnl=50.0)),
            rec(pos(id=3, market_state=None, profile_version=None, realized_pnl=10.0))]
    rep = tr.expectancy_report(rows)
    assert set(rep["by_market_state"]) == {"stressed", "normal", "none"}
    assert rep["by_market_state"]["stressed"]["trades"] == 1
    assert set(rep["by_profile_version"]) == {"v7", "none"} and rep["by_profile_version"]["v7"]["trades"] == 2


def test_empty_report_still_lists_the_new_groups():
    rep = tr.expectancy_report([])
    assert rep["by_market_state"] == {} and rep["by_profile_version"] == {}


def test_records_report_script_groups_by_market_state():
    import records_report as rr
    titles = [t for t, _ in rr.GROUPS]
    assert "market state at entry" in titles and "profile version at entry" in titles
    recs = [{"position_id": i, "symbol": "A", "net_pnl": v, "gross_pnl": v, "market_state": s, "opened_at": "2026-10-09T04:00:00+00:00",
             "closed_at": "2026-10-09T05:00:00+00:00"} for i, (s, v) in enumerate([("stressed", -10.0), ("normal", 5.0), (None, 1.0)])]
    rep = rr.build_report(recs, min_n=1)
    assert set(rep["groups"]["market state at entry"]) == {"stressed", "normal", "none"}
