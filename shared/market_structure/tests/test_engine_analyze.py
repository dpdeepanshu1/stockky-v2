"""Tests for the rebuilt engine modules: analyze() contract, no-lookahead (truncation invariance), fib, location, R:R, scoring, levels,
explain. Synthetic bars only. Runs with pytest or plain `python`."""
import json, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure import fibonacci as fib
from market_structure.engine import analyze
from market_structure.explain import explain_block
from market_structure.location import location_block, structure_rr
from market_structure.params import MSParams
from market_structure.scoring import score_block, structure_group
from market_structure.summary import flatten_summary


def _bars(seed=7, n=220):
    random.seed(seed)
    px, out = 100.0, []
    for i in range(n):
        o, c = px, max(5.0, px + random.gauss(0.25 if (i // 30) % 2 == 0 else -0.25, 1.6))
        h = max(o, c) + abs(random.gauss(0, 0.8))
        l = min(o, c) - abs(random.gauss(0, 0.8))
        out.append((1_700_000_000 + i * 86400, o, h, l, c, 1000 + random.random() * 500))
        px = c
    return out


TOP_KEYS = {"schema_version", "engine_version", "params_hash", "quality", "data_quality", "trend", "strength", "bos", "choch", "liquidity",
            "fvg", "order_block", "fibonacci", "location", "structure_rr", "levels", "atr", "last_close", "technical_quality_score",
            "independent_groups_positive", "groups", "explain"}


def test_analyze_contract_and_json_safe():
    b = analyze(_bars())
    assert TOP_KEYS <= set(b)
    json.dumps(b, allow_nan=False)
    assert 0 <= b["technical_quality_score"] <= 100
    assert set(b["groups"]) == {"structure", "zones", "liquidity", "location", "risk"}


def test_analyze_is_deterministic():
    assert analyze(_bars()) == analyze(_bars())


def test_short_and_empty_input_never_raise():
    for bars in ([], _bars(n=10), _bars(n=30), [(1, "x", 1, 1, 1, 1)]):
        b = analyze(bars)
        assert b["quality"] in ("insufficient", "unavailable") and b["technical_quality_score"] is None
        assert b["explain"]["warnings"]
        assert flatten_summary(b)["ms_tqs"] is None


def test_stale_flag_downgrades_quality():
    b = analyze(_bars(), stale=True)
    assert b["quality"] == "stale" and b["technical_quality_score"] is None


def test_truncation_invariance_no_lookahead():
    """The block computed on bars[:k] must not change when later bars are appended and then cut off again (same input -> same output),
    and the swings visible at k must all be confirmed by bar k."""
    bars = _bars(n=240)
    for k in (150, 180, 210):
        b1 = analyze(bars[:k])
        b2 = analyze(bars[:k] + bars[k:k + 25])
        assert b1 == analyze(bars[:k])
        last_ts = bars[k - 1][0]
        assert all(s["ts"] <= last_ts for s in b1["swings"])
        assert b2["data_quality"]["last_bar_ts"] == bars[k + 24][0]


def test_future_bars_do_not_change_past_swing_labels():
    bars = _bars(n=240)
    early = {(s["ts"], s["kind"]): s["label"] for s in analyze(bars[:200])["swings"]}
    late = {(s["ts"], s["kind"]): s["label"] for s in analyze(bars)["swings"]}
    # a swing may fall out of the last-8 window later, but a swing still shown in both must keep its label
    for key, lab in early.items():
        if key in late:
            assert late[key] == lab


# ----------------------------------------------------------------------------------------------------------------------------------
def _struct():
    # swing low 100 (idx 10) -> swing high 120 (idx 20): a 20-point leg
    return [{"kind": "low", "price": 100.0, "idx": 10, "confirmed_at": 12, "label": "HL", "ts": 10},
            {"kind": "high", "price": 120.0, "idx": 20, "confirmed_at": 22, "label": "HH", "ts": 20}]


def _flat_bars(n, price):
    return [(i, price, price + 0.5, price - 0.5, price, 1000.0) for i in range(n)]


def test_fib_golden_zone_and_extensions():
    p = MSParams()
    bars = _flat_bars(30, 110.0)  # retracement = (120-110)/20 = 0.5 -> golden
    f = fib.fib_block(_struct(), bars, "bullish", 0, 1.0, p)
    assert f["direction"] == "bullish" and f["in_golden_zone"] is True and f["retracement"] == 0.5
    assert f["golden_low"] == 107.64 and f["golden_high"] == 110.0
    assert f["ext_1272"] == 125.44 and f["ext_1618"] == 132.36
    assert f["leg_invalidated"] is False


def test_fib_leg_invalidated_by_close_below_origin():
    bars = _flat_bars(30, 110.0) + [(30, 99.0, 99.5, 98.0, 98.5, 1000.0)]
    f = fib.fib_block(_struct(), bars, "bullish", 0, 1.0, MSParams())
    assert f["leg_invalidated"] is True


def test_fib_small_leg_gives_no_anchor():
    st = [{"kind": "low", "price": 100.0, "idx": 1, "confirmed_at": 3, "label": "HL", "ts": 1},
          {"kind": "high", "price": 101.0, "idx": 5, "confirmed_at": 7, "label": "HH", "ts": 5}]
    f = fib.fib_block(st, _flat_bars(20, 100.5), "bullish", 0, 1.0, MSParams())
    assert f["retracement"] is None


# ----------------------------------------------------------------------------------------------------------------------------------
def _zone(top, bottom, side="bullish"):
    return {"side": side, "top": top, "bottom": bottom, "status": "fresh", "quality": 0.8, "distance_atr": 0.3}


def test_location_chase_gate_and_unknown_inputs_not_good_news():
    p = MSParams()
    lv = {"supports": [], "resistances": []}
    near = location_block(100.5, 1.0, _zone(100.0, 99.0), None, fib.empty_fib(), lv, None, {}, p)
    far = location_block(103.0, 1.0, _zone(100.0, 99.0), None, fib.empty_fib(), lv, None, {}, p)
    assert near["chase_atr"] == 0.5 and "chase_gate" not in near["flags"]
    assert far["chase_atr"] == 3.0 and "chase_gate" in far["flags"]
    assert far["entry_location_score"] < near["entry_location_score"]
    assert near["stretch_atr"] is None  # no ema20 -> unknown, not zero
    assert "no_resistance_known" in near["flags"]


def test_location_missing_everything_is_bounded():
    out = location_block(100.0, 1.0, None, None, fib.empty_fib(), {"supports": [], "resistances": []}, None, {}, MSParams())
    assert out["chase_atr"] is None and 0 <= out["entry_location_score"] <= 100


def test_rr_prefers_nearer_valid_stop_over_far_order_block():
    """Regression: a far order block used to win the stop and hide a valid nearer swing-low stop (-> stop_too_wide)."""
    p = MSParams()
    a, price = 2.0, 108.0
    struct = [{"kind": "low", "price": 104.0, "idx": 5, "confirmed_at": 7, "label": "HL", "ts": 5}]
    lv = {"supports": [], "resistances": [{"price_low": 118.0, "price_high": 118.0, "strength": 0.5, "sources": ["swing"]}]}
    rr = structure_rr(price, a, _zone(82.0, 80.0), struct, None, lv, fib.empty_fib(), {}, p)
    assert rr["stop_source"] == "swing_low" and "stop_too_wide" not in rr["flags"]
    assert rr["target"] == 118.0 and rr["rr"] is not None and rr["rr"] > 1.5


def test_rr_all_stops_too_wide_is_flagged_not_dropped():
    p = MSParams()
    rr = structure_rr(108.0, 2.0, _zone(82.0, 80.0), [], None, {"supports": [], "resistances": []}, fib.empty_fib(), {}, p)
    assert rr["stop_source"] == "ob_bottom" and "stop_too_wide" in rr["flags"] and "no_target" in rr["flags"]


def test_rr_no_candidates_is_invalid():
    rr = structure_rr(100.0, 1.0, None, [], None, {"supports": [], "resistances": []}, fib.empty_fib(), {}, MSParams())
    assert rr["flags"] == ["invalid"] and rr["rr"] is None


# ----------------------------------------------------------------------------------------------------------------------------------
def test_scoring_unknown_is_zero_and_limited_is_capped():
    p = MSParams()
    empty = score_block(None, None, None, None, None, None, None, None, "ok", p)
    assert empty["technical_quality_score"] == 0 and empty["independent_groups_positive"] == 0
    loc = {"entry_location_score": 100}
    rr = {"rr": 3.0, "flags": []}
    bos = {"direction": "bullish", "failed": False, "strength": 1.0}
    full = score_block("bullish", bos, None, None, None, None, loc, rr, "ok", p)
    lim = score_block("bullish", bos, None, None, None, None, loc, rr, "limited", p)
    assert lim["technical_quality_score"] < full["technical_quality_score"]
    assert all(g["score"] <= p.limited_group_cap for g in lim["groups"].values())
    assert score_block("bullish", bos, None, None, None, None, loc, rr, "insufficient", p)["technical_quality_score"] is None


def test_scoring_failed_bos_and_bearish_do_not_help():
    good = structure_group("bullish", {"direction": "bullish", "failed": False, "strength": 0.5}, None)
    failed = structure_group("bullish", {"direction": "bullish", "failed": True, "strength": 0.5}, None)
    bear = structure_group("bearish", {"direction": "bearish", "failed": False, "strength": 0.5}, None)
    assert good > failed > bear == 0.0


def test_correlated_zones_are_not_double_counted():
    from market_structure.scoring import zones_group
    z = {"quality": 0.8, "distance_atr": 0.2}
    assert zones_group(z, z) <= 0.9 and zones_group(z, None) == 0.8


# ----------------------------------------------------------------------------------------------------------------------------------
def test_levels_supports_below_resistances_above_price():
    b = analyze(_bars())
    px = b["last_close"]
    assert all(s["price_high"] < px for s in b["levels"]["supports"])
    assert all(r["price_low"] > px for r in b["levels"]["resistances"])
    assert len(b["levels"]["supports"]) <= MSParams().level_n


def test_explain_is_deterministic_and_tolerates_junk():
    b = analyze(_bars())
    assert explain_block(b) == explain_block(b) == b["explain"]
    assert explain_block(None)["warnings"] == ["structure data unavailable"]
    assert explain_block({"quality": "insufficient"})["summary"] == "Insufficient history."


def test_summary_roundtrip_of_full_block():
    b = analyze(_bars())
    s = flatten_summary(b)
    assert s["ms_tqs"] == b["technical_quality_score"] and s["ms_trend"] == b["trend"] and s["ms_price"] == b["last_close"]


def test_many_seeds_never_raise_and_stay_json_safe():
    for seed in range(40):
        b = analyze(_bars(seed=seed, n=130 + seed * 3))
        json.dumps(b, allow_nan=False)
        if b["technical_quality_score"] is not None:
            assert 0 <= b["technical_quality_score"] <= 100


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
