"""E81: guards for gateway_support/bound_routes_g.py, the seventh batch of handlers moved out of main.py through a MainRef (first batch: E55).
This is the first batch whose decorators and signatures use main.py names (`dependencies=[Depends(_require_admin)]`, `NotificationChannelUpdate`, `Request`,
`Optional`, `BackgroundTasks`), so the checks differ from batch F: the factory must NOT use `from __future__ import annotations`, every such name must be written
`ref.<name>`, and every admin-guarded route must keep its guard. Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py.
Source checks only, so they run where fastapi is not installed; the live-app test runs when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_g.py"), encoding="utf-8").read()

# handler -> (method, path, guarded?) per decorator, in decorator order top to bottom (registration runs bottom-up, same as before the move)
EXPECTED = {
    "set_notification_config": [("post", "/notifications/config", True)],
    "delete_notification_channel": [("delete", "/notifications/config/{channel}", True)],
    "notifications_call_me": [("api_route", "/notifications/call/me", True)],
    "test_notification_channels": [("post", "/notifications/test", True)],
    "trigger_training": [("post", "/training/train", True)],
    "ops_circuit_reset": [("post", "/ops/circuit-reset", True), ("get", "/ops/circuit-reset", False)],   # the GET stayed open on purpose
    "ops_overnight_set_config": [("post", "/ops/overnight/config", True)],
    "ops_overnight_run": [("post", "/ops/overnight/run", True)],
    "ops_power_off": [("post", "/ops/power-off", True)],
    "ops_resume_activity": [("post", "/ops/resume-activity", True)],
    "hard_reset_database": [("post", "/data-feed/hard-reset", True), ("post", "/api/data-feed/hard-reset", True), ("post", "/api/feed/hard-reset", True)],
}


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_g")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _guarded(call):
    """True when the decorator call carries dependencies=[ref.Depends(ref._require_admin)] exactly."""
    for kw in call.keywords:
        if kw.arg == "dependencies":
            return ast.unparse(kw.value) == "[ref.Depends(ref._require_admin)]"
    return False


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_guard_included():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value, _guarded(d)) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"
        for d in decos:   # no other dependency-like kwarg may slip in unguarded (api_route keeps its methods)
            assert {k.arg for k in d.keywords} <= {"dependencies", "methods"}, name
    call_me = next(d for d in got["notifications_call_me"].decorator_list)
    assert ast.unparse(next(k.value for k in call_me.keywords if k.arg == "methods")) == "['GET', 'POST']"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_g(") == 1
    assert "_bound_routes_g = _build_bound_routes_g(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_g.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_g.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path, _ in routes:
            if method != "api_route":
                assert f'@app.{method}("{path}"' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert '@app.api_route("/notifications/call/me"' not in _MAIN_SRC
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_g = _build_bound_routes_g(") < pos[0]
    assert _MAIN_SRC.index("_bound_routes_f = _build_bound_routes_f(") < _MAIN_SRC.index("_bound_routes_g = _build_bound_routes_g(")


def test_the_one_guarded_route_left_in_main_is_still_guarded():
    # send_picks_to_telegram had a nested function and stayed in main.py until E85 moved it (scope-aware, bodies AST-identical) to bound_routes_j.py.
    # The guard must be there, in exactly one of the two places, so the route can never lose it in a move.
    moved = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_j.py"), encoding="utf-8").read()
    in_main = '@app.post("/notifications/send-picks", dependencies=[Depends(_require_admin)])' in _MAIN_SRC
    in_factory = '@send_picks_to_telegram_router.post("/notifications/send-picks", dependencies=[ref.Depends(ref._require_admin)])' in moved
    assert in_main != in_factory


def test_factory_has_no_future_annotations_import_because_decorators_and_signatures_use_main_names():
    tree = ast.parse(_FACTORY_SRC)
    for n in tree.body:
        assert not (isinstance(n, ast.ImportFrom) and n.module == "__future__"), "annotations must be evaluated when the factory runs (ref.Request, ref.Optional ...)"
    handlers = _factory_handlers()
    ann = lambda name: [ast.unparse(a.annotation) for a in handlers[name].args.args if a.annotation is not None]
    assert ann("set_notification_config") == ["ref.NotificationChannelUpdate"]
    assert ann("notifications_call_me") == ["ref.Request", "str"]
    assert ann("ops_power_off") == ["ref.BackgroundTasks"]
    assert ann("ops_overnight_set_config") == ["ref.Optional[bool]", "ref.Optional[str]", "ref.Optional[str]", "ref.Optional[int]"]


def test_defaults_stay_literals():
    for name, fn in _factory_handlers().items():
        assert all(isinstance(d, ast.Constant) for d in fn.args.defaults), name


def test_live_app_registers_each_moved_route_once_guarded_and_signatures_resolve_to_the_framework_types():
    pytest.importorskip("fastapi")
    import inspect
    import main
    import admin_guard
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path, guarded in routes:
            methods = ["GET", "POST"] if method == "api_route" else [method.upper()]
            for m in methods:
                assert seen[(m, path)] == 1, f"{m} {path} registered {seen[(m, path)]} times"
                route = next(r for r in main.app.routes if r.path == path and m in (getattr(r, "methods", None) or ()))
                deps = [d.dependency for d in route.dependencies]
                assert (admin_guard.require_admin in deps) is guarded, f"{m} {path}"   # api_route: one route object, both methods guarded
        assert getattr(main, name) is getattr(main._bound_routes_g, name)
    assert list(inspect.signature(main.ops_power_off).parameters) == ["background_tasks"]
    assert list(inspect.signature(main.ops_overnight_set_config).parameters) == ["enabled", "datafeed_time", "premarket_time", "rest_between_steps_sec"]
