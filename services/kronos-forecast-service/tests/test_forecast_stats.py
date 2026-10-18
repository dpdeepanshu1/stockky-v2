"""forecast_stats + engine: pure, no torch. A fake forecaster stands in for Kronos."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

import engine
import forecast_stats as fs


def _candles(n=150, start=date(2026, 1, 1), px=100.0, drift=0.001):
    out, d, p = [], start, px
    while len(out) < n:
        if d.weekday() < 5:
            p *= 1 + drift
            out.append({"date": d.isoformat(), "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000 + len(out)})
        d += timedelta(days=1)
    return out


def _bad(fn, *a, **k):
    try:
        fn(*a, **k)
    except fs.BadHistory:
        return True
    return False


def _flat(n=20, h=3, level=100.0, hi=100.5, lo=99.5):
    return np.full((n, h), level), np.full((n, h), hi), np.full((n, h), lo)


def test_clean_drops_forming_bar_and_limits_lookback():
    c = _candles(150)
    last = date.fromisoformat(c[-1]["date"])
    df = fs.clean_history(c, today=last, lookback=100)          # the bar dated `today` is still forming
    assert len(df) == 100 and df["date"].iloc[-1] < pd.Timestamp(last)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]


def test_clean_rejects_short_nan_nonpositive_and_jump():
    assert _bad(fs.clean_history, _candles(30))
    assert _bad(fs.clean_history, [])
    c = _candles(100); c[50]["close"] = -1.0
    assert _bad(fs.clean_history, c)
    c = _candles(100); c[60]["close"] = c[60]["close"] * 2.0     # unadjusted split-like jump
    assert _bad(fs.clean_history, c)
    c = _candles(100); del c[0]["open"]
    assert _bad(fs.clean_history, c)


def test_flat_paths_have_no_edge():
    c, h, l = _flat()
    s = fs.summarize_paths(c, h, l, 100.0, 100.0, 3.0, 6.0)
    assert s["p_target_first"] == 0.0 and s["p_stop_first"] == 0.0 and s["p_neither"] == 1.0
    assert abs(s["ev_pct"]) < 1e-9 and s["p_up"] == 0.0


def test_target_hit_gives_target_ev():
    c, h, l = _flat()
    h = h.copy(); h[:, 1] = 107.0
    s = fs.summarize_paths(c, h, l, 100.0, 100.0, 3.0, 6.0)
    assert s["p_target_first"] == 1.0 and abs(s["ev_pct"] - 6.0) < 1e-9


def test_bar_holding_both_levels_counts_stop_first():
    c, h, l = _flat()
    h = h.copy(); l = l.copy(); h[:, 0] = 107.0; l[:, 0] = 96.0
    s = fs.summarize_paths(c, h, l, 100.0, 100.0, 3.0, 6.0)
    assert s["p_stop_first"] == 1.0 and s["p_target_first"] == 0.0 and abs(s["ev_pct"] + 3.0) < 1e-9


def test_rebased_on_live_price():
    c, h, l = _flat()
    h = h.copy(); h[:, 0] = 107.0                 # +7% on the last close of 100
    s = fs.summarize_paths(c, h, l, 100.0, 110.0, 3.0, 6.0)   # live 110: path is scaled x1.1 -> 117.7 >= target 116.6
    assert s["p_target_first"] == 1.0


def test_mixed_paths_probabilities_sum_to_one():
    rng = np.random.default_rng(1)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.02, (200, 5)), axis=1)
    h, l = c * 1.01, c * 0.99
    s = fs.summarize_paths(c, h, l, 100.0, 100.0, 3.0, 6.0)
    assert abs(s["p_target_first"] + s["p_stop_first"] + s["p_neither"] - 1.0) < 1e-9
    assert s["q10_ret_pct"] <= s["q50_ret_pct"] <= s["q90_ret_pct"] and s["n_paths"] == 200


def test_bad_inputs_raise():
    c, h, l = _flat()
    for args in ((c, h, l, 100.0, 100.0, 0.0, 6.0), (c, h, l, 0.0, 100.0, 3.0, 6.0)):
        try:
            fs.summarize_paths(*args)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    c2 = c.copy(); c2[0, 0] = np.nan
    try:
        fs.summarize_paths(c2, h, l, 100.0, 100.0, 3.0, 6.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for NaN")


class _Fake:
    """Deterministic stand-in: every path = last close x (1 + drift), seeded by the key. Records what it was given."""
    def __init__(self, drift=0.02):
        self.drift, self.seen = drift, []

    def sample_paths(self, df, future_ts, n_paths, seed_key):
        self.seen.append((len(df), df["date"].iloc[-1], len(future_ts), n_paths, seed_key))
        last = float(df["close"].iloc[-1])
        h = len(future_ts)
        c = np.tile(last * (1 + self.drift * np.arange(1, h + 1) / h), (n_paths, 1))
        return c, c * 1.002, c * 0.998


def test_engine_end_to_end_and_params_clamped():
    c = _candles(150)
    last_close = c[-1]["close"]
    f = _Fake(0.04)
    out = engine.run_forecast(f, c, last_close, 3.0, 6.0, horizon=5, n_paths=10, today=date(2027, 1, 1), symbol="abc")
    assert out["symbol"] == "ABC" and out["n_paths"] == 10 and out["horizon"] == 5 and out["bars_used"] == 128
    assert out["p_up"] == 1.0 and out["ev_pct"] > 0
    assert f.seen[0][2] == 5 and f.seen[0][3] == 10
    engine.run_forecast(f, c, last_close, 3.0, 6.0, horizon=999, n_paths=999, today=date(2027, 1, 1), symbol="abc")
    assert f.seen[-1][2] == 20 and f.seen[-1][3] == 50


def test_engine_rejects_ref_price_far_from_last_close():
    c = _candles(150)
    try:
        engine.run_forecast(_Fake(), c, c[-1]["close"] * 2.0, 3.0, 6.0, today=date(2027, 1, 1), symbol="X")
    except fs.BadHistory:
        return
    raise AssertionError("expected BadHistory")


def test_past_forecast_does_not_change_when_later_bars_are_added():
    """No look-ahead: the forecast for decision day D must be identical whether or not the candle list already holds bars after D."""
    full = _candles(190)
    cut = 140
    d = date.fromisoformat(full[cut]["date"])               # decision day: its own bar is still forming
    f1, f2 = _Fake(0.01), _Fake(0.01)
    px = full[cut - 1]["close"]
    a = engine.run_forecast(f1, full[:cut + 1], px, 3.0, 6.0, today=d, symbol="Z")
    b = engine.run_forecast(f2, full, px, 3.0, 6.0, today=d, symbol="Z")
    assert a == b and f1.seen == f2.seen
