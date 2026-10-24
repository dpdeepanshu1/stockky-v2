"""E84: guards for gateway_support/bound_routes_i.py, the ninth batch moved out of main.py through a MainRef: only `GET /quote/{symbol}` (proxy_quote).
It has its own factory file on purpose: the handler calls `yf.Ticker` as the last fallback, and tests/test_yfinance_callers_guard.py lists this ONE file, so no other
moved handler can start using yfinance unnoticed. Rules 1-2 for every module in gateway_support/ are enforced by test_gateway_support_split.py. Source checks only;
the live-app test runs when fastapi is installed."""
import ast
import os

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_FACTORY_SRC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_i.py"), encoding="utf-8").read()


def _handler():
    build = next(n for n in ast.parse(_FACTORY_SRC).body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_i")
    fns = [n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert [f.name for f in fns] == ["proxy_quote"]
    return fns[0]


def test_route_lives_in_the_factory_on_its_own_router():
    d = _handler().decorator_list
    assert [(x.func.attr, x.args[0].value, x.func.value.id) for x in d] == [("get", "/quote/{symbol}", "proxy_quote_router")]


def test_main_reexports_the_handler_includes_its_router_and_no_longer_declares_the_route():
    assert _MAIN_SRC.count("_build_bound_routes_i(") == 1
    assert "_bound_routes_i = _build_bound_routes_i(_MainRef(globals()))" in _MAIN_SRC
    assert "proxy_quote = _bound_routes_i.proxy_quote\n" in _MAIN_SRC
    assert _MAIN_SRC.count("app.include_router(_bound_routes_i.proxy_quote_router)") == 1
    assert '@app.get("/quote/{symbol}"' not in _MAIN_SRC and "@app.get('/quote/{symbol}'" not in _MAIN_SRC
    # same place as before: right after the batch H `/` route, before the E55 circuits router
    inc = _MAIN_SRC.index("app.include_router(_bound_routes_i.proxy_quote_router)")
    assert _MAIN_SRC.index("app.include_router(_bound_routes_h.root_router)") < inc < _MAIN_SRC.index("app.include_router(_bound_routes.circuits_router)")
    assert _MAIN_SRC.index("from gateway_support import MainRef as _MainRef") < _MAIN_SRC.index("_bound_routes_i = _build_bound_routes_i(")


def test_the_handler_asks_market_data_first_and_yfinance_is_only_the_flagged_last_fallback():
    fn = _handler()
    src = ast.get_source_segment(_FACTORY_SRC, fn)
    assert src.index("/quote/{sym}") < src.index("ref._direct_yf_ok()") < src.index("ref.yf.Ticker")
    # yfinance is reached only through ref.yf (no import of its own) and only inside the `_direct_yf_ok` guarded try
    assert "import yfinance" not in _FACTORY_SRC and "from yfinance" not in _FACTORY_SRC
    assert src.count("ref.yf.Ticker(") == 1 and src.count("yf.") == 1


def test_no_other_gateway_support_module_uses_yfinance():
    d = os.path.join(_SERVICE, "gateway_support")
    for f in sorted(os.listdir(d)):
        if f.endswith(".py") and f != "bound_routes_i.py":
            t = open(os.path.join(d, f), encoding="utf-8").read()
            assert "yf.Ticker" not in t and "yf.download" not in t and "import yfinance" not in t, f


def test_yfinance_guard_allowlists_exactly_this_factory():
    g = open(os.path.join(_SERVICE, "tests", "test_yfinance_callers_guard.py"), encoding="utf-8").read()
    assert '"api-gateway/gateway_support/bound_routes_i.py"' in g
    assert '"api-gateway/gateway_support/bound_routes_h.py"' not in g


def test_live_app_registers_the_route_once():
    pytest.importorskip("fastapi")
    import main
    n = sum(1 for r in main.app.routes if getattr(r, "path", None) == "/quote/{symbol}" and "GET" in (getattr(r, "methods", None) or ()))
    assert n == 1
    assert main.proxy_quote is main._bound_routes_i.proxy_quote
