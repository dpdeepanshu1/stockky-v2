"""E76: guards for gateway_support/bound_routes_b.py, the second batch of handlers moved out of main.py through a MainRef (first batch: E55).
Rules 1-2 (no `import main`, main names only as `ref.<name>` and every one exists) are enforced for every module in gateway_support/ by
test_gateway_support_split.py. This file adds the checks for this batch: decorators, re-exports, router order and the live app. Source checks only,
so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_b.py"), encoding="utf-8").read()

# handler -> its route decorators, in main.py order (the registration order must not change)
EXPECTED = {
    "ops_neon_keepalive": [("get", "/ops/neon-keepalive"), ("post", "/ops/neon-keepalive")],
    "market_session": [("get", "/market/session")],
    "notifications_health": [("get", "/notifications/health")],
    "get_notification_config": [("get", "/notifications/config")],
    "training_status": [("get", "/training/status")],
    "get_training_score": [("get", "/training/score/{symbol}")],
    "ops_overnight_status": [("get", "/ops/overnight/status")],
    "ops_overnight_get_config": [("get", "/ops/overnight/config")],
    "ops_activity_status": [("get", "/ops/activity")],
    "repair_all_status": [("get", "/api/feed/repair-all/status"), ("get", "/data-feed/repair-all/status")],
    "repair_all_stop": [("post", "/api/feed/repair-all/stop"), ("post", "/data-feed/repair-all/stop")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_b")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_b(") == 1          # the factory is called once per copy of main.py
    assert "_bound_routes_b = _build_bound_routes_b(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_b.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_b.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_b = _build_bound_routes_b(") < pos[0]


def test_live_app_registers_each_moved_route_once_and_patches_on_main_are_seen():
    pytest.importorskip("fastapi")
    import asyncio
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_b, name)
    old = (main._QUOTE_LOOP_ENABLED, main._SCAN_CANCEL_FLAGS)
    try:
        main._QUOTE_LOOP_ENABLED, main._SCAN_CANCEL_FLAGS = 0, {}
        out = main.ops_activity_status()
        assert out["quote_loop_enabled"] is False and out["scan_cancel_flags"] == 0
    finally:
        main._QUOTE_LOOP_ENABLED, main._SCAN_CANCEL_FLAGS = old
    old_job = dict(main._REFILL_ALL_JOB)
    try:
        main._REFILL_ALL_JOB.clear(); main._REFILL_ALL_JOB.update({"status": "idle"})
        assert asyncio.run(main.repair_all_stop())["stopped"] is False
    finally:
        main._REFILL_ALL_JOB.clear(); main._REFILL_ALL_JOB.update(old_job)
