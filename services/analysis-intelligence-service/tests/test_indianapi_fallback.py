"""
tests/test_indianapi_fallback.py — fundamental/indianapi_fallback.py
No real HTTP. requests.get monkeypatched.
"""
from __future__ import annotations
import os, sys, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest
from datetime import datetime, timedelta

import indianapi_fallback as iaf


@pytest.fixture(autouse=True)
def _clean():
    iaf._MEM_LAST_TS = 0.0
    iaf.INDIANAPI_KEY = None
    yield
    iaf._MEM_LAST_TS = 0.0
    iaf.INDIANAPI_KEY = None


class TestAddTradingDays:
    def test_adds_weekdays_only(self):
        from datetime import date
        start = date(2026, 9, 25)   # Friday
        result = iaf._add_trading_days(start, 2)
        # Skip Sat/Sun: next trading days are Mon(28) and Tue(29)
        assert result == date(2026, 9, 29)

    def test_adds_zero_days(self):
        from datetime import date
        d = date(2026, 9, 28)
        assert iaf._add_trading_days(d, 0) == d

    def test_five_trading_days(self):
        from datetime import date
        start = date(2026, 9, 28)   # Monday
        result = iaf._add_trading_days(start, 5)
        # Mon-Fri = 5 trading days → next Monday
        assert result == date(2026, 10, 5)


class TestCacheExpiry:
    def test_expiry_is_after_n_trading_days(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        cached = datetime(2026, 9, 28, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        expiry = iaf._cache_expiry(cached)
        # 5 trading days from Monday = next Monday
        assert expiry.date() >= date(2026, 10, 5)

    def test_expiry_time_is_market_open(self):
        from zoneinfo import ZoneInfo
        cached = datetime(2026, 9, 28, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        expiry = iaf._cache_expiry(cached)
        assert expiry.hour == 9
        assert expiry.minute == 15


class TestIsCacheFresh:
    def test_fresh_payload_returns_true(self):
        from zoneinfo import ZoneInfo
        payload = {"cached_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()}
        assert iaf._is_cache_fresh(payload) is True

    def test_stale_payload_returns_false(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        old = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=30)).isoformat()
        payload = {"cached_at": old}
        assert iaf._is_cache_fresh(payload) is False

    def test_missing_cached_at_returns_false(self):
        assert iaf._is_cache_fresh({}) is False

    def test_invalid_date_returns_false(self):
        assert iaf._is_cache_fresh({"cached_at": "not-a-date"}) is False


class TestCacheGetSet:
    def _kv_stub(self, monkeypatch):
        store = {}
        fake = types.ModuleType("kv_cache")
        fake.get = lambda k: store.get(k)
        fake.set = lambda k, v, ttl=None: store.update({k: v})
        monkeypatch.setattr(iaf, "_kv", fake)
        return store

    def test_cache_get_returns_stored_value(self, monkeypatch):
        store = self._kv_stub(monkeypatch)
        store["indianapi:fundamentals:RELIANCE"] = {"data": {"pe": 20}}
        result = iaf._cache_get(None, "RELIANCE")
        assert result == {"data": {"pe": 20}}

    def test_cache_get_returns_none_on_exception(self, monkeypatch):
        fake = types.ModuleType("kv_cache")
        fake.get = lambda k: (_ for _ in ()).throw(RuntimeError("down"))
        monkeypatch.setattr(iaf, "_kv", fake)
        assert iaf._cache_get(None, "X") is None

    def test_cache_get_returns_none_when_kv_none(self):
        iaf._kv = None
        assert iaf._cache_get(None, "X") is None

    def test_cache_set_stores_value(self, monkeypatch):
        store = self._kv_stub(monkeypatch)
        iaf._cache_set(None, "TCS", {"data": {"pe": 25}})
        assert "indianapi:fundamentals:TCS" in store

    def test_cache_set_swallows_exception(self, monkeypatch):
        fake = types.ModuleType("kv_cache")
        fake.set = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
        monkeypatch.setattr(iaf, "_kv", fake)
        iaf._cache_set(None, "X", {"data": {}})   # must not raise

    def test_cache_set_noop_when_kv_none(self):
        iaf._kv = None
        iaf._cache_set(None, "X", {"data": {}})   # must not raise


class TestEnforceRateLimit:
    def test_falls_back_to_mem_when_rate_limiter_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rate_limiter", None)
        iaf._MEM_LAST_TS = time.time()   # just called
        # With MIN=1.0s and last_ts=now, wait would be ~1s. Use tiny interval.
        old = iaf.MIN_REQUEST_INTERVAL_SECONDS
        iaf.MIN_REQUEST_INTERVAL_SECONDS = 0.0
        iaf._enforce_rate_limit(None)   # must not hang
        iaf.MIN_REQUEST_INTERVAL_SECONDS = old

    def test_rate_limiter_used_when_available(self, monkeypatch):
        called = []
        fake = types.ModuleType("rate_limiter")
        fake.acquire = lambda p, weight=1: called.append(p)
        monkeypatch.setitem(sys.modules, "rate_limiter", fake)
        iaf._enforce_rate_limit(None)
        assert "indianapi" in called


class TestFetchFromIndianApi:
    def test_returns_none_when_no_key(self, monkeypatch):
        iaf.INDIANAPI_KEY = None
        result = iaf._fetch_from_indianapi("RELIANCE")
        assert result is None

    def test_returns_json_on_success(self, monkeypatch):
        iaf.INDIANAPI_KEY = "test_key"
        monkeypatch.setattr(iaf, "_enforce_rate_limit", lambda r: None)
        import requests
        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"tickerId": "RELIANCE", "pe": 22}
        monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp())
        result = iaf._fetch_from_indianapi("RELIANCE")
        assert result["pe"] == 22

    def test_returns_none_on_request_exception(self, monkeypatch):
        iaf.INDIANAPI_KEY = "test_key"
        monkeypatch.setattr(iaf, "_enforce_rate_limit", lambda r: None)
        import requests
        monkeypatch.setattr(requests, "get",
            lambda *a, **kw: (_ for _ in ()).throw(requests.RequestException("timeout")))
        result = iaf._fetch_from_indianapi("RELIANCE")
        assert result is None


class TestGetFundamentalsWithFallback:
    def _kv_stub(self, monkeypatch):
        store = {}
        fake = types.ModuleType("kv_cache")
        fake.get = lambda k: store.get(k)
        fake.set = lambda k, v, ttl=None: store.update({k: v})
        monkeypatch.setattr(iaf, "_kv", fake)
        return store

    def test_yahoo_result_returned_directly(self, monkeypatch):
        self._kv_stub(monkeypatch)
        result = iaf.get_fundamentals_with_fallback(
            "RELIANCE", lambda sym: {"pe": 25}
        )
        assert result["pe"] == 25

    def test_indianapi_called_when_yahoo_returns_none(self, monkeypatch):
        self._kv_stub(monkeypatch)
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: {"pe": 30})
        result = iaf.get_fundamentals_with_fallback("TCS", lambda sym: None)
        assert result["pe"] == 30

    def test_cache_used_when_fresh(self, monkeypatch):
        from zoneinfo import ZoneInfo
        store = self._kv_stub(monkeypatch)
        cached_payload = {
            "data": {"pe": 99},
            "cached_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
        }
        store["indianapi:fundamentals:WIPRO"] = cached_payload
        fetch_called = []
        monkeypatch.setattr(iaf, "_fetch_from_indianapi",
                            lambda s: fetch_called.append(s) or {})
        result = iaf.get_fundamentals_with_fallback("WIPRO", lambda sym: None)
        assert result["pe"] == 99
        assert not fetch_called

    def test_stale_cache_refreshed(self, monkeypatch):
        from zoneinfo import ZoneInfo
        store = self._kv_stub(monkeypatch)
        stale = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=30)).isoformat()
        store["indianapi:fundamentals:INFY"] = {"data": {"pe": 10}, "cached_at": stale}
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: {"pe": 15})
        result = iaf.get_fundamentals_with_fallback("INFY", lambda sym: None)
        assert result["pe"] == 15

    def test_stale_cache_returned_when_fetch_fails(self, monkeypatch):
        from zoneinfo import ZoneInfo
        store = self._kv_stub(monkeypatch)
        stale = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=30)).isoformat()
        store["indianapi:fundamentals:HDFC"] = {"data": {"pe": 8}, "cached_at": stale}
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: None)
        result = iaf.get_fundamentals_with_fallback("HDFC", lambda sym: None)
        assert result["pe"] == 8

    def test_returns_none_when_both_fail(self, monkeypatch):
        self._kv_stub(monkeypatch)
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: None)
        result = iaf.get_fundamentals_with_fallback("X", lambda sym: None)
        assert result is None

    def test_yahoo_exception_triggers_fallback(self, monkeypatch):
        self._kv_stub(monkeypatch)
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: {"pe": 20})
        def _yahoo_fail(sym): raise RuntimeError("yf down")
        result = iaf.get_fundamentals_with_fallback("SBIN", _yahoo_fail)
        assert result["pe"] == 20


# ══ session 126: import fallback / in-memory pacing / timeout fallback / redis guard ══════════
#
# Closes the last uncovered lines of fundamental/indianapi_fallback.py:
#   44-45    `import kv_cache` fails at import time -> `_kv = None`
#   146      in-process pacing fallback actually sleeps
#   159-160  rate_limiter.suggested_timeout() raises -> default REQUEST_TIMEOUT_SECONDS is kept
#   199-201  _get_redis_client() raises RuntimeError -> log + return None (no fetch attempted)

import logging


class TestKvCacheImportFallback:
    def test_kv_is_none_when_kv_cache_cannot_be_imported(self, monkeypatch):
        # Re-execute the REAL source (compiled under its real filename so coverage attributes the
        # lines to it) with `import kv_cache` forced to fail. sys.modules[name] = None makes the
        # import statement raise ImportError, which the module's `except Exception` swallows.
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        with open(iaf.__file__, encoding="utf-8") as f:
            src = f.read()
        ns = {"__name__": "_iaf_no_kv", "__file__": iaf.__file__}
        exec(compile(src, iaf.__file__, "exec"), ns)
        assert ns["_kv"] is None
        # ...and the cache helpers degrade to no-ops on that fresh copy instead of raising.
        assert ns["_cache_get"](None, "X") is None
        ns["_cache_set"](None, "X", {"data": {}})


class TestEnforceRateLimitInProcessFallback:
    def test_sleeps_for_the_remaining_interval_and_stamps_the_time(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rate_limiter", None)   # force the fallback path
        slept = []
        monkeypatch.setattr(iaf.time, "sleep", lambda s: slept.append(s))
        iaf._MEM_LAST_TS = time.time()          # a request "just" happened
        before = iaf._MEM_LAST_TS
        iaf._enforce_rate_limit(None)
        assert len(slept) == 1
        assert 0 < slept[0] <= iaf.MIN_REQUEST_INTERVAL_SECONDS
        assert iaf._MEM_LAST_TS >= before       # stamped after the (mocked) wait

    def test_does_not_sleep_when_interval_already_elapsed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rate_limiter", None)
        slept = []
        monkeypatch.setattr(iaf.time, "sleep", lambda s: slept.append(s))
        iaf._MEM_LAST_TS = 0.0                  # long ago
        iaf._enforce_rate_limit(None)
        assert slept == []
        assert iaf._MEM_LAST_TS > 0.0


class TestFetchTimeoutFallback:
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    def _rate_limiter(self, monkeypatch, suggested):
        fake = types.ModuleType("rate_limiter")
        fake.acquire = lambda p, weight=1: None
        fake.suggested_timeout = suggested
        monkeypatch.setitem(sys.modules, "rate_limiter", fake)

    def _capture_get(self, monkeypatch):
        import requests
        seen = {}
        def _get(url, **kw):
            seen.update(kw)
            return self._Resp()
        monkeypatch.setattr(requests, "get", _get)
        return seen

    def test_default_timeout_kept_when_suggested_timeout_raises(self, monkeypatch):
        iaf.INDIANAPI_KEY = "k"
        def _boom(default, name): raise RuntimeError("limiter down")
        self._rate_limiter(monkeypatch, _boom)
        seen = self._capture_get(monkeypatch)
        assert iaf._fetch_from_indianapi("RELIANCE") == {"ok": True}
        assert seen["timeout"] == iaf.REQUEST_TIMEOUT_SECONDS

    def test_suggested_timeout_is_used_when_available(self, monkeypatch):
        iaf.INDIANAPI_KEY = "k"
        calls = []
        def _suggest(default, name):
            calls.append((default, name))
            return 3.5
        self._rate_limiter(monkeypatch, _suggest)
        seen = self._capture_get(monkeypatch)
        iaf._fetch_from_indianapi("RELIANCE")
        assert calls == [(iaf.REQUEST_TIMEOUT_SECONDS, "indianapi")]
        assert seen["timeout"] == 3.5
        assert seen["params"] == {"name": "RELIANCE"}
        assert seen["headers"] == {"x-api-key": "k"}


class TestRedisClientGuard:
    def test_runtime_error_from_redis_client_returns_none_and_skips_fetch(self, monkeypatch, caplog):
        def _no_redis(): raise RuntimeError("redis unavailable")
        monkeypatch.setattr(iaf, "_get_redis_client", _no_redis)
        fetched = []
        monkeypatch.setattr(iaf, "_fetch_from_indianapi", lambda s: fetched.append(s) or {"pe": 1})
        with caplog.at_level(logging.ERROR, logger="fundamental-analysis-service.indianapi_fallback"):
            result = iaf.get_fundamentals_with_fallback("TCS", lambda sym: None)
        assert result is None
        assert fetched == []
        assert any("redis unavailable" in r.getMessage() for r in caplog.records)

    def test_yahoo_success_never_reaches_the_redis_client(self, monkeypatch):
        def _no_redis(): raise AssertionError("must not be called")
        monkeypatch.setattr(iaf, "_get_redis_client", _no_redis)
        assert iaf.get_fundamentals_with_fallback("TCS", lambda sym: {"pe": 5}) == {"pe": 5}

    def test_get_redis_client_is_a_noop_returning_none(self):
        assert iaf._get_redis_client() is None

