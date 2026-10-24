"""E79: guards for gateway_support/bound_routes_e.py, the fifth batch of handlers moved out of main.py through a MainRef (first batch: E55).
Rules 1-2 (no `import main`, main names only as `ref.<name>` and every one exists) are enforced for every module in gateway_support/ by
test_gateway_support_split.py. This file adds the checks for this batch: decorators, re-exports, router order and the live app. Source checks only,
so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_e.py"), encoding="utf-8").read()

# handler -> its route decorators, in main.py order (the registration order must not change)
EXPECTED = {
    "wake_all_services": [("get", "/wake-all"), ("post", "/wake-all")],
    "momentum_movers_route": [("get", "/market/momentum-movers")],
    "get_watchlist": [("get", "/watchlist")],
    "remove_from_watchlist": [("delete", "/watchlist/{symbol}")],
    "get_symbol_events": [("get", "/events/{symbol}")],
    "get_searched_symbols": [("get", "/searched")],
    "get_last_scan": [("get", "/scan/last")],
    "get_scan_status": [("get", "/scan/status/{task_id}")],
    "market_top_gainers": [("get", "/market/top-gainers")],
    "market_top_losers": [("get", "/market/top-losers")],
    "market_most_active": [("get", "/market/most-active")],
    "data_feed_meta": [("get", "/data-feed/meta"), ("get", "/api/data-feed/meta")],
    "stockky_hot_status": [("get", "/stockky-hot/status")],
    "stockky_hot_result": [("get", "/stockky-hot/result")],
    "stockky_hot_premarket_status": [("get", "/stockky-hot/premarket/status")],
    "stockky_hot_stop": [("post", "/stockky-hot/stop")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_e")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_e(") == 1          # the factory is called once per copy of main.py
    assert "_bound_routes_e = _build_bound_routes_e(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_e.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_e.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_e = _build_bound_routes_e(") < pos[0]


def test_signatures_here_use_only_builtin_annotations():
    # Unlike batch D, no handler in this batch has a main.py name in its signature (`symbol: str`, `task_id: str`), so the factory may use
    # `from __future__ import annotations`; if a main.py name ever appears in a signature this must be revisited (see bound_routes_d.py).
    for name, fn in _factory_handlers().items():
        for a in fn.args.args:
            assert a.annotation is None or (isinstance(a.annotation, ast.Name) and a.annotation.id == "str"), (name, a.arg)
        assert not fn.args.defaults, name


def test_live_app_registers_each_moved_route_once_and_signatures_still_resolve_to_the_framework_types():
    pytest.importorskip("fastapi")
    import inspect
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_e, name)
    assert list(inspect.signature(main.remove_from_watchlist).parameters) == ["symbol"]
    assert list(inspect.signature(main.get_scan_status).parameters) == ["task_id"]
