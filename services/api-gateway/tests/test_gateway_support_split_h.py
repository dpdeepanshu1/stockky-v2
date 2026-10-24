"""E83: guards for gateway_support/bound_routes_h.py, the eighth batch of handlers moved out of main.py through a MainRef (first batch: E55).
Like batch G, signatures use main.py names (`WatchlistUpdate`, `Request`, `BackgroundTasks`, `Optional`), so the factory has no `from __future__ import annotations`.
Unlike the earlier batches most of these handlers sit ABOVE the old build line, so main.py builds this factory right before the first include (checked below).
Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py. Source checks only; the live-app test runs when fastapi is installed."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_h.py"), encoding="utf-8").read()

# handler -> its route decorators (method, path), top to bottom, in main.py order (the registration order must not change)
EXPECTED = {
    "root": [("get", "/")],
    "ops_rate_limits_event": [("post", "/ops/rate-limits/event")],
    "ops_check_alert": [("post", "/ops/check-alert")],
    "ops_refresh_static_params": [("post", "/ops/refresh-static-params")],
    "market_history": [("get", "/market/history/{symbol}")],
    "ops_db_status": [("get", "/ops/db-status")],
    "set_watchlist": [("post", "/watchlist")],
    "add_to_watchlist": [("post", "/watchlist/add")],
    "run_scan_post": [("post", "/scan")],
    "training_other_proxy": [("api_route", "/training/{path:path}")],
    "repair_all_missing": [("post", "/api/feed/repair-all"), ("post", "/data-feed/repair-all")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_h")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"
    proxy = got["training_other_proxy"].decorator_list[0]
    assert ast.unparse(next(k.value for k in proxy.keywords if k.arg == "methods")) == "['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_h(") == 1
    assert "_bound_routes_h = _build_bound_routes_h(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_h.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_h.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}"' not in _MAIN_SRC and f"@app.{method}('{path}'" not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_h = _build_bound_routes_h(") < pos[0]
    # MainRef must be imported before the (earlier-than-before) build line
    assert _MAIN_SRC.index("from gateway_support import MainRef as _MainRef") < _MAIN_SRC.index("_bound_routes_h = _build_bound_routes_h(")


def test_every_name_the_decorators_and_signatures_need_is_bound_in_main_before_the_factory_is_built():
    # The factory evaluates `ref.WatchlistUpdate`, `ref.Request`, ... when it is built, so they must exist by then (class / import above the build line).
    build_line = _MAIN_SRC[: _MAIN_SRC.index("_bound_routes_h = _build_bound_routes_h(")].count("\n") + 1
    tree = ast.parse(_MAIN_SRC)
    bound = {}
    for n in ast.walk(tree):
        names = []
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [n.name]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in n.names]
        for nm in names:
            bound[nm] = min(bound.get(nm, 10 ** 9), n.lineno)
    needed = set()
    for fn in _factory_handlers().values():
        for part in [*fn.decorator_list, fn.args]:
            needed |= {a.attr for a in ast.walk(part) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "ref"}
    assert needed >= {"WatchlistUpdate", "Request", "BackgroundTasks", "Optional"}
    for nm in needed:
        assert bound.get(nm, 10 ** 9) < build_line, f"{nm} is bound after the factory is built"


def test_factory_has_no_future_annotations_import_and_signatures_use_ref_names():
    for n in ast.parse(_FACTORY_SRC).body:
        assert not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    h = _factory_handlers()
    ann = lambda name: [ast.unparse(a.annotation) for a in h[name].args.args if a.annotation is not None]
    assert ann("set_watchlist") == ["ref.WatchlistUpdate"] == ann("add_to_watchlist")
    assert ann("run_scan_post") == ["bool", "ref.Optional[bool]", "ref.BackgroundTasks"]
    assert ann("training_other_proxy") == ["str", "ref.Request"]
    assert ann("repair_all_missing") == ["ref.BackgroundTasks", "int"]


def test_live_app_registers_each_moved_route_once_and_signatures_resolve_to_the_framework_types():
    pytest.importorskip("fastapi")
    import inspect
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            for m in (["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"] if method == "api_route" else [method.upper()]):
                assert seen[(m, path)] == 1, f"{m} {path} registered {seen[(m, path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_h, name)
    assert list(inspect.signature(main.run_scan_post).parameters) == [a.arg for a in _factory_handlers()["run_scan_post"].args.args]
    assert inspect.signature(main.set_watchlist).parameters["update"].annotation is main.WatchlistUpdate
