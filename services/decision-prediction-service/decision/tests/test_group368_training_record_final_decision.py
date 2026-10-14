"""group368 (item 8): the training record is made with the FINAL decision (after multi-horizon, data-quality and the structure policy) and
carries the structure block in feature_snapshot. decision/main.py needs fastapi, so the two helpers are pulled out with ast and run alone."""
from __future__ import annotations

import ast
import copy
import os
from enum import Enum

import pytest

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_SRC = open(_MAIN, encoding="utf-8").read()


class _Decision(str, Enum):
    BUY_NOW = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    WAIT = "WAIT"
    DO_NOT_BUY = "DO NOT BUY"


class _BG:
    def __init__(self):
        self.calls = []

    def add_task(self, fn, **kw):
        self.calls.append((fn, kw))


def _record_stub(**kw):  # stands in for record_prediction_for_training
    return None


def _ns():
    tree = ast.parse(_SRC)
    want = {"_structure_snapshot", "_queue_training_record"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    assert {n.name for n in nodes} == want
    assign = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_RECORDABLE" for t in n.targets)]
    mod = ast.Module(body=assign + nodes, type_ignores=[])
    ns = {"Decision": _Decision, "os": os, "record_prediction_for_training": _record_stub}
    exec(compile(ast.fix_missing_locations(mod), _MAIN, "exec"), ns)
    return ns


def _args(**over):
    a = dict(symbol="TESTCO", decision="BUY NOW", confidence=80.0, price=100.0, entry_range={"low": 99.0, "high": 101.0}, target=110.0,
             stop_loss=95.0, market_sentiment={"classification": "NEUTRAL"}, features={"technical": 80, "rsi": 58}, event_data=None,
             fundamental_metrics=None)
    a.update(over)
    return a


def _struct(applied=False, original="BUY NOW", policy="BUY NOW", rules=()):
    return {"mode": "full", "applied": applied, "original_decision": original, "policy_decision": policy,
            "rules": [{"id": r, "to": "WAIT", "reason": "x"} for r in rules], "no_opinion": None,
            "structural_levels": {"stop": 96.0, "target": 108.0, "rr": 2.0},
            "features": {"quality": "ok", "trend": "bullish", "tqs": 70, "rr": 2.0, "junk": 1, "chase_atr": None}}


def _resp(decision="BUY NOW", **over):
    r = {"decision": decision, "combined_score": 77.5, "entry_range": {"low": 98.5, "high": 100.5}, "target": 108.0, "stop_loss": 96.0,
         "structure": _struct()}
    r.update(over)
    return r


def test_actionable_final_decision_recorded_with_structure_and_final_levels():
    ns, bg = _ns(), _BG()
    assert ns["_queue_training_record"](bg, _args(), _resp()) is True
    (fn, kw), = bg.calls
    assert fn is _record_stub and kw["decision"] == "BUY NOW"
    assert kw["confidence"] == 77.5 and kw["target"] == 108.0 and kw["stop_loss"] == 96.0
    assert kw["entry_range"] == {"low": 98.5, "high": 100.5}
    st = kw["features"]["structure"]
    assert st["rules"] == [] and st["features"] == {"quality": "ok", "trend": "bullish", "tqs": 70, "rr": 2.0}
    assert kw["features"]["technical"] == 80 and kw["features"]["rsi"] == 58


def test_do_not_buy_and_missing_structure_never_crash():
    ns, bg = _ns(), _BG()
    assert ns["_queue_training_record"](bg, _args(), _resp("DO NOT BUY")) is False
    assert ns["_queue_training_record"](bg, _args(), {"decision": "BUY NOW"}) is True          # no structure block -> still recorded
    assert "structure" not in bg.calls[0][1]["features"]
    assert ns["_queue_training_record"](None, _args(), _resp()) is False                      # no BackgroundTasks (direct call)
    assert ns["_queue_training_record"](bg, _args(), None) is False
    assert ns["_structure_snapshot"](None) is None and ns["_structure_snapshot"]({"structure": "bad"}) is None


def test_original_args_not_mutated():
    ns, bg = _ns(), _BG()
    a = _args()
    before = copy.deepcopy(a)
    ns["_queue_training_record"](bg, a, _resp("PREPARE TO BUY", target=105.0))
    assert a == before and "structure" not in a["features"]


def test_prepare_final_decision_after_multi_horizon_downgrade_is_what_is_recorded():
    ns, bg = _ns(), _BG()
    ns["_queue_training_record"](bg, _args(decision="BUY NOW"), _resp("PREPARE TO BUY"))
    assert bg.calls[0][1]["decision"] == "PREPARE TO BUY"


def test_record_is_queued_after_the_structure_policy_in_main():
    i_policy = _SRC.index("_ms_apply_policy(\n                    response.get(\"decision\")")
    i_queue = _SRC.index("_queue_training_record(background_tasks, _train_args, response)")
    i_cache = _SRC.index("_cache_set_decide(symbol, response)\n        except Exception as ce")
    assert i_policy < i_queue < i_cache
    # the old pre-policy direct call must be gone from the decide route
    assert _SRC.count("background_tasks.add_task(\n                record_prediction_for_training") == 0


def test_structure_demoted_to_wait_not_recorded_by_default(monkeypatch):
    monkeypatch.delenv("STRUCTURE_RECORD_DEMOTED", raising=False)
    ns, bg = _ns(), _BG()
    resp = _resp("WAIT", structure=_struct(applied=True, original="BUY NOW", policy="WAIT", rules=["chase"]))
    assert ns["_queue_training_record"](bg, _args(), resp) is False
    assert bg.calls == []


def test_structure_demoted_recorded_when_opted_in_with_final_decision(monkeypatch):
    monkeypatch.setenv("STRUCTURE_RECORD_DEMOTED", "1")
    ns, bg = _ns(), _BG()
    resp = _resp("WAIT", structure=_struct(applied=True, original="BUY NOW", policy="WAIT", rules=["chase"]))
    assert ns["_queue_training_record"](bg, _args(), resp) is True
    kw = bg.calls[0][1]
    assert kw["decision"] == "WAIT" and kw["features"]["structure"]["rules"] == ["chase"]
    # a plain WAIT (not a structure demotion) is still never recorded
    bg2 = _BG()
    assert ns["_queue_training_record"](bg2, _args(), _resp("WAIT")) is False
