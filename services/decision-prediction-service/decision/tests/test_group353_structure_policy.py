"""group353/370 (WS-5): structure policy - always live, strict BUY NOW requirements (P7/P8), PREPARE recompute + P9 upgrade, unknown is never bullish, never raises."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structure_policy as sp  # noqa: E402


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0,
         "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


def test_clean_setup_untouched():
    r = sp.apply_policy("BUY NOW", _sum(), 100.0)
    assert r["decision"] == "BUY NOW" and r["structure"]["rules"] == []


def test_a_violation_is_applied_immediately_there_is_no_record_only_mode():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)
    assert r["decision"] == "WAIT" and r["structure"]["applied"] is True and r["structure"]["rules"][0]["id"] == "P2_CHASE"
    for m in ("shadow", "caps", "full", "bogus"):
        assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode=m)["decision"] == "WAIT"
    assert "shadow" not in sp.MODES and "caps" not in sp.MODES


def test_room_is_prepare_and_most_restrictive_rule_wins():
    r = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3), 100.0)
    assert r["decision"] == "PREPARE TO BUY" and r["structure"]["applied"] is True
    assert sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3, ms_stretch_atr=4.0), 100.0)["decision"] == "WAIT"


def test_wait_and_do_not_buy_are_never_touched():
    for d in ("WAIT", "DO NOT BUY", "HOLD"):
        assert sp.apply_policy(d, _sum(), 100.0)["decision"] == d


def test_unknown_is_never_bullish_and_never_blocks():
    for s in (None, {}, _sum(ms_quality="unavailable"), _sum(ms_quality="insufficient"), _sum(ms_atr=None)):
        r = sp.apply_policy("BUY NOW", s, 100.0)
        assert r["decision"] == "BUY NOW" and r["structure"]["no_opinion"]


def test_stale_price_drift_is_no_opinion():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=9), 140.0)
    assert r["decision"] == "BUY NOW" and "too old" in r["structure"]["no_opinion"]


def test_live_price_shifts_chase_and_rr():
    f = sp.build_structure_features(_sum(), 104.0)
    assert f["live_adjusted"] and f["chase_atr"] == 2.2 and f["room_atr"] == 1.0 and f["rr"] == 0.5


def test_bearish_trend_blocks_unless_recent_bullish_choch():
    assert sp.apply_policy("BUY NOW", _sum(ms_trend="bearish"), 100.0)["decision"] == "WAIT"
    ok = _sum(ms_trend="bearish", ms_choch_dir="bullish", ms_choch_bars=3)
    assert sp.apply_policy("BUY NOW", ok, 100.0)["decision"] == "BUY NOW"


def test_rr_flags():
    assert sp.apply_policy("BUY NOW", _sum(ms_rr_flags=["no_target"]), 100.0)["decision"] == "WAIT"


def test_p7_needs_three_independent_groups_and_location_55():
    assert sp.apply_policy("BUY NOW", _sum(ms_groups=2), 100.0)["decision"] == "PREPARE TO BUY"
    assert sp.apply_policy("BUY NOW", _sum(ms_groups=3), 100.0)["decision"] == "BUY NOW"
    r = sp.apply_policy("BUY NOW", _sum(ms_loc_score=54), 100.0)
    assert r["decision"] == "PREPARE TO BUY" and r["structure"]["rules"][0]["id"] == "P7_LOCATION"
    assert sp.apply_policy("BUY NOW", _sum(ms_loc_score=55), 100.0)["decision"] == "BUY NOW"
    assert sp.apply_policy("BUY NOW", _sum(ms_groups=None), 100.0)["decision"] == "PREPARE TO BUY"
    assert sp.apply_policy("BUY NOW", _sum(ms_loc_score=None), 100.0)["decision"] == "PREPARE TO BUY"


def test_p8_needs_a_non_failed_bullish_bos_within_10_bars():
    assert sp.apply_policy("BUY NOW", _sum(ms_bos_bars=10), 100.0)["decision"] == "BUY NOW"
    r = sp.apply_policy("BUY NOW", _sum(ms_bos_bars=11), 100.0)
    assert r["decision"] == "PREPARE TO BUY" and r["structure"]["rules"][0]["id"] == "P8_OLD_BOS"
    r = sp.apply_policy("BUY NOW", _sum(ms_bos_failed=True), 100.0)
    assert r["decision"] == "PREPARE TO BUY" and r["structure"]["rules"][0]["id"] == "P8_FAILED_BOS"
    for kw in ({"ms_bos_dir": None, "ms_bos_bars": None}, {"ms_bos_dir": "bearish"}, {"ms_bos_bars": None}):
        r = sp.apply_policy("BUY NOW", _sum(**kw), 100.0)
        assert r["decision"] == "PREPARE TO BUY" and r["structure"]["rules"][0]["id"] == "P8_NO_BOS", kw


def test_wait_for_entry_uses_demand_zone_below_price():
    s = _sum(ms_chase_atr=3, ms_ob_side="bullish", ms_ob_top=97.0, ms_ob_bottom=95.0, ms_ob_status="fresh")
    z = sp.apply_policy("BUY NOW", s, 100.0)["structure"]["wait_for_entry"]
    assert z == {"low": 95.0, "high": 97.0, "kind": "demand zone"}


def test_structural_levels_only_when_sane():
    legacy = {"stop_loss": 95.0, "target": 110.0}
    r = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3), 100.0, legacy=legacy)
    assert r["levels"] == {"stop_loss": 96.0, "target": 108.0}
    wild = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3, ms_stop=60.0), 100.0, legacy=legacy)
    assert wild["levels"] is None
    clean = sp.apply_policy("BUY NOW", _sum(), 100.0, legacy=legacy)
    assert clean["decision"] == "BUY NOW" and clean["levels"] == {"stop_loss": 96.0, "target": 108.0}


def test_off_is_the_only_switch_and_env_default_is_live(monkeypatch):
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=9), 100.0, mode="off")["decision"] == "BUY NOW"
    monkeypatch.delenv("STRUCTURE_POLICY_MODE", raising=False)
    assert sp.policy_mode() == "full"
    for v in ("bogus", "shadow", "caps"):
        monkeypatch.setenv("STRUCTURE_POLICY_MODE", v)
        assert sp.policy_mode() == "full"
    monkeypatch.setenv("STRUCTURE_POLICY_MODE", "off")
    assert sp.policy_mode() == "off"


def test_params_override_and_bad_json(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", '{"chase_atr_max": 3.0, "zzz": 1}')
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)["decision"] == "BUY NOW"
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", "{bad")
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)["decision"] == "WAIT"


def test_prepare_triggers_and_summary_line():
    s = _sum(ms_bos_bars=1, ms_choch_dir="bullish", ms_choch_bars=2, ms_in_golden=True, ms_groups=2)
    r = sp.apply_policy("PREPARE TO BUY", s, 100.0)
    assert {"bos_near", "choch", "entering_fib"} <= set(r["structure"]["prepare_triggers"])
    assert sp.summary_line(r["structure"]).startswith("Trend up · BOS up 1d")
    assert sp.summary_line({"features": {"quality": "unavailable"}}) is None


def test_prepare_arithmetic_is_active_hard_rule_failure_goes_to_wait():
    r = sp.apply_policy("PREPARE TO BUY", _sum(), 108.0)
    assert r["decision"] == "WAIT" and r["structure"]["applied"] is True
    assert sp.apply_policy("PREPARE TO BUY", _sum(ms_groups=2), 100.0)["decision"] == "PREPARE TO BUY"


def test_p9_upgrade_prepare_to_buy_now_when_trigger_and_all_requirements_met():
    r = sp.apply_policy("PREPARE TO BUY", _sum(ms_bos_bars=1), 100.0, legacy={"stop_loss": 95.0, "target": 110.0})
    assert r["decision"] == "BUY NOW" and r["structure"]["applied"] is True and r["structure"]["rules"][0]["id"] == "P9_UPGRADE"
    assert r["levels"] == {"stop_loss": 96.0, "target": 108.0}
    assert sp.apply_policy("PREPARE TO BUY", _sum(ms_bos_bars=8), 100.0)["decision"] == "PREPARE TO BUY"
    assert sp.apply_policy("PREPARE TO BUY", _sum(ms_bos_bars=1, ms_groups=2), 100.0)["decision"] == "PREPARE TO BUY"
    assert sp.apply_policy("PREPARE TO BUY", _sum(ms_bos_bars=1), 100.0, allow_upgrade=False)["decision"] == "PREPARE TO BUY"


def test_p9_upgrade_kill_switch(monkeypatch):
    monkeypatch.setenv("STRUCTURE_PREPARE_UPGRADE", "0")
    assert sp.apply_policy("PREPARE TO BUY", _sum(ms_bos_bars=1), 100.0)["decision"] == "PREPARE TO BUY"


def test_never_raises_on_garbage():
    for s in ("x", 5, {"ms_quality": "ok", "ms_price": "abc", "ms_atr": object()}, {"ms_rr_flags": 7}):
        for d in ("BUY NOW", "PREPARE TO BUY"):
            r = sp.apply_policy(d, s, "bad")
            assert r["decision"] == d

def test_asof_epoch_becomes_iso_date_and_hash_is_carried():
    """The engine reports ms_asof as epoch seconds; the observation row needs an ISO date (asof_date was stored NULL) and the params hash."""
    assert sp._asof_iso(1791936000.0) == "2026-10-14" and sp._asof_iso(1791916200) == "2026-10-14"
    assert sp._asof_iso("2026-10-14") == "2026-10-14" and sp._asof_iso(None) is None and sp._asof_iso("x") is None and sp._asof_iso(True) is None
    f = sp.build_structure_features({"ms_quality": "ok", "ms_asof": 1791936000.0, "ms_hash": "ab12cd34", "ms_ver": "1.0.0"}, None)
    assert f["asof"] == "2026-10-14" and f["hash"] == "ab12cd34" and f["ver"] == "1.0.0"
