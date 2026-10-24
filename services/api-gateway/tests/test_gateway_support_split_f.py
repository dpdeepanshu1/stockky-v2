"""E80: guards for gateway_support/bound_routes_f.py, the sixth batch of handlers moved out of main.py through a MainRef (first batch: E55).
Rules 1-2 (no `import main`, main names only as `ref.<name>` and every one exists) are enforced for every module in gateway_support/ by
test_gateway_support_split.py. This file adds the checks for this batch: decorators, re-exports, router order and the live app. Source checks only,
so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_f.py"), encoding="utf-8").read()

# handler -> its route decorators, in main.py order (the registration order must not change)
EXPECTED = {
    "ops_idle_tick": [("post", "/ops/idle-tick")],
    "health": [("get", "/health")],
    "wake_all_services_probe": [("post", "/wake/all")],
    "run_scan": [("get", "/scan")],
    "get_market_indices": [("get", "/market/indices")],
    "clear_universe_cache": [("delete", "/scan/universe/cache")],
    "catalyst_alert_status": [("get", "/catalysts/alert/status")],
    "ops_keepalive": [("get", "/ops/keepalive"), ("post", "/ops/keepalive")],
    "refresh_prepare_to_buy": [("post", "/data-feed/refresh-prepare-to-buy"), ("post", "/api/data-feed/refresh-prepare-to-buy"), ("post", "/api/feed/refresh-prepare-to-buy")],
    "data_feed_status": [("get", "/data-feed/status"), ("get", "/api/data-feed/status")],
    "purge_over_cap_feed_symbols": [("post", "/api/feed/purge-over-cap"), ("post", "/data-feed/purge-over-cap")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_f")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_f(") == 1          # the factory is called once per copy of main.py
    assert "_bound_routes_f = _build_bound_routes_f(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_f.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_f.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_f = _build_bound_routes_f(") < pos[0]


def test_signatures_here_use_only_builtin_annotations_and_literal_defaults():
    # No handler in this batch has a main.py name in its signature, so the factory may use `from __future__ import annotations` (as batches B, C and E).
    # If a main.py name ever appears in a signature this must be revisited (see bound_routes_d.py).
    for name, fn in _factory_handlers().items():
        for a in fn.args.args:
            assert a.annotation is None or (isinstance(a.annotation, ast.Name) and a.annotation.id in {"str", "int", "bool", "float"}), (name, a.arg)
        assert all(isinstance(d, ast.Constant) for d in fn.args.defaults), name


def test_live_app_registers_each_moved_route_once_and_signatures_still_resolve_to_the_framework_types():
    pytest.importorskip("fastapi")
    import inspect
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_f, name)
    assert list(inspect.signature(main.run_scan).parameters) == [a.arg for a in _factory_handlers()["run_scan"].args.args]
