"""
Delisted fast paths (group118 — AAKASH / ANNAPURNA 404s in the log).

/quote/{symbol} already short-circuited KNOWN_DELISTED_SYMBOLS, but three sibling routes still
went upstream for them: /quotes/bulk (yf.download batch), /history and /fundamentals. All three now
answer without touching Yahoo, and ordinary symbols are unaffected.
"""
from __future__ import annotations
import os, sys, types, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _mod in ("yfinance", "upstash_redis", "requests"):
    if _mod not in sys.modules:
        _stub = types.ModuleType(_mod)
        if _mod == "upstash_redis":
            _stub.Redis = lambda **kw: None
        if _mod == "requests":
            class _Session:
                def __init__(self): self.headers = {}
                def update(self, h): pass
                def get(self, *a, **kw): return types.SimpleNamespace(status_code=404, json=lambda: {})
                def post(self, *a, **kw): return None
            _stub.Session = _Session
            _stub.post = lambda *a, **kw: None
        if _mod == "yfinance":
            _stub.set_session = lambda s: None
            _stub.set_tz_cache_location = lambda p: None
            _stub.shared = types.SimpleNamespace(_session=None)
        sys.modules[_mod] = _stub

import requests as _requests_mod
if not hasattr(_requests_mod, "post"):
    _requests_mod.post = lambda *a, **kw: None

if "circuit_breaker" not in sys.modules:
    _cb = types.ModuleType("circuit_breaker")
    class _Breaker:
        def allow(self): return True
        def retry_after(self): return 0
        def record_success(self): pass
        def record_failure(self, e=""): pass
    _cb.get_breaker = lambda *a, **kw: _Breaker()
    _cb.all_snapshots = lambda: {}
    _cb.record_rate_limit_hit = lambda **kw: None
    sys.modules["circuit_breaker"] = _cb

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import main as m

import pytest
from fastapi.testclient import TestClient

client = TestClient(m.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_mem():
    m._mem._d.clear()
    yield
    m._mem._d.clear()


class TestBulkSkipsDelisted:
    def test_only_delisted_returns_no_valid_symbols_without_upstream(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("cache/upstream must not be consulted for delisted symbols")
        monkeypatch.setattr(m, "_cache_get", boom)
        r = client.post("/quotes/bulk", json={"symbols": ["AAKASH", "annapurna.ns", "TATAMTRDVR.BO"]})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["quotes"] == []

    def test_delisted_dropped_but_live_symbol_still_looked_up(self, monkeypatch):
        looked_up = []

        def fake_cache_get(key):
            looked_up.append(key)
            if key == "quote:TCS.NS":
                return {"symbol": "TCS", "price": 4000.0}
            return None
        monkeypatch.setattr(m, "_cache_get", fake_cache_get)
        r = client.post("/quotes/bulk", json={"symbols": ["AAKASH", "TCS", "ANNAPURNA"]})
        assert r.status_code == 200
        assert looked_up == ["quote:TCS.NS"]
        assert [q["symbol"] for q in r.json()["quotes"]] == ["TCS"]


class TestHistoryDelistedFastFail:
    @pytest.mark.parametrize("sym", ["AAKASH", "annapurna", "ANNAPURNA.NS", "AAKASH.BO"])
    def test_404_without_upstream(self, monkeypatch, sym):
        def boom(*a, **kw):
            raise AssertionError("_get_history_impl must not run for a delisted symbol")
        monkeypatch.setattr(m, "_get_history_impl", boom)
        r = client.get(f"/history/{sym}")
        assert r.status_code == 404
        assert "delisted" in r.json()["detail"]

    def test_live_symbol_still_reaches_impl(self, monkeypatch):
        seen = []
        monkeypatch.setattr(m, "_get_history_impl",
                            lambda symbol, period, interval, force, days: seen.append(symbol) or {"ok": True})
        r = client.get("/history/RELIANCE")
        assert r.status_code == 200
        assert seen == ["RELIANCE"]


class TestFundamentalsDelistedShortCircuit:
    @pytest.mark.parametrize("sym", ["AAKASH", "annapurna.ns"])
    def test_returns_delisted_shape_without_upstream(self, monkeypatch, sym):
        def boom(*a, **kw):
            raise AssertionError("_get_fundamentals_inner must not run for a delisted symbol")
        monkeypatch.setattr(m, "_get_fundamentals_inner", boom)
        r = client.get(f"/fundamentals/{sym}")
        assert r.status_code == 200
        body = r.json()
        assert body["error"] == "delisted"
        assert body["symbol"].endswith(".NS")
        assert body["pe_ratio"] is None and body["roe"] is None

    def test_live_symbol_still_reaches_inner(self, monkeypatch):
        monkeypatch.setattr(m, "_get_fundamentals_inner", lambda symbol, force=False: {"symbol": symbol, "pe_ratio": 20})
        r = client.get("/fundamentals/INFY")
        assert r.status_code == 200
        assert r.json()["pe_ratio"] == 20
