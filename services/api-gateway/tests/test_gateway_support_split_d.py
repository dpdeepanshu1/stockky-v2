"""E78: guards for gateway_support/bound_routes_d.py, the fourth batch of handlers moved out of main.py through a MainRef (first batch: E55).
Rules 1-2 (no `import main`, main names only as `ref.<name>` and every one exists) are enforced for every module in gateway_support/ by
test_gateway_support_split.py. This file adds the checks for this batch: decorators, re-exports, router order and the live app. Source checks only,
so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_d.py"), encoding="utf-8").read()

# handler -> its route decorators, in main.py order (the registration order must not change)
EXPECTED = {
    "ops_qstash_tick": [("post", "/ops/qstash/tick"), ("get", "/ops/qstash/tick")],
    "scan_batch": [("post", "/scan/batch")],
    "api_run_premarket_feed": [("post", "/api/surprise/run-premarket-feed"), ("post", "/surprise/run-premarket-feed"), ("get", "/api/surprise/run-premarket-feed")],
    "api_surprise_repair_batch": [("post", "/api/surprise/repair-batch"), ("post", "/surprise/repair-batch")],
    "api_ipo_notify_top_picks": [("post", "/ipo/notify-top-picks"), ("post", "/surprise/ipo/notify-top-picks")],
    "stockky_hot_endpoint": [("get", "/stockky-hot")],
    "data_feed_update_single": [("post", "/api/feed/update"), ("post", "/data-feed/update")],
    "data_feed_update_batch": [("post", "/api/feed/batch"), ("post", "/data-feed/batch")],
    "data_feed_update_batch_refresh": [("post", "/api/feed/update-batch"), ("post", "/data-feed/update-batch")],
    "data_feed_resume": [("post", "/data-feed/resume"), ("post", "/api/data-feed/resume")],
    "stockky_hot_repair_batch": [("post", "/stockky-hot/repair-batch")],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_d")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_d(") == 1          # the factory is called once per copy of main.py
    assert "_bound_routes_d = _build_bound_routes_d(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_d.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_d.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_d = _build_bound_routes_d(") < pos[0]


def test_factory_has_no_future_annotations_import_because_signatures_use_main_names():
    # `request: ref.Request`, `Query(...)` defaults and `ref.BackgroundTasks` must be evaluated when the factory runs (as the original decorators did);
    # with `from __future__ import annotations` FastAPI would try to resolve the strings from this module's globals and treat the parameters as query fields.
    tree = ast.parse(_FACTORY_SRC)
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in tree.body)


def test_live_app_registers_each_moved_route_once_and_signatures_still_resolve_to_the_framework_types():
    pytest.importorskip("fastapi")
    import inspect
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_d, name)
    assert inspect.signature(main.data_feed_resume).parameters["background_tasks"].annotation is main.BackgroundTasks
    assert inspect.signature(main.scan_batch).parameters["request"].annotation is main.Request
    _lim = inspect.signature(main.api_surprise_repair_batch).parameters["limit"].default
    # pydantic >= 2.10 keeps the bound in .metadata (annotated_types.Le) instead of a .le attribute on the FieldInfo
    _le = getattr(_lim, "le", None) or next((getattr(m, "le") for m in getattr(_lim, "metadata", []) if hasattr(m, "le")), None)
    assert _le == 100
