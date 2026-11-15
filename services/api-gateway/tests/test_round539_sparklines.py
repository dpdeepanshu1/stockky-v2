"""E557 (round 539, B3): GET /market/sparklines - symbol cleaning, close extraction, cache, deadline, switch. No network: market-data is faked.
Works with or without fastapi / httpx installed (small stand-ins are put in place only when the real ones are missing)."""
import asyncio
import importlib.util
import os
import sys
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "gateway_routers", "sparklines.py")


def _load():
    saved = {}
    for name in ("fastapi", "httpx"):
        try:
            __import__(name)
        except ImportError:
            saved[name] = None
            m = types.ModuleType(name)
            if name == "fastapi":
                class APIRouter:
                    def get(self, *a, **k):
                        return lambda f: f
                m.APIRouter = APIRouter
            else:
                m.get = lambda *a, **k: None
            sys.modules[name] = m
    spec = importlib.util.spec_from_file_location("sparklines_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in saved:
        sys.modules.pop(name, None)
    return mod


sp = _load()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def _candles(n, start=100.0):
    return [{"date": f"2026-10-{i + 1:02d}", "close": start + i} for i in range(n)]


def _reset(monkeypatch, get):
    sp._cache.clear()
    monkeypatch.setattr(sp.httpx, "get", get, raising=False)
    monkeypatch.delenv("SPARKLINES_ENABLED", raising=False)
    monkeypatch.delenv("SPARKLINES_TTL_S", raising=False)


def test_clean_symbols_keeps_plausible_tickers_once_in_order_and_caps():
    assert sp.clean_symbols(" tcs, INFY ,tcs,M&M,J&KBANK,RELIANCE.NS") == ["TCS", "INFY", "M&M", "J&KBANK", "RELIANCE"]
    assert sp.clean_symbols("../admin,a/b,,X Y,%2e%2e,TCS?x=1,OK") == ["OK"]
    assert sp.clean_symbols("") == [] and sp.clean_symbols(None) == []
    assert len(sp.clean_symbols(",".join(f"S{i}" for i in range(100)))) == sp.MAX_SYMBOLS


def test_closes_keep_the_last_n_finite_positive_values_and_need_five():
    c = _candles(50)
    c[10]["close"] = None
    c[11]["close"] = float("nan")
    c[12]["close"] = -3
    c[13]["close"] = "x"
    got = sp.closes_from_candles(c, 30)
    assert len(got) == 30 and got[-1] == 149.0 and got == sorted(got)
    assert sp.closes_from_candles(_candles(4), 30) is None
    assert sp.closes_from_candles(None, 30) is None
    assert sp.closes_from_candles([{"close": 1.234567}] * 5, 30) == [1.23] * 5


def test_collect_returns_only_names_with_usable_history(monkeypatch):
    def get(url, **k):
        if "/BAD" in url:
            return _Resp(404, {})
        if "/THIN" in url:
            return _Resp(200, {"candles": _candles(2)})
        return _Resp(200, {"candles": _candles(40)})
    _reset(monkeypatch, get)
    out = sp.collect(["TCS", "BAD", "THIN"], 30)
    assert list(out) == ["TCS"] and len(out["TCS"]) == 30


def test_a_second_call_is_served_from_the_cache_and_a_miss_is_cached_too(monkeypatch):
    calls = []

    def get(url, **k):
        calls.append(url)
        return _Resp(404, {}) if "/BAD" in url else _Resp(200, {"candles": _candles(40)})
    _reset(monkeypatch, get)
    sp.collect(["TCS", "BAD"], 30)
    n = len(calls)
    out = sp.collect(["TCS", "BAD"], 30)
    assert len(calls) == n and list(out) == ["TCS"]


def test_an_exception_from_market_data_leaves_the_name_out_and_never_raises(monkeypatch):
    def get(url, **k):
        raise RuntimeError("down")
    _reset(monkeypatch, get)
    assert sp.collect(["TCS"], 30) == {}


def test_a_slow_name_is_left_out_not_cached_and_does_not_hold_the_others(monkeypatch):
    def get(url, **k):
        if "/SLOW" in url:
            time.sleep(1.5)
        return _Resp(200, {"candles": _candles(40)})
    _reset(monkeypatch, get)
    monkeypatch.setenv("SPARKLINES_DEADLINE_S", "1")
    t = time.time()
    out = sp.collect(["TCS", "SLOW"], 30)
    assert "TCS" in out and "SLOW" not in out and time.time() - t < 1.4
    assert ("SLOW", 30) not in sp._cache


def test_endpoint_shape_clamps_days_and_the_switch_answers_empty(monkeypatch):
    _reset(monkeypatch, lambda url, **k: _Resp(200, {"candles": _candles(90)}))
    r = asyncio.run(sp.market_sparklines(symbols="TCS,NOPE/..", days=500))
    assert r["days"] == 60 and r["series"]["TCS"] and r["missing"] == [] and r["enabled"] is True and len(r["series"]["TCS"]) == 60
    r = asyncio.run(sp.market_sparklines(symbols="", days=30))
    assert r["series"] == {} and r["missing"] == []
    monkeypatch.setenv("SPARKLINES_ENABLED", "0")
    r = asyncio.run(sp.market_sparklines(symbols="TCS", days=30))
    assert r["series"] == {} and r["missing"] == ["TCS"] and r["enabled"] is False


def test_the_route_is_public_and_registered_in_main():
    main = open(os.path.join(os.path.dirname(_HERE), "main.py"), encoding="utf-8").read()
    assert "app.include_router(_sparklines_router)" in main
    guard = open(os.path.join(os.path.dirname(_HERE), "admin_guard.py"), encoding="utf-8").read()
    assert "/market/sparklines" not in guard
