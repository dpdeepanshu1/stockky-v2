import math

import pytest

import tsfm_features as tf


def _fan(h=5, last=100.0, spread=2.0):
    z = {"0.1": -1.2816, "0.2": -0.8416, "0.5": 0.0, "0.8": 0.8416, "0.9": 1.2816}
    return {"mean": [last] * h, "quantiles": {k: [last + v * spread * math.sqrt(i + 1) for i in range(h)] for k, v in z.items()}}


def test_derive_basic_shapes_and_signs():
    d = tf.derive(100.0, _fan(), trailing_vol_pct=1.0, stop_pct=3.0, target_pct=6.0, horizon=5)
    assert d["q10_ret_pct"] < d["q50_ret_pct"] < d["q90_ret_pct"]
    assert d["fan_range_pct"] > (d["q90_ret_pct"] - d["q10_ret_pct"]) * 0.99
    assert 0.0 <= d["p_stop_close_max"] <= 1.0 and 0.0 <= d["p_target_close_max"] <= 1.0
    assert d["vol_ratio"] == pytest.approx(d["forecast_vol_pct"] / 1.0, rel=1e-3)


def test_forecast_vol_matches_the_normal_equivalent():
    d = tf.derive(100.0, _fan(spread=2.0), trailing_vol_pct=1.0, horizon=5)
    sigma_h = (2 * 1.2816 * 2.0 * math.sqrt(5)) / (2 * tf.Z90)
    assert d["forecast_vol_pct"] == pytest.approx(sigma_h / math.sqrt(5), rel=1e-3)


def test_wider_fan_means_higher_stop_probability_and_vol_ratio():
    a = tf.derive(100.0, _fan(spread=1.0), trailing_vol_pct=1.0, stop_pct=3.0, horizon=5)
    b = tf.derive(100.0, _fan(spread=3.0), trailing_vol_pct=1.0, stop_pct=3.0, horizon=5)
    assert b["p_stop_close_max"] > a["p_stop_close_max"] and b["vol_ratio"] > a["vol_ratio"]


def test_stop_far_below_the_fan_has_near_zero_probability():
    d = tf.derive(100.0, _fan(spread=0.5), trailing_vol_pct=1.0, stop_pct=20.0, target_pct=20.0, horizon=5)
    assert d["p_stop_close_max"] < 0.01 and d["p_target_close_max"] < 0.01


def test_cdf_is_monotone_and_hits_the_given_quantiles():
    xs, ps = [90.0, 95.0, 100.0, 105.0, 110.0], [0.1, 0.25, 0.5, 0.75, 0.9]
    assert tf.cdf_at(100.0, xs, ps) == pytest.approx(0.5)
    assert tf.cdf_at(92.5, xs, ps) == pytest.approx(0.175)
    vals = [tf.cdf_at(p, xs, ps) for p in range(70, 131, 2)]
    assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:])) and vals[0] == 0.0 and vals[-1] == 1.0


def test_crossing_quantiles_are_sorted_per_step():
    fan = {"quantiles": {"0.1": [101.0, 99.0], "0.5": [100.0, 100.0], "0.9": [99.0, 101.0]}}
    nf = tf.normalise_fan(fan)
    for i in range(2):
        assert nf["q"][0.1][i] <= nf["q"][0.5][i] <= nf["q"][0.9][i]


@pytest.mark.parametrize("fan", [None, {}, {"quantiles": {}}, {"quantiles": {"0.1": [1, 2], "0.5": [1, 2]}},
                                 {"quantiles": {"0.1": [1], "0.5": [1, 2], "0.9": [1, 2]}},
                                 {"quantiles": {"0.1": [float("nan")], "0.5": [1], "0.9": [1]}}])
def test_bad_fans_raise_feature_error(fan):
    with pytest.raises(tf.FeatureError):
        tf.derive(100.0, fan, trailing_vol_pct=1.0)


def test_horizon_longer_than_fan_raises_and_bad_last_close_raises():
    with pytest.raises(tf.FeatureError):
        tf.derive(100.0, _fan(h=3), trailing_vol_pct=1.0, horizon=5)
    with pytest.raises(tf.FeatureError):
        tf.derive(0.0, _fan(), trailing_vol_pct=1.0)


def test_trailing_stats_and_volume_pace():
    bars = [{"close": 100.0 * (1 + 0.01 * ((-1) ** i)), "volume": 1000.0} for i in range(40)]
    ts = tf.trailing_stats(bars)
    assert ts["trailing_vol_pct"] > 0 and ts["trailing_volume"] == 1000.0
    vf = {"quantiles": {"0.1": [900.0] * 5, "0.5": [1500.0] * 5, "0.9": [2100.0] * 5}}
    assert tf.volume_pace(vf, 1000.0, 5) == pytest.approx(1.5)
    assert tf.volume_pace(None, 1000.0, 5) is None and tf.volume_pace(vf, None, 5) is None
