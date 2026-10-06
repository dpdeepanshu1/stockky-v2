"""Group 207 (item 12): dashboard summary counts only settled trades; breakevens are not losses."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from orders import review_stats, trade_stats


def row(status="TARGET_HIT", pnl=0.0, msg=None, **kw):
    return NS(status=status, realized_pnl=pnl, error_message=msg, symbol=kw.pop("symbol", "X"), **kw)


@pytest.mark.parametrize("r,expected", [
    (row(pnl=10.0), "win"),
    (row("STOP_HIT", -5.0), "loss"),
    (row("STAGNATION_EXIT", 0.0), "breakeven"),
    (row("STAGNATION_EXIT", 0.003), "breakeven"),
    (row("STAGNATION_EXIT", 0.0, "STAGNATION_EXIT_PENDING_RECONCILE: x"), "pending"),
    (row("EOD_SQUAREOFF", 0.0, "EOD_SQUAREOFF_UNRESOLVED: could not recover"), "pending"),
    (row("ERROR", 0.0, "STAGNATION_EXIT_SELL_DEAD: order X2 REJECTED"), "error"),
    (row("ERROR", None, "Entry leg REJECTED on Dhan (reconciled)"), "rejected_entry"),
    (row("OPEN", None), None),
    (row("EXIT_LEGS_REJECTED", 0.0), None),
    (row("TARGET_HIT", None), None),
])
def test_classify(r, expected):
    assert trade_stats.classify(r) == expected


def test_summary_separates_breakeven_pending_and_errors():
    rows = [row(pnl=100.0, symbol="W"), row("STOP_HIT", -40.0, symbol="L"),
            row("STAGNATION_EXIT", 0.0, symbol="B1"), row("STAGNATION_EXIT", 0.0, symbol="B2"),
            row("STAGNATION_EXIT", 0.0, "STAGNATION_EXIT_PENDING_RECONCILE: x", symbol="P"),
            row("ERROR", 0.0, "MANUAL_EXIT_SELL_DEAD: y", symbol="E"),
            row("ERROR", None, "Entry leg REJECTED on Dhan", symbol="R"),
            row("OPEN", None, symbol="O")]
    s = trade_stats.summarize(rows)
    assert (s["total_trades"], s["wins"], s["losses"], s["breakeven"]) == (4, 1, 1, 2)
    assert (s["pending_reconcile"], s["error_trades"], s["rejected_entries"]) == (1, 1, 1)
    assert s["win_rate_pct"] == 25.0 and s["total_pnl"] == 60.0
    assert s["best"].symbol == "W" and s["worst"].symbol == "L"


def test_pending_rows_do_not_drag_the_win_rate_or_pnl():
    # the old maths: 1 win / 3 trades; the two placeholders were counted as 0-rupee losses
    rows = [row(pnl=50.0), row("STAGNATION_EXIT", 0.0, "STAGNATION_EXIT_PENDING_RECONCILE: x"),
            row("STAGNATION_EXIT", 0.0, "STAGNATION_EXIT_PENDING_RECONCILE: x")]
    s = trade_stats.summarize(rows)
    assert s["total_trades"] == 1 and s["win_rate_pct"] == 100.0 and s["pending_reconcile"] == 2


def test_empty_summary():
    s = trade_stats.summarize([])
    assert s["total_trades"] == 0 and s["win_rate_pct"] is None and s["total_pnl"] == 0.0
    assert s["best"] is None and s["worst"] is None


def test_review_breakdown_leaves_out_pending_placeholders():
    from datetime import datetime, timezone
    base = dict(opened_at=datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc), window_source="5m",
                entry_price=100.0, max_price_seen=None, min_price_seen=None)
    rows = [NS(status="TARGET_HIT", realized_pnl=10.0, error_message=None, **base),
            NS(status="STAGNATION_EXIT", realized_pnl=0.0, error_message="STAGNATION_EXIT_PENDING_RECONCILE: x", **base)]
    assert review_stats.breakdown(rows)["trades"] == 1
