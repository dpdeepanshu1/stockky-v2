"""group468: scripts/entry_slippage_review.py (pure)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entry_slippage_review as es


def _r(i, slip, entry=100.0, qty=10, gross=100.0, hour="10", tab="hot_picks"):
    return {"position_id": i, "entry_slippage_pct": slip, "entry_price": entry, "quantity": qty, "gross_pnl": gross,
            "entry_hour_ist": hour, "source_tab": tab}


def test_rupee_cost_is_what_was_paid_above_the_signal():
    # signal 100, entry 101 -> slippage 1.0%; 10 shares -> 10 rupees
    assert round(es._rupees(_r(1, 1.0, entry=101.0)), 2) == 10.0
    assert es._rupees(_r(1, None)) is None and es._rupees(_r(1, 1.0, qty=0)) is None


def test_too_few_trades_gives_no_verdict():
    rep = es.review([_r(i, 0.5) for i in range(5)])
    assert rep["verdict"] == "too_few_trades"


def test_material_by_mean_and_by_share_and_not_material():
    assert es.review([_r(i, 0.3) for i in range(30)])["verdict"] == "material"
    # mean 0.05% but the rupees are 10%+ of a small gross: material by share
    small_gross = [_r(i, 0.05, entry=1000.0, qty=100, gross=40.0) for i in range(30)]
    rep = es.review(small_gross)
    assert rep["verdict"] == "material" and rep["share_of_gross_pct"] >= 10.0
    ok = es.review([_r(i, 0.02, gross=500.0) for i in range(30)])
    assert ok["verdict"] == "not_material" and ok["share_of_gross_pct"] is not None and ok["share_of_gross_pct"] < 10.0


def test_groups_and_markdown_and_missing_numbers():
    recs = [_r(i, 0.2, hour="09" if i % 2 else "11", tab="a" if i % 3 else "b") for i in range(30)] + [_r(99, None)]
    rep = es.review(recs)
    assert rep["with_slippage"] == 30 and set(rep["by_hour"]) == {"09", "11"} and set(rep["by_source_tab"]) == {"a", "b"}
    md = es.to_markdown(rep)
    assert "Verdict" in md and "by entry hour IST" in md
    assert es.review([])["verdict"] == "too_few_trades"
