"""group422: record-only policy modes. STRUCTURE_POLICY_MODE=record evaluates every rule and applies none; STRUCTURE_POLICY_SHADOW_RULES makes chosen
rules record-only while the rest stay live. Defaults are unchanged: with neither set the policy is live exactly as before."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structure_policy as sp  # noqa: E402
import structure_obs_hook as hook  # noqa: E402


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0,
         "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


def _ids(rs):
    return [r["id"] for r in rs]


def test_defaults_are_unchanged(monkeypatch):
    monkeypatch.delenv("STRUCTURE_POLICY_MODE", raising=False)
    monkeypatch.delenv("STRUCTURE_POLICY_SHADOW_RULES", raising=False)
    assert sp.policy_mode() == "full" and sp.shadow_rule_ids() == []
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)
    assert r["decision"] == "WAIT" and r["structure"]["applied"] is True and r["structure"]["shadow_rules"] == []
    assert r["structure"]["would_be_decision"] == "WAIT"


def test_stale_values_still_mean_live(monkeypatch):
    for m in ("shadow", "caps", "bogus", "FULL"):
        monkeypatch.setenv("STRUCTURE_POLICY_MODE", m)
        assert sp.policy_mode() == "full"
        assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)["decision"] == "WAIT"
    monkeypatch.setenv("STRUCTURE_POLICY_MODE", "record")
    assert sp.policy_mode() == "record"
    monkeypatch.setenv("STRUCTURE_POLICY_MODE", "off")
    assert sp.policy_mode() == "off"


def test_record_mode_applies_nothing_but_logs_everything():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="record", shadow_rules=[])
    s = r["structure"]
    assert r["decision"] == "BUY NOW" and r["levels"] is None
    assert s["applied"] is False and s["rules"] == [] and s["policy_decision"] == "BUY NOW"
    assert _ids(s["shadow_rules"]) == ["P2_CHASE"] and s["would_be_decision"] == "WAIT" and s["mode"] == "record"


def test_record_mode_clean_setup_and_non_buy_decisions_untouched():
    r = sp.apply_policy("BUY NOW", _sum(), 100.0, mode="record", shadow_rules=[])
    assert r["decision"] == "BUY NOW" and r["structure"]["shadow_rules"] == [] and r["structure"]["would_be_decision"] == "BUY NOW"
    for d in ("WAIT", "DO NOT BUY", "HOLD"):
        assert sp.apply_policy(d, _sum(ms_chase_atr=3.0), 100.0, mode="record", shadow_rules=[])["decision"] == d


def test_record_mode_missing_structure_never_changes_a_buy_now():
    r = sp.apply_policy("BUY NOW", {"ms_quality": "unavailable"}, 100.0, mode="record", shadow_rules=[])
    assert r["decision"] == "BUY NOW" and r["structure"]["applied"] is False
    assert _ids(r["structure"]["shadow_rules"]) == ["P1_NO_STRUCTURE"] and r["structure"]["would_be_decision"] == "WAIT"
    live = sp.apply_policy("BUY NOW", {"ms_quality": "unavailable"}, 100.0, mode="full", shadow_rules=[])
    assert live["decision"] == "WAIT" and _ids(live["structure"]["rules"]) == ["P1_NO_STRUCTURE"]


def test_per_rule_shadow_keeps_the_other_rules_live():
    s = _sum(ms_chase_atr=2.4, ms_bos_dir=None, ms_bos_bars=None)     # chase (P2) AND no break of structure (P8) both fire
    live = sp.apply_policy("BUY NOW", s, 100.0, mode="full", shadow_rules=[])["structure"]
    assert "P2_CHASE" in _ids(live["rules"])
    p8 = [r["id"] for r in live["rules"] if r["id"].startswith("P8")]
    assert p8, "the fixture should trip P8 too"
    r = sp.apply_policy("BUY NOW", s, 100.0, mode="full", shadow_rules=["P8"])
    assert r["decision"] == "WAIT" or r["decision"] == "PREPARE TO BUY"
    assert "P2_CHASE" in _ids(r["structure"]["rules"]) and not [i for i in _ids(r["structure"]["rules"]) if i.startswith("P8")]
    assert [i for i in _ids(r["structure"]["shadow_rules"]) if i.startswith("P8")]
    assert r["structure"]["applied"] is True


def test_shadowing_the_only_firing_rule_leaves_the_decision_alone():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="full", shadow_rules=["P2_CHASE"])
    s = r["structure"]
    assert r["decision"] == "BUY NOW" and s["applied"] is False and s["rules"] == []
    assert _ids(s["shadow_rules"]) == ["P2_CHASE"] and s["would_be_decision"] == "WAIT"


def test_rule_matching_is_exact_or_on_an_underscore_boundary():
    assert sp._is_shadow("P2_CHASE", ["P2"]) and sp._is_shadow("P2_CHASE", ["P2_CHASE"]) and sp._is_shadow("P2_CHASE", ["X", "P2"])
    assert not sp._is_shadow("P2_CHASE", ["P"]) and not sp._is_shadow("P2_CHASE", ["P20"]) and not sp._is_shadow("P2_CHASE", [])
    assert not sp._is_shadow("P2_CHASE", ["P2_CHASEX"])


def test_shadow_rules_env_parsing(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_SHADOW_RULES", " p7, P8 ;p1_no_structure,, ")
    assert sp.shadow_rule_ids() == ["P7", "P8", "P1_NO_STRUCTURE"]
    monkeypatch.setenv("STRUCTURE_POLICY_SHADOW_RULES", "P2")
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)         # env is picked up when shadow_rules is not passed
    assert r["decision"] == "BUY NOW" and _ids(r["structure"]["shadow_rules"]) == ["P2_CHASE"]
    monkeypatch.setenv("STRUCTURE_POLICY_SHADOW_RULES", "")
    assert sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0)["decision"] == "WAIT"


def test_off_mode_ignores_shadow_rules():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="off", shadow_rules=["P2"])
    assert r["decision"] == "BUY NOW" and r["structure"]["shadow_rules"] == [] and r["structure"]["no_opinion"] == "policy off"


def test_prepare_hard_rules_split_and_p9_never_upgrades_in_record_mode(monkeypatch):
    monkeypatch.setenv("STRUCTURE_PREPARE_UPGRADE", "1")
    bad = sp.apply_policy("PREPARE TO BUY", _sum(ms_chase_atr=2.4), 100.0, mode="record", shadow_rules=[])
    assert bad["decision"] == "PREPARE TO BUY" and bad["structure"]["applied"] is False
    assert bad["structure"]["would_be_decision"] == "WAIT" and bad["structure"]["shadow_rules"]
    live = sp.apply_policy("PREPARE TO BUY", _sum(ms_chase_atr=2.4), 100.0, mode="full", shadow_rules=[])
    assert live["decision"] == "WAIT"
    shadowed = sp.apply_policy("PREPARE TO BUY", _sum(ms_chase_atr=2.4), 100.0, mode="full", shadow_rules=["P2"])
    assert shadowed["decision"] == "PREPARE TO BUY" and shadowed["structure"]["would_be_decision"] == "WAIT"
    clean = sp.apply_policy("PREPARE TO BUY", _sum(), 100.0, mode="record", shadow_rules=[])
    assert clean["decision"] == "PREPARE TO BUY" and clean["structure"]["applied"] is False


def test_a_shadowed_rule_blocks_a_p9_upgrade(monkeypatch):
    monkeypatch.setenv("STRUCTURE_PREPARE_UPGRADE", "1")
    # a record-only rule that fired still blocks a LOOSENING upgrade
    r = sp.apply_policy("PREPARE TO BUY", _sum(ms_groups=1), 100.0, mode="full", shadow_rules=["P7"])
    assert r["decision"] != "BUY NOW"


def test_hook_logs_shadow_rules_and_would_be():
    resp = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="record", shadow_rules=[])
    response = {"decision": "BUY NOW", "close": 100.0, "structure": resp["structure"]}
    response["structure"]["features"] = {**resp["structure"]["features"], "quality": "ok", "asof": "2026-10-16", "ver": "t", "hash": "h"}
    p = hook.build_payload("TEST", response)
    assert p is not None and p["policy_mode"] == "record" and p["policy_applied"] is False
    assert p["rules"] == ["shadow:P2_CHASE"] and p["features"]["would_be"] == "WAIT" and p["final_decision"] == "BUY NOW"
    assert all(len(x) <= 24 for x in p["rules"])


def test_hook_unchanged_for_live_policy():
    resp = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4), 100.0, mode="full", shadow_rules=[])
    resp["structure"]["features"] = {**resp["structure"]["features"], "quality": "ok", "asof": "2026-10-16", "ver": "t", "hash": "h"}
    p = hook.build_payload("TEST", {"decision": "WAIT", "close": 100.0, "structure": resp["structure"]})
    assert p["rules"] == ["P2_CHASE"] and p["policy_applied"] is True and p["features"]["would_be"] == "WAIT"


# ── group423: strictly independent groups (P7X_INDEP), off by default, record-only first ─────────────────────────────────────────────
def test_group423_default_is_off_and_changes_nothing(monkeypatch):
    monkeypatch.delenv("STRUCTURE_POLICY_PARAMS_JSON", raising=False)
    assert sp.DEFAULTS["groups_strict_min"] == 0.0
    r = sp.apply_policy("BUY NOW", _sum(ms_groups_strict=0), 100.0, shadow_rules=[])
    assert r["decision"] == "BUY NOW" and r["structure"]["rules"] == [] and r["structure"]["shadow_rules"] == []
    assert sp.apply_policy("BUY NOW", _sum(), 100.0, shadow_rules=[])["decision"] == "BUY NOW"       # field absent (older engine): still fine when off


def test_group423_enabled_live_downgrades_to_prepare(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", '{"groups_strict_min": 2}')
    r = sp.apply_policy("BUY NOW", _sum(ms_groups_strict=1), 100.0, shadow_rules=[])
    assert r["decision"] == "PREPARE TO BUY" and _ids(r["structure"]["rules"]) == ["P7X_INDEP"]
    assert sp.apply_policy("BUY NOW", _sum(ms_groups_strict=2), 100.0, shadow_rules=[])["decision"] == "BUY NOW"
    unk = sp.apply_policy("BUY NOW", _sum(), 100.0, shadow_rules=[])          # unknown is never bullish
    assert unk["decision"] == "PREPARE TO BUY" and "unknown" in unk["structure"]["rules"][0]["reason"]


def test_group423_shadow_p7x_records_without_touching_the_decision(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", '{"groups_strict_min": 2}')
    r = sp.apply_policy("BUY NOW", _sum(ms_groups_strict=1), 100.0, shadow_rules=["P7X"])
    assert r["decision"] == "BUY NOW" and r["structure"]["applied"] is False
    assert _ids(r["structure"]["shadow_rules"]) == ["P7X_INDEP"] and r["structure"]["would_be_decision"] == "PREPARE TO BUY"
    # P7 (the loose count) does NOT cover P7X: separate switches
    r2 = sp.apply_policy("BUY NOW", _sum(ms_groups_strict=1), 100.0, shadow_rules=["P7"])
    assert r2["decision"] == "PREPARE TO BUY"


def test_group423_feature_is_carried_to_the_features_and_the_hook(monkeypatch):
    r = sp.apply_policy("BUY NOW", _sum(ms_groups_strict=3), 100.0, mode="full", shadow_rules=[])
    assert r["structure"]["features"]["groups_strict"] == 3
    r["structure"]["features"].update({"quality": "ok", "asof": "2026-10-16", "ver": "t", "hash": "h"})
    p = hook.build_payload("TEST", {"decision": "BUY NOW", "close": 100.0, "structure": r["structure"]})
    assert p["features"]["groups_strict"] == 3
