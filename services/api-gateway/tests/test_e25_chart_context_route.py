import ast
import os

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
# E87: the route moved to gateway_support/bound_routes_l.py (main.py names are read as `ref.<name>`); main.py keeps `_CHART_TFS` and includes the router.
FAC = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_l.py"), encoding="utf-8").read()


def _handler_node():
    build = next(n for n in ast.parse(FAC).body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_l")
    return next(n for n in build.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "stock_chart_context")


def test_route_is_registered_before_the_catch_all_stock_route():
    fn = _handler_node()
    assert [d.args[0].value for d in fn.decorator_list] == ["/stock/{symbol}/chart-context"]
    assert "@app.get(\"/stock/{symbol}/chart-context\")" not in SRC
    a = SRC.index("app.include_router(_bound_routes_l.stock_chart_context_router)")
    assert a < SRC.index("app.include_router(_bound_routes_l.get_stock_decision_router)")


def test_route_is_fail_open_and_checks_the_schema():
    body = ast.get_source_segment(FAC, _handler_node())
    assert '"ok": False' in body and 'data.get("schema") != 1' in body and "return empty" in body
    assert "raise ref.HTTPException" not in body and "raise HTTPException" not in body          # context only: never turns a missing chart into an error page
    assert "max(30, min(int(tail), cap))" in body and "cap = 1500 if intraday else 500" in body      # E27: daily still 500, intraday 1500


# ---- E27: the same route for 1m / 5m / 15m / 30m / 1h (behaviour, with the route function exec'd against fakes)
import asyncio
from typing import Optional


def _route():
    calls = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _HTTPX:
        AsyncClient = _Client

    class _Resp:
        def __init__(self, status, body):
            self.status_code, self._b = status, body

        def json(self):
            return self._b

    state = {"resp": _Resp(200, {"schema": 1, "ok": True, "tf": "5m", "bars": [1]}), "raise": None}

    async def cb_get(client, name, url, timeout=5.0, **kw):
        calls.append({"name": name, "url": url, "timeout": timeout})
        if state["raise"]:
            raise state["raise"]
        return state["resp"]

    import logging
    from types import SimpleNamespace
    tfs = ast.literal_eval(SRC[SRC.index("_CHART_TFS = ") + len("_CHART_TFS = "):].split("\n", 1)[0])
    ref = SimpleNamespace(Optional=Optional, httpx=_HTTPX, _cb_get=cb_get, TECHNICAL_URL="http://tech:8002", logger=logging.getLogger("t"), _CHART_TFS=tfs)
    fn = _handler_node()
    fn.decorator_list = []          # the router decorator needs fastapi; the handler body is what is under test
    mod = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    ns = {"ref": ref}
    exec(compile(mod, "route", "exec"), ns)
    return ns["stock_chart_context"], calls, state, _Resp


def _run(fn, *a, **k):
    return asyncio.run(fn(*a, **k))


def test_intraday_request_uses_its_own_breaker_a_longer_timeout_and_a_bigger_tail_cap():
    fn, calls, _st, _R = _route()
    out = _run(fn, "infy.ns", tf="5m", tail=9999)
    assert out["ok"] is True and calls[0]["name"] == "technical_chart" and calls[0]["timeout"] == 30
    assert calls[0]["url"] == "http://tech:8002/chart-context/INFY?tf=5m&tail=1500"
    _run(fn, "INFY", tf="1d", tail=9999)
    assert calls[1]["name"] == "technical" and calls[1]["timeout"] == 25 and calls[1]["url"].endswith("?tf=1d&tail=500")
    _run(fn, "INFY", tf="1h", tail=3, force=True)
    assert calls[2]["url"].endswith("?tf=1h&tail=30&force=true")
    _run(fn, "INFY", tf="60m")
    assert calls[3]["url"].endswith("?tf=1h") and "tail" not in calls[3]["url"]
    _run(fn, "INFY")
    assert calls[4]["url"].endswith("?tf=1d")


def test_unknown_timeframe_or_symbol_never_reaches_the_upstream():
    fn, calls, _st, _R = _route()
    out = _run(fn, "INFY", tf="7m")
    assert out["ok"] is False and out["tf"] == "1d" and out["bars"] == [] and calls == []
    assert _run(fn, "", tf="5m")["ok"] is False and calls == []


def test_every_failure_is_the_empty_answer_and_a_reason_is_passed_on():
    fn, calls, st, R = _route()
    st["resp"] = R(503, {})
    assert _run(fn, "INFY", tf="5m")["ok"] is False
    st["resp"] = R(200, {"schema": 1, "ok": False, "reason": "thin", "tf": "1m"})
    out = _run(fn, "INFY", tf="1m")
    assert out["ok"] is False and out["reason"] == "thin" and out["tf"] == "1m" and out["bars"] == []
    st["resp"] = R(200, {"schema": 2, "ok": True})
    assert _run(fn, "INFY", tf="5m")["ok"] is False
    st["resp"] = R(200, ["x"])
    assert _run(fn, "INFY", tf="5m")["ok"] is False
    st["raise"] = RuntimeError("boom")
    assert _run(fn, "INFY", tf="5m")["ok"] is False


def test_the_intraday_breaker_is_separate_so_thin_data_cannot_open_the_analysis_one():
    assert '"technical_chart" if intraday else "technical"' in FAC   # E87: the route lives in bound_routes_l.py now
