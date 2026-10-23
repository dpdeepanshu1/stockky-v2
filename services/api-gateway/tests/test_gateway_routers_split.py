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
        # E52: a module may define its own `logger = logging.getLogger("api-gateway")` (same logger name as main's) and import what it needs
        top_defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        top_defined |= {(a.asname or a.name).split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                assert mod != "main" and "main" not in names, f"{fname} imports main"
        for h in _handlers(tree):
            used = {x.id for x in ast.walk(h) if isinstance(x, ast.Name)}
            leaked = (used & main_names) - top_defined - {"router", "lag_router", "circuit_router", "ipo_status_router", "ipo_stop_router", "premarket_status_router", "surprise_stop_router", "bulk_cache_router", "feed_job_router", "hot_table_router", "hot_audit_router", "ipo_scan_router", "ipo_list_router", "ipo_audit_router", "ipo_add_router", "ipo_repair_router", "ipo_purge_router", "buys_edge_router", "surprise_static_router", "asyncio", "math", "HTTPException", "Request", "APIRouter"}
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
    from fastapi import APIRouter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    checked = 0
    for fname in _router_files():
        mod = __import__(f"gateway_routers.{fname[:-3]}", fromlist=["x"])
        # E49: a module can hold several routers (one per place its routes used to sit in main.py).
        routers = [v for v in vars(mod).values() if isinstance(v, APIRouter)]
        assert routers, f"{fname} defines no APIRouter"
        for router in routers:
            for r in router.routes:
                for m in r.methods:
                    checked += 1
                    assert seen[(m, r.path)] == 1, f"{m} {r.path} registered {seen[(m, r.path)]} times"
    assert checked >= 20


def test_every_router_in_a_module_is_included_in_main():
    for fname in _router_files():
        mod = fname[:-3]
        tree = ast.parse(open(os.path.join(_ROUTER_DIR, fname), encoding="utf-8").read())
        names = [t.id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                 and getattr(n.value.func, "id", "") == "APIRouter" for t in n.targets if isinstance(t, ast.Name)]
        assert names, f"{fname} defines no APIRouter"
        for name in names:
            assert f"{name} as _{name}" in _MAIN_SRC or name == "router", f"{fname}: router {name} is not imported in main.py"
            assert f"app.include_router(_{name})" in _MAIN_SRC or name == "router", f"{fname}: router {name} is not included in main.py"


def test_moved_paths_are_still_answered_by_the_moved_handler():
    """Order check: the FIRST route that matches a moved path is the moved handler. (`/data-feed/bulk-cache` is also matched by the
    pattern route `/data-feed/{symbol}`, so a router included in the wrong place would silently change which handler answers.)"""
    pytest.importorskip("fastapi")
    import main
    from fastapi import APIRouter
    from starlette.routing import Match
    for fname in _router_files():
        mod = __import__(f"gateway_routers.{fname[:-3]}", fromlist=["x"])
        for router in [v for v in vars(mod).values() if isinstance(v, APIRouter)]:
            for r in router.routes:
                for m in r.methods:
                    scope = {"type": "http", "method": m, "path": r.path, "root_path": "", "headers": []}
                    first = next(x for x in main.app.routes if getattr(x, "matches", None) and x.matches(scope)[0] == Match.FULL)
                    assert first.endpoint is r.endpoint, f"{m} {r.path} is answered by {first.endpoint.__name__}, not {r.endpoint.__name__}"
