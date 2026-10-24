"""E85: guards for gateway_support/bound_routes_j.py, the tenth batch of handlers moved out of main.py through a MainRef (first batch: E55).
Unlike batches A-I these handlers contain NESTED functions / lambdas / comprehensions. The move was scope-aware: a name that is a local of the handler or of a nested
function stays as it was, every name that resolves to main.py's globals is read as `ref.<name>` at call time (checked below with `symtable`: the only global a handler
may still read is `ref`, a builtin, or its own router). Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py.
Source checks only; the live-app test runs when fastapi is installed."""
import ast
import builtins
import os
import symtable

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_j.py"), encoding="utf-8").read()

# handler -> its route decorators (method, path), top to bottom, in main.py order (the registration order must not change)
EXPECTED = {
    "ops_wake_db_all": [("get", "/ops/wake-db-all"), ("post", "/ops/wake-db-all")],
    "system_health": [("get", "/system/health")],
    "start_scan": [("post", "/scan/start")],
    "stream_market_scan": [("get", "/api/scan/stream"), ("get", "/scan/stream")],
    "scan_watchlist": [("get", "/scan/watchlist")],
    "send_picks_to_telegram": [("post", "/notifications/send-picks")],
    "api_surprise_scan_stream": [("get", "/api/surprise/scan/stream"), ("get", "/surprise/scan/stream")],
    "api_surprise_notify_top_picks": [("post", "/surprise/notify-top-picks")],
    "catalyst_alert_scan": [("get", "/catalysts/alert"), ("post", "/catalysts/alert")],
    "stockky_hot_premarket": [("post", "/stockky-hot/premarket")],
    "stockky_hot_run": [("post", "/stockky-hot/run")],
    "api_hotpicks_notify_top_picks": [("post", "/stockky-hot/notify-top-picks")],
}

def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_j")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_the_admin_guard_on_send_picks_moved_with_the_route():
    d = _factory_handlers()["send_picks_to_telegram"].decorator_list[0]
    assert [ast.unparse(k.value) for k in d.keywords if k.arg == "dependencies"] == ["[ref.Depends(ref._require_admin)]"]


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_j(") == 1
    assert "_bound_routes_j = _build_bound_routes_j(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_j.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_j.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append(_MAIN_SRC.index(inc))
        for method, path in routes:
            assert f'@app.{method}("{path}"' not in _MAIN_SRC and f"@app.{method}('{path}'" not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert pos == sorted(pos), "routers must be included in the order the routes used to be declared"
    assert _MAIN_SRC.index("_bound_routes_j = _build_bound_routes_j(") < pos[0]
    assert _MAIN_SRC.index("from gateway_support import MainRef as _MainRef") < _MAIN_SRC.index("_bound_routes_j = _build_bound_routes_j(")


def test_every_name_the_decorators_and_signatures_need_is_bound_in_main_before_the_factory_is_built():
    build_line = _MAIN_SRC[: _MAIN_SRC.index("_bound_routes_j = _build_bound_routes_j(")].count("\n") + 1
    tree = ast.parse(_MAIN_SRC)
    bound = {}
    for n in ast.walk(tree):
        names = []
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [n.name]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in n.names]
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names = [n.id]
        for nm in names:
            bound[nm] = min(bound.get(nm, 10 ** 9), n.lineno)
    needed = set()
    for fn in _factory_handlers().values():
        for part in [*fn.decorator_list, fn.args]:   # evaluated when the factory runs: decorators, defaults, annotations
            needed |= {a.attr for a in ast.walk(part) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "ref"}
    assert needed >= {"BackgroundTasks", "Depends", "_require_admin", "Query"}
    for nm in needed:
        assert bound.get(nm, 10 ** 9) < build_line, f"{nm} is bound after the factory is built"


def test_after_the_move_a_handler_reads_no_global_except_ref_a_builtin_or_its_own_router():
    # scope-aware proof: symtable over each handler (incl. nested functions, lambdas, comprehensions); locals of nested functions are not globals
    main_bound = set()
    for n in ast.parse(_MAIN_SRC).body:
        for m in ([n] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ast.walk(n)):
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                main_bound.add(m.name)
            elif isinstance(m, ast.Name) and isinstance(m.ctx, ast.Store):
                main_bound.add(m.id)
            elif isinstance(m, (ast.Import, ast.ImportFrom)):
                main_bound |= {(a.asname or a.name).split(".")[0] for a in m.names}
    for name, fn in _factory_handlers().items():
        leaked = set()

        def walk(t):
            for s in t.get_symbols():
                if s.is_global() and s.is_referenced() and s.get_name() not in ("ref", f"{name}_router"):
                    if s.get_name() in main_bound or not hasattr(builtins, s.get_name()):
                        leaked.add(s.get_name())
            for c in t.get_children():
                walk(c)

        walk(symtable.symtable(ast.unparse(fn), "<handler>", "exec"))
        assert not leaked, f"{name} still reads main.py names without ref.: {sorted(leaked)}"


def test_factory_has_no_future_annotations_import_and_signatures_use_ref_names():
    for n in ast.parse(_FACTORY_SRC).body:
        assert not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    h = _factory_handlers()
    ann = lambda name: [ast.unparse(a.annotation) for a in h[name].args.args if a.annotation is not None]
    assert ann("start_scan") == ["bool", "bool", "ref.BackgroundTasks"]
    assert ann("stockky_hot_run") == ["ref.BackgroundTasks", "bool"]
    assert ann("catalyst_alert_scan")[0] == "ref.BackgroundTasks"


def test_live_app_registers_each_moved_route_once():
    pytest.importorskip("fastapi")
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_j, name)
