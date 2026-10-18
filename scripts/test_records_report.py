"""group 430: scripts/records_report.py (closed-trade report). Pure; synthetic records."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import records_report as rr


def rec(pid, ret, net=None, **kw):
    d = {"position_id": pid, "mode": "REAL", "symbol": f"S{pid}", "return_pct": ret, "gross_pnl": ret * 10, "net_pnl": net if net is not None else ret * 10 - 5,
         "charges": 5, "opened_at": f"2026-10-{(pid % 20) + 1:02d}T04:00:00+00:00", "closed_at": f"2026-10-{(pid % 20) + 1:02d}T09:30:00+00:00",
         "exit_reason": "eod_squareoff", "entry_hour_ist": 10, "hold_bucket": "same_day"}
    d.update(kw)
    return d


def test_num_and_buckets():
    assert rr.num("1.5") == 1.5 and rr.num(None) is None and rr.num(True) is None and rr.num("x") is None and rr.num(float("nan")) is None
    assert [rr.imb_bucket(v) for v in (-0.5, 0.1, 0.5, None)] == ["ask_heavy", "balanced", "bid_heavy", "unknown"]
    assert [rr.spread_bucket(v) for v in (0.1, 0.2, 0.5, None)] == ["tight", "normal", "wide", "unknown"]


def test_load_dedupes_by_position_id_filters_mode_and_skips_bad_files(tmp_path, capsys):
    a, b = tmp_path / "rt_a.json", tmp_path / "rt_b.json"
    a.write_text(json.dumps({"records": [rec(1, -1.0), rec(2, 2.0, mode="DEMO")]}))
    b.write_text(json.dumps({"records": [rec(1, -3.0), {"no_id": 1}]}))
    (tmp_path / "rt_bad.json").write_text("{not json")
    got = rr.load_records([str(a), str(b), str(tmp_path / "rt_bad.json"), str(tmp_path / "missing.json")], "REAL")
    assert [r["position_id"] for r in got] == [1] and got[0]["return_pct"] == -3.0        # the later file wins; DEMO and id-less rows dropped
    assert "skip" in capsys.readouterr().err
    assert len(rr.load_records([str(a)], None)) == 2


def test_report_totals_medians_and_outlier_free_net():
    rows = [rec(i, -1.0) for i in range(1, 9)] + [rec(9, 30.0, net=290), rec(10, -40.0, net=-400, exit_reason="emergency_gap_down")]
    rep = rr.build_report(rows, outlier_pct=15.0)
    assert rep["trades"] == 10 and rep["all"]["median_pct"] == -1.0 and rep["all"]["win_pct"] == 10.0
    rt = rep["robust_totals"]
    assert rt["best_trade"]["position_id"] == 9 and rt["net_without_best_trade"] == rep["all"]["net"] - 290
    assert {o["position_id"] for o in rt["outliers"]} == {9, 10} and rt["trades_without_outliers"] == 8
    assert rt["net_without_outliers"] == sum(r["net_pnl"] for r in rows[:8])
    assert rep["gap_or_ghost"][0]["position_id"] == 10


def test_since_until_filter_by_opened_day():
    rows = [rec(i, 1.0) for i in range(1, 6)]                  # opened 2026-10-02 .. 2026-10-06
    assert rr.build_report(rows, since="2026-10-04")["trades"] == 3
    assert rr.build_report(rows, until="2026-10-03")["trades"] == 2
    assert rr.build_report(rows, since="2027-01-01")["trades"] == 0


def test_order_flow_buckets_and_unknown_when_not_recorded():
    rows = [rec(1, -2.0, imbalance_5=-0.6, imbalance_20=0.3, flow_spread_pct=0.4), rec(2, 1.0, imbalance_5=0.6), rec(3, 0.5)]
    g = rr.build_report(rows)["groups"]
    assert g["order flow: best-5 imbalance"]["ask_heavy"]["n"] == 1 and g["order flow: best-5 imbalance"]["unknown"]["n"] == 1
    assert g["order flow: spread at entry"]["wide"]["n"] == 1
    md = rr.to_markdown(rr.build_report([rec(1, 1.0)]))
    assert "No trade carries these numbers yet" in md


def test_stop_overshoot_and_late_eod_listing():
    rows = [rec(1, -4.8, exit_reason="stop_hit", stop_pct=3.0), rec(2, -3.0, exit_reason="stop_hit", stop_pct=3.0),
            rec(3, -2.0, held_days=4), rec(4, -1.0, held_days=0)]
    rep = rr.build_report(rows)
    sh = rep["stop_hits"]
    assert sh["n"] == 2 and sh["with_stop_pct"] == 2 and sh["mean_overshoot_pct"] == 0.9 and sh["max_overshoot_pct"] == 1.8 and sh["with_max_gain"] == 0
    assert [r["position_id"] for r in rep["late_eod_squareoff"]] == [3]


def test_few_marker_and_empty_and_missing_fields():
    rows = [rec(i, 1.0, source_tab="a") for i in range(1, 4)]
    md = rr.to_markdown(rr.build_report(rows, min_n=10))
    assert "(few)" in md and "Totals:" in md
    assert "No trades in range" in rr.to_markdown(rr.build_report([]))
    sparse = rr.build_report([{"position_id": 1, "mode": "REAL"}, {"position_id": 2, "mode": "REAL", "return_pct": "x"}])
    assert sparse["trades"] == 2 and sparse["all"]["mean_pct"] is None
    rr.to_markdown(sparse)                                                                    # must not raise


def test_cli_end_to_end(tmp_path, capsys):
    f = tmp_path / "rt_1.json"
    f.write_text(json.dumps({"records": [rec(i, -1.0 if i % 3 else 2.0) for i in range(1, 15)]}))
    out = tmp_path / "o.json"
    assert rr.main([str(f), "--since", "2026-10-05", "--json", str(out)]) == 0
    assert "Closed-trade report" in capsys.readouterr().out and json.loads(out.read_text())["trades"] > 0
    assert rr.main([str(tmp_path / "nothing_*.json")]) == 1
