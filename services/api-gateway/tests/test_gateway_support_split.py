"""E55: guards for gateway_support/ (route handlers that moved out of main.py but still read main.py helpers through a MainRef).
Rules 1-4 of gateway_support/__init__.py, checked from the source so they hold where fastapi is not installed; the live-app tests run when it is."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SUPPORT_DIR = os.path.join(_SERVICE, "gateway_support")
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()

EXPECTED = {
    "circuits_status": [("get", "/circuits")],
    "ops_rate_limits": [("get", "/ops/rate-limits"), ("get", "/api/rate-limits"), ("get", "/api/ops/rate-limits")],
    "metrics_endpoint": [("get", "/metrics")],
    "ops_qstash_publish": [("post", "/ops/qstash/publish")],
    "ready": [("get", "/ready")],
    "audit_missing_feed_data": [("get", "/api/feed/audit-missing"), ("get", "/data-feed/audit-missing"), ("get", "/api/data-feed/audit-missing")],
}


def _module_level_names(tree):
    """Names bound at module scope of main.py, including inside try/if/with blocks (qstash_client is bound in a try/except)."""
    names = set()

    def visit(stmts):
        for n in stmts:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
            for field in ("body", "orelse", "finalbody"):
                visit(getattr(n, field, []) if isinstance(n, (ast.Try, ast.If, ast.With)) else [])
            if isinstance(n, ast.Try):
                for h in n.handlers:
                    visit(h.body)

    visit(tree.body)
    return names


def _support_files():
    return sorted(f for f in os.listdir(_SUPPORT_DIR) if f.endswith(".py") and f != "__init__.py")


def _handlers(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.decorator_list:
            yield n


def test_factory_modules_never_import_main():
    for fname in _support_files():
        tree = ast.parse(open(os.path.join(_SUPPORT_DIR, fname), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                assert mod != "main" and "main" not in names, f"{fname} imports main"


def test_handlers_use_main_names_only_through_ref_and_every_ref_name_exists_in_main():
    main_tree = ast.parse(_MAIN_SRC)
    main_names = _module_level_names(main_tree)
    for fname in _support_files():
        tree = ast.parse(open(os.path.join(_SUPPORT_DIR, fname), encoding="utf-8").read())
        own = {(a.asname or a.name).split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        own |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        own |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        own |= {a.arg for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for a in n.args.args + n.args.kwonlyargs}
        own |= {"ref", "self"}
        for h in _handlers(tree):
            # E78: a name the handler itself imports or assigns (`from data_feed import extract_feed_payload` inside the body) is a local, not a main.py name
            local = {(a.asname or a.name).split(".")[0] for x in ast.walk(h) if isinstance(x, (ast.Import, ast.ImportFrom)) for a in x.names}
            local |= {x.id for x in ast.walk(h) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            for x in ast.walk(h):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in main_names and x.id not in own and x.id not in local:
                    raise AssertionError(f"{fname}:{h.name} uses main.py name {x.id!r} directly; write ref.{x.id}")
                if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "ref":
                    assert x.attr in main_names, f"{fname}:{h.name} reads ref.{x.attr} but main.py defines no such name"


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators():
    tree = ast.parse(open(os.path.join(_SUPPORT_DIR, "bound_routes.py"), encoding="utf-8").read())
    got = {}
    for h in _handlers(tree):
        got[h.name] = sorted((d.func.attr, d.args[0].value) for d in h.decorator_list if isinstance(d, ast.Call))
    for name, routes in EXPECTED.items():
        assert got.get(name) == sorted(routes), f"{name}: {got.get(name)} != {sorted(routes)}"


def test_main_reexports_each_handler_and_includes_each_router_and_no_longer_declares_the_routes():
    assert "_bound_routes = _build_bound_routes(_MainRef(globals()))" in _MAIN_SRC
    assert _MAIN_SRC.count("_build_bound_routes(") == 1, "the factory must be called once per copy of main.py"
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        for method, path in routes:
            assert f'@app.{method}("{path}")' not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    assert _MAIN_SRC.count("app.include_router(_bound_routes.") == 6


def test_factory_is_called_before_the_first_use_and_routers_keep_their_order():
    order = [n for n in ("circuits_router", "rate_limits_router", "metrics_router", "qstash_publish_router", "ready_router", "audit_missing_router")]
    pos = [_MAIN_SRC.index(f"app.include_router(_bound_routes.{r})") for r in order]
    assert pos == sorted(pos)
    assert _MAIN_SRC.index("_bound_routes = _build_bound_routes(") < pos[0]
    # the audit-missing report must stay registered before /api/feed/{symbol}
    # E87: /api/feed/{symbol} moved to gateway_support/bound_routes_l.py (data_feed_symbol), so compare against its include.
    sym = _MAIN_SRC.index("app.include_router(_bound_routes_l.data_feed_symbol_router)")
    assert pos[-1] < sym


# ------------------------------------------------------------------------------------------------------------- MainRef
def test_mainref_reads_late_writes_through_and_names_a_missing_global():
    from gateway_support import MainRef
    g = {"a": 1}
    ref = MainRef(g)
    assert ref.a == 1 and "a" in ref and "b" not in ref
    g["a"] = 2                       # a patch on the module (monkeypatch.setattr(main, "a", 2)) is seen at the next call
    assert ref.a == 2
    ref.b = 3                        # a write is a module-global write
    assert g["b"] == 3
    with pytest.raises(AttributeError) as e:
        ref.nope
    assert "nope" in str(e.value)


def test_two_refs_do_not_share_state():
    from gateway_support import MainRef
    a, b = {"x": 1}, {"x": 2}
    assert MainRef(a).x == 1 and MainRef(b).x == 2


# ------------------------------------------------------------------------------------------------------------ live app
def test_live_app_registers_each_moved_route_once_and_patches_on_main_are_seen():
    pytest.importorskip("fastapi")
    import asyncio
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes, name)
    # a patch on main is seen by the moved handler (the whole point of MainRef)
    assert main.ready().__class__.__name__ in ("dict", "JSONResponse")
    old = main._durable_cache_configured
    try:
        main._durable_cache_configured = lambda: False
        assert main.ready().status_code == 503
        main._durable_cache_configured = lambda: True
        assert main.ready() == {"ready": True}
    finally:
        main._durable_cache_configured = old
    old_q = main.qstash_client
    try:
        main.qstash_client = None
        class _Req:
            async def json(self):
                return {}
        out = asyncio.run(main.ops_qstash_publish(_Req()))
        assert out == {"ok": False, "error": "QStash not configured (set QSTASH_TOKEN)"}
    finally:
        main.qstash_client = old_q
