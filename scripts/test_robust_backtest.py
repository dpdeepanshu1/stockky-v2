"""group 428: median / clipped / day-cluster checks (scripts/_robust.py) and their use in the entry-rules and legacy-gate reports."""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _robust as rb
import entry_rules_backtest as er
import legacy_gate_backtest as lg
from test_entry_rules_backtest import _write_universe


def _rows(n_days=60, per_day=40, acted_mean=-1.0, left_mean=1.0, outlier=None, seed=1):
    rng = random.Random(seed)
    rows = []
    for d in range(n_days):
        for k in range(per_day):
            acted = k % 4 == 0
            rows.append({"sym": f"S{k}", "month": f"2025-{d % 12 + 1:02d}", "day": f"D{d}", "r10": rng.gauss(acted_mean if acted else left_mean, 3.0), "acted": acted})
    if outlier is not None:
        rows[5]["acted"], rows[5]["r10"] = True, outlier            # one absurd return on an acted-on entry
    return rows


def test_a_real_difference_is_helps_on_every_measure():
    rs = rb.robust_stats(_rows(), lambda r: r["acted"], "r10", 200)
    assert rs["median_edge"] > 1.5 and rs["wins_edge"] > 1.5 and rs["day_ci"][0] > 0
    assert rb.robust_verdict("helps", rs) == "helps"


def test_one_absurd_return_cannot_make_a_rule_look_like_it_hurts():
    rows = _rows(acted_mean=0.0, left_mean=0.0, outlier=40000.0)
    rs = rb.robust_stats(rows, lambda r: r["acted"], "r10", 200)
    assert rs["extreme_rows"] == 1
    assert abs(rs["median_edge"]) < 0.5 and abs(rs["wins_edge"]) < 0.5
    assert rb.robust_verdict("hurts", rs) == "no_edge"             # the raw-mean verdict said hurts; the robust one does not


def test_hurts_needs_all_three_to_agree_and_insufficient_stays():
    rs = rb.robust_stats(_rows(acted_mean=2.0, left_mean=-0.5), lambda r: r["acted"], "r10", 200)
    assert rb.robust_verdict("no_edge", rs) == "hurts"
    assert rb.robust_verdict("insufficient_sample", rs) == "insufficient_sample"
    assert rb.robust_verdict("helps", None) == "helps"
    assert rb.robust_verdict("helps", {"day_ci": None, "median_edge": 1, "wins_edge": 1}) == "no_edge"


def test_empty_and_one_sided_input_never_raise():
    assert rb.robust_stats([], lambda r: True, "r10") is None
    rs = rb.robust_stats([{"sym": "A", "month": "m", "r10": 1.0}], lambda r: False, "r10", 100)
    assert rs["median_edge"] is None and rs["day_ci"] is None


def test_jump_in_window():
    c = [100.0, 101.0, 50.5, 51.0, 52.0]                            # a 50 % one-day fall at index 2 (a 1:2 split)
    assert rb.jump_in_window(c, 1, 5, 35.0) and not rb.jump_in_window(c, 3, 5, 35.0)
    assert not rb.jump_in_window(c, 1, 5, 0.0)


def test_entry_rows_carry_day_and_jump_flags():
    import datetime as dt
    bars, px, d = [], 100.0, dt.date(2023, 1, 2)
    while len(bars) < 330:
        if d.weekday() < 5:
            nx = px * (0.5 if len(bars) == 200 else 1.001)           # one unadjusted 1:2 split
            bars.append((dt.datetime(d.year, d.month, d.day, 4, tzinfo=dt.timezone.utc).timestamp(), px, max(px, nx) * 1.002, min(px, nx) * 0.998, nx, 1e6))
            px = nx
        d += dt.timedelta(days=1)
    rows, _ = er.rows_for(bars, "AAA", 3, 0.3, 0.1, 1, dt.date(2025, 9, 1), None, 5)
    assert rows and all("day" in r and "jump" in r for r in rows)
    assert any(r["jump"] for r in rows) and any(not r["jump"] for r in rows)


def test_grid_materiality_label():
    def cell(diff, lo, hi):
        grids = [{(f"S{k}", "2025-01"): [[10, 1.0, 5, 1, 1, diff * 10] for _ in range(20)] for k in range(40)}]
        return grids
    rep = er.grid_report(cell(0.013, 0, 0), 100, 10, min_effect_pp=0.05)
    assert all(c["verdict"] in ("live", "same", "better_tiny", "worse_tiny", "BETTER", "WORSE", "insufficient_sample") for c in rep["cells"])
    assert not any(c["verdict"] == "BETTER" for c in rep["cells"])


def test_end_to_end_reports_have_the_robust_tables():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _write_universe(d)
        assert er.main([str(d), "--boot", "60", "--min-n", "20", "--out", str(d / "o1")]) == 0
        rep = json.loads((d / "o1" / "entry_rules_report.json").read_text())
        g = rep["gates"]["cal_friday"]
        assert "verdict_robust" in g and "median_edge" in g and "extreme_rows" in g
        assert "outlier-proof" in (d / "o1" / "entry_rules_report.md").read_text()
        assert lg.main([str(d), "--boot", "60", "--min-n", "20", "--out", str(d / "o2")]) in (0, 1)
