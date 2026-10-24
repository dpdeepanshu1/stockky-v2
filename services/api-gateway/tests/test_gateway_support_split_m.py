"""E88: guards for gateway_support/bound_routes_m.py, the thirteenth and last batch of handlers moved out of main.py through a MainRef (first batch: E55).
These three were the framework hooks that E86/E87 left behind; main.py now declares no `@app.<...>` function at all:
  add_cors_header              was `@app.middleware("http")`          -> main.py registers it with `app.add_middleware(BaseHTTPMiddleware, dispatch=...)` (what that decorator does), at the same place
  universal_exception_handler  was `@app.exception_handler(Exception)` -> main.py registers it with `app.add_exception_handler(Exception, ...)` (what that decorator does), at the same place
  websocket_endpoint           was `@app.websocket("/ws")`            -> its own router, included where the route was
The `app.add_middleware(CORSMiddleware, ...)` configuration stays in main.py (it is setup, not a handler), and the middleware order is unchanged.
Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py. Source checks only; the live-app test runs when fastapi is installed."""
import ast
import builtins
import os
import symtable

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_m.py"), encoding="utf-8").read()
_NAMES = ["add_cors_header", "universal_exception_handler", "websocket_endpoint"]


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_m")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_factory_holds_the_three_hooks_and_only_the_websocket_has_a_router():
    got = _factory_handlers()
    assert set(got) == set(_NAMES)
    assert got["add_cors_header"].decorator_list == [] and got["universal_exception_handler"].decorator_list == []
    (d,) = got["websocket_endpoint"].decorator_list
    assert (d.func.attr, d.func.value.id, d.args[0].value) == ("websocket", "websocket_endpoint_router", "/ws")
    assert isinstance(got["add_cors_header"], ast.AsyncFunctionDef) and isinstance(got["universal_exception_handler"], ast.AsyncFunctionDef)


def test_main_reexports_each_hook_and_registers_it_exactly_as_the_decorator_did():
    assert _MAIN_SRC.count("_build_bound_routes_m(") == 1
    assert "_bound_routes_m = _build_bound_routes_m(_MainRef(globals()))" in _MAIN_SRC
    for name in _NAMES:
        assert f"{name} = _bound_routes_m.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
    assert _MAIN_SRC.count("app.add_middleware(_BaseHTTPMiddleware, dispatch=add_cors_header)") == 1
    assert _MAIN_SRC.count("app.add_exception_handler(Exception, universal_exception_handler)") == 1
    assert _MAIN_SRC.count("app.include_router(_bound_routes_m.websocket_endpoint_router)") == 1
    assert "from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware" in _MAIN_SRC
    for old in ('@app.middleware("http")', "@app.exception_handler(Exception)", '@app.websocket("/ws")'):
        assert old not in _MAIN_SRC, old


def test_middleware_order_is_unchanged_cors_config_first_then_the_wildcard_stamp_then_the_error_handler():
    cors = _MAIN_SRC.index("app.add_middleware(\n    CORSMiddleware,")
    stamp = _MAIN_SRC.index("app.add_middleware(_BaseHTTPMiddleware, dispatch=add_cors_header)")
    err = _MAIN_SRC.index("app.add_exception_handler(Exception, universal_exception_handler)")
    build = _MAIN_SRC.index("_bound_routes_m = _build_bound_routes_m(")
    assert cors < build < stamp < err
    assert _MAIN_SRC.index("from gateway_support import MainRef as _MainRef") < build


def test_every_name_the_signature_needs_is_bound_in_main_before_the_factory_is_built():
    build_line = _MAIN_SRC[: _MAIN_SRC.index("_bound_routes_m = _build_bound_routes_m(")].count("\n") + 1
    bound = {}
    for n in ast.walk(ast.parse(_MAIN_SRC)):
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
    assert "WebSocket" in needed
    for nm in needed:
        assert bound.get(nm, 10 ** 9) < build_line, f"{nm} is bound after the factory is built"


def test_after_the_move_a_hook_reads_no_global_except_ref_a_builtin_or_its_own_router():
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

        src = ast.unparse(fn)
        walk(symtable.symtable(src, "<hook>", "exec"))
        assert not leaked, f"{name} still reads main.py names without ref.: {sorted(leaked)}"
        assert not any(isinstance(n, ast.Global) for n in ast.walk(fn)), name


def test_the_cors_wildcard_stamp_and_the_websocket_origin_check_still_go_through_cors_policy():
    got = _factory_handlers()
    cors = ast.get_source_segment(_FACTORY_SRC, got["add_cors_header"])
    assert "ref._cors_policy.is_wildcard()" in cors and "ref._cors_policy.WILDCARD_HEADERS" in cors
    err = ast.get_source_segment(_FACTORY_SRC, got["universal_exception_handler"])
    assert "status_code=500" in err and "ref._cors_policy.response_headers(request)" in err
    ws = ast.get_source_segment(_FACTORY_SRC, got["websocket_endpoint"])
    assert "ref._cors_policy.origin_allowed(_ws_origin)" in ws and "code=1008" in ws


def test_main_declares_no_route_or_hook_any_more():
    top = [n for n in ast.parse(_MAIN_SRC).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    left = [n.name for n in top if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and getattr(d.func.value, "id", "") == "app" for d in n.decorator_list)]
    assert left == [], left


def test_factory_has_no_future_annotations_import_and_the_websocket_signature_uses_a_ref_name():
    for n in ast.parse(_FACTORY_SRC).body:
        assert not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    (arg,) = _factory_handlers()["websocket_endpoint"].args.args
    assert ast.unparse(arg.annotation) == "ref.WebSocket"


def test_live_app_registers_the_websocket_once_and_the_hooks_once():
    pytest.importorskip("fastapi")
    import main
    assert sum(1 for r in main.app.routes if getattr(r, "path", None) == "/ws") == 1
    assert main.app.exception_handlers.get(Exception) is main.universal_exception_handler
    assert sum(1 for m in main.app.user_middleware if getattr(m, "kwargs", {}).get("dispatch") is main.add_cors_header) == 1
    assert getattr(main, "websocket_endpoint") is getattr(main._bound_routes_m, "websocket_endpoint")
