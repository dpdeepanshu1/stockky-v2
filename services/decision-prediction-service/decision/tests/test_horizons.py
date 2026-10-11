"""group319: direct tests for decision/horizons.py (multi-horizon scoring), which had none.

Pure Python. Run from services/decision-prediction-service/decision:  python3 -m pytest tests/test_horizons.py -q
Also pins two fixes: a real 0 is no longer treated as "missing", and news time-decay now reaches the score.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import horizons as h  # noqa: E402


def _tech(**kw):
    d = {"technical_score": 60}
    d.update(kw)
    return d


def _neutral_pillars(**over):
    p = {k: 50.0 for k in h.HORIZON_WEIGHTS["short"]}
    p.update(over)
    return p


# ---- constants -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("hz", ["short", "mid", "long"])
def test_weights_sum_to_one(hz):
    assert math.isclose(sum(h.HORIZON_WEIGHTS[hz].values()), 1.0, abs_tol=1e-9)


def test_every_horizon_has_meta_and_same_pillars():
    keys = set(h.HORIZON_WEIGHTS["short"])
    for hz in ("short", "mid", "long"):
        assert set(h.HORIZON_WEIGHTS[hz]) == keys
        assert {"label", "days_min", "days_max", "holding"} <= set(h.HORIZON_META[hz])


# ---- small helpers ---------------------------------------------------------------------------------------------
def test_n_defaults_and_conversion():
    assert h._n(None, 7) == 7.0
    assert h._n("12.5") == 12.5
    assert h._n("abc", 3) == 3.0
    assert h._n(0) == 0.0


def test_first_keeps_zero():
    assert h._first(None, 0, 5) == 0
    assert h._first(None, None) is None
    assert h._first(3, 4) == 3


def test_clamp():
    assert h._clamp(-5) == 0.0 and h._clamp(150) == 100.0 and h._clamp(42) == 42


def test_time_decay_weight():
    assert h.time_decay_weight(None) == 1.0
    assert h.time_decay_weight(0) == 1.0
    assert math.isclose(h.time_decay_weight(4.0, 4.0), 0.5, abs_tol=1e-9)
    assert h.time_decay_weight(1000) == 0.15          # floor
    assert h.time_decay_weight("x") == 1.0            # bad input never raises


def test_age_hours():
    assert h._age_hours(None) is None
    assert h._age_hours("garbage") is None
    two_h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert 1.9 < h._age_hours(two_h) < 2.1
    z = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert 2.9 < h._age_hours(z) < 3.1
    assert h._age_hours(datetime.now(timezone.utc) + timedelta(hours=5)) == 0.0   # future clamps to 0


def test_regime_multipliers():
    bull = h.regime_multipliers(70)
    bear = h.regime_multipliers(30)
    neutral = h.regime_multipliers(50)
    assert bull["volume_rs"] > 1 > bull["fundamental"]
    assert bear["quality_peers"] > 1 > bear["technical"]
    assert set(neutral.values()) == {1.0}
    assert h.regime_multipliers(50, "Strong BULL trend")["technical"] == bull["technical"]
    assert h.regime_multipliers(50, "bearish")["fundamental"] == bear["fundamental"]
    assert h.regime_multipliers(None) == neutral       # None -> 50


# ---- closed-loop thresholds ------------------------------------------------------------------------------------
def test_threshold_offsets_cases():
    assert h._threshold_offsets(None) == (0.0, 0.0, "neutral")
    assert h._threshold_offsets("bad", 30) == (0.0, 0.0, "neutral")
    assert h._threshold_offsets(0.9, 7)[2] == "neutral_sparse"
    d, extra, label = h._threshold_offsets(0.30, 25)          # weak edge -> raise the bar
    assert label == "raise_bar" and d > 0 and extra == -d * 0.5
    d2, _, label2 = h._threshold_offsets(80, 25)              # percentage form, strong edge -> ease
    assert label2 == "ease_bar" and d2 < 0
    assert h._threshold_offsets(0.55, 25)[2] == "neutral"
    assert h._threshold_offsets(0.0, 25)[0] == 8.0            # capped at +8
    assert h._threshold_offsets(1.0, 25)[0] == -6.0           # capped at -6


def test_threshold_confidence_ramps_with_samples():
    low_n = abs(h._threshold_offsets(0.2, 8)[0])
    high_n = abs(h._threshold_offsets(0.2, 25)[0])
    assert low_n < high_n


# ---- score_to_decision -----------------------------------------------------------------------------------------
def _dec(score, **kw):
    base = dict(already_owned=False, event_risk=False, extended=False, thin_history=False,
                low_liquidity=False, horizon="short")
    base.update(kw)
    return h.score_to_decision(score, **base)


def test_thin_history_or_low_liquidity_never_buys():
    assert _dec(99, thin_history=True) == h.DO_NOT_BUY
    assert _dec(99, low_liquidity=True) == h.DO_NOT_BUY
    assert _dec(99, thin_history=True, already_owned=True) == h.DO_NOT_BUY


def test_owned_positions_hold_or_sell():
    assert _dec(70, already_owned=True) == h.HOLD
    assert _dec(50, already_owned=True) == h.HOLD
    assert _dec(39.9, already_owned=True) == h.SELL


def test_short_horizon_bars():
    assert _dec(66) == h.BUY_NOW
    assert _dec(65.9) == h.PREPARE
    assert _dec(54) == h.PREPARE
    assert _dec(53.9) == h.DO_NOT_BUY            # 45..54 is still DO NOT BUY for a non-owned stock
    assert _dec(10) == h.DO_NOT_BUY


def test_mid_long_horizon_bars_are_higher():
    for hz in ("mid", "long"):
        assert _dec(70, horizon=hz) == h.BUY_NOW
        assert _dec(69.9, horizon=hz) == h.PREPARE
        assert _dec(58, horizon=hz) == h.PREPARE
        assert _dec(57.9, horizon=hz) == h.DO_NOT_BUY


def test_extended_penalties():
    assert _dec(69, extended=True) == h.BUY_NOW                       # short: -3 -> 66
    assert _dec(68.9, extended=True) == h.PREPARE
    assert _dec(75, extended=True, horizon="mid") == h.PREPARE        # mid: -6 -> 69 < 70
    assert _dec(76, extended=True, horizon="mid") == h.BUY_NOW


def test_event_risk_and_extended_short_only_hit_short():
    assert _dec(70, event_risk=True) == h.BUY_NOW                     # -4 -> 66
    assert _dec(69.9, event_risk=True) == h.PREPARE
    assert _dec(74, extended_short=True) == h.BUY_NOW                 # -8 -> 66
    assert _dec(73.9, extended_short=True) == h.PREPARE
    assert _dec(70, event_risk=True, extended_short=True, horizon="mid") == h.BUY_NOW   # neither applies to mid


def test_live_win_rate_moves_the_bars():
    assert _dec(66, live_win_rate=0.20, live_win_rate_n=25) != h.BUY_NOW       # weak edge raises the bar
    assert _dec(62, live_win_rate=0.90, live_win_rate_n=25) == h.BUY_NOW       # strong edge lowers it
    assert _dec(66, live_win_rate=0.20, live_win_rate_n=3) == h.BUY_NOW        # too few samples: ignored


# ---- compute_pillar_scores -------------------------------------------------------------------------------------
def test_pillars_defaults_are_neutral():
    p = h.compute_pillar_scores({}, {}, None, None, None, {})
    assert p["technical"] == 50 and p["news"] == 50 and p["event"] == 50 and p["regime"] == 50
    assert p["prediction"] == 50.0
    assert p["quality_peers"] == 50.0
    assert p["volume_rs"] == pytest.approx(0.45 * 50 + 0.35 * 55 + 0.20 * 50)


def test_volume_surge_lifts_volume_pillar():
    base = h.compute_pillar_scores(_tech(), {}, None, None, None, {})["volume_rs"]
    surge = h.compute_pillar_scores(_tech(volume_surge=True), {}, None, None, None, {})["volume_rs"]
    assert surge - base == pytest.approx(0.35 * 20)


def test_prediction_only_counts_when_model_loaded():
    off = h.compute_pillar_scores({}, {}, None, None, {"model_loaded": False, "prediction_score": 90}, {})
    on = h.compute_pillar_scores({}, {}, None, None, {"model_loaded": True, "prediction_score": 90}, {})
    assert off["prediction"] == 50.0 and on["prediction"] == 90.0


def test_real_zero_values_are_kept_not_replaced():
    # event score 0 = strongly negative event; it used to fall through `or` to the neutral 50
    p = h.compute_pillar_scores({}, {}, None, {"event_score": 0}, None, {}, extras={"event_score": 0})
    assert p["event"] == 0.0
    p2 = h.compute_pillar_scores({}, {}, None, {"event_score": 20}, None, {}, extras={"event_score": 0})
    assert p2["event"] == 0.0                                  # extras wins even when it is 0
    p3 = h.compute_pillar_scores({}, {}, None, {"event_score": 20}, None, {}, extras={})
    assert p3["event"] == 20.0                                 # falls back only when extras has none
    rs0 = h.compute_pillar_scores(_tech(rs_score=80), {}, None, None, None, {}, extras={"rs_vs_nifty": 0})["volume_rs"]
    rs_none = h.compute_pillar_scores(_tech(rs_score=80), {}, None, None, None, {}, extras={})["volume_rs"]
    assert rs0 < rs_none
    q0 = h.compute_pillar_scores({}, {"fundamental_score": 70}, None, None, None, {}, extras={"quality_score": 0})
    assert q0["quality_peers"] == pytest.approx(0.45 * 50)     # quality 0, peer 50 (default)


# ---- score_horizon ---------------------------------------------------------------------------------------------
def test_neutral_pillars_score_fifty_and_do_not_buy():
    r = h.score_horizon("short", _neutral_pillars(), 50, "", {})
    assert r["score"] == 50.0 and r["decision"] == h.DO_NOT_BUY and r["confidence"] == "Low"
    assert r["label"] == "Short-term" and r["days_min"] == 3 and r["holding_period"] == "3–21 trading days"


def test_confidence_bands():
    hi = h.score_horizon("short", _neutral_pillars(**{k: 90.0 for k in _neutral_pillars()}), 50, "", {})
    mid = h.score_horizon("short", _neutral_pillars(**{k: 60.0 for k in _neutral_pillars()}), 50, "", {})
    assert hi["confidence"] == "High" and mid["confidence"] == "Medium"


def test_all_high_pillars_buy_now_short_and_long():
    pill = {k: 90.0 for k in _neutral_pillars()}
    for hz in ("short", "mid", "long"):
        assert h.score_horizon(hz, pill, 50, "", {})["decision"] == h.BUY_NOW


def test_regime_tilts_the_score():
    pill = _neutral_pillars(technical=90.0, volume_rs=90.0, fundamental=20.0, quality_peers=20.0)
    bull = h.score_horizon("mid", pill, 75, "", {})["score"]
    bear = h.score_horizon("mid", pill, 30, "", {})["score"]
    assert bull > bear


def test_score_horizon_does_not_mutate_input_pillars():
    old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    pill = _neutral_pillars(news=95.0)
    snapshot = dict(pill)
    for hz in ("short", "mid", "long"):
        h.score_horizon(hz, pill, 50, "", {"news_as_of": old})
    assert pill == snapshot


def test_stale_news_counts_for_less_in_the_score():
    fresh_time = datetime.now(timezone.utc).isoformat()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    pill = _neutral_pillars(news=100.0)
    fresh = h.score_horizon("short", pill, 50, "", {"news_as_of": fresh_time})
    stale = h.score_horizon("short", pill, 50, "", {"news_as_of": old_time})
    assert stale["score"] < fresh["score"]                 # decay used to change only the shown pillar, never the score
    assert stale["pillars"]["news"] < fresh["pillars"]["news"]
    # 24h old, 4h half-life -> weight floors at 0.15: 100*0.15 + 50*0.85 = 57.5
    assert stale["pillars"]["news"] == pytest.approx(57.5, abs=0.1)
    # unknown age = full weight
    assert h.score_horizon("short", pill, 50, "", {})["score"] == fresh["score"]


def test_news_decay_is_independent_per_horizon():
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    pill = _neutral_pillars(news=100.0)
    shown = [h.score_horizon(hz, pill, 50, "", {"news_as_of": old})["pillars"]["news"] for hz in ("short", "mid", "long")]
    assert shown[0] == shown[1] == shown[2]                # not compounded across horizons (the earlier mutation bug)
    assert shown[0] == pytest.approx(75.0, abs=0.5)        # one half-life: halfway to 50


def test_live_win_rate_nudges_the_score_within_limits():
    base = h.score_horizon("short", _neutral_pillars(), 50, "", {})["score"]
    up = h.score_horizon("short", _neutral_pillars(), 50, "", {"live_win_rate": 1.0, "live_win_rate_n": 30})["score"]
    down = h.score_horizon("short", _neutral_pillars(), 50, "", {"live_win_rate": 0.0, "live_win_rate_n": 30})["score"]
    assert up == pytest.approx(base + 8.0) and down == pytest.approx(base - 8.0)
    sparse = h.score_horizon("short", _neutral_pillars(), 50, "", {"live_win_rate": 1.0, "live_win_rate_n": 5})["score"]
    assert sparse == base
    junk = h.score_horizon("short", _neutral_pillars(), 50, "", {"live_win_rate": "x", "live_win_rate_n": 30})
    assert junk["score"] == base                           # a bad win-rate value never raises
    pct = h.score_horizon("short", _neutral_pillars(), 50, "", {"live_win_rate": 100, "live_win_rate_n": 30})["score"]
    assert pct == up                                       # 0-100 form is accepted


def test_flags_reach_the_decision():
    pill = {k: 90.0 for k in _neutral_pillars()}
    assert h.score_horizon("short", pill, 50, "", {"thin_history": True})["decision"] == h.DO_NOT_BUY
    assert h.score_horizon("short", pill, 50, "", {"already_owned": True})["decision"] == h.HOLD


# ---- multi_horizon_decide --------------------------------------------------------------------------------------
def test_multi_horizon_shape_and_short_primary():
    out = h.multi_horizon_decide(_tech(technical_score=80), {"fundamental_score": 60}, {"news_score": 70}, None,
                                 None, {"market_score": 55})
    assert set(out["horizons"]) == {"short", "mid", "long"}
    assert out["decision"] == out["horizons"]["short"]["decision"]
    assert out["combined_score"] == out["horizons"]["short"]["score"]
    fv = out["final_verdict"]
    assert fv["preferred_horizon"] == "short" and fv["primary_decision"] == out["decision"]
    assert "Primary (Short)" in fv["summary"]


def test_best_horizon_prefers_long_when_fundamentals_dominate():
    out = h.multi_horizon_decide(_tech(technical_score=30), {"fundamental_score": 95, "quality_score": 95}, None, None,
                                 None, {"market_score": 50}, extras={"peer_relative_score": 95})
    assert out["final_verdict"]["best_horizon"] in ("mid", "long")
    assert out["horizons"]["long"]["score"] > out["horizons"]["short"]["score"]


def test_multi_horizon_handles_all_empty_inputs():
    out = h.multi_horizon_decide({}, {}, None, None, None, {})
    assert out["decision"] == h.DO_NOT_BUY and out["confidence"] == "Low"
    assert out["holding_period"] == "3–21 trading days"


def test_classification_comes_from_trend_when_missing():
    a = h.multi_horizon_decide(_tech(), {}, None, None, None, {"market_score": 50, "trend": "bull run"})
    b = h.multi_horizon_decide(_tech(), {}, None, None, None, {"market_score": 50})
    assert a["horizons"]["short"]["score"] != b["horizons"]["short"]["score"]
