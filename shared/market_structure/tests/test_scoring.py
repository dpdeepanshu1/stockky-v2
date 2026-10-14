"""T-SCORE: the technical quality score. Bounded, monotone in its evidence, capped when data is limited, silent when data is unusable, and
never reads unknown inputs as good news."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import walk
from market_structure.engine import analyze
from market_structure.params import MSParams
from market_structure.scoring import GROUP_POSITIVE, GROUP_WEIGHTS, risk_group, score_block, structure_group

P = MSParams()
GOOD_RR = {"rr": 2.5, "flags": []}
GOOD_LOC = {"entry_location_score": 90}


def _score(trend="bullish", bos=None, choch=None, sweep=None, ob=None, fvg=None, loc=None, rr=None, quality="ok"):
    return score_block(trend, bos, choch, sweep, ob, fvg, loc, rr, quality, P)


def test_weights_sum_to_100():
    assert sum(w for _, w in GROUP_WEIGHTS) == 100


def test_score_is_bounded_and_integer_over_many_series():
    for seed in range(8):
        b = analyze(walk(220, seed=seed, drift=(-0.2, 0.0, 0.3)[seed % 3]))
        s = b["technical_quality_score"]
        assert isinstance(s, int) and 0 <= s <= 100
        assert 0 <= b["independent_groups_positive"] <= len(GROUP_WEIGHTS)


def test_unusable_quality_gives_no_score():
    for q in ("insufficient", "stale", "unavailable"):
        r = _score(quality=q, loc=GOOD_LOC, rr=GOOD_RR)
        assert r["technical_quality_score"] is None and r["groups"] is None


def test_unknown_inputs_score_zero_not_good():
    r = _score(trend=None)
    assert r["technical_quality_score"] == 0 and r["independent_groups_positive"] == 0


def test_better_evidence_never_lowers_the_score():
    base = _score(loc={"entry_location_score": 40}, rr={"rr": 1.3, "flags": []})["technical_quality_score"]
    better_loc = _score(loc={"entry_location_score": 90}, rr={"rr": 1.3, "flags": []})["technical_quality_score"]
    better_rr = _score(loc={"entry_location_score": 40}, rr={"rr": 2.5, "flags": []})["technical_quality_score"]
    assert better_loc >= base and better_rr >= base and max(better_loc, better_rr) > base


def test_bearish_structure_scores_below_bullish_all_else_equal():
    bull = _score("bullish", loc=GOOD_LOC, rr=GOOD_RR)["technical_quality_score"]
    bear = _score("bearish", loc=GOOD_LOC, rr=GOOD_RR)["technical_quality_score"]
    assert bear < bull


def test_fresh_bearish_break_pulls_structure_down():
    plain = structure_group("bullish", None, None)
    hit = structure_group("bullish", {"direction": "bearish", "failed": False, "strength": 0.8}, None)
    assert hit < plain
    failed = structure_group("bullish", {"direction": "bearish", "failed": True, "strength": 0.8}, None)
    assert failed == plain


def test_limited_quality_caps_every_group():
    ok = _score(loc={"entry_location_score": 100}, rr={"rr": 3.0, "flags": []}, quality="ok")
    lim = _score(loc={"entry_location_score": 100}, rr={"rr": 3.0, "flags": []}, quality="limited")
    assert lim["technical_quality_score"] <= ok["technical_quality_score"]
    assert all(g["score"] <= P.limited_group_cap + 1e-9 for g in lim["groups"].values())


def test_group_positive_flag_follows_the_threshold():
    r = _score(loc={"entry_location_score": 49}, rr=GOOD_RR)
    assert r["groups"]["location"]["positive"] is False
    r = _score(loc={"entry_location_score": 50}, rr=GOOD_RR)
    assert r["groups"]["location"]["positive"] is (0.5 >= GROUP_POSITIVE)


def test_risk_group_penalises_wide_or_invalid_stops():
    assert risk_group({"rr": 2.5, "flags": ["stop_too_wide"]}, P) <= 0.3
    assert risk_group({"rr": 2.5, "flags": ["invalid"]}, P) <= 0.3
    assert risk_group({"rr": None, "flags": []}, P) == 0.0
    assert risk_group({"rr": 2.5, "flags": []}, P) > risk_group({"rr": 1.3, "flags": []}, P) > risk_group({"rr": 0.5, "flags": []}, P)
