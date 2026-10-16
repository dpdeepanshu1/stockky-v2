"""group409: the replayable policy rules (shared/market_structure/backtest.policy_flags) must say the same as the live policy
(services/decision-prediction-service/decision/structure_policy.py). If either side changes, this fails - the A/B --policy numbers would be about a
different rule set otherwise. Plain asserts; also collected by pytest."""
import itertools
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "services", "decision-prediction-service", "decision"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market_structure.backtest import POLICY_RULES, policy_flags  # noqa: E402
import structure_policy as sp  # noqa: E402

MAP = {"P4_REWARD": "p4_reward", "P5_BREAK": "p5_break", "P7_QUALITY": "p7_groups", "P7_LOCATION": "p7_location",
       "P8_FAILED_BOS": "p8_failed_bos", "P8_NO_BOS": "p8_no_bos", "P8_OLD_BOS": "p8_old_bos"}


def _summary(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0,
         "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


def _live(summary):
    f = sp.build_structure_features(summary, None)
    return {MAP[r["id"]] for r in sp._rules(f, sp._params()) if r["id"] in MAP}


def _replay(summary):
    return {k for k, v in policy_flags(summary).items() if v}


def test_every_policy_rule_is_mapped_both_ways():
    assert set(MAP.values()) == set(POLICY_RULES)


def test_clean_setup_trips_nothing_on_both_sides():
    assert _live(_summary()) == _replay(_summary()) == set()


def test_replay_agrees_with_the_live_policy_over_a_grid_of_summaries():
    grid = itertools.product(
        ("bullish", "bearish", "range"),                                       # trend
        ("bullish", "bearish", None), (1, 4, 6, 9, 11, None), (False, True),    # bos dir / bars / failed
        ("bullish", "bearish", None), (2, 5, 8, None),                          # choch dir / bars
        (1, 2, 3, None), (40, 70, None), (101.5, 108.0, 99.0, None))            # groups, loc score, target
    n = 0
    for tr, bd, bb, bf, cd, cb, g, loc, tg in grid:
        s = _summary(ms_trend=tr, ms_bos_dir=bd, ms_bos_bars=bb, ms_bos_failed=bf, ms_choch_dir=cd, ms_choch_bars=cb, ms_groups=g, ms_loc_score=loc, ms_target=tg)
        assert _live(s) == _replay(s), s
        n += 1
    assert n > 5000


def test_thresholds_can_be_overridden_and_zero_disables():
    s = _summary(ms_target=101.5, ms_bos_dir="bearish", ms_bos_bars=2)
    assert {"p4_reward", "p5_break"} <= _replay(s)
    f = policy_flags(s, {"reward_pct_min": 0, "bearish_break_bars": 0})
    assert not f["p4_reward"] and not f["p5_break"]
    assert policy_flags(_summary(ms_bos_bars=12), {"bos_max_bars": 15})["p8_old_bos"] is False


def test_p7_defaults_group410_and_zero_switches_the_rule_off_on_both_sides():
    assert sp.DEFAULTS["groups_min"] == 2.0 and sp.DEFAULTS["loc_min"] == 0.0
    f = policy_flags(_summary(ms_groups=None, ms_loc_score=None))
    assert f["p7_groups"] is True and f["p7_location"] is False          # unknown groups still not bullish; location rule is off
    on = {"groups_min": 3, "loc_min": 55}
    assert policy_flags(_summary(ms_loc_score=54), on)["p7_location"] is True and policy_flags(_summary(ms_loc_score=55), on)["p7_location"] is False
    assert policy_flags(_summary(ms_groups=None), {"groups_min": 0})["p7_groups"] is False


# ---- group413: P8 retest / CHOCH branch, live policy and replay must agree --------------------------------------------------------------------
def _live_retest(summary, on):
    f = sp.build_structure_features(summary, None)
    p = sp._params()
    p["retest_entry"] = 1.0 if on else 0.0
    return {MAP[r["id"]] for r in sp._rules(f, p) if r["id"] in MAP}


def _replay_retest(summary, on):
    return {k for k, v in policy_flags(summary, {"retest_entry": 1.0 if on else 0.0}).items() if v}


def test_retest_defaults_are_off_and_identical_on_both_sides():
    assert sp.DEFAULTS["retest_entry"] == 0.0 and sp.DEFAULTS["retest_confirm_bars"] == 5.0
    from market_structure.backtest import POLICY_THRESHOLDS
    assert POLICY_THRESHOLDS["retest_entry"] == 0.0 and POLICY_THRESHOLDS["retest_confirm_bars"] == 5.0


def test_retest_branch_replay_agrees_with_the_live_policy_over_a_grid():
    grid = itertools.product(("bullish", "bearish", "range"), (4, 12, 30, None), (False, True), ("bullish", "bearish", None), (2, 5, 8, None),
                             (None, "bullish", "bearish"), ("open", "filled"), (95.0, 99.8, 101.0), (False, True))
    n = 0
    for tr, bb, bf, cd, cb, fvg, fst, px, golden in grid:
        s = _summary(ms_trend=tr, ms_bos_bars=bb, ms_bos_failed=bf, ms_choch_dir=cd, ms_choch_bars=cb, ms_fvg_side=fvg, ms_fvg_status=fst,
                     ms_fvg_top=100.5, ms_fvg_bottom=98.0, ms_in_golden=golden, ms_price=px)
        for on in (False, True):
            assert _live_retest(s, on) == _replay_retest(s, on), (on, s)
        n += 1
    assert n > 3000


def test_retest_branch_excuses_only_an_old_intact_bos():
    base = dict(ms_bos_bars=14, ms_choch_dir="bullish", ms_choch_bars=3, ms_fvg_side="bullish", ms_fvg_status="open", ms_fvg_top=101.0, ms_fvg_bottom=98.0)
    assert "p8_old_bos" in _replay_retest(_summary(**base), False)
    assert "p8_old_bos" not in _replay_retest(_summary(**base), True)
    assert "p8_old_bos" in _replay_retest(_summary(**dict(base, ms_choch_bars=7)), True)            # CHOCH too old
    assert "p8_old_bos" in _replay_retest(_summary(**dict(base, ms_fvg_status="filled")), True)     # zone used up
    assert "p8_old_bos" in _replay_retest(_summary(**dict(base, ms_fvg_top=90.0, ms_fvg_bottom=88.0)), True)   # not in a zone
    assert "p8_failed_bos" in _replay_retest(_summary(**dict(base, ms_bos_failed=True)), True)      # failed BOS is never excused
    assert "p8_no_bos" in _replay_retest(_summary(**dict(base, ms_bos_dir=None)), True)             # no BOS is never excused
