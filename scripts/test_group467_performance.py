"""group467: whole-book performance block, exit slippage and charges share of gross profit (scripts/records_report.py). Pure."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import records_report as rr


def _days(nets, start=1):
    return [{"exit_day_ist": f"2026-10-{start + i:02d}", "net_pnl": n} for i, n in enumerate(nets)]


def test_performance_block_hand_computed():
    p = rr.performance_block(_days([100.0, -50.0, 200.0, -30.0, -100.0, 200.0]))
    assert p["days"] == 6 and p["total_net"] == 320.0
    assert p["max_drawdown"] == 130.0
    assert (p["drawdown_start"], p["drawdown_end"]) == ("2026-10-03", "2026-10-05")
    assert p["days_in_drawdown"] == 3                      # 10-02, 10-04, 10-05
    assert p["return_maxdd"] == round(320.0 / 130.0, 2)
    assert p["max_win_streak"] == 1
    assert p["best_day"] == {"day": "2026-10-03", "net": 200.0}


def test_same_day_trades_are_summed_and_streak_counts_consecutive_winning_days():
    recs = _days([10.0, 20.0, 30.0, -5.0, 40.0])
    recs.append({"exit_day_ist": "2026-10-01", "net_pnl": 5.0})        # second trade on day 1
    p = rr.performance_block(recs)
    assert p["days"] == 5 and p["equity"][0] == {"day": "2026-10-01", "cum_net": 15.0}
    assert p["max_win_streak"] == 3 and p["max_drawdown"] == 5.0


def test_no_drawdown_when_book_only_rises():
    p = rr.performance_block(_days([10.0, 20.0]))
    assert p["max_drawdown"] == 0.0 and p["drawdown_start"] is None and p["return_maxdd"] is None and p["days_in_drawdown"] == 0


def test_first_day_loss_is_a_drawdown_from_zero():
    p = rr.performance_block(_days([-40.0, 10.0]))
    assert p["max_drawdown"] == 40.0 and p["drawdown_end"] == "2026-10-01"


def test_empty_and_none_safe():
    assert rr.performance_block([])["days"] == 0
    assert rr.performance_block([{"exit_day_ist": None, "net_pnl": None}, {"net_pnl": "x"}])["days"] == 0


def test_exit_slippage_uses_initial_stop_and_only_stop_hits():
    recs = [
        {"exit_reason": "stop_hit", "entry_price": 100.0, "stop_pct": 2.0, "exit_price": 97.9},   # trigger 98 -> -0.102%
        {"exit_reason": "stop_hit", "entry_price": 100.0, "stop_pct": 2.0, "exit_price": 98.0},   # 0
        {"exit_reason": "eod_squareoff", "entry_price": 100.0, "stop_pct": 2.0, "exit_price": 50.0},
        {"exit_reason": "stop_hit", "entry_price": 100.0, "stop_pct": None, "exit_price": 98.0},
    ]
    s = rr.exit_slippage_pct(recs)
    assert s["n"] == 2 and s["worst"] == -0.102 and s["best"] == 0.0 and s["mean"] == -0.051
    assert rr.exit_slippage_pct([{"exit_reason": "eod_squareoff"}]) is None


def _flat(n, gross, charges, net):
    return [{"position_id": i, "mode": "REAL", "opened_at": "2026-10-01", "return_pct": 1.0, "gross_pnl": gross,
             "charges": charges, "net_pnl": net, "exit_day_ist": "2026-10-02"} for i in range(n)]


def test_charges_share_of_gross_profit_and_markdown():
    rep = rr.build_report(_flat(12, 1000.0, 200.0, 800.0))
    rt = rep["robust_totals"]
    assert rt["charges_share_of_gross_profit_pct"] == 20 and rt["charges_share_of_gross_loss_pct"] is None
    md = rr.to_markdown(rep)
    assert "20% of the gross profit" in md and "Whole-book performance" in md


def test_charges_share_of_gross_loss_unchanged_and_no_profit_share_when_gross_negative():
    rt = rr.build_report(_flat(12, -1000.0, 200.0, -1200.0))["robust_totals"]
    assert rt["charges_share_of_gross_loss_pct"] == 20 and rt["charges_share_of_gross_profit_pct"] is None


def test_small_sample_gets_no_profit_share():
    assert rr.build_report(_flat(3, 1000.0, 200.0, 800.0))["robust_totals"]["charges_share_of_gross_profit_pct"] is None


def test_drawdown_matches_quantstats_when_installed():
    """Optional cross-check only; QuantStats is never a runtime dependency. Tiny returns so additive ~ compounded."""
    try:
        import pandas as pd
        import quantstats as qs
    except Exception:  # noqa: BLE001
        return
    nets = [0.001, -0.0005, 0.002, -0.0003, -0.001, 0.002]
    p = rr.performance_block(_days(nets))
    peak = max(c["cum_net"] for c in p["equity"])
    assert abs(p["max_drawdown"] / peak - abs(qs.stats.max_drawdown(pd.Series(nets)))) < 5e-3


def test_stored_exit_slippage_overall_by_reason_and_markdown():
    recs = _flat(12, 100.0, 10.0, 90.0)
    for i, r in enumerate(recs):
        r["exit_reason"] = "stop_hit" if i % 2 else "eod_squareoff"
        r["exit_slippage_pct"] = -0.3 if i % 2 else 0.1
    x = rr.exit_slippage_stored(recs)
    assert x["overall"]["n"] == 12 and x["by_exit_reason"]["stop_hit"]["mean"] == -0.3 and x["by_exit_reason"]["eod_squareoff"]["best"] == 0.1
    assert rr.exit_slippage_stored([{"exit_reason": "x"}]) is None
    md = rr.to_markdown(rr.build_report(recs))
    assert "fill vs the exit trigger" in md and "- stop_hit: mean -0.3%" in md
