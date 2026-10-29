"""E257: report fixes found in the 29 Oct audit (short replay PASS bar, P8 small-sample interval, 10:30 cutoff readout, probe row in the daily report)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily_trade_report as D  # noqa: E402
import p8_retest_evidence as P8  # noqa: E402
import short_history_replay as R  # noqa: E402
import strategy_readout as SR  # noqa: E402


def _trades(per_day):
    return [{"day": f"d{i}", "outcome": "eod", "net_pct": v} for i, vs in enumerate(per_day) for v in vs]


def test_short_replay_tiny_positive_mean_is_not_a_pass():
    t = _trades([[0.4, -0.4], [-0.3, 0.3], [0.5, -0.45]] * 4)   # mean about +0.009, noisy across sessions
    sell = R.summarize(t, 1.0, 2.0)
    verdict, reason, _ = R.evaluate(sell, {"n": 0}, 5, 10)
    assert verdict == "NOT_ENOUGH_DATA" and "not distinguishable from zero" in reason


def test_short_replay_clear_edge_still_passes():
    t = _trades([[0.5, 0.6]] * 12)
    verdict, _, _ = R.evaluate(R.summarize(t, 1.0, 2.0), {"n": 0}, 5, 10)
    assert verdict == "PASS"


def test_short_replay_negative_mean_still_fails():
    t = _trades([[-0.3, -0.2]] * 12)
    verdict, _, _ = R.evaluate(R.summarize(t, 1.0, 2.0), {"n": 0}, 5, 10)
    assert verdict == "FAIL"


def test_p8_markdown_hides_the_interval_when_the_sample_is_too_small():
    rep = {"verdict": "insufficient_sample", "counts": {"rescued": 1, "blocked": 40, "clean": 100, "other": 0}, "min_n": 30,
           "rescued_minus_blocked_pp": 4.8, "rescued_minus_clean_pp": 3.0,
           "bootstrap_vs_blocked": {"ci90": [2.9, 6.1], "p_positive": 1.0}, "bootstrap_vs_clean": {"ci90": [1, 2]}}
    text = "\n".join(P8._edge_lines(rep, str))
    assert "sample too small" in text and "P(>0)" not in text and "2.9" not in text


def test_readout_flags_a_cutoff_inside_a_big_hourly_bucket():
    buckets = {"10": {"trades": 5, "expectancy": -1.0}, "11": {"trades": 6, "expectancy": -2.0}, "13": {"trades": 6, "expectancy": 1.0}}
    v = SR.entry_window_verdict(buckets, "10:30", 30)
    assert v["left_out_trades"] == 5 and v["verdict"].startswith("NOT ENOUGH DATA - the 10:30 cutoff falls inside")


def test_daily_report_skips_the_sell_probe_and_signs_a_short_return():
    base = {"id": 1, "symbol": "AAA", "status": "TARGET_HIT", "entry_price": 100.0, "exit_price": 99.0, "realized_pnl": 5.0, "charges": 1.0,
            "opened_at": "2026-10-28T04:00:00+00:00", "closed_at": "2026-10-28T05:00:00+00:00", "quantity": 5, "exit_reason": "target"}
    probe = dict(base, id=2, window_source="PROBE", side="SHORT", quantity=1)
    short = dict(base, id=3, side="SHORT")
    rows = D.rows_from_ps({"trades": [probe, short]})
    assert len(rows) == 1 and rows[0]["ret"] > 0
