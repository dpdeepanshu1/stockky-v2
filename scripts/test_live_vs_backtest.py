"""Tests for scripts/live_vs_backtest.py (B17). Run: python3 scripts/test_live_vs_backtest.py  or  python3 -m pytest scripts/test_live_vs_backtest.py -q"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_vs_backtest as lv  # noqa: E402

TXT = {"chase": "chase 2.1 ATR above the nearest demand/imbalance zone (max 1.5)", "stretch": "price stretched 3.4 ATR above EMA20 (max 3)",
       "room": "only 0.2 ATR of room to the next resistance (min 0.5)", "rr": "structure R:R 0.90 below 1.2", "stop_wide": "structural stop is too wide",
       "bearish": "daily structure is bearish"}


def _row(i, ret, verdict="passed", reasons=None, h=10):
    return {"symbol": f"S{i}", "obs_day": f"2026-09-{1 + i % 25:02d}", "verdict": verdict, "reasons": reasons or [], f"ret_{h}d": ret}


def _ab(gap_rr=1.5, rej_n=100, cand=1.0, horizon=10):
    by = {r: {"rejected": {"n": rej_n, "mean_net": cand - gap_rr}} for r in lv.RULES}
    return {"horizon": horizon, "candidate": {"mean_net": cand}, "rejected": {"n": rej_n, "mean_net": cand - gap_rr}, "by_rule": by}


def _live(pass_mean, rej_mean, n=120, rule="rr", seed=1, sd=0.8):
    rng = random.Random(seed)
    rows = [_row(i, rng.gauss(pass_mean, sd)) for i in range(n)]
    rows += [_row(i + n, rng.gauss(rej_mean, sd), "rejected_enforced", [TXT[rule]]) for i in range(n)]
    return rows


def test_reason_text_maps_to_rule_ids():
    for rid, txt in TXT.items():
        assert lv.rules_of([txt]) == [rid], (rid, lv.rules_of([txt]))
    assert lv.rules_of(["fresh bearish break of structure 3 bar(s) ago"]) == ["bearish"]
    assert lv.rules_of(["something new"]) == [] and lv.rules_of(None) == [] and lv.rules_of([5]) == []
    assert sorted(lv.rules_of([TXT["rr"], TXT["room"]])) == ["room", "rr"]


def test_load_live_accepts_report_list_and_observations():
    rows = [_row(1, 1.0)]
    assert lv.load_live({"recent": rows, "stats": {}}) == rows and lv.load_live(rows) == rows and lv.load_live({"observations": rows}) == rows
    assert lv.load_live({"error": "x"}) == [] and lv.load_live(None) == []


def test_confirmed_when_live_gap_matches_backtest():
    rep = lv.build(_live(1.0, -0.5), _ab(1.2), 30, 300)           # live gap ~1.3, interval about 1.1-1.45
    e = rep["rules"]["rr"]
    assert e["status"] == "confirmed" and abs(e["live"]["gap"] - 1.3) < 0.2, e
    assert rep["gate"]["status"] == "confirmed"


def test_weaker_than_backtest():
    e = lv.build(_live(1.0, 0.7), _ab(1.5), 30, 300)["rules"]["rr"]        # live gap ~0.3, interval entirely under 1.5 and above 0
    assert e["status"] == "weaker_than_backtest", e


def test_reversed_when_rule_rejected_better_stocks():
    e = lv.build(_live(0.0, 1.5), _ab(1.5), 30, 300)["rules"]["rr"]
    assert e["status"] == "reversed" and e["live"]["gap"] < 0, e


def test_no_backtest_edge_and_untouched_rules_are_insufficient():
    rep = lv.build(_live(1.0, -0.5), _ab(-0.4), 30, 300)
    assert rep["rules"]["rr"]["status"] == "no_backtest_edge"
    assert rep["rules"]["chase"]["status"] == "insufficient_live" and rep["rules"]["chase"]["live"]["tripped_n"] == 0


def test_floor_is_15_even_with_min_n_1_and_boundary():
    few = _live(1.0, -0.5, n=14)
    assert lv.build(few, _ab(1.5), 1, 300)["rules"]["rr"]["status"] == "insufficient_live"
    ok = _live(1.0, -0.5, n=15)
    assert lv.build(ok, _ab(1.5), 1, 300)["rules"]["rr"]["status"] != "insufficient_live"
    assert lv.build(_live(1.0, -0.5), _ab(1.5, rej_n=14), 30, 300)["rules"]["rr"]["status"] == "insufficient_live"   # backtest side too thin


def test_unfinished_rows_ignored_and_unmapped_counted():
    rows = _live(1.0, -0.5)
    rows.append(_row(900, None))
    rows.append(_row(901, 1.0, "rejected_enforced", ["brand new reason"]))
    rep = lv.build(rows, _ab(1.5), 30, 100)
    assert rep["unmapped"] == 1 and rep["rules"]["rr"]["live"]["passed_n"] == 120


def test_horizon_follows_the_backtest_report():
    rows = _live(1.0, -0.5)
    for r in rows:
        r["ret_5d"] = r.pop("ret_10d")
    assert lv.build(rows, _ab(1.2, horizon=5), 30, 100)["rules"]["rr"]["status"] == "confirmed"
    assert lv.build(rows, _ab(1.5, horizon=10), 30, 100)["rules"]["rr"]["status"] == "insufficient_live"


def test_deterministic_and_empty_safe():
    r = _live(1.0, -0.5)
    assert lv.build(r, _ab(), 30, 200) == lv.build(r, _ab(), 30, 200)
    rep = lv.build([], {}, 30, 100)
    assert rep["gate"]["status"] == "insufficient_live" and "Live vs backtest" in lv.to_markdown(rep)


def test_cli_roundtrip_and_bad_input():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "live.json").write_text(json.dumps({"mode": "REAL", "recent": _live(1.0, -0.5)}))
        (t / "ab.json").write_text(json.dumps(_ab(1.2)))
        assert lv.main(["--live", str(t / "live.json"), "--ab", str(t / "ab.json"), "--boot", "100"]) == 0
        rep = json.loads((t / "lvb_out" / "live_vs_backtest.json").read_text())
        assert rep["rules"]["rr"]["status"] == "confirmed" and (t / "lvb_out" / "live_vs_backtest.md").exists()
        assert lv.main(["--live", str(t / "nope.json"), "--ab", str(t / "ab.json")]) == 2


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
