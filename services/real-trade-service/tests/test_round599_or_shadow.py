"""Round 599: shadow-only OR rules A and B. They record; they must never change an entry."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from entry_engine import or_shadow as o  # noqa: E402

EV = {"polarity": 1, "type": "results", "strength": 0.8, "age_min": 40}
STACK = "3 soft size penalties stacked (structure_chase, structure_room, structure_stretch) and together push the size under 0.35 of the base risk: skipped instead of lifted to the floor"
WIN = "WINDOW_NO_SETUP: only a setup that matched (S1 / S2 / S3) may enter; a plain shortlist entry waits"


def test_rule_a_needs_refusal_and_a_matched_setup():
    assert o.rule_a(STACK, {"id": "S1_CATALYST_UP"})["rule"] == "A"
    assert o.rule_a("", {"id": "S1_CATALYST_UP"}) is None
    assert o.rule_a(STACK, {"id": None, "skip": "NO_SETUP"}) is None
    assert o.rule_a(STACK, None) is None and o.rule_a(STACK, "x") is None


def test_rule_b_needs_every_condition():
    ok = o.rule_b(WIN, EV, True, "top", None, 0.5)
    assert ok and ok["rule"] == "B" and ok["strength"] == 0.8
    assert o.rule_b("", EV, True, "top") is None                                    # no refusal, nothing to record
    assert o.rule_b(WIN, None, True, "top") is None                                 # no event
    assert o.rule_b(WIN, {**EV, "polarity": -1}, True, "top") is None
    assert o.rule_b(WIN, {**EV, "strength": 0.3}, True, "top", None, 0.5) is None   # weak event
    assert o.rule_b(WIN, {**EV, "strength": None}, True, "top") is None             # unknown strength is not true
    assert o.rule_b(WIN, EV, False, "top") is None and o.rule_b(WIN, EV, None, "top") is None   # not above VWAP / unknown
    assert o.rule_b(WIN, EV, True, "mid") is None and o.rule_b(WIN, EV, True, None) is None
    assert o.rule_b(WIN, EV, True, "top", 1.4) is None                              # priced in
    assert o.rule_b(WIN, EV, True, "top", 0.9) is not None


def test_stamp_appends_keeps_other_keys_and_caps():
    hit = {"rule": "A"}
    s = o.stamp(json.dumps({"tm": {"x": 1}}), hit)
    d = json.loads(s)
    assert d["tm"] == {"x": 1} and d["or_shadow"] == [hit]
    for _ in range(6):
        s = o.stamp(s, hit)
    assert len(json.loads(s)["or_shadow"]) == 4                                     # capped, the row stays small
    assert json.loads(o.stamp(None, hit))["or_shadow"] == [hit]
    assert o.stamp("not json", hit) == "not json"                                   # never clobbers unreadable text
    assert o.stamp("[1,2]", hit) == "[1,2]" and o.stamp("{}", None) == "{}"


def test_mode_switch():
    assert o.enabled("log") and o.enabled(None) and o.enabled("LOG") and not o.enabled("off") and not o.enabled(" OFF ")


def test_entry_py_hooks_record_before_the_refusal_and_add_no_new_exit():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entry_engine", "entry.py"), encoding="utf-8").read()
    a = re.search(r"        if _stack_reject:\n(.*?)            continue\n", src, re.S).group(1)
    assert a.index("_or_shadow.rule_a") < a.index("_wait_with_preview(_stack_reject)") and 'gate_tag = "soft_stack"' in a
    b = re.search(r"        if _wns_reject:\n(.*?)            continue\n", src, re.S).group(1)
    assert b.index("_or_shadow.rule_b") < b.index("_wait_with_preview(_wns_reject)") and 'gate_tag = "no_setup_window"' in b
    # the helper only records: it must not touch gate_tag, return a value or call continue
    h = re.search(r"def _or_shadow_record\(.*?\n\n\n", src, re.S).group(0)
    assert "gate_tag" not in h and "return " not in h.replace("        return\n", "") and "continue" not in h


def test_config_defaults():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()
    assert 'or "log"' in src and "OR_SHADOW_MIN_EVENT_STRENGTH" in src
