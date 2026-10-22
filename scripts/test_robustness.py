"""Tests for scripts/robustness.py (B14). Run: python3 scripts/test_robustness.py  or  python3 -m pytest scripts/test_robustness.py -q"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ms_ab_report as ab  # noqa: E402
import robustness as rb  # noqa: E402

GOOD = {"ms_chase_atr": 0.5, "ms_stretch_atr": 1.0, "ms_room_atr": 2.0, "ms_rr": 2.0, "ms_rr_flags": [], "ms_trend": "bullish"}


def _row(i, ret, rr=2.0, chase=0.5, month=None, with_s=True):
    s = dict(GOOD, ms_rr=rr, ms_chase_atr=chase)
    r = {"sym": f"S{i % 7}", "ts": 1000.0 + i, "month": month or f"2025-{1 + i % 6:02d}", "rej": [], "r10": ret, "x10": None, "mae": -1.0,
         "stop_first": False, "target_first": False, "bull": None, "hv": None}
    if with_s:
        r["s"] = s
    return r


def _edgy(n=300, seed=1):
    """Rows where low-R:R days (the ones the gate rejects) lose and the rest win: a real, stable edge."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        bad = i % 4 == 0
        rows.append(_row(i, rng.gauss(-1.0 if bad else 1.0, 0.5), rr=0.6 if bad else 2.0))
        if bad:
            rows[-1]["rej"] = ["rr"]
    return rows


def _noise(n=300, seed=2):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        bad = i % 4 == 0
        rows.append(_row(i, rng.gauss(0.0, 2.0), rr=0.6 if bad else 2.0))
        if bad:
            rows[-1]["rej"] = ["rr"]
    return rows


def test_random_entry_detects_real_edge_and_not_noise():
    rows = _edgy()
    cand = [r for r in rows if not r["rej"]]
    good = rb.random_entry(rows, cand, 10, 400, 7)
    assert good["p_random"] < 0.01 and good["candidate_mean"] > good["random_p95"], good
    rows = _noise()
    cand = [r for r in rows if not r["rej"]]
    bad = rb.random_entry(rows, cand, 10, 400, 7)
    assert bad["p_random"] > 0.05, bad


def test_random_entry_is_deterministic_and_p_never_zero():
    rows = _edgy()
    cand = [r for r in rows if not r["rej"]]
    a, b = rb.random_entry(rows, cand, 10, 200, 7), rb.random_entry(rows, cand, 10, 200, 7)
    assert a == b and a["p_random"] > 0


def test_random_entry_preserves_month_mix():
    rows = [_row(i, 1.0, month="2025-01") for i in range(10)] + [_row(i + 10, -1.0, month="2025-02") for i in range(10)]
    cand = [r for r in rows if r["month"] == "2025-01"]       # all January: a draw must also be all January
    out = rb.random_entry(rows, cand, 10, 50, 3)
    assert out["random_mean"] == 1.0 and out["candidate_mean"] == 1.0


def test_random_entry_empty_never_raises():
    assert rb.random_entry([], [], 10, 100)["p_random"] is None


def test_shuffle_final_equity_is_order_free_and_drawdown_is_not():
    rets = [5.0, -5.0, 5.0, -5.0, 5.0, -5.0, 5.0, -5.0, -5.0, -5.0, 5.0, 5.0]
    rows = [{"ts": float(i), "r10": r} for i, r in enumerate(rets)]
    out = rb.trade_shuffle(rows, 10, 300, 50.0, 7)
    eq = 1.0
    for r in rets:
        eq *= 1 + 0.5 * r / 100
    assert abs(out["final_return_pct"] - round((eq - 1) * 100, 2)) < 0.01
    assert out["shuffle_dd_p95_pct"] >= out["shuffle_dd_median_pct"] > 0
    assert out["n_trades"] == 12 and 0 <= out["real_dd_percentile"] <= 100


def test_shuffle_all_winners_has_no_drawdown_and_empty_is_safe():
    rows = [{"ts": float(i), "r10": 1.0} for i in range(20)]
    out = rb.trade_shuffle(rows, 10, 50, 10.0, 7)
    assert out["real_max_dd_pct"] == 0 and out["shuffle_dd_p95_pct"] == 0 and out["p_dd_over_limit"] == 0
    assert rb.trade_shuffle([], 10, 50, 10.0)["n_trades"] == 0


def test_shuffle_samples_when_too_many_trades():
    rows = [{"ts": float(i), "r10": 0.1} for i in range(rb.MAX_SHUFFLE_TRADES + 50)]
    out = rb.trade_shuffle(rows, 10, 5, 10.0, 7)
    assert out["sampled"] is True and out["n_trades"] == rb.MAX_SHUFFLE_TRADES


def test_position_size_is_capped_at_100_percent():
    rows = [{"ts": 0.0, "r10": -50.0}, {"ts": 1.0, "r10": 10.0}]
    out = rb.trade_shuffle(rows, 10, 10, 500.0, 7)         # 500 % clamps to 100 %: lose half, then gain 10 %
    assert abs(out["final_return_pct"] - (-45.0)) < 0.01


def test_sweep_nudges_each_threshold_and_directions_are_right():
    rows = _edgy()
    sw = rb.sweep(rows, 10, 20.0)
    names = [v["name"] for v in sw["variants"]]
    assert "rr -20%" in names and "rr +20%" in names and "all tighter 20%" in names and "all looser 20%" in names
    v = {x["name"]: x for x in sw["variants"]}
    assert abs(v["rr -20%"]["value"] - 0.96) < 1e-9 and abs(v["rr +20%"]["value"] - 1.44) < 1e-9
    assert v["all tighter 20%"]["n"] <= v["all looser 20%"]["n"]    # tighter gate keeps fewer or equal signals
    assert sw["status"] == "stable", sw


def test_sweep_flags_a_fragile_edge():
    """Losers sit at rr 1.1 (rejected by the 1.2 floor), winners at rr 1.3: lifting the floor 20 % to 1.44 rejects the winners too and the edge turns negative."""
    rows = []
    for i in range(200):
        k = i % 3
        rr, ret = ((1.1, -2.0), (1.3, 2.0), (3.0, 0.0))[k]
        rows.append(_row(i, ret, rr=rr))
        rows[-1]["rej"] = ["rr"] if rr < 1.2 else []
    sw = rb.sweep(rows, 10, 20.0)
    assert sw["base"]["edge_pp"] > 0 and sw["status"] == "fragile" and "rr +20%" in sw["why"], sw


def test_sweep_needs_summaries_and_floor():
    assert rb.sweep([_row(i, 1.0, with_s=False) for i in range(50)], 10, 20.0)["status"] == "insufficient_sample"
    assert rb.sweep([_row(i, 1.0) for i in range(10)], 10, 20.0)["status"] == "insufficient_sample"


def test_sweep_never_calls_a_gate_without_edge_stable():
    rows = [_row(i, -1.0 if i % 4 else 1.0, rr=0.6 if i % 4 == 0 else 2.0) for i in range(200)]   # the rejected days are the winners
    assert rb.sweep(rows, 10, 20.0)["status"] == "no_edge_to_test"


def test_verdict_robust_not_robust_and_floor():
    assert rb.build(_edgy(), 10, 300, 20.0, 10.0, 7, {})["verdict"] == "robust"
    assert rb.build(_noise(), 10, 300, 20.0, 10.0, 7, {})["verdict"] == "not_robust"
    few = _edgy(n=40)                                             # 30 candidate rows -> fine; cut to under the floor below
    tiny = [r for r in few if not r["rej"]][:14] + [r for r in few if r["rej"]][:3]
    assert rb.build(tiny, 10, 100, 20.0, 10.0, 7, {})["verdict"] == "insufficient_sample"
    assert rb.build([], 10, 100, 20.0, 10.0, 7, {})["verdict"] == "no_data"


def test_markdown_renders_all_sections_even_when_empty():
    for rows in (_edgy(), []):
        md = rb.to_markdown(rb.build(rows, 10, 50, 20.0, 10.0, 7, {}))
        assert "Random-entry benchmark" in md and "Trade-order shuffle" in md and "Parameter sweep" in md and "Verdict" in md


def test_ab_rows_keep_summary_only_on_request():
    import test_ms_ab_report as tm
    orig = ab.replay
    ab.replay = tm._fake_replay(tm.GOOD)
    try:
        plain = ab._rows(tm._bars(200), "X", 1, {}, 0.3, 0.1, None)[0]
        kept = ab._rows(tm._bars(200), "X", 1, {"_keep_summary": True}, 0.3, 0.1, None)[0]
    finally:
        ab.replay = orig
    assert "s" not in plain and kept["s"]["ms_rr"] == 2.0 and set(kept["s"]) == set(ab.SUMMARY_KEYS)
    assert plain["rej"] == kept["rej"]                             # keeping the summary never changes which rules trip


def test_end_to_end_cli_with_replayed_signals():
    """main() -> replay rows (stubbed engine, real pricing / outcome code) -> report files. 3 symbols x 30 signal days, every 4th day trips the R:R rule."""
    import test_ms_ab_report as tm

    def rp(bars, step=1, **kw):
        out = []
        for n, i in enumerate(range(125, 155)):
            bad = n % 4 == 0
            out.append({"i": i, "ts": bars[i][0], "price": bars[i][4], "quality": "ok", "summary": dict(tm.GOOD, ms_rr=0.6 if bad else 2.0)})
        return out

    with tempfile.TemporaryDirectory() as td:
        for name in ("AAA", "BBB", "CCC"):
            cs = [{"date": f"{2021 + k // 300}-{1 + (k % 300) // 25:02d}-{1 + k % 25:02d}", "open": 100 + k * 0.1, "high": 110 + k * 0.1, "low": 98 + k * 0.1, "close": 100 + k * 0.1, "volume": 1000} for k in range(200)]
            (Path(td) / f"{name}.json").write_text(json.dumps({"candles": cs}))
        out = Path(td) / "o"
        orig = ab.replay
        ab.replay = rp
        try:
            rc = rb.main([td, "--out", str(out), "--perms", "50"])
        finally:
            ab.replay = orig
        rep = json.loads((out / "robustness.json").read_text())
        assert rc == 0 and rep["signals"]["baseline"] == 90 and rep["signals"]["candidate"] == 66, rep["signals"]
        assert rep["sweep"]["base"]["n"] == 66 and rep["verdict"] in ("robust", "not_robust")
        assert (out / "robustness.md").exists()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
