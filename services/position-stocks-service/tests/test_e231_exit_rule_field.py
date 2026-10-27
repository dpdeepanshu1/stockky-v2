"""E231: /trades/history exposes which stagnation rule closed a trade (main._row_exit_rule)."""
import json
from types import SimpleNamespace

import main


def _r(flow):
    return SimpleNamespace(flow_json=flow)


def test_known_rules_are_returned():
    assert main._row_exit_rule(_r(json.dumps({"exit_rule": "flat_band"}))) == "flat_band"
    assert main._row_exit_rule(_r(json.dumps({"orb": {}, "exit_rule": "no_followthrough"}))) == "no_followthrough"


def test_absent_unknown_or_unreadable_is_none():
    for flow in (None, "", "not json", "[]", json.dumps({"exit_rule": "something_else"}), json.dumps({"orb": {}})):
        assert main._row_exit_rule(_r(flow)) is None
