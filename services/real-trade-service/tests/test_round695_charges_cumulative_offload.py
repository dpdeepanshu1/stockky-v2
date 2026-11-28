"""Round 695 (E717, D7c): GET /charges/{mode}/cumulative runs as one offloaded worker-thread step. Source level (main.py needs the full service stack to import)."""
from __future__ import annotations

import ast
import os

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_TREE = ast.parse(open(_MAIN, encoding="utf-8").read())


def _fn():
    return next(n for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "charges_cumulative")


def test_is_a_plain_def_behind_offload_and_keeps_its_route():
    n = _fn()
    assert isinstance(n, ast.FunctionDef)
    decs = [ast.unparse(d) for d in n.decorator_list]
    assert "_offload" in decs and any("/charges/{mode}/cumulative" in d for d in decs), decs


def test_body_has_no_await_and_no_nested_thread_hop():
    n = _fn()
    assert not any(isinstance(x, ast.Await) for x in ast.walk(n))
    assert "to_thread" not in ast.unparse(n) and "asyncio" not in ast.unparse(n)
