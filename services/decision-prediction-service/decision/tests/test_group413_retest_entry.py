"""group413: P8 retest / CHOCH branch (STRUCTURE_RETEST_ENTRY, default off). An old but intact bullish BOS is accepted only when price retests a
live bullish zone and a bullish CHOCH confirmed it. Pure stdlib; no fixtures."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structure_policy as sp  # noqa: E402


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0,
         "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 14, "ms_bos_failed": False,
         "ms_choch_dir": "bullish", "ms_choch_bars": 3,
         "ms_fvg_side": "bullish", "ms_fvg_status": "open", "ms_fvg_top": 101.0, "ms_fvg_bottom": 98.0}
    d.update(kw)
    return d


class _Env:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_default_is_off_old_bos_stays_prepare():
    assert sp.DEFAULTS["retest_entry"] == 0.0 and sp.retest_entry_enabled() is False
    r = sp.apply_policy("BUY NOW", _sum(), 100.0)
    assert r["decision"] == "PREPARE TO BUY" and [x["id"] for x in r["structure"]["rules"]] == ["P8_OLD_BOS"]
    assert r["structure"]["retest_entry"] is None


def test_switch_on_retest_with_choch_is_buy_now_and_reported():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        assert sp.retest_entry_enabled() is True
        r = sp.apply_policy("BUY NOW", _sum(), 100.0)
    assert r["decision"] == "BUY NOW" and r["structure"]["rules"] == []
    assert r["structure"]["retest_entry"] == ["entering_fvg"]


def test_golden_pocket_and_order_block_also_count_as_a_retest():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        s = _sum(ms_fvg_side=None, ms_in_golden=True)
        assert sp.apply_policy("BUY NOW", s, 100.0)["structure"]["retest_entry"] == ["entering_fib"]
        s = _sum(ms_fvg_side=None, ms_ob_side="bullish", ms_ob_status="open", ms_ob_top=101.0, ms_ob_bottom=99.0)
        assert sp.apply_policy("BUY NOW", s, 100.0)["structure"]["retest_entry"] == ["entering_ob"]


def test_each_missing_confirmation_keeps_the_old_bos_rule():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        for kw in ({"ms_choch_bars": 7}, {"ms_choch_dir": None, "ms_choch_bars": None},
                   {"ms_fvg_status": "filled"}, {"ms_fvg_top": 90.0, "ms_fvg_bottom": 88.0}):
            r = sp.apply_policy("BUY NOW", _sum(**kw), 100.0)
            assert r["decision"] == "PREPARE TO BUY" and r["structure"]["rules"][0]["id"] == "P8_OLD_BOS", kw


def test_a_bearish_choch_is_not_a_confirmation():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        r = sp.apply_policy("BUY NOW", _sum(ms_choch_dir="bearish", ms_choch_bars=3), 100.0)
    assert r["decision"] == "WAIT" and r["structure"]["retest_entry"] is None      # P5_BREAK (fresh bearish CHOCH) decides


def test_failed_missing_or_bearish_never_excused():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        assert sp.apply_policy("BUY NOW", _sum(ms_bos_failed=True), 100.0)["structure"]["rules"][0]["id"] == "P8_FAILED_BOS"
        assert sp.apply_policy("BUY NOW", _sum(ms_bos_dir=None, ms_bos_bars=None), 100.0)["structure"]["rules"][0]["id"] == "P8_NO_BOS"
        r = sp.apply_policy("BUY NOW", _sum(ms_trend="bearish", ms_choch_dir="bullish", ms_choch_bars=3), 100.0)
        assert r["structure"]["retest_entry"] is None          # a bearish trend never takes the branch (P5_TREND / old BOS still decide)


def test_other_rules_still_apply_with_the_branch_on():
    with _Env(STRUCTURE_RETEST_ENTRY="1"):
        assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)["decision"] == "WAIT"
        assert sp.apply_policy("BUY NOW", _sum(ms_groups=1), 100.0)["decision"] == "PREPARE TO BUY"


def test_a_recent_bos_is_unchanged_by_the_switch():
    for on in ("0", "1"):
        with _Env(STRUCTURE_RETEST_ENTRY=on):
            r = sp.apply_policy("BUY NOW", _sum(ms_bos_bars=2), 100.0)
            assert r["decision"] == "BUY NOW" and r["structure"]["retest_entry"] is None


def test_params_json_can_switch_the_branch_on_and_set_the_choch_window():
    with _Env(STRUCTURE_POLICY_PARAMS_JSON='{"retest_entry": 1, "retest_confirm_bars": 8}'):
        assert sp.apply_policy("BUY NOW", _sum(ms_choch_bars=7), 100.0)["decision"] == "BUY NOW"


def test_prepare_triggers_unchanged_by_the_zone_refactor():
    f = sp.build_structure_features(_sum(ms_bos_bars=2, ms_sweep_dir="bullish", ms_sweep_bars=1, ms_in_golden=True), 100.0)
    assert sp.prepare_triggers(f) == ["bos_near", "choch", "sweep", "entering_fvg", "entering_fib"]
