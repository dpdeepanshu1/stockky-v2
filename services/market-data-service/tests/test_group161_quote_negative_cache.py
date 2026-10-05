"""
group161 (item 1): "no price" negative cache on /quote and the new /last-close route.

BMISL, QUALIANCE, BAGMANE, 3PLAND, MFML, AVALON, CMRGREEN and SUNLOC are not live equities: every /quote
walked the whole waterfall and could hold a worker for the 18 s yfinance timeout, so real-trade-service's
8 s read timed out each cycle. After QUOTE_NEG_AFTER full failures in a row /quote answers "no price" at
once; /last-close (which did not exist, so it always 404ed) now answers from cache / bhavcopy only.
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



_SOURCES = ("_yahoo_ohlcv_quote", "_waterfall_nse_direct_price", "_waterfall_angelone_price",
            "_waterfall_indianapi_price", "_waterfall_twelvedata_price", "_waterfall_alphavantage_price",
            "_waterfall_polygon_price", "_waterfall_bhavcopy_price")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    m._mem._d.clear()
    m._neg_reset()
    for k in ("QUOTE_NEG_CACHE", "QUOTE_NEG_AFTER", "QUOTE_NEG_TTL_S", "QUOTE_NEG_MAX_S"):
        monkeypatch.delenv(k, raising=False)
    m._UPSTREAM_COOLDOWN.clear()
    yield
    m._mem._d.clear()
    m._neg_reset()


@pytest.fixture
def waterfall(monkeypatch):
    """Every price source answers nothing; returns the list of calls made."""
    calls = []
    for name in _SOURCES:
        monkeypatch.setattr(m, name, (lambda n: lambda *a, **kw: calls.append(n) or None)(name))
    return calls


def _quote(sym="BMISL"):
    r = client.get(f"/quote/{sym}")
    assert r.status_code == 200
    return r.json()


class TestNegativeCache:
    def test_first_two_failures_walk_the_waterfall(self, waterfall):
        assert _quote()["source"] == "failed"
        n1 = len(waterfall)
        assert n1 > 0
        assert _quote()["source"] == "failed"
        assert len(waterfall) == 2 * n1

    def test_third_call_answers_from_cache_without_upstream(self, waterfall):
        _quote(); _quote()
        before = len(waterfall)
        body = _quote()
        assert body["source"] == "negative_cache"
        assert body["price"] is None
        assert len(waterfall) == before

    def test_other_symbols_unaffected(self, waterfall):
        _quote("BMISL"); _quote("BMISL")
        before = len(waterfall)
        assert _quote("QUALIANCE")["source"] == "failed"
        assert len(waterfall) > before

    def test_spellings_share_one_entry(self, waterfall):
        _quote("BMISL"); _quote("bmisl.ns")
        assert _quote("BMISL.BO")["source"] == "negative_cache"

    def test_window_expires(self, waterfall, monkeypatch):
        _quote(); _quote()
        for ent in m._NEG_QUOTE.values():
            ent[1] = 0.0           # window over
        before = len(waterfall)
        assert _quote()["source"] == "failed"
        assert len(waterfall) > before

    def test_backoff_doubles_and_caps(self, waterfall, monkeypatch):
        monkeypatch.setenv("QUOTE_NEG_TTL_S", "100")
        monkeypatch.setenv("QUOTE_NEG_MAX_S", "250")
        sym = m.normalize_symbol("BMISL")
        waits = []
        for _ in range(5):
            m._neg_record_failure(sym)
            ent = m._NEG_QUOTE[sym]
            waits.append(round(ent[1] - time.monotonic()))
            ent[1] = 0.0
        assert waits[0] <= 0 or waits[0] == 0          # first failure: below threshold, no window
        assert waits[1] == 100 and waits[2] == 200 and waits[3] == 250 and waits[4] == 250

    def test_a_real_price_clears_the_count(self, waterfall, monkeypatch):
        _quote()
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: 123.4)
        assert _quote()["price"] == 123.4
        assert m.normalize_symbol("BMISL") not in m._NEG_QUOTE

    def test_yfinance_cooldown_failures_are_not_counted(self, waterfall):
        m._set_cooldown("yfinance", 60)
        _quote(); _quote(); _quote()
        assert m._NEG_QUOTE == {}

    def test_off_switch(self, waterfall, monkeypatch):
        monkeypatch.setenv("QUOTE_NEG_CACHE", "0")
        for _ in range(4):
            assert _quote()["source"] == "failed"
        assert m._NEG_QUOTE == {}

    def test_threshold_env(self, waterfall, monkeypatch):
        monkeypatch.setenv("QUOTE_NEG_AFTER", "1")
        _quote()
        assert _quote()["source"] == "negative_cache"

    def test_bad_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("QUOTE_NEG_AFTER", "abc")
        monkeypatch.setenv("QUOTE_NEG_TTL_S", "-5")
        assert m._neg_cfg() == (True, 2, 300.0, 3600.0)

    def test_last_good_price_is_never_negative_cached(self, waterfall):
        sym = m.normalize_symbol("BMISL")
        m._NEG_QUOTE[sym] = [5, time.monotonic() + 999]
        m._fallback_set(f"quote:{sym}", {"symbol": sym, "price": 55.0})
        body = _quote()
        assert body["price"] == 55.0          # served the last-good price, not "negative_cache"

    def test_delisted_still_404(self, waterfall):
        assert client.get("/quote/AAKASH").status_code == 404

    def test_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(m, "_NEG_MAX_ENTRIES", 3)
        now = time.monotonic()
        for i in range(3):
            m._NEG_QUOTE[f"S{i}.NS"] = [2, now + 999]
        m._neg_record_failure("NEW.NS")
        assert "NEW.NS" not in m._NEG_QUOTE and len(m._NEG_QUOTE) == 3

    def test_expired_entries_make_room(self, monkeypatch):
        monkeypatch.setattr(m, "_NEG_MAX_ENTRIES", 3)
        for i in range(3):
            m._NEG_QUOTE[f"S{i}.NS"] = [2, 0.0]
        m._neg_record_failure("NEW.NS")
        assert "NEW.NS" in m._NEG_QUOTE and len(m._NEG_QUOTE) == 1

    def test_helpers_never_raise(self, monkeypatch):
        monkeypatch.setattr(m, "_neg_cfg", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert m._neg_blocked("A.NS") is False
        m._neg_record_failure("A.NS")
        m._neg_clear("A.NS")


class TestLastClose:
    def test_delisted_is_404(self):
        r = client.get("/last-close/AAKASH")
        assert r.status_code == 404 and "delisted" in r.json()["detail"]

    def test_unknown_symbol_is_404(self, monkeypatch):
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: None)
        assert client.get("/last-close/SUNLOC").status_code == 404

    def test_bhavcopy_close(self, monkeypatch):
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: 321.5)
        body = client.get("/last-close/RELIANCE").json()
        assert body["price"] == 321.5 and body["close"] == 321.5 and body["source"] == "bhavcopy_eod"

    def test_cached_quote_wins_and_prefers_previous_close(self, monkeypatch):
        sym = m.normalize_symbol("TCS")
        m._cache_set(f"quote:{sym}", {"symbol": sym, "price": 4100.0, "previous_close": 4050.0})
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: 1.0)
        body = client.get("/last-close/TCS").json()
        assert body["price"] == 4050.0 and body["source"] == "last_close_cache"

    def test_fallback_store_used(self, monkeypatch):
        sym = m.normalize_symbol("INFY")
        m._fallback_set(f"quote:{sym}", {"symbol": sym, "price": 1500.0})
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: None)
        body = client.get("/last-close/INFY").json()
        assert body["price"] == 1500.0 and body["source"] == "last_close_fallback"

    def test_never_touches_yahoo_or_paid_apis(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("last-close must not call a live source")
        for name in _SOURCES[:-1]:
            monkeypatch.setattr(m, name, boom)
        monkeypatch.setattr(m, "_waterfall_bhavcopy_price", lambda *a, **kw: 10.0)
        assert client.get("/last-close/WIPRO").status_code == 200
