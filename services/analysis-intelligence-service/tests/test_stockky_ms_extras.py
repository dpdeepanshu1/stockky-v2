"""GENERATED COPY of shared/market_structure/tests/test_extras.py (imports the vendored stockky_ms) so the service coverage gate sees the WS-14 modules.
Edit the shared file, then re-copy (sed s/market_structure/stockky_ms/, _bars -> _ms_bars)."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _ms_bars import DAY, T0, walk
from stockky_ms.extras import analyze_extras
from stockky_ms.ml_features import FEATURE_COLUMNS_V2, assert_comparable, build_features_v2, columns_hash, vector
from stockky_ms.volume_profile import volume_profile
from stockky_ms.weekly import alignment, to_weekly, weekly_context, weekly_params
from stockky_ms.wyckoff import wyckoff_context

MON = 1_699_813_800   # 2023-11-13 (Mon) 00:00 IST


def _days(n, start=MON, price=100.0, vol=1000.0):
    out, t, px = [], start, price
    while len(out) < n:
        wd = ((t + 19800) // DAY + 3) % 7        # 0 = Monday
        if wd < 5:
            out.append((t, px, px + 1, px - 1, px + 0.5, vol))
            px += 0.5
        t += DAY
    return out


# ---- weekly ----
def test_weekly_groups_by_iso_week_and_aggregates():
    w = to_weekly(_days(10))
    assert len(w) == 2
    first = _days(10)[:5]
    assert w[0][0] == first[0][0] and w[0][1] == first[0][1] and w[0][4] == first[-1][4]
    assert w[0][2] == max(b[2] for b in first) and w[0][3] == min(b[3] for b in first) and w[0][5] == 5000.0


def test_weekly_drops_forming_week_unless_complete():
    eight = _days(8)
    assert len(to_weekly(eight)) == 1
    assert len(to_weekly(eight, complete_only=False)) == 2
    assert len(to_weekly(_days(5))) == 1


def test_weekly_skips_bad_rows_and_sorts():
    bars = list(reversed(_days(10)))
    bars.append((MON, float("nan"), 1, 1, 1, 1))
    bars.append((1, 2))
    assert len(to_weekly(bars)) == 2
    assert to_weekly([]) == []
    nv = [(b[0], b[1], b[2], b[3], b[4], float("nan")) for b in _days(5)]
    assert to_weekly(nv)[0][5] == 0.0


def test_weekly_context_and_params():
    p = weekly_params()
    assert p.min_bars == 30 and p.full_bars == 52 and p.swing_left == 2
    ctx = weekly_context(walk(400, seed=3))
    assert ctx["timeframe"] == "1w" and ctx["bars"] > 40 and ctx["quality"] in ("ok", "limited")
    assert ctx["trend"] in ("bullish", "bearish", "neutral", "transition")
    thin = weekly_context(walk(40, seed=3))
    assert thin["quality"] == "insufficient" and thin["last_swing_high"] is None


def test_alignment_never_reads_unknown_as_agreement():
    assert alignment("bullish", "bullish") == "aligned_bull"
    assert alignment("bearish", "bearish") == "aligned_bear"
    assert alignment("bullish", "bearish") == "conflict" and alignment("bearish", "bullish") == "conflict"
    assert alignment("neutral", "bullish") == "unknown" and alignment(None, "bullish") == "unknown" and alignment("bullish", None) == "unknown"


# ---- volume profile ----
def _band_bars(heavy_lo=100.0, heavy_hi=102.0):
    bars = [(T0 + i * DAY, 100, 110, 90, 105, 100.0) for i in range(60)]
    bars += [(T0 + i * DAY, 101, heavy_hi, heavy_lo, 101.5, 5000.0) for i in range(60, 120)]
    return bars


def test_volume_profile_poc_sits_in_the_heavy_band():
    vp = volume_profile(_band_bars())
    assert vp["method"] == "range_uniform" and vp["bars"] == 120
    assert 99.0 <= vp["poc"] <= 103.5 and vp["val"] <= vp["poc"] <= vp["vah"]
    assert vp["position"] == "inside_value" and 0 < vp["poc_share"] <= 1


def test_volume_profile_position_above_and_below_value():
    up = _band_bars()
    up.append((T0 + 121 * DAY, 108, 112, 107, 111, 100.0))
    assert volume_profile(up)["position"] == "above_value"
    dn = _band_bars()
    dn.append((T0 + 121 * DAY, 92, 93, 88, 89, 100.0))
    assert volume_profile(dn)["position"] == "below_value"


def test_volume_profile_none_without_usable_volume_or_range():
    assert volume_profile([]) is None
    assert volume_profile([(T0 + i * DAY, 1, 2, 1, 1.5, 0.0) for i in range(30)]) is None
    assert volume_profile([(T0 + i * DAY, 5, 5, 5, 5, 10.0) for i in range(30)]) is None
    assert volume_profile(_band_bars(), buckets=2) is None
    assert volume_profile(_band_bars()[:5]) is None
    bad = _band_bars()
    bad[3] = (bad[3][0], 1, float("nan"), 1, 1, 10.0)
    assert volume_profile(bad)["bars"] == 119


def test_volume_profile_value_area_expands_both_ways():
    vp = volume_profile(_band_bars(), value_area=0.99)
    assert vp["val"] < vp["poc"] < vp["vah"]


# ---- wyckoff ----
def _trend(n, start, step, t0, vol=1000.0, wick=0.4):
    out, px = [], start
    for i in range(n):
        o, c = px, px + step
        out.append((t0 + i * DAY, o, max(o, c) + wick, min(o, c) - wick, c, vol))
        px = c
    return out


def _range(n, mid, t0, amp=2.0, up_vol=1500.0, dn_vol=800.0, spring=False, upthrust=False):
    out = []
    for i in range(n):
        up = i % 2 == 0
        o = mid - (amp / 2 if up else -amp / 2)
        c = mid + (amp / 2 if up else -amp / 2) * 0.9
        out.append([t0 + i * DAY, o, max(o, c) + 0.3, min(o, c) - 0.3, c, up_vol if up else dn_vol])
    if spring:
        k = n - 10
        out[k][3] = mid - amp * 2.2
        out[k][4] = mid + 0.2
    if upthrust:
        k = n - 10
        out[k][2] = mid + amp * 2.2
        out[k][4] = mid - 0.2
    return [tuple(x) for x in out]


def test_wyckoff_markup_and_markdown():
    up = wyckoff_context(_trend(120, 50, 0.6, T0))
    assert up["phase"] == "markup" and 0.5 <= up["confidence"] <= 1.0 and up["context_only"] is True and "net_rise" in up["evidence"]
    dn = wyckoff_context(_trend(120, 200, -0.6, T0))
    assert dn["phase"] == "markdown" and "net_fall" in dn["evidence"]


def test_wyckoff_accumulation_after_decline():
    r = wyckoff_context(_trend(60, 150, -1.0, T0) + _range(60, 90, T0 + 60 * DAY, spring=True))
    assert r["phase"] == "accumulation" and "prior_decline" in r["evidence"] and r["confidence"] >= 0.75
    assert "spring_reclaim" in r["evidence"] and "up_volume_dominates" in r["evidence"]


def test_wyckoff_distribution_after_advance():
    r = wyckoff_context(_trend(60, 50, 1.0, T0) + _range(60, 110, T0 + 60 * DAY, up_vol=800.0, dn_vol=1500.0, upthrust=True))
    assert r["phase"] == "distribution" and "upthrust_rejection" in r["evidence"] and "down_volume_dominates" in r["evidence"]


def test_wyckoff_unknown_when_unclear_or_thin():
    thin = wyckoff_context(walk(60, seed=1))
    assert thin["phase"] == "unknown" and thin["reason"] == "insufficient_history" and thin["confidence"] is None
    flat = wyckoff_context(_range(130, 100, T0))
    assert flat["phase"] == "unknown" and flat["reason"] == "no_clear_phase" and "metrics" in flat

    def _nv(spring):
        raw = _trend(60, 150, -1.0, T0) + _range(60, 90, T0 + 60 * DAY, spring=spring)
        return wyckoff_context([(b[0], b[1], b[2], b[3], b[4], 0.0) for b in raw])
    assert _nv(False)["phase"] == "unknown"      # no volume, no spring: 2 of 4 checks only
    r = _nv(True)                                # spring + decline + tight = 3 of 4 even without volume
    assert r["phase"] == "accumulation" and "up_volume_dominates" not in r["evidence"] and r["confidence"] == 0.75


# ---- V2 features ----
def test_feature_columns_are_unique_fixed_and_hashed():
    assert len(set(FEATURE_COLUMNS_V2)) == len(FEATURE_COLUMNS_V2) == 32
    assert columns_hash() == columns_hash() and len(columns_hash()) == 8
    assert not any("outcome" in c or c.startswith("ret_") for c in FEATURE_COLUMNS_V2)


def test_features_from_a_real_block_and_vector_order():
    built = analyze_extras(walk(300, seed=3))["features_v2"]
    assert set(built["features"]) == set(FEATURE_COLUMNS_V2) and built["meta"]["usable"] is True
    assert built["meta"]["columns_hash"] == columns_hash() and built["meta"]["params_hash"]
    vec = vector(built)
    assert len(vec) == 32 and vec == [built["features"][c] for c in FEATURE_COLUMNS_V2]
    assert all(v is not None for v in vector(built, fill=0.0))


def test_enum_encoding_and_none_handling():
    s = {"ms_quality": "ok", "ms_trend": "bullish", "ms_bos_dir": "bearish", "ms_bos_failed": False, "ms_fvg_status": "partial", "ms_in_golden": True,
         "ms_rr": "x", "ms_loc_score": 70, "ms_ver": "1.0.0", "ms_hash": "abc"}
    f = build_features_v2(s, weekly={"quality": "ok", "trend": "bullish"}, volume={"position": "below_value", "price_vs_poc_pct": -3.0},
                          wyckoff={"phase": "markdown"})["features"]
    assert f["ms_trend_num"] == 1.0 and f["ms_bos_dir_num"] == -1.0 and f["ms_bos_failed_num"] == 0.0 and f["ms_fvg_status_num"] == 0.66
    assert f["ms_in_golden_num"] == 1.0 and f["ms_rr"] is None and f["ms_loc_score"] == 70.0 and f["ms_choch_dir_num"] is None
    assert f["wk_trend_num"] == 1.0 and f["wk_alignment_num"] == 1.0 and f["vp_position_num"] == -1.0 and f["wy_phase_num"] == -1.0
    assert build_features_v2(s, weekly={"quality": "insufficient", "trend": "bullish"})["features"]["wk_trend_num"] is None


def test_unusable_summary_gives_all_none_never_zero():
    for s in (None, {}, {"ms_quality": "insufficient", "ms_trend": "bullish", "ms_tqs": 90}, {"ms_quality": "stale"}):
        b = build_features_v2(s)
        assert b["meta"]["usable"] is False
        assert all(v is None for k, v in b["features"].items() if k.startswith("ms_"))


def test_rows_from_different_versions_are_not_comparable():
    a = build_features_v2({"ms_quality": "ok", "ms_ver": "1.0.0", "ms_hash": "aaa"})
    b = build_features_v2({"ms_quality": "ok", "ms_ver": "1.0.0", "ms_hash": "aaa"})
    c = build_features_v2({"ms_quality": "ok", "ms_ver": "1.0.0", "ms_hash": "bbb"})
    assert assert_comparable([a, b]) and not assert_comparable([a, c]) and assert_comparable([])


# ---- extras wrapper ----
def test_extras_shape_and_context_flag():
    ex = analyze_extras(walk(300, seed=4))
    assert ex["context_only"] is True and ex["errors"] == [] and ex["feature_columns"] == {"n": 32, "hash": columns_hash()}
    assert ex["weekly"]["timeframe"] == "1w" and ex["volume_profile"]["method"] == "range_uniform" and ex["wyckoff"]["context_only"] is True
    assert ex["alignment"] in ("aligned_bull", "aligned_bear", "conflict", "unknown")


def test_extras_reuses_a_given_block_and_survives_short_or_bad_input():
    from stockky_ms.engine import analyze
    bars = walk(300, seed=4)
    blk = analyze(bars)
    assert analyze_extras(bars, block=blk)["features_v2"]["meta"]["params_hash"] == blk["params_hash"]
    short = analyze_extras(walk(20, seed=4))
    assert short["features_v2"]["meta"]["usable"] is False and short["wyckoff"]["phase"] == "unknown"
    assert analyze_extras([])["errors"] == [] and analyze_extras([])["features_v2"]["meta"]["usable"] is False
    assert analyze_extras(None)["errors"] != []


def test_extras_isolates_a_failing_section():
    import stockky_ms.extras as ex_mod
    orig = ex_mod.volume_profile
    ex_mod.volume_profile = lambda *_a, **_k: (_ for _ in ()).throw(ValueError("boom"))
    try:
        r = analyze_extras(walk(300, seed=4))
    finally:
        ex_mod.volume_profile = orig
    assert r["volume_profile"] is None and r["errors"] == ["volume_profile:ValueError"] and r["weekly"] is not None
    orig_a = ex_mod.analyze
    ex_mod.analyze = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    try:
        r2 = analyze_extras(walk(300, seed=4))
    finally:
        ex_mod.analyze = orig_a
    assert r2["errors"] == ["analyze:RuntimeError"] and r2["features_v2"] is None
