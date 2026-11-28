"""Round 689 (E711): the WAIT paths of entry_engine/entry.py that used to be written with NO gate_tag now carry one, so scripts/gates_daily.sh can price them like the others.
Source-level (no database, no network): every `_wait(...)` / `_wait_with_preview(...)` call named below is followed (within a few statements, before the branch stores the decision) by `decision.gate_tag = "<tag>"`."""
from __future__ import annotations

import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entry_engine", "entry.py")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# first words of each message -> tag
WANT = {"Market regime score": "regime", "Regime rules:": "regime_rules", "R:R ": "rr_floor", "Even 1 share exceeds": "risk_cap", "Price too old to enter on": "stale_price",
        "No live quote": "no_quote", "No current price available": "no_quote",
        # round 690 (E712): the three rare ones
        "Source decision": "not_actionable", "Base VOLUME_SHOCK tier": "vs_base_off", "Computed stop is not below entry": "bad_stop"}


def _text(call: ast.Call) -> str:
    parts = []
    for n in ast.walk(call):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
    return "".join(parts)


def _suites():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list):
                yield body


def test_each_judgement_wait_is_tagged_before_it_is_stored():
    found = {}
    for body in _suites():
        for i, st in enumerate(body):
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", "") in ("_wait", "_wait_with_preview"):
                txt = _text(st.value)
                for head, tag in WANT.items():
                    if txt.startswith(head):
                        tags = [s.value.value for s in body[i + 1:i + 8] if isinstance(s, ast.Assign) and isinstance(s.value, ast.Constant)
                                and any(isinstance(t, ast.Attribute) and t.attr == "gate_tag" for t in s.targets)]
                        assert tags and tags[0] == tag, (head, tags)          # the tag is set right after the _wait call; the shared db.add(decision) of the branch follows it
                        found.setdefault(head, 0)
                        found[head] += 1
    assert set(found) == set(WANT), set(WANT) - set(found)


def test_the_price_drift_wait_is_tagged_too():
    src = open(SRC, encoding="utf-8").read()
    i = src.index("_wait_with_preview(drift_reason)")
    assert 'decision.gate_tag = "entry_drift"' in src[i:i + 200]


def test_tags_fit_the_column_and_the_daily_read_lists_them():
    assert all(len(t) <= 32 for t in set(WANT.values()) | {"entry_drift"})            # trade_decisions.gate_tag is VARCHAR(32)
    line = [ln for ln in open(os.path.join(ROOT, "scripts", "gates_daily.sh"), encoding="utf-8").read().splitlines() if ln.startswith("GATES=")][0]
    for tag in ("regime", "regime_rules", "entry_drift", "rr_floor", "risk_cap"):
        assert tag in line, tag
