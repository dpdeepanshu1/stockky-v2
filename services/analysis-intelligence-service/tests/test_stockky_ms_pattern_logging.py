"""GENERATED COPY of shared/stockky_ms/tests/test_pattern_logging.py (imports the vendored stockky_ms) so the service coverage gate sees it.
Edit the shared file, then re-copy (sed s/stockky_ms/stockky_ms/, _bars -> _ms_bars).

C21: pattern signals are LOGGED (separate pattern feature row + shadow evidence), never scored. FEATURE_COLUMNS_V2 and the technical quality
score must stay exactly as they were. Synthetic bars, no network, no clock."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _ms_bars import walk
from stockky_ms.engine import analyze
from stockky_ms.extras import analyze_extras
from stockky_ms.ml_features import (FEATURE_COLUMNS_V2, PATTERN_COLUMNS, build_pattern_features, columns_hash, pattern_columns_hash,
                                          pattern_vector)
from stockky_ms.patterns import analyze_patterns
from stockky_ms.scoring import pattern_shadow


def test_pattern_columns_unique_hashed_and_v2_untouched():
    assert len(set(PATTERN_COLUMNS)) == len(PATTERN_COLUMNS) == 23
    assert all(c.startswith("pt_") for c in PATTERN_COLUMNS) and not set(PATTERN_COLUMNS) & set(FEATURE_COLUMNS_V2)
    assert not any("outcome" in c or c.startswith("ret_") for c in PATTERN_COLUMNS)
    assert len(FEATURE_COLUMNS_V2) == 32                       # the stored V2 rows stay comparable
    assert len(pattern_columns_hash()) == 8 and pattern_columns_hash() != columns_hash()


def test_real_series_gives_full_row_in_column_order():
    pat = analyze_patterns(walk(300, seed=3))
    built = build_pattern_features(pat)
    assert built["meta"]["usable"] is True and built["meta"]["log_only"] is True and built["meta"]["columns_hash"] == pattern_columns_hash()
    assert set(built["features"]) == set(PATTERN_COLUMNS)
    vec = pattern_vector(built)
    assert len(vec) == 23 and vec == [built["features"][c] for c in PATTERN_COLUMNS]
    assert built["features"]["pt_bb_squeeze_num"] in (0.0, 1.0) and built["features"]["pt_ma50_200_above_num"] in (0.0, 1.0)


def test_missing_sections_stay_none_never_zero():
    for pat in (None, {}, "x", analyze_patterns(walk(10, seed=1))):          # 10 bars: every section is None
        b = build_pattern_features(pat)
        assert b["meta"]["usable"] is False and all(v is None for v in b["features"].values())
    assert all(v is None for v in pattern_vector(build_pattern_features(None)))
    assert all(v == 0.0 for v in pattern_vector(build_pattern_features(None), fill=0.0))


def test_encoding_of_known_input():
    pat = {"bars": 300,
           "candles": [{"name": "hammer", "bias": "bullish", "volume_confirmed": True}, {"name": "doji", "bias": "neutral", "volume_confirmed": True},
                       {"name": "shooting_star", "bias": "bearish", "volume_confirmed": False}],
           "chart": [{"name": "double_bottom", "bias": "bullish", "status": "confirmed"}, {"name": "bull_flag", "bias": "bullish", "status": "forming"}],
           "bollinger": {"width": 0.05, "percentile": 8.0, "squeeze": False, "release": "bullish"},
           "crosses": {"ma50_200": {"fast_above_slow": True, "event": {"type": "golden_cross", "bars_ago": 2}},
                       "ma20_50": {"fast_above_slow": False, "event": {"type": "death_cross", "bars_ago": 4}}},
           "confluence": {"state": "bearish", "rsi": 44.0, "macd_above_signal": False},
           "sr_flip": [{"type": "support_flip", "retest_bars_ago": 6}, {"type": "support_flip", "retest_bars_ago": 3}]}
    f = build_pattern_features(pat)["features"]
    assert (f["pt_candle_bull_n"], f["pt_candle_bear_n"], f["pt_candle_vol_confirmed_n"]) == (1.0, 1.0, 1.0)   # neutral candles not counted as directional
    assert (f["pt_chart_bull_confirmed_n"], f["pt_chart_bull_forming_n"], f["pt_chart_bear_confirmed_n"]) == (1.0, 1.0, 0.0)
    assert (f["pt_bb_squeeze_num"], f["pt_bb_release_num"], f["pt_bb_percentile"]) == (0.0, 1.0, 8.0)
    assert (f["pt_ma50_200_event_num"], f["pt_ma50_200_event_bars"], f["pt_ma20_50_event_num"], f["pt_ma20_50_above_num"]) == (1.0, 2.0, -1.0, 0.0)
    assert (f["pt_conf_state_num"], f["pt_conf_rsi"], f["pt_conf_macd_above_num"]) == (-1.0, 44.0, 0.0)
    assert (f["pt_srflip_support_num"], f["pt_srflip_resist_num"], f["pt_srflip_retest_bars"]) == (1.0, 0.0, 3.0)


def test_shadow_groups_are_separate_and_none_is_not_zero():
    assert pattern_shadow(None)["groups"] is None and pattern_shadow({})["net"] is None
    pat = {"candles": [{"bias": "bullish"}], "chart": [{"bias": "bearish", "status": "forming"}],        # forming chart pattern is not counted
           "bollinger": {"release": "bullish"}, "crosses": {"ma50_200": None, "ma20_50": {"fast_above_slow": False}},
           "confluence": {"state": "neutral"}, "sr_flip": []}
    s = pattern_shadow(pat)
    assert s["log_only"] is True and s["in_tqs"] is False
    assert s["groups"] == {"pattern": 1, "squeeze": 1, "trend": -1, "momentum": 0, "sr_flip": 0}
    assert (s["bull_groups"], s["bear_groups"], s["net"]) == (2, 1, 1)
    both = pattern_shadow({"candles": [{"bias": "bullish"}, {"bias": "bearish"}], "sr_flip": [{"type": "support_flip"}, {"type": "resistance_flip"}]})
    assert both["groups"]["pattern"] == 0 and both["groups"]["sr_flip"] == 0 and both["groups"]["squeeze"] is None


def test_shadow_never_changes_the_technical_quality_score():
    for seed in range(6):
        bars = walk(260, seed=seed, drift=(-0.2, 0.0, 0.3)[seed % 3])
        before = analyze(bars)
        analyze_extras(bars, block=before)
        after = analyze(bars)
        assert before["technical_quality_score"] == after["technical_quality_score"]
        assert "pattern" not in str(before.get("groups")) and "pattern_shadow" not in before


def test_extras_carries_the_log_only_rows_and_v2_is_unchanged():
    ex = analyze_extras(walk(300, seed=4))
    assert ex["errors"] == [] and ex["context_only"] is True
    assert ex["feature_columns"] == {"n": 32, "hash": columns_hash()}
    assert ex["pattern_columns"] == {"n": 23, "hash": pattern_columns_hash()}
    assert ex["pattern_features"]["meta"]["log_only"] is True and set(ex["pattern_features"]["features"]) == set(PATTERN_COLUMNS)
    assert ex["pattern_shadow"]["log_only"] is True and ex["pattern_shadow"]["in_tqs"] is False
    short = analyze_extras(walk(12, seed=4))                    # too few bars: rows exist but are empty, nothing raises
    assert short["pattern_features"]["meta"]["usable"] is False and short["pattern_shadow"]["groups"] is None


def test_pattern_row_is_deterministic_and_does_not_mutate_input():
    bars = walk(320, seed=7)
    snap = [tuple(b) for b in bars]
    a = build_pattern_features(analyze_patterns(bars))
    b = build_pattern_features(analyze_patterns(bars))
    assert a == b and [tuple(x) for x in bars] == snap
