"""E575 round 550: scripts/evidence_report.py (one evidence verdict per group of closed trades). Pure; synthetic records."""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import evidence_report as er


def rec(pid, net, day=None, entry=100.0, qty=10, label="BUY NOW", tab="swing", **kw):
    d = {"position_id": pid, "mode": "REAL", "origin": "system", "symbol": f"S{pid}", "entry_price": entry, "quantity": qty, "net_pnl": net,
         "gross_pnl": net + 5, "charges": 5.0, "return_pct": net / (entry * qty) * 100.0,
         "opened_at": f"2026-10-{(day if day is not None else (pid % 20) + 1):02d}T04:00:00+00:00",
         "closed_at": f"2026-10-{(day if day is not None else (pid % 20) + 1):02d}T09:30:00+00:00",
         "exit_reason": "eod_squareoff", "decision_label": label, "source_tab": tab, "tier_name": "T1", "profile_version": "v1"}
    d.update(kw)
    return d


def series(n, mean, sd, seed=1):
    rnd = random.Random(seed)
    return [rec(i + 1, rnd.gauss(mean, sd)) for i in range(n)]


def test_net_return_pct_reads_money_in_and_never_raises():
    assert er.net_return_pct({"net_pnl": 50, "entry_price": 100, "quantity": 10}) == 5.0
    assert er.net_return_pct({"net_pnl": 50, "entry_price": 0, "quantity": 10}) is None
    assert er.net_return_pct({"net_pnl": None, "entry_price": 100, "quantity": 10}) is None
    assert er.net_return_pct({"net_pnl": "x"}) is None and er.net_return_pct({}) is None


def test_bounds_straddle_the_mean_and_need_two_trades():
    lo, hi = er.lower_upper_bound([1.0, 2.0, 3.0, 2.0, 2.0], ["a", "a", "b", "b", "c"])
    assert lo < 2.0 < hi
    assert er.lower_upper_bound([1.0], ["a"]) == (None, None)
    # 5+ session days: the error is taken across day means
    vals = [1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0, 3.0]
    days = ["d1", "d1", "d2", "d2", "d3", "d3", "d4", "d4", "d5", "d5"]
    lo2, hi2 = er.lower_upper_bound(vals, days)
    assert abs((lo2 + hi2) / 2 - 2.0) < 1e-9 and hi2 - lo2 < 1e-9   # every day averages 2.0: no day-to-day spread


def test_sharpe_moments_edge_cases():
    assert er.sharpe_moments([1.0, 1.0]) is None            # too few
    assert er.sharpe_moments([2.0, 2.0, 2.0, 2.0]) is None    # no spread
    m = er.sharpe_moments([1.0, 2.0, 3.0, 4.0, 5.0])
    assert m and m["sr"] > 0 and abs(m["skew"]) < 1e-9


def test_dsr_high_for_a_real_edge_and_lower_with_more_trials():
    vals = [er.net_return_pct(r) for r in series(120, 0.8, 1.5, seed=3)]
    p1 = er.deflated_sharpe_prob(vals, 1)
    p50 = er.deflated_sharpe_prob(vals, 50)
    p5000 = er.deflated_sharpe_prob(vals, 5000)
    assert p1 is not None and p1 > 0.95
    assert p50 <= p1 and p5000 <= p50                        # more variants tried => less credit
    assert p5000 < p1


def test_dsr_about_half_or_less_for_pure_noise_and_none_when_unreadable():
    vals = [er.net_return_pct(r) for r in series(120, 0.0, 1.5, seed=5)]
    p = er.deflated_sharpe_prob(vals, 1)
    assert 0.02 < p < 0.98 and p < 0.95
    assert er.deflated_sharpe_prob([1.0, 2.0], 1) is None
    assert er.deflated_sharpe_prob([], 3) is None


def test_verdict_states():
    assert er.verdict(10, 0.5, 1.0, 0.99, 100.0) == "NOT_ENOUGH_DATA"
    assert er.verdict(60, None, None, None, 0.0) == "NOT_ENOUGH_DATA"
    assert er.verdict(60, -2.0, -0.1, 0.01, -500.0) == "NEGATIVE"
    assert er.verdict(60, 0.2, 1.0, 0.97, 500.0) == "POSITIVE"
    assert er.verdict(60, -0.1, 1.0, 0.97, 500.0) == "NOT_PROVEN"      # lower bound not above zero
    assert er.verdict(60, 0.2, 1.0, 0.80, 500.0) == "NOT_PROVEN"       # deflated probability too low
    assert er.verdict(60, 0.2, 1.0, 0.97, -5.0) == "NOT_PROVEN"        # net not positive


def test_group_stats_numbers():
    rows = [rec(1, 100.0), rec(2, -50.0), rec(3, 30.0), rec(4, -20.0)]
    g = er.group_stats(rows, 1)
    assert g["n"] == 4 and g["net"] == 60.0 and g["win_pct"] == 50.0
    assert g["profit_factor"] == round(130.0 / 70.0, 2)
    assert g["charges"] == 20.0 and g["net_if_charges_x2"] == 40.0 and g["survives_2x_charges"] is True
    assert g["verdict"] == "NOT_ENOUGH_DATA"                  # only 4 trades
    only_win = er.group_stats([rec(1, 10.0)], 1)
    assert only_win["profit_factor"] == 999.0


def test_group_stats_survives_garbage_rows():
    g = er.group_stats([{"net_pnl": "x"}, {}, {"net_pnl": None, "gross_pnl": "?", "charges": None}], 1)
    assert g["n"] == 3 and g["n_ret"] == 0 and g["verdict"] == "NOT_ENOUGH_DATA"


def test_report_flags_a_clear_winner_a_clear_loser_and_thin_groups():
    rows = []
    rnd = random.Random(11)
    pid = 1
    for _ in range(80):                                        # strong group
        rows.append(rec(pid, rnd.gauss(12.0, 15.0), label="GOOD")); pid += 1
    for _ in range(80):                                        # clearly losing group
        rows.append(rec(pid, rnd.gauss(-12.0, 15.0), label="BAD")); pid += 1
    for _ in range(5):                                         # thin
        rows.append(rec(pid, 100.0, label="THIN")); pid += 1
    rep = er.build_report(rows, n_trials=1)
    lab = rep["groups"]["decision_label"]
    assert lab["GOOD"]["verdict"] == "POSITIVE" and lab["BAD"]["verdict"] == "NEGATIVE" and lab["THIN"]["verdict"] == "NOT_ENOUGH_DATA"
    assert rep["weakest"][0]["key"] == "BAD"
    assert rep["verdict_counts"]["POSITIVE"] >= 1 and "book" in rep and rep["book"]["n"] == 165
    json.dumps(rep)                                            # JSON-safe
    txt = er.render(rep)
    assert "EVIDENCE REPORT" in txt and "BAD" in txt and "never changes a switch" in txt


def test_many_trials_can_take_a_positive_back_to_not_proven():
    rnd = random.Random(21)
    rows = [rec(i + 1, rnd.gauss(2.0, 12.0)) for i in range(70)]
    one = er.group_stats(rows, 1)
    many = er.group_stats(rows, 100000)
    assert many["dsr_prob"] <= one["dsr_prob"]
    assert many["trials_used"] == 100000 and one["trials_used"] == 1
    assert many["verdict"] in ("NOT_PROVEN", "NEGATIVE", "NOT_ENOUGH_DATA")


def test_trials_from_file(tmp_path):
    p = tmp_path / "trials.json"
    p.write_text(json.dumps({"runs": 3, "ideas": 7, "dropped": 2, "log": []}))
    assert er.trials_from_file(str(p)) == 8
    assert er.trials_from_file(str(tmp_path / "missing.json")) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert er.trials_from_file(str(bad)) == 1


def test_main_writes_json_and_exit_codes(tmp_path):
    f = tmp_path / "rt_latest.json"
    f.write_text(json.dumps({"records": series(40, 1.0, 5.0, seed=2)}))
    out = tmp_path / "sub" / "evidence_report.json"
    assert er.main([str(f), "--json", str(out), "--trials", "5"]) == 0
    doc = json.loads(out.read_text())
    assert doc["trials"] == 5 and doc["book"]["n"] == 40 and not os.path.exists(str(out) + ".tmp")
    assert er.main([str(f), "--since", "2030-01-01"]) == 1           # nothing that recent
    assert er.main([str(tmp_path / "nope.json")]) == 1               # unreadable -> no trades
    # DEMO rows are ignored by the default REAL filter
    d = tmp_path / "demo.json"
    d.write_text(json.dumps({"records": [rec(1, 10.0, mode="DEMO")]}))
    assert er.main([str(d)]) == 1


def test_group_by_extra_field(tmp_path):
    f = tmp_path / "rt.json"
    rows = series(40, 1.0, 5.0, seed=8)
    for i, r in enumerate(rows):
        r["exit_reason"] = "stop_hit" if i % 2 else "target_hit"
    f.write_text(json.dumps({"records": rows}))
    out = tmp_path / "o.json"
    assert er.main([str(f), "--group-by", "exit_reason", "--json", str(out)]) == 0
    assert set(json.loads(out.read_text())["groups"]["exit_reason"]) == {"stop_hit", "target_hit"}


def test_composite_key_and_default_strategy_groups():
    f = er._key_fn("source_tab+decision_label")
    assert f({"source_tab": "swing", "decision_label": "BUY NOW"}) == "swing | BUY NOW"
    assert f({"source_tab": "swing"}) == "swing | none" and f({}) == "none | none"
    assert er._key_fn("tier_name")({"tier_name": "T1"}) == "T1"
    recs = [rec(i + 1, 5.0, tab=("swing" if i % 2 else "volume_shock"), label="BUY NOW") for i in range(8)]
    rep = er.build_report(recs, n_trials=1, min_n=3)
    assert set(rep["groups"]["strategy"]) == {"swing | BUY NOW", "volume_shock | BUY NOW"}
    assert sum(c["n"] for c in rep["groups"]["strategy"].values()) == 8
    assert set(rep["groups"]["strategy_by_profile"]) == {"swing | BUY NOW | v1", "volume_shock | BUY NOW | v1"}
