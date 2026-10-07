"""
/history reuse (2026-10-04, item 3 — AngelOne 403 "exceeding access rate"):
  * a shorter period is answered by slicing an already-cached longer one
  * force=true reuses a result fetched within HISTORY_FORCE_REUSE_S, and
    identical concurrent forced requests coalesce into one upstream call
  * nothing else about /history changes (days= window, negative cache, caps)
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


def _candles(span_days: int, step: int = 1):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    out = []
    for back in range(span_days, -1, -step):
        d = today - timedelta(days=back)
        out.append({"date": f"{d.isoformat()} 00:00", "open": 1.0, "high": 2.0, "low": 0.5,
                    "close": 1.5, "volume": 10})
    return out


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    m._mem._d.clear()
    m._history_flights.clear()
    monkeypatch.setattr(m, "cache", None)
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 60.0)
    monkeypatch.setenv("HISTORY_WIDEN_DAILY", "0")   # group231: these tests pin the per-period fetch path
    monkeypatch.setattr(m, "_history_last_good_get", lambda k: None)   # group230 durable store must not answer "all failed"
    yield
    m._mem._d.clear()
    m._history_flights.clear()


@pytest.fixture()
def angel(monkeypatch):
    calls = []
    state = {"delay": 0.0}

    def fake(sym, period, interval, days, start_date, end_date):
        calls.append(period)
        if state["delay"]:
            time.sleep(state["delay"])
        span = m._HISTORY_PERIOD_DAYS.get(period, 180)
        return _candles(span)

    monkeypatch.setattr(m, "_angelone_history_candles", fake)
    return calls, state


def _get(period, force=False, days=None):
    return m._get_history_impl("TCS", period, "1d", force, days)


# ── derive shorter period from a cached longer one ────────────────────────────
def test_shorter_periods_are_sliced_from_cached_1y(angel):
    calls, _ = angel
    r1y = _get("1y")
    assert calls == ["1y"] and r1y["period"] == "1y"
    r6 = _get("6mo")
    r3 = _get("3mo")
    r1 = _get("1mo")
    assert calls == ["1y"], "shorter periods must not go upstream"
    assert [r["period"] for r in (r6, r3, r1)] == ["6mo", "3mo", "1mo"]
    assert all(r["derived_from"] == "1y" for r in (r6, r3, r1))
    assert len(r1["candles"]) < len(r3["candles"]) < len(r6["candles"]) < len(r1y["candles"])
    cutoff = (datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=30)).isoformat()
    assert min(c["date"][:10] for c in r1["candles"]) >= cutoff
    assert r6["symbol"] == "TCS.NS" and r6["candles"][-1] == r1y["candles"][-1]


def test_derived_result_is_not_cached_and_does_not_mutate_source(angel):
    calls, _ = angel
    r1y = _get("1y")
    n = len(r1y["candles"])
    _get("1mo")
    assert m._cache_get("history:TCS.NS:1mo:1d:") is None
    assert len(m._cache_get("history:TCS.NS:1y:1d:")["candles"]) == n
    assert "derived_from" not in m._cache_get("history:TCS.NS:1y:1d:")


def test_longer_period_is_never_derived_from_shorter(angel):
    calls, _ = angel
    _get("6mo")
    _get("1y")
    assert calls == ["6mo", "1y"]


def test_smallest_cached_longer_period_wins(angel):
    calls, _ = angel
    _get("1y")
    _get("6mo")          # derived, not cached
    r = _get("3mo")
    assert r["derived_from"] == "1y" and calls == ["1y"]


def test_days_window_never_derives(angel):
    calls, _ = angel
    _get("1y")
    start = datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=20)
    monkey_calls = []
    # days= requests are exact windows keyed separately — they must go upstream
    r = m._get_history_impl("TCS", "1mo", "1d", False, 20)
    assert "derived_from" not in r
    assert len(calls) == 2 and start  # second upstream call happened


def test_too_few_bars_after_slice_falls_through_to_upstream(angel, monkeypatch):
    calls, _ = angel
    m._cache_set("history:TCS.NS:1y:1d:", {"symbol": "TCS", "period": "1y", "interval": "1d",
                                        "candles": _candles(400, step=100)})   # ~5 bars, 1 inside 30d
    r = _get("1mo")
    assert calls == ["1mo"] and "derived_from" not in r


def test_unknown_period_never_derives(angel):
    assert m._history_from_longer_cache("TCS.NS", "10y", "1d", None) is None


def test_derive_helper_swallows_bad_cache_shapes(angel):
    m._cache_set("history:TCS.NS:1y:1d:", {"candles": "not-a-list"})
    assert m._history_from_longer_cache("TCS.NS", "1mo", "1d", None) is None
    m._cache_set("history:TCS.NS:1y:1d:", {"candles": [None, 5, {"date": None}]})
    assert m._history_from_longer_cache("TCS.NS", "1mo", "1d", None) is None


def test_other_symbols_and_intervals_are_not_mixed(angel):
    calls, _ = angel
    _get("1y")
    m._get_history_impl("INFY", "1mo", "1d", False, None)
    m._get_history_impl("TCS", "1mo", "1h", False, None)
    assert calls == ["1y", "1mo", "1mo"]


# ── force=true reuse ──────────────────────────────────────────────────────────
def test_forced_request_reuses_result_fetched_moments_ago(angel):
    calls, _ = angel
    _get("6mo", force=True)
    _get("6mo", force=True)
    _get("6mo", force=True)
    assert calls == ["6mo"]


def test_forced_request_goes_upstream_when_reuse_disabled(angel, monkeypatch):
    calls, _ = angel
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 0.0)
    _get("6mo", force=True)
    _get("6mo", force=True)
    assert calls == ["6mo", "6mo"]


def test_forced_request_goes_upstream_once_fresh_stamp_expires(angel):
    calls, _ = angel
    _get("6mo", force=True)
    m._mem._d.pop("history:TCS.NS:6mo:1d::fresh", None)       # stamp expired, cache itself still warm
    _get("6mo", force=True)
    assert calls == ["6mo", "6mo"]


def test_forced_request_derives_only_from_a_fresh_longer_result(angel):
    calls, _ = angel
    _get("1y", force=True)
    r = _get("1mo", force=True)
    assert calls == ["1y"] and r["derived_from"] == "1y"
    m._mem._d.pop("history:TCS.NS:1y:1d::fresh", None)         # longer entry still cached but no longer fresh
    _get("3mo", force=True)
    assert calls == ["1y", "3mo"]


def test_unforced_fetch_also_stamps_fresh_for_later_forced_callers(angel):
    calls, _ = angel
    _get("6mo")
    _get("6mo", force=True)
    assert calls == ["6mo"]


def test_failed_fetch_leaves_no_fresh_stamp(angel, monkeypatch):
    monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: None)
    monkeypatch.setattr(m, "_nse_history_candles", lambda *a, **k: None)

    class _T:
        def history(self, *a, **k):
            import pandas as pd
            return pd.DataFrame()
    monkeypatch.setattr(m.yf, "Ticker", lambda c: _T(), raising=False)
    with pytest.raises(Exception):
        _get("6mo", force=True)
    assert m._mem.get("history:TCS.NS:6mo:1d::fresh") is None


def test_concurrent_identical_forced_requests_hit_upstream_once(angel):
    calls, state = angel
    state["delay"] = 0.3
    results, errs = [], []

    def worker():
        try:
            results.append(m.get_history("TCS", period="6mo", interval="1d", force=True, days=None))
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not errs and len(results) == 6
    assert calls == ["6mo"], calls


def test_forced_requests_for_different_periods_still_each_fetch_when_nothing_cached(angel):
    calls, _ = angel
    _get("1mo", force=True)
    _get("3mo", force=True)
    assert calls == ["1mo", "3mo"]


def test_route_forced_with_reuse_disabled_bypasses_flight(angel, monkeypatch):
    calls, _ = angel
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 0.0)
    m.get_history("TCS", period="6mo", interval="1d", force=True, days=None)
    m.get_history("TCS", period="6mo", interval="1d", force=True, days=None)
    assert calls == ["6mo", "6mo"]


def test_store_helper_tolerates_stamp_failure(monkeypatch):
    class _Boom:
        _d: dict = {}
        def set(self, *a, **k): raise RuntimeError("mem down")
    monkeypatch.setattr(m, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(m, "_mem", _Boom())
    m._history_store("k", {"a": 1}, 10)      # must not raise
