"""group230 (log review items 1 and 2): longer daily /history cache while the market is open, and the last good
daily candles kept durably and served (stale=true) when every source fails or the AngelOne candle cooldown runs.
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
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    m._mem._d.clear()
    m._history_flights.clear()
    monkeypatch.setattr(m, "cache", None)
    for k in ("HISTORY_DAILY_OPEN_TTL_S", "HISTORY_LAST_GOOD", "HISTORY_LAST_GOOD_MAX_AGE_S",
              "HISTORY_LAST_GOOD_SAVE_EVERY_S"):
        monkeypatch.delenv(k, raising=False)
    yield
    m._mem._d.clear()
    m._history_flights.clear()


class FakeKV:
    """Stands in for kv_cache (get/set/delete) - the durable store."""

    def __init__(self):
        self.store = {}
        self.sets = []
        self.fail = False

    def get(self, key):
        if self.fail:
            raise RuntimeError("kv down")
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        if self.fail:
            raise RuntimeError("kv down")
        self.sets.append((key, ttl))
        self.store[key] = value


@pytest.fixture()
def kv(monkeypatch):
    fake = FakeKV()
    mod = types.ModuleType("kv_cache")
    mod.get, mod.set = fake.get, fake.set
    monkeypatch.setitem(sys.modules, "kv_cache", mod)
    return fake


def _res(n=30):
    return {"symbol": "ZZ.NS", "requested": "ZZ", "period": "1mo", "interval": "1d",
            "candles": [{"date": f"2026-09-{i + 1:02d} 00:00", "open": 1.0, "high": 2.0, "low": 0.5,
                         "close": 1.5, "volume": 10} for i in range(n)]}


KEY = "history:ZZ.NS:1mo:1d:"


# -- cache TTL ----------------------------------------------------------------

def test_daily_history_stays_cached_an_hour_while_open(monkeypatch):
    monkeypatch.setattr(m, "is_market_open", lambda: True)
    assert m._history_ttl("1d") == 3600
    assert m._history_ttl("1wk") == 3600
    assert m._history_ttl("1D") == 3600


def test_intraday_history_keeps_the_short_ttl_while_open(monkeypatch):
    monkeypatch.setattr(m, "is_market_open", lambda: True)
    assert m._history_ttl("1h") == 900
    assert m._history_ttl("5m") == 900


def test_closed_market_ttl_is_unchanged(monkeypatch):
    monkeypatch.setattr(m, "is_market_open", lambda: False)
    assert m._history_ttl("1d") == 21600 and m._history_ttl("1h") == 21600


def test_daily_ttl_env_override_and_floor(monkeypatch):
    monkeypatch.setattr(m, "is_market_open", lambda: True)
    monkeypatch.setenv("HISTORY_DAILY_OPEN_TTL_S", "900")
    assert m._history_ttl("1d") == 900
    monkeypatch.setenv("HISTORY_DAILY_OPEN_TTL_S", "5")
    assert m._history_ttl("1d") == 60
    monkeypatch.setenv("HISTORY_DAILY_OPEN_TTL_S", "abc")
    assert m._history_ttl("1d") == 3600


# -- durable last-good --------------------------------------------------------

def test_save_then_get_roundtrip_is_flagged_stale(kv):
    m._history_last_good_save(KEY, _res())
    out = m._history_last_good_get(KEY)
    assert out["stale"] is True and out["source"] == "last_good" and out["stale_age_s"] >= 0
    assert len(out["candles"]) == 30


def test_save_is_throttled_per_key(kv):
    m._history_last_good_save(KEY, _res())
    m._history_last_good_save(KEY, _res())
    assert len(kv.sets) == 1


def test_save_skips_intraday_empty_and_stale_results(kv):
    m._history_last_good_save("history:ZZ.NS:1mo:1h:", _res())
    m._history_last_good_save(KEY, {"candles": []})
    m._history_last_good_save(KEY, dict(_res(), stale=True))
    m._history_last_good_save("bad", _res())
    assert kv.sets == []


def test_store_hook_saves_daily_results(kv):
    m._history_store(KEY, _res(), 60)
    assert len(kv.sets) == 1 and m._cache_get(KEY)["candles"]


def test_old_entries_are_not_served(kv, monkeypatch):
    m._history_last_good_save(KEY, _res())
    kv.store[m._history_lg_key(KEY)]["saved_at"] -= 5 * 86400
    assert m._history_last_good_get(KEY) is None


def test_max_age_env(kv, monkeypatch):
    m._history_last_good_save(KEY, _res())
    kv.store[m._history_lg_key(KEY)]["saved_at"] -= 2 * 86400
    assert m._history_last_good_get(KEY) is not None
    monkeypatch.setenv("HISTORY_LAST_GOOD_MAX_AGE_S", "3600")
    assert m._history_last_good_get(KEY) is None


def test_switch_off(kv, monkeypatch):
    monkeypatch.setenv("HISTORY_LAST_GOOD", "0")
    m._history_last_good_save(KEY, _res())
    assert kv.sets == [] and m._history_last_good_get(KEY) is None


def test_kv_failures_never_raise(kv):
    kv.fail = True
    m._history_last_good_save(KEY, _res())
    assert m._history_last_good_get(KEY) is None


def test_missing_garbage_and_future_entries_are_ignored(kv):
    assert m._history_last_good_get(KEY) is None
    kv.store[m._history_lg_key(KEY)] = "junk"
    assert m._history_last_good_get(KEY) is None
    kv.store[m._history_lg_key(KEY)] = {"saved_at": 9e12, "result": _res()}
    assert m._history_last_good_get(KEY) is None
    kv.store[m._history_lg_key(KEY)] = {"saved_at": 1.0, "result": {"candles": []}}
    assert m._history_last_good_get(KEY) is None


# -- candle cooldown detection ------------------------------------------------

def test_candle_cooling_follows_the_candle_family(monkeypatch):
    import angelone_budget as b
    b._reset()
    assert m._history_candle_cooling() is False
    b.trip("getCandleData")
    assert m._history_candle_cooling() is True
    b._reset()


def test_candle_cooling_is_false_when_the_budget_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "angelone_budget", None)
    assert m._history_candle_cooling() is False


# -- /history behaviour -------------------------------------------------------

@pytest.fixture()
def no_upstream(monkeypatch):
    """Angel returns nothing, yfinance raises (counted), NSE returns nothing."""
    calls = {"yf": 0, "angel": 0}

    def angel(*a, **k):
        calls["angel"] += 1
        return None

    def ticker(sym):
        calls["yf"] += 1
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(m, "_angelone_history_candles", angel)
    monkeypatch.setattr(m.yf, "Ticker", ticker, raising=False)
    monkeypatch.setattr(m, "_nse_history_candles", lambda *a, **k: None)
    monkeypatch.setattr(m, "is_market_open", lambda: True)
    return calls


def test_every_source_failing_serves_the_last_good_candles(kv, no_upstream):
    m._history_last_good_save("history:ZZTEST.NS:1mo:1d:", _res())
    out = m._get_history_impl("ZZTEST", "1mo", "1d", False, None)
    assert out["stale"] is True and out["candles"]


def test_every_source_failing_without_last_good_is_still_an_error(kv, no_upstream):
    with pytest.raises(HTTPException):
        m._get_history_impl("ZZNONE", "1mo", "1d", False, None)


def test_candle_cooldown_serves_last_good_without_touching_yfinance(kv, no_upstream):
    import angelone_budget as b
    b._reset()
    b.trip("getCandleData")
    m._history_last_good_save("history:ZZCOOL.NS:1mo:1d:", _res())
    out = m._get_history_impl("ZZCOOL", "1mo", "1d", False, None)
    assert out["stale"] is True
    assert no_upstream["yf"] == 0 and no_upstream["angel"] == 0
    b._reset()


def test_candle_cooldown_without_last_good_still_falls_to_the_old_path(kv, no_upstream):
    import angelone_budget as b
    b._reset()
    b.trip("getCandleData")
    with pytest.raises(HTTPException):
        m._get_history_impl("ZZCOLD2", "1mo", "1d", False, None)
    assert no_upstream["yf"] >= 1
    b._reset()


def test_force_requests_ignore_the_cooldown_shortcut(kv, no_upstream):
    import angelone_budget as b
    b._reset()
    b.trip("getCandleData")
    m._history_last_good_save("history:ZZFORCE.NS:1mo:1d:", _res())
    m._get_history_impl("ZZFORCE", "1mo", "1d", True, None)   # served by the all-failed fallback, after trying upstream
    assert no_upstream["yf"] >= 1
    b._reset()


def test_a_fresh_upstream_answer_replaces_nothing_stale(kv, monkeypatch):
    monkeypatch.setattr(m, "is_market_open", lambda: True)
    monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _res()["candles"])
    out = m._get_history_impl("ZZFRESH", "1mo", "1d", False, None)
    assert out["source"] == "angelone" and not out.get("stale")
    assert len(kv.sets) == 1                                    # and it is kept for the next outage
