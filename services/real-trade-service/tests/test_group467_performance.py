"""group467: trade_records.performance_block + charges share of gross profit (pure)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import trade_records as tr


def _r(day, net, gross=None, charges=0.0):
    return {"exit_day_ist": day, "net_pnl": net, "gross_pnl": net if gross is None else gross, "charges": charges,
            "exit_reason": "stop_hit"}


def test_performance_block_matches_hand_computation():
    nets = [100.0, -50.0, 200.0, -30.0, -100.0, 200.0]
    p = tr.performance_block([_r(f"2026-10-{i + 1:02d}", n) for i, n in enumerate(nets)])
    assert p["total_net"] == 320.0 and p["max_drawdown"] == 130.0
    assert (p["drawdown_start"], p["drawdown_end"], p["days_in_drawdown"]) == ("2026-10-03", "2026-10-05", 3)
    assert p["return_maxdd"] == 2.46 and p["max_win_streak"] == 1


def test_performance_block_empty_and_garbage_never_raises():
    assert tr.performance_block([])["days"] == 0
    assert tr.performance_block([{"net_pnl": None}, {}])["days"] == 0


def test_agg_exposes_charges_share_of_gross_profit():
    a = tr._agg([_r("2026-10-01", 800.0, gross=1000.0, charges=200.0)], 5)
    assert a["charges_share_of_gross_profit_pct"] == 20.0 and a["gross_pnl"] == 1000.0 and a["charges"] == 200.0
    neg = tr._agg([_r("2026-10-01", -1200.0, gross=-1000.0, charges=200.0)], 5)
    assert neg["charges_share_of_gross_profit_pct"] is None
    unknown = tr._agg([{"exit_day_ist": "d", "net_pnl": None, "gross_pnl": 1000.0, "charges": None}], 5)
    assert unknown["charges_share_of_gross_profit_pct"] is None


def test_expectancy_report_carries_performance():
    rep = tr.expectancy_report([_r("2026-10-01", 10.0), _r("2026-10-02", -4.0)])
    assert rep["performance"]["days"] == 2 and rep["performance"]["max_drawdown"] == 4.0
    assert tr.expectancy_report([])["trades"] == 0


def test_daily_message_mentions_charges_share_of_gross_profit():
    snap = {"mode": "REAL", "day": "2026-10-02", "week_days": 7,
            "today": {"overall": tr._agg([_r("2026-10-02", 800.0, gross=1000.0, charges=200.0)], 5), "by_exit_reason": {}}}
    assert "Charges were 20% of the gross profit" in tr.format_daily_message(snap)


def test_daily_message_shows_window_drawdown_only_when_there_is_one():
    wp = tr.performance_block([_r("2026-10-01", 100.0), _r("2026-10-02", -60.0), _r("2026-10-03", 10.0)])
    wp.pop("equity")
    base = {"mode": "REAL", "day": "2026-10-03", "week_days": 7,
            "today": {"overall": tr._agg([_r("2026-10-03", 10.0)], 5), "by_exit_reason": {}}}
    assert "Last 7 days drawdown" in tr.format_daily_message({**base, "week_performance": wp})
    assert "drawdown" not in tr.format_daily_message(base)                                  # old snapshots without the key still format
    up = tr.performance_block([_r("2026-10-01", 5.0), _r("2026-10-02", 6.0)])
    assert "drawdown" not in tr.format_daily_message({**base, "week_performance": up})
