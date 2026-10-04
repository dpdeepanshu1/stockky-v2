"""group101 item 7: Render-era `/health?warm=true` wake pings in the decision, prediction and training services.

api-gateway and the technical service already skip these on the always-on VM (ORACLE_DSN set; WAKE_PINGS=1/0
overrides). The three services here still sent them. Guarded two ways:
  * AST guard: every `params={"warm": "true"}` call in decision/main.py, prediction/main.py and
    training/app.py must sit under an `if` that tests `_wake_pings_enabled()`.
  * Behaviour: `/health?warm=true` on the decision service makes no downstream call when pings are off.
Run from services/decision-prediction-service/decision:  python3 -m pytest tests/test_wake_pings_gated.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FILES = ["decision/main.py", "prediction/main.py", "training/app.py"]
_counter = itertools.count()


def _tree(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _is_warm_call(node):
    if not isinstance(node, ast.Call):
        return False
    for kw in node.keywords:
        if kw.arg == "params" and isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values):
                if isinstance(k, ast.Constant) and k.value == "warm":
                    return True
    return False


def _mentions_gate(test):
    return any(isinstance(n, ast.Name) and n.id == "_wake_pings_enabled" for n in ast.walk(test))


@pytest.mark.parametrize("rel", _FILES)
def test_every_warm_ping_is_behind_the_wake_gate(rel):
    tree = _tree(rel)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    sites = [n for n in ast.walk(tree) if _is_warm_call(n)]
    if rel == "decision/main.py":
        # the /health handler loops over URLs without params={"warm"}; its guard is on `if warm and ...`
        pass
    for n in sites:
        cur, gated = n, False
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.If) and _mentions_gate(cur.test):
                gated = True
                break
        assert gated, f"{rel}:{n.lineno} sends a warm ping outside `if _wake_pings_enabled()`"


@pytest.mark.parametrize("rel", _FILES)
def test_helper_rule(rel, monkeypatch):
    src = open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8").read()
    mod = ast.parse(src)
    fn = next(n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name == "_wake_pings_enabled")
    ns = {"os": os}
    exec(compile(ast.Module([fn], []), rel, "exec"), ns)
    f = ns["_wake_pings_enabled"]
    monkeypatch.delenv("WAKE_PINGS", raising=False)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    assert f() is True                      # Render/Neon: unchanged
    monkeypatch.setenv("ORACLE_DSN", "db_tp")
    assert f() is False                     # Oracle VM: off
    monkeypatch.setenv("WAKE_PINGS", "1")
    assert f() is True
    monkeypatch.setenv("WAKE_PINGS", "0")
    monkeypatch.delenv("ORACLE_DSN")
    assert f() is False


def test_decision_health_warm_makes_no_calls_when_off(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "0")
    d = os.path.join(_ROOT, "decision")
    added = d not in sys.path
    if added:
        sys.path.insert(0, d)
    try:
        name = f"_dec_main_wake_{next(_counter)}"
        spec = importlib.util.spec_from_file_location(name, os.path.join(d, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # pragma: no cover - optional deps missing in this env
            pytest.skip(f"decision/main.py not importable here: {e}")
        import httpx

        def boom(*a, **k):
            raise AssertionError("no downstream call expected")

        monkeypatch.setattr(httpx, "get", boom)
        out = mod.health(warm=True)
        assert out["status"] == "ok" and out["warmed"] is None
    finally:
        if added:
            sys.path.remove(d)
        sys.modules.pop(name, None)
