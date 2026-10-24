"""E86: guards for gateway_support/bound_routes_k.py, the eleventh batch of handlers moved out of main.py through a MainRef (first batch: E55).
These five were left behind by E83-E85 because something pinned them; each pin was dealt with instead of worked around:
  market_trending              its nested helper called yf directly -> the yfinance last resort is now `_trending_yf_row` in main.py (already allowed there), same behaviour
  start_bulk_feed              its docstring said "yf.download" (the yfinance guard matches text) -> reworded
  data_feed_refill_additional  test_refill_additional.py pinned its import in main.py -> that test now also reads gateway_support/bound_routes*.py
  data_feed_run                test_env_flag_sweep.py pinned DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK to main.py -> same, and the sweep now also reads `ref.os.getenv`
  api_surprise_premarket_proxy used `global _SURPRISE_SCHEMA_CONFIRMED` -> reads/writes `ref._SURPRISE_SCHEMA_CONFIRMED` (MainRef writes the module global)
Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py. Source checks only; the live-app test runs when fastapi is installed."""
import ast
import builtins
import os
import symtable

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_k.py"), encoding="utf-8").read()

EXPECTED = {
    "market_trending": [("get", "/market/trending")],
    "api_surprise_premarket_proxy": [("post", "/surprise/premarket"), ("get", "/surprise/premarket")],
    "start_bulk_feed": [("post", "/data-feed/start-bulk-feed"), ("post", "/api/data-feed/start-bulk-feed"), ("post", "/api/feed/start-bulk-feed")],
    "data_feed_refill_additional": [("post", "/data-feed/refill-additional"), ("post", "/api/data-feed/refill-additional")],
    "data_feed_run": [("post", "/data-feed/run"), ("post", "/api/data-feed/run")],
}
# what is still declared in main.py, and why (each is pinned by a test or is a framework hook)
STAYS = {}   # E88: the 3 framework hooks moved to bound_routes_m.py; nothing is declared in main.py any more


def _factory_handlers():
    tree = ast.parse(_FACTORY_SRC)
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_k")
    return {n.name: n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_expected_route_lives_in_the_factory_with_the_same_decorators_on_its_own_router():
    got = _factory_handlers()
    assert set(got) == set(EXPECTED)
    for name, routes in EXPECTED.items():
        decos = [d for d in got[name].decorator_list if isinstance(d, ast.Call)]
        assert [(d.func.attr, d.args[0].value) for d in decos] == routes, name
        assert {d.func.value.id for d in decos} == {f"{name}_router"}, f"{name} must use its own router"


def test_main_reexports_each_handler_includes_each_router_in_order_and_no_longer_declares_the_routes():
    assert _MAIN_SRC.count("_build_bound_routes_k(") == 1
    assert "_bound_routes_k = _build_bound_routes_k(_MainRef(globals()))" in _MAIN_SRC
    pos = []
    for name, routes in EXPECTED.items():
        assert f"{name} = _bound_routes_k.{name}\n" in _MAIN_SRC, f"{name} is no longer importable from main"
        inc = f"app.include_router(_bound_routes_k.{name}_router)"
        assert _MAIN_SRC.count(inc) == 1, name
        pos.append((_MAIN_SRC.index(inc), name))
        for method, path in routes:
            assert f'@app.{method}("{path}"' not in _MAIN_SRC and f"@app.{method}('{path}'" not in _MAIN_SRC, f"@app.{method}({path!r}) is still declared in main.py"
    # the registration order is the order the routes were declared in main.py: market/trending, surprise/premarket, start-bulk-feed, refill-additional, run
    assert [n for _, n in sorted(pos)] == ["market_trending", "api_surprise_premarket_proxy", "start_bulk_feed", "data_feed_refill_additional", "data_feed_run"]
    assert _MAIN_SRC.index("_bound_routes_k = _build_bound_routes_k(") < min(p for p, _ in pos)
    assert _MAIN_SRC.index("from gateway_support import MainRef as _MainRef") < _MAIN_SRC.index("_bound_routes_k = _build_bound_routes_k(")


def test_every_name_the_decorators_and_signatures_need_is_bound_in_main_before_the_factory_is_built():
    build_line = _MAIN_SRC[: _MAIN_SRC.index("_bound_routes_k = _build_bound_routes_k(")].count("\n") + 1
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
    assert needed >= {"BackgroundTasks", "Request"}
    for nm in needed:
        assert bound.get(nm, 10 ** 9) < build_line, f"{nm} is bound after the factory is built"


def test_after_the_move_a_handler_reads_no_global_except_ref_a_builtin_or_its_own_router():
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


def test_no_handler_uses_a_global_statement_any_more_and_the_schema_flag_goes_through_ref():
    for name, fn in _factory_handlers().items():
        assert not any(isinstance(n, ast.Global) for n in ast.walk(fn)), name
    fn = _factory_handlers()["api_surprise_premarket_proxy"]
    src = ast.get_source_segment(_FACTORY_SRC, fn)
    assert "ref._SURPRISE_SCHEMA_CONFIRMED = True" in src and "ref._SURPRISE_SCHEMA_CONFIRMED = False" in src
    # an undefined main.py global raises AttributeError through MainRef (a bare name raised NameError), so the first-use probe must catch that
    assert "except AttributeError" in src and "except NameError" not in src


def test_market_trending_keeps_its_yfinance_last_resort_in_main_py_not_in_the_factory():
    top = {n.name: n for n in ast.parse(_MAIN_SRC).body if isinstance(n, ast.FunctionDef)}
    helper = ast.get_source_segment(_MAIN_SRC, top["_trending_yf_row"])
    assert helper.index("_direct_yf_ok()") < helper.index("resolve_ns_ticker(") < helper.index("yf.Ticker(")
    assert "ref._trending_yf_row(sym)" in ast.get_source_segment(_FACTORY_SRC, _factory_handlers()["market_trending"])
    for bad in ("yf.Ticker", "yf.download", "import yfinance", "from yfinance"):
        assert bad not in _FACTORY_SRC, bad


def test_main_declares_no_route_or_hook_any_more():
    top = [n for n in ast.parse(_MAIN_SRC).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    left = [n.name for n in top if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and getattr(d.func.value, "id", "") == "app" for d in n.decorator_list)]
    assert left == [], left


def test_factory_has_no_future_annotations_import_and_signatures_use_ref_names():
    for n in ast.parse(_FACTORY_SRC).body:
        assert not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    h = _factory_handlers()
    ann = lambda name: [ast.unparse(a.annotation) for a in h[name].args.args if a.annotation is not None]
    assert ann("api_surprise_premarket_proxy") == ["ref.Request"]
    assert ann("data_feed_run")[0] == "ref.BackgroundTasks"


def test_live_app_registers_each_moved_route_once():
    pytest.importorskip("fastapi")
    import main
    from collections import Counter
    seen = Counter((m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ()))
    for name, routes in EXPECTED.items():
        for method, path in routes:
            assert seen[(method.upper(), path)] == 1, f"{method} {path} registered {seen[(method.upper(), path)]} times"
        assert getattr(main, name) is getattr(main._bound_routes_k, name)
