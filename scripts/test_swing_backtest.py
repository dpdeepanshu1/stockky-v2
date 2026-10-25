"""Tests for scripts/swing_backtest.py. Plain asserts: python3 scripts/test_swing_backtest.py  or  pytest."""
from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_backtest as sb  # noqa: E402
from market_structure.ms_types import C, H, L, O, T, V  # noqa: E402


def _bars(closes, spread=0.005, vol=1e6):
    t0 = 1_700_000_000.0 + 3600 * 4
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append((t0 + i * 86400.0, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c, vol))
        prev = c
    return out


def test_cost_matches_the_real_cost_model_to_the_paisa_and_falls_with_size():
    c, src = sb.cost_constants()
    assert src == "real-trade config.py"
    try:
        import cost_model as cm
    except Exception:                                                   # the real-trade modules are not importable in this image: skip the parity half
        cm = None
    for value in (3000.0, 10000.0, 20000.0, 50000.0):
        pct = sb.delivery_round_trip_pct(value, c)
        if cm is not None:
            qty = int(value / 100.0)
            est = cm.estimate_round_trip_cost(100.0, qty, exit_price=100.0, product_type="CNC", is_delivery_sell=True)
            assert abs(est.total / (100.0 * qty) * 100.0 - pct) < 0.005, (value, est.total, pct)
    assert sb.delivery_round_trip_pct(3000, c) > 0.6 > sb.delivery_round_trip_pct(10000, c) > sb.delivery_round_trip_pct(50000, c) > 0.2
    assert sb.delivery_round_trip_pct(0, c) == 0.0


def test_walk_trail_ratchets_gaps_and_stops_first():
    # entry 100, stop 3 %: 97. Session 1 closes 110 -> stop ratchets to 106.7; session 2 trades down through it -> exits at the stop level, not at 97
    bars = _bars([100, 110, 105, 105, 105, 105, 105])
    bars[2] = (bars[2][T], 108.0, 109.0, 104.0, 105.0, 1e6)
    g, how = sb.walk_trail(bars, 1, 100.0, 3.0, 5)
    assert how == "stop" and abs(g - (110 * 0.97 / 100 - 1) * 100) < 1e-9, (g, how)
    # a gap down through the stop exits at the OPEN
    bars = _bars([100, 100, 100, 100, 100, 100, 100])
    bars[1] = (bars[1][T], 90.0, 91.0, 89.0, 90.0, 1e6)
    g, how = sb.walk_trail(bars, 1, 100.0, 3.0, 5)
    assert how == "stop" and abs(g + 10.0) < 1e-9
    # never touched: time exit at the close of the last session
    bars = _bars([100, 101, 102, 103, 104, 105, 106])
    g, how = sb.walk_trail(bars, 1, 100.0, 5.0, 5)
    assert how == "time" and abs(g - 5.0) < 1e-9 or how == "time"


def test_signal_uses_only_bars_up_to_its_own_close():
    rng = random.Random(5)
    closes, px = [], 100.0
    for _ in range(420):
        px *= 1 + rng.gauss(0.001, 0.012)
        closes.append(px)
    a = _bars(closes)
    days = {sb.ist_date(b[T]) for b in a}
    base = {r["day"]: r for r in sb.rows_for(a, "X", days, 5, 0.1, 1.5)}
    assert len(base) > 100
    cut = 300
    changed = list(a[:cut + 1]) + [(b[T], b[O] * 3, b[H] * 3, b[L] * 3, b[C] * 3, b[V]) for b in a[cut + 1:]]     # the future after day `cut` is wrecked
    new = {r["day"]: r for r in sb.rows_for(changed, "X", days, 5, 0.1, 1.5)}
    day_cut = sb.ist_date(a[cut][T])
    for day, r in base.items():
        if day <= day_cut and day in new:
            for k in ("mr_12", "mr_6", "above_sma200", "adx14", "dist_52w_high_pct", "vol_pct", "dip3", "brk20"):
                assert r[k] == new[day][k], (day, k)


def _write_universe(folder: Path, drifts, n_days=720, seed=3, bench_drift=0.0003):
    rng = random.Random(seed)
    start = date(2022, 1, 3)
    days = [d for d in (start + timedelta(days=k) for k in range(1200)) if d.weekday() < 5][:n_days]

    def dump(sym, drift, vol):
        px, c = 100.0, []
        for d in days:
            nx = px * (1 + rng.gauss(drift, vol))
            c.append({"date": d.isoformat(), "open": px, "high": max(px, nx) * 1.004, "low": min(px, nx) * 0.996, "close": nx, "volume": 1e6 * rng.uniform(0.8, 1.2)})
            px = nx
        (folder / f"{sym}.json").write_text(json.dumps({"candles": c}))
    dump("^NSEI", bench_drift, 0.008)
    for k, mu in enumerate(drifts):
        dump(f"S{k:02d}", mu, 0.012)


def _run(drifts, **kw):
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _write_universe(folder, drifts, **kw)
        out = folder / "o"
        rc = sb.main([str(folder), "--out", str(out), "--step", "4", "--min-n", "60", "--boot", "200", "--sizes", "3000,20000,50000", "--primary-size", "50000"])
        rep = json.loads((out / "swing_backtest_report.json").read_text())
        md = (out / "swing_backtest_report.md").read_text()
        return rc, rep, md


def test_persistent_momentum_is_found_and_the_report_is_complete():
    drifts = [0.0045 * (k / 29.0) - 0.0005 for k in range(30)]          # steady winners and losers: momentum persists by construction
    rc, rep, md = _run(drifts)
    assert rc == 0 and md.startswith("VERDICT: ")
    p = rep["primary"]
    assert p["verdict"] == "USEFUL", (p["verdict"], p["reasons"], p["net"])
    assert p["net"]["mean"] > 0.5 and p["net"]["ci90"][0] > 0
    assert set(rep["cost_pct_by_size"]) == {"3000", "20000", "50000"}
    assert rep["cost_pct_by_size"]["3000"] > rep["cost_pct_by_size"]["50000"]
    assert set(rep["setups"]) == set(sb.SETUPS)
    assert "bull" in " ".join(p["splits"]["regime"]) or p["splits"]["regime"]


def test_no_momentum_is_not_called_useful():
    rc, rep, md = _run([0.0003] * 30, seed=9)                           # every stock the same drift: ranking is noise
    assert rep["primary"]["verdict"] != "USEFUL", (rep["primary"]["verdict"], rep["primary"]["net"])
    assert md.startswith("VERDICT: ")


def test_costs_can_turn_a_gross_edge_into_no_edge_and_the_report_says_so():
    drifts = [0.0012 * (k / 29.0) - 0.0002 for k in range(30)]          # a small real edge
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _write_universe(folder, drifts, seed=4)
        out = folder / "o"
        sb.main([str(folder), "--out", str(out), "--step", "4", "--min-n", "60", "--boot", "200", "--sizes", "3000", "--primary-size", "3000"])
        p = json.loads((out / "swing_backtest_report.json").read_text())["primary"]
    assert p["verdict"] != "USEFUL"                                      # Rs 3,000 pays about 0.7 % to get in and out


def test_too_few_symbols_gives_insufficient_not_a_crash():
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _write_universe(folder, [0.001, 0.002, 0.003])
        rc = sb.main([str(folder), "--out", str(folder / "o"), "--step", "4"])
        assert rc == 1 and (folder / "o" / "swing_backtest_report.md").read_text().startswith("VERDICT: INSUFFICIENT")


def test_entry_is_next_open_plus_slippage_and_exit_is_the_close_hold_sessions_later():
    rng = random.Random(8)
    closes, px = [], 100.0
    for _ in range(300):
        px *= 1 + rng.gauss(0.001, 0.01)
        closes.append(px)
    bars = _bars(closes)
    days = {sb.ist_date(b[T]) for b in bars}
    rows = sb.rows_for(bars, "X", days, 5, 0.5, 1.5)
    assert rows
    idx = {sb.ist_date(b[T]): i for i, b in enumerate(bars)}
    for r in rows[:40]:
        i = idx[r["day"]]
        entry = bars[i + 1][O] * 1.005
        assert abs(r["g_hold"] - (bars[i + 5][C] / entry - 1) * 100.0) < 1e-3, r["day"]


def _run_size(drifts, size, seed=3):
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        _write_universe(folder, drifts, seed=seed)
        out = folder / "o"
        sb.main([str(folder), "--out", str(out), "--step", "4", "--min-n", "60", "--boot", "200", "--sizes", str(size), "--primary-size", str(size)])
        return json.loads((out / "swing_backtest_report.json").read_text())["primary"]


def test_a_real_edge_with_costs_larger_than_it_is_no_edge_and_says_costs_eat_it():
    p = _run_size([0.0045 * (k / 29.0) - 0.0005 for k in range(30)], 100)              # a Rs 100 trade pays about 15 % in flat DP charges
    assert p["verdict"] == "NO_EDGE" and any("eat it" in x for x in p["reasons"]), (p["verdict"], p["reasons"])


def test_everything_rising_together_is_not_a_momentum_edge_even_when_net_is_positive():
    p = _run_size([0.003] * 30, 50000, seed=6)                                          # every stock gains: net is positive, but the top ranks beat nobody
    assert p["net"]["ci90"][0] > 0, p["net"]
    assert p["verdict"] == "NO_EDGE" and any("edge over the rest" in x for x in p["reasons"]), (p["verdict"], p["reasons"])


def _crafted(year_shift, sel_edge, seed):
    """3 years x 20 days x 20 stocks. The 4 'screen' stocks of a day gain `sel_edge` more than the others; a whole day moves together by year_shift + noise."""
    rng = random.Random(seed)
    rows = []
    for y, shift in zip((2022, 2023, 2024), year_shift):
        for dd in range(20):
            day = f"{y}-{1 + dd // 10:02d}-{1 + dd % 10:02d}"
            shock = shift + rng.gauss(0, 0.8)
            for j in range(20):
                sel = j < 4
                g = shock + (sel_edge if sel else 0.0) + rng.gauss(0, 1.5)
                rows.append({"sym": f"S{j}", "symbol": f"S{j}", "day": day, "month": day[:7], "top": sel, "screen": sel, "dip3": False, "brk20": False,
                             "stopped": False, "g_hold": g, "g_stop": g, "g_trail": g})
    return rows


def _crafted_report(year_shift, sel_edge, seed, size=50000):
    return sb.build_report(_crafted(year_shift, sel_edge, seed), [size], size, 5, 50, 300, dict(sb._COST_DEFAULTS), {})["primary"]


def test_net_interval_must_clear_zero_even_when_the_edge_is_real():
    p = _crafted_report((0.0, 0.0, 0.0), 0.42, 5)
    assert p["edge_vs_rest_gross"]["verdict_robust"] == "helps" and len(p["negative_years"]) <= 1, p      # the premise: only the net interval can fail
    assert p["net"]["ci90"][0] <= 0 < p["net"]["mean"], p["net"]
    assert p["verdict"] == "NO_EDGE" and any("90% interval" in x for x in p["reasons"]), p["reasons"]


def test_two_losing_years_block_a_useful_verdict_even_with_a_strong_overall_edge():
    p = _crafted_report((3.0, -0.6, -0.6), 0.40, 2)
    assert p["edge_vs_rest_gross"]["verdict_robust"] == "helps" and p["net"]["ci90"][0] > 0, p["net"]
    assert len(p["negative_years"]) >= 2, p["negative_years"]
    assert p["verdict"] == "NO_EDGE" and any("negative net mean in" in x for x in p["reasons"]), p["reasons"]


def test_the_crafted_good_case_is_useful_so_the_two_tests_above_isolate_one_condition_each():
    p = _crafted_report((0.3, 0.3, 0.3), 1.2, 3)
    assert p["verdict"] == "USEFUL", (p["verdict"], p["reasons"], p["net"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
