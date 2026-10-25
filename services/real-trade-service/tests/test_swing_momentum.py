"""E130 (swing-hold plan S1): the read-only momentum measures and ranker. Plain asserts: runs under pytest or `python3 tests/test_swing_momentum.py` from the service folder."""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swing import momentum as m  # noqa: E402
from swing import ranker  # noqa: E402


def _bars(closes, spread=0.01, t0=1_700_000_000.0):
    return [(t0 + i * 86400.0, c, c * (1 + spread), c * (1 - spread), c, 1000.0) for i, c in enumerate(closes)]


def _trend(n, daily=0.003, start=100.0):
    return [start * (1 + daily) ** i for i in range(n)]


def test_skip_return_uses_the_skip_month_and_needs_enough_closes():
    c = [100.0] * 300
    c[-22] = 150.0           # the close 21 days ago
    c[-253] = 100.0          # 252 days ago
    c[-1] = 10.0             # today's crash must NOT enter a 12-1 figure
    assert abs(m.skip_return_pct(c, 252) - 50.0) < 1e-9
    assert m.skip_return_pct(c[-252:], 252) is None            # one close short
    assert m.skip_return_pct([100.0] * 10, 126) is None


def test_volatility_and_ratio():
    flat = [100.0] * 300
    assert m.daily_vol_pct(flat) == 0.0
    assert m.daily_vol_pct([100.0] * 30) is None                 # under 60 returns
    s = m.score_symbol("X", _bars(_trend(300)))
    assert s["status"] == "ok" and s["ret_12_1"] > 0 and s["above_sma200"] is True
    assert s["mr_12"] is None or math.isfinite(s["mr_12"])       # smooth trend: tiny volatility, huge ratio, still finite or None


def test_a_flat_series_has_no_volatility_and_is_not_ranked():
    s = m.score_symbol("FLAT", _bars([100.0] * 300))
    assert s["status"] == "insufficient" and s["mr_12"] is None and s["mr_6"] is None


def test_status_levels():
    assert m.score_symbol("A", _bars(_trend(300)))["status"] == "ok"
    lim = m.score_symbol("B", _bars([100 + (i % 7) * 0.5 + i * 0.05 for i in range(200)]))
    assert lim["status"] == "limited" and lim["ret_12_1"] is None and lim["ret_6_1"] is not None
    short = m.score_symbol("C", _bars(_trend(50)))
    assert short["status"] == "insufficient"
    assert m.score_symbol("D", [])["status"] == "insufficient"
    assert m.score_symbol("E", [("bad",)])["status"] == "insufficient"        # never raises


def test_adx_trend_vs_range():
    up = m.adx14(_bars(_trend(120, 0.01)))
    rng = m.adx14(_bars([100 + (3 if i % 2 else -3) for i in range(120)]))
    assert up is not None and rng is not None and up > 40 and rng < 15, (up, rng)
    assert m.adx14(_bars(_trend(20))) is None


def test_distance_from_high_and_sma200():
    c = _trend(260)
    c[-1] = c[-1] * 0.90
    s = m.score_symbol("X", _bars(c, spread=0.0))
    assert -11.0 < s["dist_52w_high_pct"] < -1.0
    down = m.score_symbol("Y", _bars([200 - i * 0.3 for i in range(260)]))
    assert down["above_sma200"] is False and down["sma200_gap_pct"] < 0


def test_relative_strength_vs_benchmark():
    c = _trend(260, 0.004)
    b = _trend(260, 0.001)
    assert m.rs_vs_bench_pct(c, b) > 0
    assert m.rs_vs_bench_pct(c, None) is None and m.rs_vs_bench_pct(c[:50], b) is None


def test_normalised_score_is_the_nse_shape():
    assert m.normalised(0.0) == 1.0 and m.normalised(1.0) == 2.0
    assert abs(m.normalised(-1.0) - 0.5) < 1e-12 and m.normalised(-5.0) > 0


def _universe():
    rows = []
    for i, d in enumerate([0.0035, 0.0030, 0.0022, 0.0015, 0.0008, 0.0002, -0.0004, -0.0010]):
        noisy = [100 * (1 + d) ** k * (1 + 0.002 * (-1) ** k) for k in range(300)]
        rows.append(m.score_symbol(f"S{i}", _bars(noisy)))
    return rows


def test_ranking_orders_strongest_first_and_screen_needs_uptrend():
    ranked = m.rank_scores(_universe(), min_pct_rank=80.0)
    syms = [r["symbol"] for r in ranked]
    assert syms[0] == "S0" and syms[-1] == "S7", syms
    assert ranked[0]["rank"] == 1 and ranked[0]["pct_rank"] == 100.0 and ranked[-1]["pct_rank"] == 0.0
    assert ranked[0]["screen"] is True and all(r["screen"] is False for r in ranked[2:])
    assert ranked[0]["norm_score"] > ranked[1]["norm_score"] > 0


def test_a_downtrending_leader_is_not_screened():
    rows = _universe()
    for r in rows:
        r["above_sma200"] = False
    assert not any(r["screen"] for r in m.rank_scores(rows))


def test_small_universe_is_not_ranked_and_says_so():
    ranked = m.rank_scores(_universe()[:3])
    assert all(r.get("rank") is None for r in ranked)
    assert any("too small" in " ".join(r["reasons"]) for r in ranked)
    assert m.rank_scores([]) == [] and m.rank_scores(None) == []


def test_unrankable_rows_go_last_and_inputs_are_not_mutated():
    rows = _universe() + [m.score_symbol("ZZ", _bars(_trend(30)))]
    snapshot = [dict(r) for r in rows]
    ranked = m.rank_scores(rows)
    assert ranked[-1]["symbol"] == "ZZ" and ranked[-1].get("rank") is None
    assert rows == snapshot


def _rows_for(closes):
    return [{"date": 1_700_000_000 + i * 86400, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000} for i, c in enumerate(closes)]


def test_snapshot_ranks_tolerates_failures_and_reports_asof():
    data = {f"S{i}": _rows_for([100 * (1 + d) ** k for k in range(300)]) for i, d in enumerate([0.003, 0.002, 0.001, 0.0005, -0.001, 0.0025])}

    def fake(sym):
        if sym == "BAD":
            raise RuntimeError("history HTTP 500")
        return data.get(sym, [])
    snap = ranker.build_snapshot(["s0.ns", "S1", "S2", "S3", "S4", "S5", "BAD", "NONE", "S0"], fetch=fake)
    assert snap["read_only"] is True and snap["n_requested"] == 8            # duplicate S0 and the .NS suffix are normalised
    assert snap["n_ok"] == 6 and snap["n_no_data"] == 2
    assert {f["symbol"] for f in snap["failed"]} == {"BAD", "NONE"}
    assert snap["rows"][0]["symbol"] == "S0" and snap["rows"][0]["rank"] == 1
    assert snap["asof_bar_date"] is not None


def test_snapshot_without_symbols_or_data_says_why():
    assert "no symbols" in " ".join(ranker.build_snapshot([], fetch=lambda s: [])["notes"])
    snap = ranker.build_snapshot(["A", "B"], fetch=lambda s: _rows_for(_trend(300)))
    assert any("nothing ranked" in n for n in snap["notes"])


def test_benchmark_failure_only_blanks_the_strength_column():
    def fake(sym):
        if sym == "BENCH":
            raise RuntimeError("down")
        return _rows_for(_trend(300, 0.001 * (1 + len(sym))))
    snap = ranker.build_snapshot(["AA", "BBB", "CCCC", "DDDDD", "EEEEEE"], fetch=fake, benchmark="BENCH")
    assert snap["n_ok"] == 5 and all(r["rs_6m_vs_bench_pct"] is None for r in snap["rows"])
    assert any("benchmark" in n for n in snap["notes"])


def test_the_module_has_no_order_or_write_path():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "swing", "ranker.py")).read()
    src += open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "swing", "momentum.py")).read()
    for banned in ("dhan_client", "place_order", "db.add", "db.commit", "models.", "open(", ".write("):
        assert banned not in src, banned


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
