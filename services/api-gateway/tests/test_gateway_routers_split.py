"""E48: guards for the gateway router split (gateway_routers/). Rules 1-4 of gateway_routers/__init__.py, checked from the source so they
hold even where fastapi is not installed; the last test checks the live app when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTER_DIR = os.path.join(_SERVICE, "gateway_routers")
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()


def _main_level_names():
    names = set()
    for n in ast.parse(_MAIN_SRC).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
    return names


def _router_files():
    return sorted(f for f in os.listdir(_ROUTER_DIR) if f.endswith(".py") and f != "__init__.py")


def _handlers(tree):
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.decorator_list:
            yield n


def test_router_modules_do_not_reach_back_into_main_or_use_main_helpers():
    main_names = _main_level_names()
    for fname in _router_files():
        tree = ast.parse(open(os.path.join(_ROUTER_DIR, fname), encoding="utf-8").read())
        top_defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                assert mod != "main" and "main" not in names, f"{fname} imports main"
        for h in _handlers(tree):
            used = {x.id for x in ast.walk(h) if isinstance(x, ast.Name)}
            leaked = (used & main_names) - top_defined - {"router", "asyncio", "math", "HTTPException", "Request", "APIRouter"}
            assert not leaked, f"{fname}:{h.name} uses main.py helpers {sorted(leaked)} (tests patch those on main)"


def test_every_moved_handler_is_re_exported_and_its_router_included_in_main():
    for fname in _router_files():
        mod = fname[:-3]
        tree = ast.parse(open(os.path.join(_ROUTER_DIR, fname), encoding="utf-8").read())
        for h in _handlers(tree):
            assert h.name in _MAIN_SRC, f"{h.name} ({fname}) is no longer importable from main"
            assert f"gateway_routers.{mod} import" in _MAIN_SRC
        assert "app.include_router(" in _MAIN_SRC


def test_moved_routes_are_not_also_declared_in_main():
    for fname in _router_files():
        tree = ast.parse(open(os.path.join(_ROUTER_DIR, fname), encoding="utf-8").read())
        for h in _handlers(tree):
            for d in h.decorator_list:
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                    path = d.args[0].value
                    method = d.func.attr
                    needle = f'@app.{method}("{path}")'
                    assert needle not in _MAIN_SRC, f"{needle} is still in main.py"


def test_live_app_registers_each_moved_route_once():
    pytest.importorskip("fastapi")
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for fname in _router_files():
        mod = __import__(f"gateway_routers.{fname[:-3]}", fromlist=["router"])
        for r in mod.router.routes:
            for m in r.methods:
                assert seen[(m, r.path)] == 1, f"{m} {r.path} registered {seen[(m, r.path)]} times"
