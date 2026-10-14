"""group353 (WS-5): structure policy - shadow records, caps/full apply, only tightens, unknown is never bullish, never raises."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structure_policy as sp  # noqa: E402


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0,
         "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


def test_clean_setup_untouched_in_every_mode():
    for m in ("shadow", "caps", "full"):
        r = sp.apply_policy("BUY NOW", _sum(), 100.0, mode=m)
        assert r["decision"] == "BUY NOW" and r["structure"]["rules"] == []


def test_shadow_records_but_does_not_change():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="shadow")
    assert r["decision"] == "BUY NOW" and r["structure"]["applied"] is False
    assert r["structure"]["policy_decision"] == "WAIT" and r["structure"]["rules"][0]["id"] == "P2_CHASE"


def test_caps_applies_chase_wait_and_room_prepare():
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="caps")["decision"] == "WAIT"
    r = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3), 100.0, mode="caps")
    assert r["decision"] == "PREPARE TO BUY" and r["structure"]["applied"] is True


def test_most_restrictive_rule_wins():
    r = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3, ms_stretch_atr=4.0), 100.0, mode="caps")
    assert r["decision"] == "WAIT"


def test_never_upgrades():
    for d in ("PREPARE TO BUY", "WAIT", "DO NOT BUY", "HOLD"):
        assert sp.apply_policy(d, _sum(), 100.0, mode="full")["decision"] == d


def test_unknown_is_never_bullish_and_never_blocks():
    for s in (None, {}, _sum(ms_quality="unavailable"), _sum(ms_quality="insufficient"), _sum(ms_atr=None)):
        r = sp.apply_policy("BUY NOW", s, 100.0, mode="caps")
        assert r["decision"] == "BUY NOW" and r["structure"]["no_opinion"]


def test_stale_price_drift_is_no_opinion():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=9), 140.0, mode="caps")
    assert r["decision"] == "BUY NOW" and "too old" in r["structure"]["no_opinion"]


def test_live_price_shifts_chase_and_rr():
    f = sp.build_structure_features(_sum(), 104.0)          # +2 ATR above the structure close
    assert f["live_adjusted"] and f["chase_atr"] == 2.2 and f["room_atr"] == 1.0 and f["rr"] == 0.5


def test_bearish_trend_blocks_unless_recent_bullish_choch():
    assert sp.apply_policy("BUY NOW", _sum(ms_trend="bearish"), 100.0, mode="caps")["decision"] == "WAIT"
    ok = _sum(ms_trend="bearish", ms_choch_dir="bullish", ms_choch_bars=3)
    assert sp.apply_policy("BUY NOW", ok, 100.0, mode="caps")["decision"] == "BUY NOW"


def test_rr_flags_and_failed_bos():
    assert sp.apply_policy("BUY NOW", _sum(ms_rr_flags=["no_target"]), 100.0, mode="caps")["decision"] == "WAIT"
    assert sp.apply_policy("BUY NOW", _sum(ms_bos_failed=True), 100.0, mode="caps")["decision"] == "PREPARE TO BUY"


def test_wait_for_entry_uses_demand_zone_below_price():
    s = _sum(ms_chase_atr=3, ms_ob_side="bullish", ms_ob_top=97.0, ms_ob_bottom=95.0, ms_ob_status="fresh")
    z = sp.apply_policy("BUY NOW", s, 100.0, mode="caps")["structure"]["wait_for_entry"]
    assert z == {"low": 95.0, "high": 97.0, "kind": "demand zone"}


def test_full_mode_structural_levels_only_when_sane():
    legacy = {"stop_loss": 95.0, "target": 110.0}
    r = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3), 100.0, legacy=legacy, mode="full")
    assert r["levels"] == {"stop_loss": 96.0, "target": 108.0}
    wild = sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3, ms_stop=60.0), 100.0, legacy=legacy, mode="full")
    assert wild["levels"] is None
    assert sp.apply_policy("BUY NOW", _sum(ms_room_atr=0.3), 100.0, legacy=legacy, mode="caps")["levels"] is None


def test_off_mode_and_env_mode_default(monkeypatch):
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=9), 100.0, mode="off")["decision"] == "BUY NOW"
    monkeypatch.delenv("STRUCTURE_POLICY_MODE", raising=False)
    assert sp.policy_mode() == "full"            # group354: live by default
    monkeypatch.setenv("STRUCTURE_POLICY_MODE", "bogus")
    assert sp.policy_mode() == "full"
    monkeypatch.setenv("STRUCTURE_POLICY_MODE", "shadow")
    assert sp.policy_mode() == "shadow"          # explicit opt-out still works


def test_params_override_and_bad_json(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", '{"chase_atr_max": 3.0, "zzz": 1}')
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="caps")["decision"] == "BUY NOW"
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", "{bad")
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="caps")["decision"] == "WAIT"


def test_prepare_triggers_and_summary_line():
    s = _sum(ms_bos_bars=1, ms_choch_dir="bullish", ms_choch_bars=2, ms_in_golden=True)
    r = sp.apply_policy("PREPARE TO BUY", s, 100.0, mode="shadow")
    assert {"bos_near", "choch", "entering_fib"} <= set(r["structure"]["prepare_triggers"])
    assert sp.summary_line(r["structure"]).startswith("Trend up · BOS up 1d")
    assert sp.summary_line({"features": {"quality": "unavailable"}}) is None


def test_never_raises_on_garbage():
    for s in ("x", 5, {"ms_quality": "ok", "ms_price": "abc", "ms_atr": object()}, {"ms_rr_flags": 7}):
        r = sp.apply_policy("BUY NOW", s, "bad", mode="caps")
        assert r["decision"] == "BUY NOW"
