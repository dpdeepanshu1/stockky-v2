"""E77: guards for gateway_support/bound_routes_c.py, the third batch of handlers moved out of main.py through a MainRef (first batch: E55).
Rules 1-2 (no `import main`, main names only as `ref.<name>` and every one exists) are enforced for every module in gateway_support/ by
test_gateway_support_split.py. This file adds the checks for this batch: decorators, re-exports, router order and the live app. Source checks only,
so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_c.py"), encoding="utf-8").read()

# handler -> its route decorators, in main.py order (the registration order must not change)
EXPECTED = {
    "stock_structure": [("get", "/stock/{symbol}/structure")],
    "cancel_scan": [("post", "/scan/cancel/{task_id}")],
    "scan_stop_all": [("post", "/scan/stop-all")],
    "get_scan_universe": [("get", "/scan/universe")],
    "get_universe": [("get", "/api/universe"), ("get", "/universe")],
    "api_surprise_audit": [("get", "/api/surprise/audit"), ("get", "/surprise/audit")],
    "api_surprise_scan": [("get", "/api/surprise/scan"), ("get", "/surprise/scan")],
    "api_evaluate_price_alerts": [("post", "/api/price-alerts/evaluate"), ("post", "/price-alerts/evaluate")],
    "repair_single_stock": [("post", "/api/feed/repair-single/{symbol}"), ("post", "/data-feed/repair-single/{symbol}"), ("post", "/api/data-feed/repair-single/{symbol}"), ("post", "/feed/repair-single/{symbol}")],
    "repair_batch_missing": [("post", "/api/feed/repair-batch"), ("post", "/data-feed/repair-batch")],
    "data_feed_stop": [("post", "/data-feed/stop"), ("post", "/api/data-feed/stop")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_c")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_c(") == 1          # the factory is called once per copy of main.py
    assert "_bound_routes_c = _build_bound_routes_c(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_c.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_c.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_c = _build_bound_routes_c(") < pos[0]


def test_live_app_registers_each_moved_route_once_and_patches_on_main_are_seen():
    pytest.importorskip("fastapi")
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_c, name)
    # a patch applied on main AFTER startup is seen by the moved handler (the MainRef reads the live module global)
    old_flags, old_kv = main._SCAN_CANCEL_FLAGS, dict(main._mem_kv)
    try:
        main._SCAN_CANCEL_FLAGS = set()
        main._mem_kv.clear()
        out = main.scan_stop_all()
        assert out["stopped"] == 0 and "__ALL__" in main._SCAN_CANCEL_FLAGS
    finally:
        main._SCAN_CANCEL_FLAGS = old_flags
        main._mem_kv.clear(); main._mem_kv.update(old_kv)
