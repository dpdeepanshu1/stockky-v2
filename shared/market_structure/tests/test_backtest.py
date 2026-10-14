"""WS-7: forward outcomes, rule flags, rule effects, walk-forward and calibration on hand-built and synthetic data."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import DAY, T0, walk
from market_structure.backtest import (HORIZONS, build_samples, calibration, forward_outcome, rule_effects, rule_flags, walk_forward)


def _b(i, o, h, l, c):
    return (T0 + i * DAY, o, h, l, c, 1000.0)


def test_forward_returns_use_the_close_of_the_nth_bar():
    after = [_b(i, 100, 101 + i, 99, 100 + i) for i in range(1, 25)]       # closes 101, 102, ... (after[k] closes at 101 + k)
    o = forward_outcome(100.0, None, None, after)
    assert o["ret_1d"] == 1.0 and o["ret_5d"] == 5.0 and o["ret_10d"] == 10.0 and o["ret_20d"] == 20.0
    assert o["complete"] is True and o["bars_seen"] == 20


def test_partial_window_leaves_later_horizons_none_and_incomplete():
    after = [_b(i, 100, 102, 99, 101) for i in range(1, 7)]
    o = forward_outcome(100.0, None, None, after)
    assert o["ret_1d"] == 1.0 and o["ret_5d"] == 1.0 and o["ret_10d"] is None and o["ret_20d"] is None
    assert o["complete"] is False and o["bars_seen"] == 6


def test_no_bars_no_outcome():
    o = forward_outcome(100.0, 95.0, 110.0, [])
    assert o["bars_seen"] == 0 and o["ret_1d"] is None and o["mfe_pct"] is None and o["first_touch"] is None
    assert forward_outcome(None, 95.0, 110.0, [_b(1, 1, 1, 1, 1)])["bars_seen"] == 0


def test_mfe_mae_and_first_touch_target():
    after = [_b(1, 100, 103, 99, 102), _b(2, 102, 111, 101, 110), _b(3, 110, 112, 90, 95)]
    o = forward_outcome(100.0, 92.0, 110.0, after)
    assert o["first_touch"] == "target" and o["touch_bars"] == 2 and o["ambiguous"] is False
    assert o["mfe_pct"] == 12.0 and o["mae_pct"] == -10.0


def test_stop_first_and_same_bar_is_conservative():
    o = forward_outcome(100.0, 95.0, 110.0, [_b(1, 100, 101, 94, 96), _b(2, 96, 120, 95, 118)])
    assert o["first_touch"] == "stop" and o["touch_bars"] == 1
    o = forward_outcome(100.0, 95.0, 110.0, [_b(1, 100, 111, 94, 100)])
    assert o["first_touch"] == "stop" and o["ambiguous"] is True


def test_bad_stop_or_target_is_ignored():
    after = [_b(1, 100, 130, 70, 100)]
    o = forward_outcome(100.0, 105.0, 90.0, after)            # stop above entry / target below entry: nothing to touch
    assert o["first_touch"] is None


def test_rule_flags_trip_on_the_gate_thresholds_and_never_on_missing_data():
    s = {"ms_chase_atr": 2.0, "ms_stretch_atr": 4.0, "ms_room_atr": 0.2, "ms_rr": 0.9, "ms_rr_flags": ["stop_too_wide"], "ms_trend": "bearish"}
    assert all(rule_flags(s).values())
    assert not any(rule_flags({}).values()) and not any(rule_flags(None).values())
    assert rule_flags({"ms_chase_atr": 2.0}, {"chase_atr": 3.0})["chase"] is False
    assert rule_flags({"ms_chase_atr": 2.0}, {"chase_atr": 0})["chase"] is False          # 0 switches the rule off
    fresh = {"ms_bos_dir": "bearish", "ms_bos_failed": False, "ms_bos_bars": 3}
    assert rule_flags(fresh)["bearish"] is True
    assert rule_flags({**fresh, "ms_bos_failed": True})["bearish"] is False
    assert rule_flags({**fresh, "ms_bos_bars": 9})["bearish"] is False


def _sample(i, chase, ret):
    return {"i": i, "ts": T0 + i * DAY, "summary": {"ms_chase_atr": chase}, "outcome": {"ret_10d": ret}}


def test_rule_effects_measures_edge_and_respects_min_n():
    rows = [_sample(i, 3.0, -2.0) for i in range(30)] + [_sample(30 + i, 0.5, 1.0) for i in range(30)]
    e = rule_effects(rows, 10, min_n=20)["rules"]["chase"]
    assert e["rejected"]["n"] == 30 and e["passed"]["n"] == 30 and e["edge"] == 3.0 and e["verdict"] == "helps"
    small = rule_effects(rows[:5] + rows[30:35], 10, min_n=20)["rules"]["chase"]
    assert small["verdict"] == "insufficient_sample"
    wrong = [_sample(i, 3.0, 4.0) for i in range(30)] + [_sample(30 + i, 0.5, 1.0) for i in range(30)]
    assert rule_effects(wrong, 10, min_n=20)["rules"]["chase"]["verdict"] == "hurts"


def test_rule_effects_ignores_samples_without_that_horizon():
    rows = [_sample(i, 3.0, None) for i in range(10)]
    assert rule_effects(rows, 10)["n"] == 0


def test_walk_forward_finds_a_real_threshold_out_of_sample():
    rows = []
    for i in range(300):
        chase = (i * 7 % 40) / 10.0            # 0 .. 3.9 spread over time
        rows.append(_sample(i, chase, -3.0 if chase > 2.0 else 1.0))
    r = walk_forward(rows, "chase", [1.0, 1.5, 2.0, 3.0], folds=3, min_train=40, min_n=5)
    assert r["oos_edge"] is not None and r["oos_edge"] > 2.0 and r["stable"] is True
    assert all(f["threshold"] == 2.0 for f in r["folds"])


def test_walk_forward_refuses_tiny_or_unknown():
    assert walk_forward([_sample(i, 1.0, 1.0) for i in range(10)], "chase", [1.0]).get("note") == "insufficient_sample"
    assert walk_forward([_sample(i, 1.0, 1.0) for i in range(300)], "nope", [1.0]).get("note") == "unknown_rule"


def test_calibration_buckets_and_monotonic_flag():
    rows = [{"i": i, "ts": i, "summary": {"ms_tqs": i % 100}, "outcome": {"ret_10d": (i % 100) / 20.0}} for i in range(500)]
    c = calibration(rows, "ms_tqs", bins=5, horizon=10)
    assert len(c["bins"]) == 5 and c["monotonic"] is True and c["spread"] > 3
    rows = [{"i": i, "ts": i, "summary": {"ms_tqs": i % 100}, "outcome": {"ret_10d": -(i % 100) / 20.0}} for i in range(500)]
    assert calibration(rows, "ms_tqs", bins=5)["monotonic"] is False
    assert calibration(rows[:20], "ms_tqs", bins=5).get("note") == "insufficient_sample"


def test_build_samples_never_uses_the_future_and_completes_old_days():
    bars = walk(220, seed=21)
    s = build_samples(bars, step=5)
    assert s and all(x["outcome"]["bars_seen"] == min(20, len(bars) - 1 - x["i"]) for x in s)
    old = [x for x in s if x["i"] <= len(bars) - 21]
    assert old and all(x["outcome"]["complete"] for x in old)
    assert all(h and f"ret_{h}d" in s[0]["outcome"] for h in HORIZONS)
