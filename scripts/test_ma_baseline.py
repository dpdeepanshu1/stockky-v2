"""Tests for scripts/ma_baseline.py (B16). Run: python3 scripts/test_ma_baseline.py  or  python3 -m pytest scripts/test_ma_baseline.py -q"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ma_baseline as mb  # noqa: E402
import ms_ab_report as ab  # noqa: E402
import test_ms_ab_report as tm  # noqa: E402


def _bars_from(closes, op=None):
    return [(tm.T0 + k * tm.DAY, (op[k] if op else c), c * 1.01, c * 0.99, c, 1000.0) for k, c in enumerate(closes)]


def _v_shape(n_down=60, n_up=60, tail=40):
    cl = [100.0 - k * 0.5 for k in range(n_down)] + [70.0 + k * 0.8 for k in range(n_up)] + [118.0] * tail
    return cl


def test_sma_values_and_warmup():
    s = mb.sma([1, 2, 3, 4, 5], 3)
    assert s[:2] == [None, None] and s[2:] == [2.0, 3.0, 4.0]


def test_cross_detected_once_with_next_open_entry_and_cost():
    cl = _v_shape()
    bars = _bars_from(cl)
    rows = mb.cross_rows(bars, "X", 5, 20, 0.30, 0.10)
    assert len(rows) == 1, rows
    i = next(k for k, r in enumerate(bars) if r[mb.T] == rows[0]["ts"])
    entry = bars[i + 1][mb.O] * 1.001
    exp = (bars[i + 1][mb.C] / entry - 1) * 100 - 0.30                  # T+1 close of the entry session
    assert abs(rows[0]["r1"] - exp) < 0.01, (rows[0]["r1"], exp)
    assert rows[0]["rej"] == []


def test_no_cross_in_a_straight_trend_and_bad_params():
    assert mb.cross_rows(_bars_from([100 + k for k in range(200)]), "X", 5, 20, 0.3, 0.1) == []
    assert mb.cross_rows(_bars_from(_v_shape()), "X", 20, 5, 0.3, 0.1) == []        # fast >= slow
    assert mb.cross_rows(_bars_from([100.0] * 30), "X", 5, 20, 0.3, 0.1) == []      # too short


def test_cross_near_the_end_without_20_sessions_is_dropped():
    cl = [100.0 - k * 0.5 for k in range(60)] + [70.0 + k * 1.5 for k in range(25)]   # crosses ~10 bars before the end
    assert mb.cross_rows(_bars_from(cl), "X", 5, 20, 0.3, 0.1) == []


def _r(i, v, month=None):
    return {"sym": f"S{i % 9}", "ts": float(i), "month": month or f"2025-{1 + i % 8:02d}", "rej": [], "r10": v}


def test_verdicts():
    rng = random.Random(3)
    good = [_r(i, rng.gauss(2.0, 0.5)) for i in range(200)]
    dumb = [_r(i, rng.gauss(0.0, 0.5)) for i in range(200)]
    b = mb.boot_diff(good, dumb, 10, 300)
    assert mb.verdict(200, 200, b["ci90"], 2.0) == "beats_dumb"
    b = mb.boot_diff(dumb, good, 10, 300)
    assert mb.verdict(200, 200, b["ci90"], 0.0) == "worse_than_dumb"
    same = [_r(i, rng.gauss(0.0, 2.0)) for i in range(200)]
    b = mb.boot_diff(same, dumb, 10, 300)
    assert mb.verdict(200, 200, b["ci90"], 0.1) == "no_better"


def test_floor_and_empty():
    assert mb.verdict(14, 500, [1.0, 2.0], 3.0) == "insufficient_sample"       # 14 candidate entries never earn a verdict
    assert mb.verdict(500, 14, [1.0, 2.0], 3.0) == "insufficient_sample"
    assert mb.verdict(500, 500, None, 3.0) == "insufficient_sample"
    assert mb.verdict(0, 0, None, None) == "no_data"
    rep = mb.build([], [], 10, 100, {"fast": 20, "slow": 50, "symbols": 0, "skipped": 0, "cost_pct": 0.3, "slip_pct": 0.1})
    assert rep["verdict"] == "no_data" and "Verdict" in mb.to_markdown(rep)


def test_bootstrap_is_deterministic_and_needs_clusters():
    a = [_r(i, 1.0) for i in range(100)]
    b = [_r(i, 0.0) for i in range(100)]
    assert mb.boot_diff(a, b, 10, 100) == mb.boot_diff(a, b, 10, 100)
    assert mb.boot_diff(a[:3], b[:3], 10, 100)["ci90"] is None


def test_end_to_end_cli():
    """3 symbols x one V-shaped path (one cross each) plus stubbed gate signals; the report files are written and counts add up."""
    def rp(bars, step=1, **kw):
        return [{"i": i, "ts": bars[i][0], "price": bars[i][4], "quality": "ok", "summary": dict(tm.GOOD, ms_rr=0.6 if n % 4 == 0 else 2.0)}
                for n, i in enumerate(range(125, 150))]

    with tempfile.TemporaryDirectory() as td:
        cl = _v_shape(60, 60, 60)
        for name in ("AAA", "BBB", "CCC"):
            cs = [{"date": f"{2021 + k // 300}-{1 + (k % 300) // 25:02d}-{1 + k % 25:02d}", "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
                  for k, c in enumerate(cl)]
            (Path(td) / f"{name}.json").write_text(json.dumps({"candles": cs}))
        out = Path(td) / "o"
        orig = ab.replay
        ab.replay = rp
        try:
            rc = mb.main([td, "--out", str(out), "--fast", "5", "--slow", "20", "--boot", "100"])
        finally:
            ab.replay = orig
        rep = json.loads((out / "ma_baseline.json").read_text())
        assert rc == 0 and rep["groups"]["ma_cross"]["n"] == 3 and rep["groups"]["baseline"]["n"] == 75, rep["groups"]
        assert rep["verdict"] == "insufficient_sample" and (out / "ma_baseline.md").exists()
    assert mb.main(["x", "--fast", "30", "--slow", "20"]) == 2


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
