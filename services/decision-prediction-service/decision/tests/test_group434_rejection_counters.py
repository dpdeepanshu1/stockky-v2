"""group434: structure_rejections / rr_rejections / chase_rejections counters (and their record-only *_would_reject twins)."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import structure_policy as sp  # noqa: E402


def _out(mode, rules=(), shadow=()):
    return {"decision": "WAIT", "structure": {"mode": mode, "rules": [dict(id=i, to="WAIT") for i in rules], "shadow_rules": [dict(id=i, to="WAIT") for i in shadow]}}


def test_applied_rr_and_chase_are_counted_once_each():
    sp._reset_rejection_stats_for_tests()
    sp._tally(_out("full", rules=["P2_CHASE", "P4_RR", "P4_RR"]))
    s = sp.rejection_stats()
    assert (s["considered"], s["structure_rejections"], s["rr_rejections"], s["chase_rejections"]) == (1, 1, 1, 1)
    assert s["structure_would_reject"] == s["rr_would_reject"] == s["chase_would_reject"] == 0


def test_record_only_rules_count_as_would_reject_not_rejections():
    sp._reset_rejection_stats_for_tests()
    sp._tally(_out("record", shadow=["P2_CHASE", "P7_QUALITY"]))
    s = sp.rejection_stats()
    assert (s["structure_rejections"], s["chase_rejections"], s["rr_rejections"]) == (0, 0, 0)
    assert (s["structure_would_reject"], s["chase_would_reject"], s["rr_would_reject"]) == (1, 1, 0)


def test_off_mode_and_upgrade_and_garbage_are_ignored():
    sp._reset_rejection_stats_for_tests()
    sp._tally(_out("off", rules=["P4_RR"]))
    sp._tally({"structure": {"mode": "full", "rules": [{"id": "P9_UPGRADE", "to": "BUY NOW"}]}})
    sp._tally(None)
    s = sp.rejection_stats()
    assert s["considered"] == 1 and s["structure_rejections"] == 0     # only the upgrade call was considered


def test_apply_policy_wrapper_counts_a_real_call_and_keeps_contract():
    sp._reset_rejection_stats_for_tests()
    r = sp.apply_policy("BUY NOW", None, 100.0, mode="full", require_data=True)      # no summary -> P1_NO_STRUCTURE applied
    assert r["decision"] == "WAIT"
    s = sp.rejection_stats()
    assert s["considered"] == 1 and s["structure_rejections"] == 1 and s["rr_rejections"] == 0
    r = sp.apply_policy("BUY NOW", None, 100.0, mode="record", require_data=True)    # same call, record-only
    assert r["decision"] == "BUY NOW"
    s = sp.rejection_stats()
    assert s["structure_would_reject"] == 1 and s["structure_rejections"] == 1
