"""D22-D24: triple-barrier labels, calibration / baseline comparison, point-in-time universe, F&O ban + OI features.
Needs numpy + pandas only. Plain asserts; runs with pytest or `python3 tests/test_d_triple_barrier_universe_fo.py`."""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import pandas as pd

_T = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(_T)
sys.path.insert(0, HERE)

import calibration as cal  # noqa: E402
import fo_features as fo  # noqa: E402
import pit_universe as pit  # noqa: E402
import triple_barrier as tb  # noqa: E402
from targets import TargetGenerator  # noqa: E402


def _flat(n=40):
    """Constant bars: close 100, high 101, low 99 -> ATR = 2, so target = entry + 4, stop = entry - 2 with the default 2 / 1 ATR."""
    return pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.0] * n})


def _row(df, i, **kw):
    out = tb.triple_barrier_labels(df, **kw)
    return out.iloc[i]


# ---------------- D22 triple barrier ----------------
def test_target_hit_is_net_of_costs():
    df = _flat()
    df.loc[22, "high"] = 105.0
    r = _row(df, 20)
    assert r["tb_exit"] == "target" and r["tb_label"] == 1 and r["tb_bars"] == 2 and r["tb_end_idx"] == 22
    assert abs(r["tb_net_return"] - (0.04 - 0.004)) < 1e-9 and r["tb_y"] == 1


def test_stop_hit_and_same_bar_tie_goes_to_stop():
    df = _flat()
    df.loc[22, "low"] = 97.0
    r = _row(df, 20)
    assert r["tb_exit"] == "stop" and r["tb_label"] == -1 and abs(r["tb_net_return"] - (-0.02 - 0.004)) < 1e-9 and r["tb_y"] == 0
    df.loc[22, "high"] = 105.0  # both touched on one bar
    assert _row(df, 20)["tb_exit"] == "stop"


def test_timeout_exits_at_close_of_last_bar_and_costs_make_flat_a_loss():
    df = _flat()
    r = _row(df, 20)
    assert r["tb_exit"] == "timeout" and r["tb_label"] == 0 and r["tb_bars"] == 5 and r["tb_end_idx"] == 25
    assert abs(r["tb_net_return"] + 0.004) < 1e-9 and r["tb_y"] == 0


def test_entry_is_next_open_and_entry_bar_counts():
    df = _flat()
    df.loc[21, "open"] = 102.0  # gap up: entry 102, target 106, stop 100
    df.loc[21, "low"] = 99.5    # the entry bar itself dips under the stop after its open
    r = _row(df, 20)
    assert r["tb_exit"] == "stop" and r["tb_bars"] == 1
    assert abs(r["tb_net_return"] - (100.0 / 102.0 - 1 - 0.004)) < 1e-9


def test_unfinished_windows_are_nan_never_labelled():
    df = _flat(40)
    out = tb.triple_barrier_labels(df, horizon=5)
    assert out["tb_y"].iloc[-5:].isna().all() and out["tb_exit"].iloc[-5:].isna().all()
    assert out["tb_y"].iloc[:13].isna().all()  # ATR not warmed up
    assert out["tb_y"].iloc[14:35].notna().all()
    assert len(out) == 40


def test_labels_do_not_look_ahead():
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 1, 120))
    df = pd.DataFrame({"open": c + rng.normal(0, .2, 120), "high": c + 1.5, "low": c - 1.5, "close": c})
    full = tb.triple_barrier_labels(df, horizon=5)
    part = tb.triple_barrier_labels(df.iloc[:80].copy(), horizon=5)
    a, b = full.iloc[:75], part.iloc[:75]  # rows whose whole window sits inside the first 80 bars
    assert a["tb_net_return"].fillna(-9).round(12).tolist() == b["tb_net_return"].fillna(-9).round(12).tolist()
    assert a["tb_exit"].tolist() == b["tb_exit"].tolist()


def test_bad_params_rejected():
    for kw in ({"horizon": 0}, {"up_atr": 0}, {"dn_atr": -1}):
        try:
            tb.triple_barrier_labels(_flat(), **kw)
        except ValueError:
            continue
        raise AssertionError(kw)


def test_target_generator_triple_barrier_types_and_default_unchanged():
    df = _flat()
    df.loc[22, "high"] = 105.0
    t, g = TargetGenerator(target_type="TripleBarrier_Net", forecast_horizon=5).generate(df)
    assert abs(t.iloc[20] - 0.036) < 1e-9 and "tb_end_idx" in g.columns
    t2, _ = TargetGenerator(target_type="TripleBarrier_Class", forecast_horizon=5, barrier_params={"up_atr": 3.0}).generate(df)
    assert t2.iloc[20] == 0  # target now entry + 6, not reached
    t3, g3 = TargetGenerator(target_type="Log_Return", forecast_horizon=5).generate(df)
    assert "tb_y" not in g3.columns and abs(t3.iloc[0]) < 1e-12


def test_purge_overlapping_keeps_only_rows_finished_before_validation():
    pairs = [(0, 3.0), (1, 9.0), (2, float("nan")), (3, 7.0)]
    assert tb.purge_overlapping(pairs, 8) == [0, 3]


# ---------------- D22 calibration / baseline ----------------
def test_brier_and_reliability():
    assert cal.brier([1, 0], [1, 0]) == 0.0 and abs(cal.brier([0.5, 0.5], [1, 0]) - 0.25) < 1e-12
    p = [0.1] * 10 + [0.9] * 10
    y = [0] * 9 + [1] + [1] * 9 + [0]
    rt = cal.reliability_table(p, y, bins=10)
    assert rt["n"] == 20 and abs(rt["ece"]) < 1e-9 and len(rt["bins"]) == 2


def test_isotonic_is_monotone_and_fixes_overconfidence():
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, 2000)
    y = (rng.uniform(0, 1, 2000) < 0.5 + 0.2 * (p - 0.5)).astype(float)  # true hit-rate is flatter than p claims
    iso = cal.IsotonicCalibrator().fit(p, y)
    q = iso.predict(np.linspace(0, 1, 50))
    assert np.all(np.diff(q) >= -1e-12)
    assert cal.reliability_table(iso.predict(p), y)["ece"] < cal.reliability_table(p, y)["ece"]


def test_ma_cross_signals_fire_once_on_the_crossing_bar():
    c = np.r_[np.full(60, 100.0), np.linspace(100, 130, 30)]
    df = pd.DataFrame({"close": c})
    s = cal.ma_cross_signals(df, 5, 20)
    assert s.sum() == 1 and int(np.argmax(s.to_numpy())) >= 60
    trunc = cal.ma_cross_signals(df.iloc[: int(np.argmax(s.to_numpy())) + 1], 5, 20)
    assert trunc.iloc[-1]  # decided with data up to that bar only


def test_compare_to_baseline_verdicts():
    rng = np.random.default_rng(5)
    assert cal.compare_to_baseline([0.01] * 14, [0.0] * 40)["verdict"] == "insufficient_sample"
    good = cal.compare_to_baseline(rng.normal(0.02, 0.01, 60), rng.normal(0.0, 0.01, 60))
    assert good["verdict"] == "beats_dumb" and good["edge_pp"] > 0
    bad = cal.compare_to_baseline(rng.normal(-0.02, 0.01, 60), rng.normal(0.0, 0.01, 60))
    assert bad["verdict"] == "worse_than_dumb"
    x = rng.normal(0.0, 0.02, 40)  # identical samples: zero edge, the interval must straddle 0
    same = cal.compare_to_baseline(x, x.copy())
    assert same["verdict"] == "no_better"


# ---------------- D23 point-in-time universe ----------------
def _panel():
    dates = pd.bdate_range("2024-01-01", periods=100)
    rows = []
    for sym, n in (("GOOD", 100), ("DEAD", 60)):
        for d in dates[:n]:
            rows.append({"date": d, "symbol": sym, "close": 100.0, "volume": 1e6})
    return pd.DataFrame(rows)


def test_membership_uses_only_prior_bars_and_keeps_delisted_symbols():
    p = pit.add_membership(_panel(), min_history=20, adv_window=10, min_adv=1e7)
    dead = p[p["symbol"] == "DEAD"]
    assert int(dead["in_universe"].sum()) == 40  # bars 20..59 of its life, then it stopped
    assert not dead["in_universe"].iloc[:20].any()
    rep = pit.survivorship_report(p)
    assert rep["ended_early_symbols"] == ["DEAD"] and rep["ended_early_rows"] == 40 and 0 < rep["ended_early_share"] < 1
    # changing a FUTURE bar must not change any earlier membership decision
    q = _panel()
    q.loc[(q["symbol"] == "GOOD") & (q["date"] >= q["date"].iloc[80]), "volume"] = 1.0
    p2 = pit.add_membership(q, min_history=20, adv_window=10, min_adv=1e7)
    a = p[(p["symbol"] == "GOOD") & (p["date"] <= q["date"].iloc[80])]["in_universe"].tolist()
    b = p2[(p2["symbol"] == "GOOD") & (p2["date"] <= q["date"].iloc[80])]["in_universe"].tolist()
    assert a == b


def test_membership_top_n_and_liquidity_floor():
    dates = pd.bdate_range("2024-01-01", periods=40)
    rows = [{"date": d, "symbol": s, "close": 100.0, "volume": v} for s, v in (("A", 3e6), ("B", 2e6), ("C", 1e6)) for d in dates]
    p = pit.add_membership(pd.DataFrame(rows), min_history=10, adv_window=5, min_adv=0, top_n=2)
    last = p[p["date"] == p["date"].max()].set_index("symbol")["in_universe"]
    assert last["A"] and last["B"] and not last["C"]
    p = pit.add_membership(pd.DataFrame(rows), min_history=10, adv_window=5, min_adv=1.5e8)
    last = p[p["date"] == p["date"].max()].set_index("symbol")["in_universe"]
    assert last["A"] and last["B"] and not last["C"]


def test_load_archive_reads_csv_gz_with_index_dates_and_name_symbol():
    with tempfile.TemporaryDirectory() as d:
        df = pd.DataFrame({"close": [1.0, 2.0], "volume": [10, 20]}, index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
        df.index.name = "date"
        df.to_csv(os.path.join(d, "TCS_2024_01.csv.gz"), compression="gzip")
        df.assign(close=[5.0, 6.0]).to_csv(os.path.join(d, "RELIANCE_2024_01.csv"))
        p = pit.load_archive(d)
        assert sorted(p["symbol"].unique()) == ["RELIANCE", "TCS"] and len(p) == 4
        assert p["date"].is_monotonic_increasing or p.groupby("symbol")["date"].apply(lambda s: s.is_monotonic_increasing).all()
        assert len(pit.load_archive(os.path.join(d, "nothing"))) == 0


# ---------------- D24 F&O ban + OI ----------------
def test_parse_secban():
    txt = 'Securities in Ban For Trade Date 22-OCT-2026:\n1,RBLBANK\n2,"SAIL"\n\n'
    p = fo.parse_secban(txt, "2026-10-22")
    assert p["symbol"].tolist() == ["RBLBANK", "SAIL"] and (p["date"] == pd.Timestamp("2026-10-22")).all()
    assert len(fo.parse_secban("No Securities in Ban", "2026-10-22")) == 0


def test_ban_flags_are_dated_and_prior_only():
    dates = pd.bdate_range("2024-01-01", periods=30)
    panel = pd.DataFrame([{"date": d, "symbol": s, "close": 1.0} for s in ("X", "Y") for d in dates])
    ban = pd.DataFrame({"date": [dates[10], dates[11]], "symbol": ["X", "X"]})
    out = fo.add_ban_flags(panel, ban)
    x = out[out["symbol"] == "X"].reset_index(drop=True)
    assert x["in_fo_ban"].tolist()[9:13] == [0, 1, 1, 0]
    assert x["ban_prev"].tolist()[10:14] == [0, 1, 1, 0]
    assert x["ban_days_20"].iloc[10] == 0 and x["ban_days_20"].iloc[12] == 2  # this row excluded from its own count
    assert out[out["symbol"] == "Y"]["in_fo_ban"].sum() == 0
    assert fo.add_ban_flags(panel, pd.DataFrame(columns=["date", "symbol"]))["in_fo_ban"].sum() == 0


def test_oi_buildup_codes_and_non_fo_stays_nan():
    dates = pd.bdate_range("2024-01-01", periods=6)
    fut = pd.DataFrame({"date": dates, "symbol": "F", "close": [100, 101, 100, 99, 100, 100], "open_interest": [1000, 1100, 1200, 1100, 1100, 1200]})
    non = pd.DataFrame({"date": dates, "symbol": "N", "close": [10.0] * 6, "open_interest": [np.nan] * 6})
    out = fo.add_oi_features(pd.concat([fut, non], ignore_index=True))
    f = out[out["symbol"] == "F"].reset_index(drop=True)
    assert np.isnan(f["oi_buildup"].iloc[0])
    assert f["oi_buildup"].tolist()[1:] == [2.0, -2.0, -1.0, 0.0, 0.0]  # up+OI up, down+OI up, down+OI down, up+OI flat, flat price
    assert f["oi_long_buildup"].iloc[1] == 1.0 and f["oi_short_buildup"].iloc[2] == 1.0 and f["oi_long_unwinding"].iloc[3] == 1.0
    assert abs(f["oi_chg_1d"].iloc[1] - 0.1) < 1e-12 and abs(f["oi_chg_5d"].iloc[5] - 0.2) < 1e-12
    assert out[out["symbol"] == "N"]["oi_buildup"].isna().all() and out[out["symbol"] == "N"]["oi_chg_1d"].isna().all()
    assert fo.add_oi_features(fut.drop(columns=["open_interest"]))["oi_buildup"].isna().all()


if __name__ == "__main__":
    import traceback

    ok = bad = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); ok += 1
            except Exception:  # noqa: BLE001
                bad += 1
                print("FAIL", name)
                traceback.print_exc()
    print(f"{ok} passed {bad} failed")
    sys.exit(1 if bad else 0)
