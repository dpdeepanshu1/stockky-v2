"""
tests/test_technical_main.py — coverage for technical/main.py

No network, no DB. httpx.get / httpx.Client are faked, yfinance and sqlalchemy
are replaced through sys.modules, rate_limit_report is stubbed, and the
history fetch chain is monkeypatched where analyze() is under test. Route
functions are called directly (FastAPI's @app.get returns the original
function), so no TestClient is needed.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_technical_main.py -v
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys
import types
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "technical"))

import httpx
import numpy as np
import pandas as pd
import pytest

import main as tm  # noqa: E402  (technical/main.py)


def run(coro):
    return asyncio.run(coro)


# ── shared helpers ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate the module-level caches for every test."""
    tm._mem_tech.clear()
    tm._mem_tech_exp.clear()
    monkeypatch.setattr(tm, "cache", None)
    yield
    tm._mem_tech.clear()
    tm._mem_tech_exp.clear()


@pytest.fixture()
def rl(monkeypatch):
    """Stub rate_limit_report; records calls."""
    calls = {"hit": [], "report": []}
    fake = types.ModuleType("rate_limit_report")
    fake.record_rate_limit_hit = lambda **kw: calls["hit"].append(kw)
    fake.report_if_rate_limited = lambda exc, **kw: calls["report"].append((exc, kw))
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    return calls


class _Resp:
    def __init__(self, status=200, payload=None, content=b"x", bad_json=False):
        self.status_code = status
        self._payload = payload
        self.content = content
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


def _candles(n, start=100.0, step=1.0, vol=1000):
    base = _dt.date(2026, 1, 1)
    out = []
    for i in range(n):
        c = start + step * i
        out.append({
            "date": (base + _dt.timedelta(days=i)).isoformat(),
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": vol,
        })
    return out


def _df(n, start=100.0, step=1.0, vol=1000, closes=None):
    """OHLCV frame in the shape _candles_to_df produces (capitalised columns)."""
    if closes is None:
        closes = [start + step * i for i in range(n)]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({
        "Open": c, "High": c + 1, "Low": c - 1, "Close": c,
        "Volume": [vol] * len(closes),
    }, index=idx)


def _series(vals):
    return pd.Series(vals, dtype=float)


# ── module import / optional-dependency blocks ────────────────────────────────

class TestImport:
    def test_constants(self):
        assert tm.CORPORATE_ACTION_JUMP_THRESHOLD == 30.0
        assert tm.MARKET_DATA_URL and not tm.MARKET_DATA_URL.endswith("/")

    def test_app_metadata(self):
        assert tm.app.version == "0.3.0"


def _reload_with(monkeypatch, tmp_path, *, block=(), env=None, extra_modules=None):
    """Exec technical/main.py fresh under controlled import conditions."""
    import importlib.util

    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    for name in block:
        monkeypatch.setitem(sys.modules, name, None)  # forces ImportError
    for name, mod in (extra_modules or {}).items():
        monkeypatch.setitem(sys.modules, name, mod)
    path = os.path.join(os.path.dirname(_HERE), "technical", "main.py")
    spec = importlib.util.spec_from_file_location("technical_main_fresh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestImportFallbacks:
    def test_return_sanity_missing_uses_inline_clamp(self, monkeypatch, tmp_path):
        mod = _reload_with(monkeypatch, tmp_path, block=("return_sanity",))
        assert mod.CORPORATE_ACTION_JUMP_THRESHOLD == 30.0
        assert mod._clamp_for_atr(5.0) == 5.0
        assert mod._clamp_for_atr(-30.0) == -30.0
        assert mod._clamp_for_atr(30.01) is None
        assert mod._clamp_for_atr(-31) is None
        assert mod._clamp_for_atr(None) is None

    def test_upstash_missing_sets_redis_none(self, monkeypatch, tmp_path):
        mod = _reload_with(monkeypatch, tmp_path, block=("upstash_redis",))
        assert mod.Redis is None
        assert mod.cache is None

    def test_redis_enabled_and_pings(self, monkeypatch, tmp_path):
        pinged = {}

        class FakeRedis:
            def __init__(self, url, token):
                pinged["init"] = (url, token)

            def ping(self):
                pinged["ping"] = True

        fake_mod = types.ModuleType("upstash_redis")
        fake_mod.Redis = FakeRedis
        mod = _reload_with(
            monkeypatch, tmp_path,
            env={"USE_REDIS": "1", "UPSTASH_REDIS_REST_URL": "https://u", "UPSTASH_REDIS_REST_TOKEN": "tok"},
            extra_modules={"upstash_redis": fake_mod},
        )
        assert isinstance(mod.cache, FakeRedis)
        assert pinged == {"init": ("https://u", "tok"), "ping": True}

    def test_redis_ping_failure_falls_back_to_memory(self, monkeypatch, tmp_path):
        class BadRedis:
            def __init__(self, url, token):
                pass

            def ping(self):
                raise RuntimeError("no route")

        fake_mod = types.ModuleType("upstash_redis")
        fake_mod.Redis = BadRedis
        mod = _reload_with(
            monkeypatch, tmp_path,
            env={"USE_REDIS": "true", "UPSTASH_REDIS_REST_URL": "https://u", "UPSTASH_REDIS_REST_TOKEN": "tok"},
            extra_modules={"upstash_redis": fake_mod},
        )
        assert mod.cache is None

    def test_use_redis_without_credentials_stays_memory(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
        monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
        mod = _reload_with(monkeypatch, tmp_path, env={"USE_REDIS": "1"})
        assert mod.cache is None

    def test_market_data_url_trailing_slash_stripped(self, monkeypatch, tmp_path):
        mod = _reload_with(monkeypatch, tmp_path, env={"MARKET_DATA_URL": "http://md:8001///"})
        assert mod.MARKET_DATA_URL == "http://md:8001"


# ── _rs_vs_nifty ──────────────────────────────────────────────────────────────

class TestRsVsNifty:
    def test_none_series(self):
        assert tm._rs_vs_nifty(None) == (50.0, False)

    def test_short_series(self):
        assert tm._rs_vs_nifty(_series(range(1, 21))) == (50.0, False)

    def test_no_nifty_series_treats_nifty_as_flat(self):
        closes = _series([100.0] * 20 + [105.0])  # +5% over 20 sessions back
        score, ext = tm._rs_vs_nifty(closes)
        assert score == 75.0  # 50 + 5*5
        assert ext is False

    def test_short_nifty_series_ignored(self):
        closes = _series([100.0] * 20 + [105.0])
        score, _ = tm._rs_vs_nifty(closes, _series([1, 2, 3]))
        assert score == 75.0

    def test_excess_return_vs_nifty(self):
        closes = _series([100.0] * 20 + [110.0])
        nifty = _series([200.0] * 20 + [204.0])  # +2%
        score, ext = tm._rs_vs_nifty(closes, nifty)
        assert score == 90.0  # excess 8pp → 50 + 40
        assert ext is False

    def test_score_clamped_high(self):
        closes = _series([100.0] * 20 + [150.0])
        score, ext = tm._rs_vs_nifty(closes)
        assert score == 100.0
        assert ext is True

    def test_score_clamped_low(self):
        closes = _series([100.0] * 20 + [70.0])
        score, ext = tm._rs_vs_nifty(closes)
        assert score == 0.0
        assert ext is False

    def test_extended_boundary(self):
        assert tm._rs_vs_nifty(_series([100.0] * 20 + [118.0]))[1] is False
        assert tm._rs_vs_nifty(_series([100.0] * 20 + [118.5]))[1] is True

    def test_exception_returns_neutral(self):
        class Boom:
            def __len__(self):
                return 30

            @property
            def iloc(self):
                raise RuntimeError("bad")

        assert tm._rs_vs_nifty(Boom()) == (50.0, False)


# ── market hours / TTL ────────────────────────────────────────────────────────

def _fake_now(monkeypatch, y, m, d, hh, mm):
    real = _dt.datetime(y, m, d, hh, mm, tzinfo=_dt.timezone(_dt.timedelta(hours=5, minutes=30)))

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return real

    monkeypatch.setattr(tm, "datetime", FakeDT)


class TestMarketHours:
    # 2026-09-29 is a Tuesday; 2026-09-26 Saturday; 2026-09-27 Sunday.
    def test_weekday_midday_open(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 29, 11, 0)
        assert tm.is_market_open() is True
        assert tm.get_cache_ttl() == 300

    def test_open_boundary_inclusive(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 29, 9, 15)
        assert tm.is_market_open() is True

    def test_close_boundary_inclusive(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 29, 15, 30)
        assert tm.is_market_open() is True

    def test_before_open(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 29, 9, 14)
        assert tm.is_market_open() is False
        assert tm.get_cache_ttl() == 21600

    def test_after_close(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 29, 15, 31)
        assert tm.is_market_open() is False

    def test_saturday_closed(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 26, 11, 0)
        assert tm.is_market_open() is False
        assert tm.get_cache_ttl() == 21600

    def test_sunday_closed(self, monkeypatch):
        _fake_now(monkeypatch, 2026, 9, 27, 11, 0)
        assert tm.is_market_open() is False

    def test_real_clock_returns_bool(self):
        assert isinstance(tm.is_market_open(), bool)
        assert tm.get_cache_ttl() in (300, 21600)


# ── cache ─────────────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self, store=None, get_exc=None, set_exc=None):
        self.store = store or {}
        self.get_exc = get_exc
        self.set_exc = set_exc
        self.setex_calls = []

    def get(self, key):
        if self.get_exc:
            raise self.get_exc
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.set_exc:
            raise self.set_exc
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


class TestCache:
    def test_get_miss_without_redis(self):
        assert tm._cache_get("nope") is None

    def test_set_then_get_memory(self):
        tm._cache_set("k", {"a": 1}, ttl=60)
        assert tm._cache_get("k") == {"a": 1}

    def test_set_uses_default_ttl_when_none(self, monkeypatch):
        monkeypatch.setattr(tm, "get_cache_ttl", lambda: 123)
        monkeypatch.setattr(tm.time, "time", lambda: 1000.0)
        tm._cache_set("k", {"a": 1})
        assert tm._mem_tech_exp["k"] == 1123.0

    def test_expired_entry_not_returned(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(tm.time, "time", lambda: now["t"])
        tm._cache_set("k", {"a": 1}, ttl=10)
        now["t"] = 1009.0
        assert tm._cache_get("k") == {"a": 1}
        now["t"] = 1011.0
        assert tm._cache_get("k") is None

    def test_entry_without_expiry_is_returned(self):
        tm._mem_tech["k"] = {"a": 1}
        assert tm._cache_get("k") == {"a": 1}

    def test_expired_memory_falls_through_to_redis(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(tm.time, "time", lambda: now["t"])
        r = _FakeRedis(store={"k": '{"from": "redis"}'})
        monkeypatch.setattr(tm, "cache", r)
        tm._cache_set("k", {"from": "mem"}, ttl=1)
        r.store["k"] = '{"from": "redis"}'   # Redis copy changed independently of memory
        now["t"] = 2000.0
        assert tm._cache_get("k") == {"from": "redis"}

    def test_redis_miss_returns_none(self, monkeypatch):
        monkeypatch.setattr(tm, "cache", _FakeRedis())
        assert tm._cache_get("k") is None

    def test_redis_get_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(tm, "cache", _FakeRedis(get_exc=RuntimeError("down")))
        assert tm._cache_get("k") is None

    def test_redis_bad_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(tm, "cache", _FakeRedis(store={"k": "{not json"}))
        assert tm._cache_get("k") is None

    def test_set_writes_through_to_redis(self, monkeypatch):
        r = _FakeRedis()
        monkeypatch.setattr(tm, "cache", r)
        tm._cache_set("k", {"a": 1}, ttl=45)
        assert r.setex_calls == [("k", 45, '{"a": 1}')]

    def test_set_serialises_non_json_types_with_default_str(self, monkeypatch):
        r = _FakeRedis()
        monkeypatch.setattr(tm, "cache", r)
        tm._cache_set("k", {"d": _dt.date(2026, 1, 2)}, ttl=5)
        assert '"2026-01-02"' in r.setex_calls[0][2]

    def test_set_redis_failure_propagates(self, monkeypatch):
        # Pin current behaviour: memory is written first, then the Redis error surfaces.
        monkeypatch.setattr(tm, "cache", _FakeRedis(set_exc=RuntimeError("down")))
        with pytest.raises(RuntimeError):
            tm._cache_set("k", {"a": 1}, ttl=5)
        assert tm._cache_get("k") == {"a": 1}


# ── small helpers ─────────────────────────────────────────────────────────────

class TestNormalizeSymbol:
    @pytest.mark.parametrize("raw,expected", [
        ("tcs", "TCS"), (" reliance.ns ", "RELIANCE"), ("infy.BO", "INFY"),
        ("HDFCBANK.NS", "HDFCBANK"), ("m&m", "M&M"),
    ])
    def test_cases(self, raw, expected):
        assert tm.normalize_symbol(raw) == expected

    def test_lowercase_suffix_is_uppercased_first(self):
        # unlike news/main.py's _base_symbol, upper() runs before the suffix strip
        assert tm.normalize_symbol("tcs.ns") == "TCS"


class TestSafe:
    def test_number(self):
        assert tm._safe(3.14159) == 3.14

    def test_decimals(self):
        assert tm._safe(3.14159, 3) == 3.142

    def test_string_number(self):
        assert tm._safe("2.5") == 2.5

    @pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), float("-inf"), [1]])
    def test_bad_values(self, bad):
        assert tm._safe(bad) is None

    def test_zero_is_zero_not_none(self):
        assert tm._safe(0) == 0.0


class TestFetchQuotePrice:
    def test_ok(self, monkeypatch):
        seen = {}

        def fake_get(url, timeout=None):
            seen["url"], seen["timeout"] = url, timeout
            return _Resp(200, {"price": 123.4})

        monkeypatch.setattr(tm.httpx, "get", fake_get)
        assert tm._fetch_quote_price("TCS") == 123.4
        assert seen["url"].endswith("/quote/TCS") and seen["timeout"] == 10

    def test_non_200(self, monkeypatch):
        monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: _Resp(500, {"price": 1}))
        assert tm._fetch_quote_price("TCS") is None

    def test_missing_price_key(self, monkeypatch):
        monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: _Resp(200, {}))
        assert tm._fetch_quote_price("TCS") is None

    def test_exception(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("no")

        monkeypatch.setattr(tm.httpx, "get", boom)
        assert tm._fetch_quote_price("TCS") is None


# ── _candles_to_df ────────────────────────────────────────────────────────────

class TestCandlesToDf:
    @pytest.mark.parametrize("c", [None, [], _candles(4)])
    def test_too_few(self, c):
        assert tm._candles_to_df(c) is None

    def test_ok_shape_and_index(self):
        df = tm._candles_to_df(_candles(10))
        assert len(df) == 10
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name == "date"
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_capitalises_arbitrary_columns(self):
        cs = _candles(6)
        for c in cs:
            c["adj_close"] = c["close"]
        assert "Adj_close" in tm._candles_to_df(cs).columns

    def test_non_numeric_close_rows_dropped(self):
        cs = _candles(8)
        cs[2]["close"] = "n/a"
        cs[5]["close"] = None
        df = tm._candles_to_df(cs)
        assert len(df) == 6
        assert df["Close"].notna().all()

    def test_numeric_strings_coerced(self):
        cs = _candles(6)
        for c in cs:
            c["close"] = str(c["close"])
            c["volume"] = str(c["volume"])
        df = tm._candles_to_df(cs)
        assert df["Close"].dtype.kind == "f"

    def test_too_few_after_dropna(self):
        cs = _candles(6)
        for i in range(3):
            cs[i]["close"] = None
        assert tm._candles_to_df(cs) is None

    def test_missing_ohlv_columns_tolerated(self):
        cs = [{"date": c["date"], "close": c["close"]} for c in _candles(6)]
        df = tm._candles_to_df(cs)
        assert list(df.columns) == ["Close"]


# ── _fetch_history_yfinance ───────────────────────────────────────────────────

def _fake_yf(monkeypatch, frames, raise_on=None):
    """frames: list of DataFrames/None returned by successive Ticker().history()."""
    seen = {"symbols": [], "kw": []}
    it = iter(frames)

    class Ticker:
        def __init__(self, sym):
            seen["symbols"].append(sym)
            if raise_on == "ctor":
                raise RuntimeError("yahoo down")

        def history(self, **kw):
            seen["kw"].append(kw)
            return next(it)

    mod = types.ModuleType("yfinance")
    mod.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return seen


def _yf_frame(n, nan_volume_at=None):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    c = np.arange(100.0, 100.0 + n)
    vol = np.full(n, 5000.0)
    if nan_volume_at is not None:
        vol[nan_volume_at] = np.nan
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


class TestFetchHistoryYfinance:
    def test_ns_history_used(self, monkeypatch):
        seen = _fake_yf(monkeypatch, [_yf_frame(30)])
        df = tm._fetch_history_yfinance("TCS")
        assert seen["symbols"] == ["TCS.NS"]
        assert seen["kw"][0] == {"period": "6mo", "interval": "1d", "auto_adjust": True}
        assert len(df) == 30 and df["Volume"].iloc[0] == 5000

    def test_falls_back_to_bare_symbol_when_ns_empty(self, monkeypatch):
        seen = _fake_yf(monkeypatch, [pd.DataFrame(), _yf_frame(10)])
        df = tm._fetch_history_yfinance("TCS")
        assert seen["symbols"] == ["TCS.NS", "TCS"]
        assert len(df) == 10

    def test_none_history_falls_back_to_bare_symbol(self, monkeypatch):
        seen = _fake_yf(monkeypatch, [None, _yf_frame(10)])
        assert len(tm._fetch_history_yfinance("TCS")) == 10
        assert seen["symbols"] == ["TCS.NS", "TCS"]

    def test_both_empty_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, [pd.DataFrame(), None])
        assert tm._fetch_history_yfinance("TCS") is None

    def test_nan_volume_becomes_zero(self, monkeypatch):
        _fake_yf(monkeypatch, [_yf_frame(10, nan_volume_at=3)])
        df = tm._fetch_history_yfinance("TCS")
        assert df["Volume"].iloc[3] == 0

    def test_bad_row_skipped(self, monkeypatch):
        f = _yf_frame(10).astype(object)
        f.iloc[4, f.columns.get_loc("Close")] = "oops"
        _fake_yf(monkeypatch, [f])
        df = tm._fetch_history_yfinance("TCS")
        assert len(df) == 9

    def test_too_few_rows_after_conversion_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, [_yf_frame(4)])
        assert tm._fetch_history_yfinance("TCS") is None

    def test_exception_reports_rate_limit_and_returns_none(self, monkeypatch, rl):
        _fake_yf(monkeypatch, [], raise_on="ctor")
        assert tm._fetch_history_yfinance("TCS") is None
        assert len(rl["report"]) == 1
        exc, kw = rl["report"][0]
        assert isinstance(exc, RuntimeError)
        assert kw == {"provider": "market_data", "path": "technical/yfinance", "symbol": "TCS"}

    def test_exception_with_reporter_failure_still_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, [], raise_on="ctor")
        fake = types.ModuleType("rate_limit_report")

        def boom(*a, **k):
            raise RuntimeError("reporter down")

        fake.report_if_rate_limited = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        assert tm._fetch_history_yfinance("TCS") is None

    def test_exception_with_reporter_import_failure_still_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, [], raise_on="ctor")
        monkeypatch.setitem(sys.modules, "rate_limit_report", None)
        assert tm._fetch_history_yfinance("TCS") is None

    def test_yfinance_import_failure_returns_none(self, monkeypatch, rl):
        monkeypatch.setitem(sys.modules, "yfinance", None)
        assert tm._fetch_history_yfinance("TCS") is None


# ── _fetch_history_from_market_data ───────────────────────────────────────────

class TestFetchHistoryFromMarketData:
    def _get(self, monkeypatch, resp=None, exc=None):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["url"], seen["params"], seen["timeout"] = url, params, timeout
            if exc:
                raise exc
            return resp

        monkeypatch.setattr(tm.httpx, "get", fake_get)
        return seen

    def test_ok(self, monkeypatch):
        seen = self._get(monkeypatch, _Resp(200, {"candles": _candles(30)}))
        df = tm._fetch_history_from_market_data("TCS", period="3mo", force=True)
        assert len(df) == 30
        assert seen["url"].endswith("/history/TCS")
        assert seen["params"] == {"period": "3mo", "force": "true"}
        assert seen["timeout"] == 35

    def test_defaults_force_false_period_6mo(self, monkeypatch):
        seen = self._get(monkeypatch, _Resp(200, {"candles": _candles(30)}))
        tm._fetch_history_from_market_data("TCS")
        assert seen["params"] == {"period": "6mo", "force": "false"}

    def test_short_candles_returns_none(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"candles": _candles(3)}))
        assert tm._fetch_history_from_market_data("TCS") is None

    def test_missing_candles_key(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {}))
        assert tm._fetch_history_from_market_data("TCS") is None

    @pytest.mark.parametrize("status", [429, 503])
    def test_rate_limit_status_recorded(self, monkeypatch, rl, status):
        self._get(monkeypatch, _Resp(status))
        assert tm._fetch_history_from_market_data("TCS") is None
        assert rl["hit"] == [{"provider": "market_data", "status": status,
                              "path": "/history/TCS", "symbol": "TCS"}]

    @pytest.mark.parametrize("status", [404, 500, 502])
    def test_other_status_not_recorded(self, monkeypatch, rl, status):
        self._get(monkeypatch, _Resp(status))
        assert tm._fetch_history_from_market_data("TCS") is None
        assert rl["hit"] == []

    def test_record_hit_failure_swallowed(self, monkeypatch):
        self._get(monkeypatch, _Resp(429))
        fake = types.ModuleType("rate_limit_report")

        def boom(**kw):
            raise RuntimeError("reporter down")

        fake.record_rate_limit_hit = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        assert tm._fetch_history_from_market_data("TCS") is None

    def test_http_error_reports_and_returns_none(self, monkeypatch, rl):
        err = httpx.ReadTimeout("slow")
        self._get(monkeypatch, exc=err)
        assert tm._fetch_history_from_market_data("TCS") is None
        assert rl["report"] == [(err, {"provider": "market_data", "path": "/history/TCS", "symbol": "TCS"})]

    def test_http_error_reporter_failure_swallowed(self, monkeypatch):
        self._get(monkeypatch, exc=httpx.ConnectError("no"))
        monkeypatch.setitem(sys.modules, "rate_limit_report", None)
        assert tm._fetch_history_from_market_data("TCS") is None

    def test_non_http_exception_propagates(self, monkeypatch):
        self._get(monkeypatch, exc=ValueError("not an httpx error"))
        with pytest.raises(ValueError):
            tm._fetch_history_from_market_data("TCS")


# ── _fetch_history_bhavcopy_hint ──────────────────────────────────────────────

class TestBhavcopyHint:
    def _get(self, monkeypatch, resp=None, exc=None):
        seen = {}

        def fake_get(url, timeout=None):
            seen["url"], seen["timeout"] = url, timeout
            if exc:
                raise exc
            return resp

        monkeypatch.setattr(tm.httpx, "get", fake_get)
        return seen

    def test_ok_builds_single_bar(self, monkeypatch):
        seen = self._get(monkeypatch, _Resp(200, {"price": 250.5, "volume": 1200}))
        df = tm._fetch_history_bhavcopy_hint("TCS")
        assert seen["url"].endswith("/quote/TCS") and seen["timeout"] == 12
        assert len(df) == 1
        row = df.iloc[0]
        assert row["Open"] == row["High"] == row["Low"] == row["Close"] == 250.5
        assert row["Volume"] == 1200
        assert df.attrs["bhavcopy_hint"] is True

    def test_non_200(self, monkeypatch):
        self._get(monkeypatch, _Resp(404, {"price": 5}))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None

    def test_empty_body_treated_as_empty_quote(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, None, content=b""))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None

    def test_key_priority_order(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"close": 90, "ltp": 95, "cmp": 99, "price": 0}))
        df = tm._fetch_history_bhavcopy_hint("TCS")
        assert df["Close"].iloc[0] == 99.0  # price=0 skipped, cmp wins over ltp/close

    def test_prev_close_used_last(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"prev_close": 77.0}))
        assert tm._fetch_history_bhavcopy_hint("TCS")["Close"].iloc[0] == 77.0

    def test_non_numeric_values_skipped(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"price": "abc", "cmp": None, "last_price": "12.5"}))
        assert tm._fetch_history_bhavcopy_hint("TCS")["Close"].iloc[0] == 12.5

    def test_negative_price_ignored(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"price": -5, "ltp": 0}))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None

    def test_no_price_returns_none(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"volume": 10}))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None

    def test_missing_volume_defaults_zero(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, {"price": 10}))
        assert tm._fetch_history_bhavcopy_hint("TCS")["Volume"].iloc[0] == 0

    def test_exception_returns_none(self, monkeypatch):
        self._get(monkeypatch, exc=httpx.ConnectError("no"))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None

    def test_bad_json_returns_none(self, monkeypatch):
        self._get(monkeypatch, _Resp(200, bad_json=True))
        assert tm._fetch_history_bhavcopy_hint("TCS") is None


# ── _fetch_history (fallback chain) ───────────────────────────────────────────

class _Chain:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.md_calls = []
        self.md_results = {}   # period -> df (or list of dfs consumed in order)
        self.md_exc = None
        self.health_exc = None
        self.health_calls = []
        self.yf = None
        self.yf_calls = 0
        self.hint = None
        self.hint_calls = 0
        monkeypatch.setattr(tm.httpx, "get", self._health)
        monkeypatch.setattr(tm, "_fetch_history_from_market_data", self._md)
        monkeypatch.setattr(tm, "_fetch_history_yfinance", self._yf)
        monkeypatch.setattr(tm, "_fetch_history_bhavcopy_hint", self._hint)

    def _health(self, url, params=None, timeout=None):
        self.health_calls.append((url, params, timeout))
        if self.health_exc:
            raise self.health_exc
        return _Resp(200, {})

    def _md(self, symbol, period="6mo", force=False):
        self.md_calls.append((symbol, period, force))
        if self.md_exc:
            raise self.md_exc
        return self.md_results.get(period)

    def _yf(self, symbol):
        self.yf_calls += 1
        return self.yf

    def _hint(self, symbol):
        self.hint_calls += 1
        return self.hint


class TestFetchHistory:
    def test_first_period_with_20_plus_rows_wins(self, monkeypatch):
        c = _Chain(monkeypatch)
        good = _df(25)
        c.md_results["6mo"] = good
        assert tm._fetch_history("TCS") is good
        assert c.md_calls == [("TCS", "6mo", False)]
        assert c.yf_calls == 0

    def test_warms_market_data_health_first(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.md_results["6mo"] = _df(25)
        tm._fetch_history("TCS")
        url, params, timeout = c.health_calls[0]
        assert url.endswith("/health") and params == {"warm": "true"} and timeout == 8

    def test_health_failure_ignored(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.health_exc = httpx.ConnectError("cold")
        c.md_results["6mo"] = _df(25)
        assert len(tm._fetch_history("TCS")) == 25

    def test_falls_through_periods_in_order(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.md_results["1mo"] = _df(22)
        df = tm._fetch_history("TCS", force=True)
        assert len(df) == 22
        assert [p for _, p, _ in c.md_calls] == ["6mo", "3mo", "1mo"]
        assert all(f is True for _, _, f in c.md_calls)

    def test_short_series_accepted_after_all_periods_tried(self, monkeypatch):
        c = _Chain(monkeypatch)
        short = _df(8)
        c.md_results = {"6mo": short, "3mo": short, "1mo": short, "1y": short}
        assert tm._fetch_history("TCS") is short
        # 4 period attempts + 1 final "accept short" 6mo retry
        assert [p for _, p, _ in c.md_calls] == ["6mo", "3mo", "1mo", "1y", "6mo"]

    def test_mixed_short_then_none_still_uses_final_retry(self, monkeypatch):
        c = _Chain(monkeypatch)
        short = _df(6)
        c.md_results = {"6mo": short}   # only the first/last 6mo attempts hit
        assert tm._fetch_history("TCS") is short

    def test_very_short_series_below_5_rows_also_returned_by_final_retry(self, monkeypatch):
        c = _Chain(monkeypatch)
        tiny = _df(3)
        c.md_results = {"6mo": tiny}
        assert tm._fetch_history("TCS") is tiny

    def test_all_market_data_none_falls_to_yfinance(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.yf = _df(40)
        df = tm._fetch_history("TCS")
        assert len(df) == 40
        assert c.yf_calls == 1 and c.hint_calls == 0

    def test_yfinance_result_trimmed_to_last_260(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.yf = _df(300)
        df = tm._fetch_history("TCS")
        assert len(df) == 260
        assert df["Close"].iloc[-1] == c.yf["Close"].iloc[-1]

    def test_yfinance_exactly_260_not_trimmed(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.yf = _df(260)
        assert len(tm._fetch_history("TCS")) == 260

    def test_market_data_chain_exception_falls_to_yfinance(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.md_exc = RuntimeError("chain blew up")
        c.yf = _df(40)
        assert len(tm._fetch_history("TCS")) == 40

    def test_yfinance_none_falls_to_hint(self, monkeypatch):
        c = _Chain(monkeypatch)
        c.hint = _df(1)
        assert tm._fetch_history("TCS") is c.hint
        assert c.hint_calls == 1

    def test_everything_fails_returns_none(self, monkeypatch):
        _Chain(monkeypatch)
        assert tm._fetch_history("TCS") is None


# ── indicators ────────────────────────────────────────────────────────────────

class TestRsi:
    def test_alternating_series_is_50(self):
        s = _series([10, 11] * 20)
        assert tm._rsi(s).iloc[-1] == pytest.approx(50.0, abs=1.0)

    def test_known_value(self):
        # 14 changes: 7 gains of +2, 7 losses of -1 → avg gain 1, avg loss 0.5 → RS 2 → RSI 66.67
        vals = [100.0]
        for i in range(7):
            vals += [vals[-1] + 2, vals[-1] + 2 - 1]
        s = _series(vals)
        assert tm._rsi(s).iloc[-1] == pytest.approx(66.6667, abs=0.01)

    def test_monotonic_fall_is_zero(self):
        assert tm._rsi(_series(np.arange(40, 0, -1))).iloc[-1] == 0.0

    def test_monotonic_rise_is_nan_not_100(self):
        # Source quirk, pinned: no losses → loss.replace(0, nan) → RS NaN → RSI NaN.
        assert np.isnan(tm._rsi(_series(np.arange(1, 40))).iloc[-1])

    def test_flat_series_is_nan(self):
        assert np.isnan(tm._rsi(_series([10.0] * 30)).iloc[-1])

    def test_warmup_is_nan(self):
        assert tm._rsi(_series(np.arange(1, 40) % 3)).iloc[:13].isna().all()

    def test_custom_period(self):
        s = _series([10, 11] * 20)
        assert not np.isnan(tm._rsi(s, period=5).iloc[5])


class TestEmaMacd:
    def test_ema_constant_series(self):
        assert tm._ema(_series([5.0] * 30), 10).iloc[-1] == pytest.approx(5.0)

    def test_ema_first_value_is_seed(self):
        assert tm._ema(_series([7.0, 9.0, 11.0]), 3).iloc[0] == 7.0

    def test_ema_recursion(self):
        # span 3 → alpha 0.5, adjust=False: e1 = 0.5*9 + 0.5*7 = 8
        assert tm._ema(_series([7.0, 9.0]), 3).iloc[1] == pytest.approx(8.0)

    def test_macd_returns_line_and_signal(self):
        line, sig = tm._macd(_series(np.arange(1, 60, dtype=float)))
        assert len(line) == len(sig) == 59
        assert line.iloc[-1] > 0  # uptrend: fast EMA above slow
        assert sig.iloc[-1] == pytest.approx(tm._ema(line, 9).iloc[-1])

    def test_macd_flat_is_zero(self):
        line, sig = tm._macd(_series([50.0] * 40))
        assert line.iloc[-1] == pytest.approx(0.0) and sig.iloc[-1] == pytest.approx(0.0)

    def test_macd_downtrend_negative(self):
        line, _ = tm._macd(_series(np.arange(60, 1, -1, dtype=float)))
        assert line.iloc[-1] < 0


class TestAdxAtr:
    def _ohlc(self, n=60, step=1.0):
        c = _series(100 + step * np.arange(n))
        return c + 1, c - 1, c

    def test_atr_constant_range(self):
        h, l, c = self._ohlc(step=0.0)
        assert tm._atr(h, l, c).iloc[-1] == pytest.approx(2.0)

    def test_atr_true_range_includes_overnight_gap(self):
        c = _series([100.0] * 30 + [110.0])   # +10% gap: below the 30% corporate-action cut
        a = tm._atr(c + 1, c - 1, c)
        # last TR = high 111 - prev close 100 = 11; window = 13 × 2 + 11 → 37/14
        assert a.iloc[-1] == pytest.approx(37 / 14)

    def test_atr_min_periods_5(self):
        h, l, c = self._ohlc(n=10, step=0.0)
        a = tm._atr(h, l, c)
        # bar 0 has no pct_change (NaN → treated as "not clamped-in"), so 5 valid TRs first exist at bar 5
        assert a.iloc[:5].isna().all()          # min_periods=5
        assert a.iloc[5] == pytest.approx(2.0)
        assert a.iloc[-1] == pytest.approx(2.0)

    def test_atr_excludes_corporate_action_day(self):
        n = 40
        c = np.full(n, 100.0)
        c[20:] = 200.0                       # +100% one-day jump = split/bonus
        s = _series(c)
        a = tm._atr(s + 1, s - 1, s)
        # jump-day TR (~101) is excluded from the rolling mean, so ATR stays ≈ 2
        assert a.iloc[-1] == pytest.approx(2.0)
        assert a.iloc[25] == pytest.approx(2.0)

    def test_atr_keeps_moves_just_under_threshold(self):
        c = np.full(40, 100.0)
        c[20:] = 129.0                       # +29% → kept (threshold is 30%)
        s = _series(c)
        a = tm._atr(s + 1, s - 1, s)
        assert a.iloc[20] > 3.5              # jump-day TR (~30) is in the window: (13×2 + 30)/14 = 4.0
        assert a.iloc[20] == pytest.approx(4.0)

    def test_atr_drops_moves_just_over_threshold(self):
        c = np.full(40, 100.0)
        c[20:] = 131.0                       # +31% → excluded
        s = _series(c)
        assert tm._atr(s + 1, s - 1, s).iloc[20] == pytest.approx(2.0)

    def test_adx_strong_trend_high(self):
        h, l, c = self._ohlc(n=80, step=2.0)
        assert tm._adx(h, l, c).iloc[-1] > 25

    def test_adx_returns_series_same_length(self):
        h, l, c = self._ohlc(n=50, step=1.0)
        assert len(tm._adx(h, l, c)) == 50

    def test_adx_flat_market_is_nan(self):
        s = _series([100.0] * 60)
        assert np.isnan(tm._adx(s, s, s).iloc[-1])

    def test_adx_choppy_low(self):
        c = _series(100 + np.tile([0, 1, 0, -1], 30))
        assert tm._adx(c + 1, c - 1, c).iloc[-1] < 25


class TestBollingerSupportResistance:
    def test_bollinger_flat(self):
        up, lo = tm._bollinger(_series([10.0] * 30))
        assert up.iloc[-1] == lo.iloc[-1] == 10.0

    def test_bollinger_symmetric_bands(self):
        s = _series(np.arange(1, 41, dtype=float))
        up, lo = tm._bollinger(s)
        mid = s.rolling(20).mean()
        assert (up.iloc[-1] - mid.iloc[-1]) == pytest.approx(mid.iloc[-1] - lo.iloc[-1])
        assert up.iloc[-1] > lo.iloc[-1]

    def test_bollinger_two_std(self):
        s = _series(np.arange(1, 41, dtype=float))
        up, _ = tm._bollinger(s)
        assert up.iloc[-1] == pytest.approx(s.tail(20).mean() + 2 * s.tail(20).std())

    def test_bollinger_custom_period(self):
        up, _ = tm._bollinger(_series(np.arange(1, 11, dtype=float)), period=5)
        assert not np.isnan(up.iloc[4])

    def test_support_resistance_window(self):
        df = _df(30)
        sup, res = tm._support_resistance(df, window=10)
        assert sup == df["Low"].tail(10).min() == 119.0   # Low = close - 1 over closes 120..129
        assert res == df["High"].tail(10).max() == 130.0  # High = close + 1

    def test_support_resistance_default_window_20(self):
        df = _df(40)
        sup, res = tm._support_resistance(df)
        assert sup == 119.0 and res == 140.0  # last 20 closes are 120..139; Low = close-1, High = close+1

    def test_support_resistance_returns_floats(self):
        sup, res = tm._support_resistance(_df(10), window=5)
        assert isinstance(sup, float) and isinstance(res, float)


# ── simple routes ─────────────────────────────────────────────────────────────

class TestSimpleRoutes:
    def test_health(self):
        assert tm.health() == {"status": "ok", "service": "technical-analysis-service"}

    def test_root(self):
        r = tm.root()
        assert r["service"] == "technical-analysis-service"
        assert r["version"] == "0.3.0" and r["status"] == "ok"
        assert "/analyze/{symbol}" in r["endpoints"]

    def test_http_routes_registered(self):
        paths = {r.path for r in tm.app.routes}
        assert {"/health", "/", "/analyze/{symbol}", "/sector-strength/{symbol}"} <= paths

    def test_cors_middleware_installed(self):
        assert any("CORSMiddleware" in str(m) for m in tm.app.user_middleware)


# ── analyze(): fallback + cache behaviour ─────────────────────────────────────

@pytest.fixture()
def env(monkeypatch):
    """Wires analyze()'s external hooks to controllable fakes."""
    e = SimpleNamespace(df=None, quote=None, history_calls=[], quote_calls=[],
                        delivery=None, delivery_exc=None, delivery_calls=[])

    def fake_history(sym, force=False):
        e.history_calls.append((sym, force))
        return e.df

    def fake_quote(sym):
        e.quote_calls.append(sym)
        return e.quote

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            e.delivery_calls.append(url)
            if e.delivery_exc:
                raise e.delivery_exc
            return e.delivery or _Resp(404)

    monkeypatch.setattr(tm, "_fetch_history", fake_history)
    monkeypatch.setattr(tm, "_fetch_quote_price", fake_quote)
    monkeypatch.setattr(tm.httpx, "Client", FakeClient)
    return e


class TestAnalyzeFallbacks:
    def test_no_history_but_quote_gives_quote_based_result(self, env):
        env.quote = 250.456
        r = tm.analyze("tcs.ns")
        assert r["symbol"] == "TCS"
        assert r["technical_score"] == 50 and r["trend_strength"] == "unknown"
        assert r["close"] == r["price"] == 250.456
        assert r["data_insufficient"] is True
        assert r["support"] is None and r["resistance"] is None
        assert "₹250.46" in r["summary"] and "₹250.46" in r["reasons"][0]
        assert r["extended"] is False and r["extended_short"] is False

    def test_no_history_no_quote_gives_unavailable_result(self, env):
        r = tm.analyze("TCS")
        assert r["close"] is None and r["price"] is None
        assert r["data_insufficient"] is True and r["technical_score"] == 50
        assert "unavailable" in r["summary"]
        assert "TCS" in r["reasons"][0]

    def test_short_history_under_5_rows_uses_quote_path(self, env):
        env.df = _df(3)
        env.quote = 99.0
        r = tm.analyze("TCS")
        assert r["price"] == 99.0 and r["data_insufficient"] is True

    def test_fallback_result_is_cached(self, env):
        env.quote = 10.0
        tm.analyze("TCS")
        assert "tech_analysis:TCS" in tm._mem_tech

    def test_fallback_cached_result_served_next_call(self, env):
        env.quote = 10.0
        first = tm.analyze("TCS")
        env.quote = 99.0
        assert tm.analyze("TCS") == first
        assert len(env.history_calls) == 1

    def test_force_flag_forwarded_to_history_fetch(self, env):
        tm.analyze("TCS", force=True)
        assert env.history_calls == [("TCS", True)]

    def test_force_bypasses_cache(self, env):
        tm._cache_set("tech_analysis:TCS", {"cached": True}, ttl=600)
        env.quote = 5.0
        r = tm.analyze("TCS", force=True)
        assert "cached" not in r

    def test_cache_hit_short_circuits(self, env):
        tm._cache_set("tech_analysis:TCS", {"cached": True}, ttl=600)
        assert tm.analyze("TCS") == {"cached": True}
        assert env.history_calls == []

    @pytest.mark.parametrize("kw", [
        {"rsi_oversold": 25.0}, {"rsi_overbought": 75.0}, {"extended_1m_pct": 0.25},
        {"extended_short_pct": 0.08}, {"trend_weight": 1.5}, {"meanrev_weight": 0.5},
    ])
    def test_any_override_bypasses_cache_read(self, env, kw):
        tm._cache_set("tech_analysis:TCS", {"cached": True}, ttl=600)
        env.quote = 5.0
        assert "cached" not in tm.analyze("TCS", **kw)

    def test_default_valued_overrides_do_not_bypass_cache(self, env):
        tm._cache_set("tech_analysis:TCS", {"cached": True}, ttl=600)
        r = tm.analyze("TCS", rsi_oversold=30.0, rsi_overbought=70.0, trend_weight=1.0)
        assert r == {"cached": True}

    def test_override_result_not_written_to_cache_on_full_path(self, env):
        env.df = _df(60)
        tm.analyze("TCS", trend_weight=2.0)
        assert "tech_analysis:TCS" not in tm._mem_tech

    def test_default_full_result_is_cached(self, env):
        env.df = _df(60)
        r = tm.analyze("TCS")
        assert tm._mem_tech["tech_analysis:TCS"] is r

    def test_fallback_result_cached_even_with_overrides(self, env):
        # Pin current behaviour: the insufficient-data branch caches unconditionally.
        env.quote = 10.0
        tm.analyze("TCS", trend_weight=2.0)
        assert "tech_analysis:TCS" in tm._mem_tech


# ── analyze(): full path with controlled indicators ───────────────────────────

class _Ind:
    """Overrides for the indicator helpers so every scoring branch is deterministic."""

    def __init__(self, monkeypatch, n, *, rsi=50.0, macd=(0.0, 0.0), prev_macd=(0.0, 0.0),
                 ema=(100.0, 100.0, 100.0), adx=15.0, atr=1.0, bb=(110.0, 90.0),
                 sr=(50.0, 200.0)):
        tm._mem_tech.clear()   # analyze() caches default-param results; keep tests independent
        idx = pd.RangeIndex(n)

        def const(v):
            return pd.Series([v] * n, index=idx, dtype=float)

        def last_two(prev, last):
            s = pd.Series([prev] * n, index=idx, dtype=float)
            s.iloc[-1] = last
            return s

        monkeypatch.setattr(tm, "_rsi", lambda close, period=14: const(rsi))
        monkeypatch.setattr(tm, "_macd", lambda close: (last_two(prev_macd[0], macd[0]),
                                                       last_two(prev_macd[1], macd[1])))
        # analyze() calls _ema in order ema20 (n>=5), ema50 (n>=10), ema200 (n>=30); _macd is patched.
        # Span alone can't identify the call (min(50, n) and min(200, n) collide on short frames).
        import itertools
        order = itertools.cycle([v for v, ok in zip(ema, (n >= 5, n >= 10, n >= 30)) if ok])
        monkeypatch.setattr(tm, "_ema", lambda close, span: const(next(order)))
        monkeypatch.setattr(tm, "_adx", lambda h, l, c, period=14: const(adx))
        monkeypatch.setattr(tm, "_atr", lambda h, l, c, period=14: const(atr))
        monkeypatch.setattr(tm, "_bollinger", lambda close, period=20: (const(bb[0]), const(bb[1])))
        monkeypatch.setattr(tm, "_support_resistance", lambda df, window=20: sr)


def _flat_df(n, close=100.0, vol=1000):
    return _df(n, closes=[close] * n, vol=vol)


def _reasons(r):
    return " | ".join(r["reasons"])


class TestAnalyzeRsiBranches:
    def test_neutral(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=50.0)
        r = tm.analyze("TCS")
        assert "RSI at 50.0 — neutral" in _reasons(r)

    def test_oversold_adds_12(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=25.0)
        r = tm.analyze("TCS")
        assert "oversold (adaptive floor 30, weight 1.00x)" in _reasons(r)
        # flat frame: SMA20 not above (-8), MACD equal (-5), close not above 200 EMA (-5) → 32; +12 oversold
        assert r["technical_score"] == 44

    def test_overbought_subtracts_12(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=75.0)
        r = tm.analyze("TCS")
        assert "overbought (adaptive ceiling 70, weight 1.00x)" in _reasons(r)

    def test_custom_thresholds_shift_bands(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=40.0)
        assert "neutral" in _reasons(tm.analyze("TCS"))
        r = tm.analyze("TCS", rsi_oversold=45.0)
        assert "oversold (adaptive floor 45" in _reasons(r)
        r = tm.analyze("TCS", rsi_overbought=35.0)
        assert "overbought (adaptive ceiling 35" in _reasons(r)

    def test_meanrev_weight_scales_rsi_delta(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=25.0)
        base = tm.analyze("TCS")["technical_score"]
        doubled = tm.analyze("TCS", meanrev_weight=2.0)["technical_score"]
        zero = tm.analyze("TCS", meanrev_weight=0.0)["technical_score"]
        assert doubled - zero == 24 and base - zero == 12

    def test_rsi_exactly_at_threshold_is_neutral(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=30.0)
        assert "neutral" in _reasons(tm.analyze("TCS"))
        _Ind(monkeypatch, 60, rsi=70.0)
        assert "neutral" in _reasons(tm.analyze("TCS"))

    def test_rsi_zero_is_reported_as_50_source_quirk(self, env, monkeypatch):
        # `_safe(0.0) or 50.0` treats a genuine RSI of 0.0 as missing. Pinned.
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=0.0)
        r = tm.analyze("TCS")
        assert r["rsi"] == 50.0 and "neutral" in _reasons(r)

    def test_nan_rsi_falls_back_to_50(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, rsi=float("nan"))
        assert tm.analyze("TCS")["rsi"] == 50.0


class TestAnalyzeMacdBranches:
    def test_bullish_cross(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(1.0, 0.5), prev_macd=(-1.0, 0.0))
        r = tm.analyze("TCS")
        assert "MACD bullish crossover (weight 1.00x)" in _reasons(r)

    def test_bearish_cross(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(-1.0, -0.5), prev_macd=(1.0, 0.0))
        assert "MACD bearish crossover" in _reasons(tm.analyze("TCS"))

    def test_above_signal_no_cross(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(2.0, 1.0), prev_macd=(2.0, 1.0))
        assert "MACD above signal line" in _reasons(tm.analyze("TCS"))

    def test_below_signal_no_cross(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(1.0, 2.0), prev_macd=(1.0, 2.0))
        assert "MACD below signal line" in _reasons(tm.analyze("TCS"))

    def test_equal_macd_and_signal_counts_as_below(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(1.0, 1.0), prev_macd=(1.0, 1.0))
        assert "MACD below signal line" in _reasons(tm.analyze("TCS"))

    def test_trend_weight_scales_macd(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, macd=(1.0, 0.5), prev_macd=(-1.0, 0.0))
        lo = tm.analyze("TCS", trend_weight=0.0)["technical_score"]
        hi = tm.analyze("TCS", trend_weight=1.0 + 1e-12)["technical_score"]
        # at weight 1: +15 (cross) -5 (below 200 EMA) -8 (SMA20) = +2 over the weight-0 baseline of 50
        assert lo == 50 and hi == 52

    def test_macd_needs_26_bars(self, env, monkeypatch):
        env.df = _flat_df(25)
        _Ind(monkeypatch, 25, macd=(1.0, 0.5), prev_macd=(-1.0, 0.0))
        assert "MACD: insufficient data" in _reasons(tm.analyze("TCS"))

    def test_macd_available_at_exactly_26_bars(self, env, monkeypatch):
        env.df = _flat_df(26)
        _Ind(monkeypatch, 26, macd=(1.0, 0.5), prev_macd=(-1.0, 0.0))
        assert "MACD bullish crossover" in _reasons(tm.analyze("TCS"))


class TestAnalyzeEmaBranches:
    def test_bullish_stack(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, ema=(90.0, 80.0, 70.0))
        assert "Bullish EMA stack (weight 1.00x)" in _reasons(tm.analyze("TCS"))

    def test_bearish_stack(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, ema=(110.0, 120.0, 130.0))
        assert "Bearish EMA stack" in _reasons(tm.analyze("TCS"))

    def test_above_200_without_full_stack(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, ema=(105.0, 80.0, 70.0))
        assert "Above 200 EMA" in _reasons(tm.analyze("TCS"))

    def test_below_200_without_full_stack(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, ema=(90.0, 95.0, 130.0))
        assert "Below 200 EMA" in _reasons(tm.analyze("TCS"))

    def test_ema_needs_30_bars(self, env, monkeypatch):
        env.df = _flat_df(29)
        _Ind(monkeypatch, 29, ema=(90.0, 80.0, 70.0))
        assert "EMA trend: insufficient data" in _reasons(tm.analyze("TCS"))

    def test_ema_available_at_exactly_30_bars(self, env, monkeypatch):
        env.df = _flat_df(30)
        _Ind(monkeypatch, 30, ema=(90.0, 80.0, 70.0))
        assert "Bullish EMA stack" in _reasons(tm.analyze("TCS"))

    def test_ema_spans_capped_by_data_length(self, env, monkeypatch):
        seen = []
        env.df = _flat_df(12)
        _Ind(monkeypatch, 12)
        monkeypatch.setattr(tm, "_ema", lambda close, span: (seen.append(span), close)[1])
        tm.analyze("TCS")
        assert seen == [12, 12]   # ema20 → min(20, 12), ema50 → min(50, 12); ema200 skipped below 30 bars

    def test_trend_score_arithmetic(self, env, monkeypatch):
        """All-neutral inputs except a bullish stack: score = 50 + 15."""
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, ema=(90.0, 80.0, 70.0), bb=(110.0, 90.0), sr=(50.0, 200.0))
        r = tm.analyze("TCS")
        # SMA20 == close (not above) → -8 ; MACD equal → -5 ; stack +15 → 50 + 15 - 8 - 5
        assert r["technical_score"] == 52
        assert r["ema20"] == 90.0 and r["ema50"] == 80.0 and r["ema200"] == 70.0


class TestAnalyzeSmaAdxBb:
    def test_above_sma20(self, env, monkeypatch):
        closes = [90.0] * 40 + [110.0]
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        assert "Above 20-day SMA" in _reasons(tm.analyze("TCS"))

    def test_below_sma20(self, env, monkeypatch):
        closes = [110.0] * 40 + [90.0]
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        assert "Below 20-day SMA" in _reasons(tm.analyze("TCS"))

    def test_sma_needs_20_bars(self, env, monkeypatch):
        env.df = _flat_df(19)
        _Ind(monkeypatch, 19)
        r = tm.analyze("TCS")
        assert "Short-term momentum: insufficient data" in _reasons(r)
        assert "ADX: insufficient data" in _reasons(r)
        assert "Bollinger Bands: insufficient data" in _reasons(r)

    @pytest.mark.parametrize("adx,label,expected", [
        (30.0, "strong", "ADX 30.0 — strong trend"),
        (25.0, "strong", "ADX 25.0 — strong trend"),
        (24.9, "moderate", "ADX 24.9 — weak/no trend"),
        (20.0, "moderate", "ADX 20.0 — weak/no trend"),
        (19.9, "weak", "ADX 19.9 — weak/no trend"),
        (5.0, "weak", "ADX 5.0 — weak/no trend"),
    ])
    def test_adx_bands(self, env, monkeypatch, adx, label, expected):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, adx=adx)
        r = tm.analyze("TCS")
        assert r["trend_strength"] == label
        assert expected in _reasons(r)

    def test_adx_nan_becomes_zero_weak(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, adx=float("nan"))
        r = tm.analyze("TCS")
        assert r["adx"] == 0.0 and r["trend_strength"] == "weak"

    def test_near_lower_bb_bonus(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, bb=(200.0, 95.0))  # pct = 5/105 ≈ 5%
        assert "Near lower BB (5%, weight 1.00x)" in _reasons(tm.analyze("TCS"))

    def test_near_upper_bb_penalty(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, bb=(101.0, 1.0))  # pct = 99%
        assert "Near upper BB (99%, weight 1.00x)" in _reasons(tm.analyze("TCS"))

    def test_mid_bb_no_reason(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, bb=(110.0, 90.0))
        assert "BB" not in _reasons(tm.analyze("TCS"))

    def test_degenerate_bb_uses_range_of_one_and_reads_as_near_lower(self, env, monkeypatch):
        # Source quirk, pinned: collapsed bands (flat/halted stock) give bb_range=1 → pct=0
        # → "Near lower BB" and a +8 mean-reversion bonus, even though price is mid-band.
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, bb=(100.0, 100.0))
        r = tm.analyze("TCS")
        assert r["bb_upper"] == r["bb_lower"] == 100.0
        assert "Near lower BB (0%, weight 1.00x)" in _reasons(r)

    def test_meanrev_weight_scales_bb(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, bb=(200.0, 95.0))
        a = tm.analyze("TCS", meanrev_weight=0.0)["technical_score"]
        b = tm.analyze("TCS", meanrev_weight=2.0)["technical_score"]
        assert b - a == 16


class TestAnalyzeSupportResistanceVolume:
    def test_near_resistance_penalties(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.0, 101.0))  # 1.0% below resistance
        r = tm.analyze("TCS")
        assert "1.0% below resistance" in _reasons(r)
        assert "regime penalty applied" in _reasons(r)

    def test_regime_penalty_band_2_to_5_pct(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.0, 104.0))
        r = tm.analyze("TCS")
        assert "below resistance" not in _reasons(r)
        assert "Near resistance (4.0%)" in _reasons(r)

    def test_far_from_resistance_no_penalty(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.0, 110.0))
        r = tm.analyze("TCS")
        assert "resistance" not in _reasons(r).lower()

    def test_resistance_penalty_total_is_13(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.0, 200.0))
        far = tm.analyze("TCS")["technical_score"]
        _Ind(monkeypatch, 60, sr=(50.0, 101.0))
        near = tm.analyze("TCS")["technical_score"]
        assert far - near == 13   # -8 (within 2%) and -5 (regime penalty within 5%)

    def test_near_support_bonus(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(99.0, 200.0))
        assert "1.0% above support" in _reasons(tm.analyze("TCS"))

    def test_far_from_support_no_bonus(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.0, 200.0))
        assert "above support" not in _reasons(tm.analyze("TCS"))

    def test_zero_support_resistance_report_none_and_999_distance(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(0.0, 0.0))
        r = tm.analyze("TCS")
        assert r["support"] is None and r["resistance"] is None
        assert "below resistance" not in _reasons(r)

    def test_volume_surge(self, env, monkeypatch):
        vols = [1000] * 59 + [5000]
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        c = pd.Series([100.0] * 60, index=idx)
        env.df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vols}, index=idx)
        _Ind(monkeypatch, 60)
        r = tm.analyze("TCS")
        assert r["volume_surge"] is True and "Volume surge" in _reasons(r)
        assert r["volume_ratio"] == round(5000 / ((1000 * 19 + 5000) / 20), 3)

    def test_no_volume_surge(self, env, monkeypatch):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60)
        r = tm.analyze("TCS")
        assert r["volume_surge"] is False and r["volume_ratio"] == 1.0

    def test_volume_surge_boundary_is_strictly_greater(self, env, monkeypatch):
        # last 20 vols: 19 × 1000 + 1 × X. surge iff X > 1.5 * mean.  X=1500 → mean 1025 → 1537 needed.
        vols = [1000] * 59 + [1500]
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        c = pd.Series([100.0] * 60, index=idx)
        env.df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vols}, index=idx)
        _Ind(monkeypatch, 60)
        assert tm.analyze("TCS")["volume_surge"] is False

    def test_zero_average_volume(self, env, monkeypatch):
        env.df = _flat_df(60, vol=0)
        _Ind(monkeypatch, 60)
        r = tm.analyze("TCS")
        assert r["volume_ratio"] is None and r["volume_surge"] is False

    def test_volume_average_for_short_history_uses_latest_bar(self, env, monkeypatch):
        # 5 rows → vol_avg20 uses tail(min(20,5)) mean; len>=5 so mean branch
        env.df = _flat_df(5, vol=700)
        _Ind(monkeypatch, 5)
        r = tm.analyze("TCS")
        assert r["volume_ratio"] == 1.0


class TestAnalyzeExtended:
    def test_extended_1m_flag(self, env, monkeypatch):
        closes = [100.0] * 30 + [125.0] * 2
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        r = tm.analyze("TCS")
        assert r["extended"] is True

    def test_not_extended_1m_below_cutoff(self, env, monkeypatch):
        closes = [100.0] * 30 + [110.0] * 2
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        assert tm.analyze("TCS")["extended"] is False

    def test_extended_1m_custom_cutoff(self, env, monkeypatch):
        closes = [100.0] * 30 + [110.0] * 2
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        assert tm.analyze("TCS", extended_1m_pct=0.05)["extended"] is True

    def test_extended_1m_needs_22_bars(self, env, monkeypatch):
        closes = [100.0] * 20 + [200.0]
        env.df = _df(0, closes=closes)  # 21 bars
        _Ind(monkeypatch, len(closes))
        assert tm.analyze("TCS")["extended"] is False

    def test_extended_short_flag_and_reason(self, env, monkeypatch):
        closes = [100.0] * 57 + [104.0, 106.0, 108.0]
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        r = tm.analyze("TCS")
        assert r["extended_short"] is True
        assert "Extended short-term: +8.0% over ~3 sessions (adaptive cutoff 5%) — chase risk" in _reasons(r)

    def test_extended_short_not_flagged_under_cutoff(self, env, monkeypatch):
        closes = [100.0] * 57 + [101.0, 102.0, 103.0]
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        r = tm.analyze("TCS")
        assert r["extended_short"] is False and "Extended short-term" not in _reasons(r)

    def test_extended_short_custom_cutoff(self, env, monkeypatch):
        closes = [100.0] * 57 + [101.0, 102.0, 103.0]
        env.df = _df(0, closes=closes)
        _Ind(monkeypatch, len(closes))
        assert tm.analyze("TCS", extended_short_pct=0.02)["extended_short"] is True

    def test_extended_flags_fail_open_on_bad_close_arithmetic(self, env, monkeypatch):
        """Decimal - float raises TypeError inside the guarded block → both flags stay False."""
        from decimal import Decimal
        closes = [Decimal("100")] * 60
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        c = pd.Series(closes, index=idx, dtype=object)
        env.df = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c,
                               "Volume": [1000] * 60}, index=idx)
        _Ind(monkeypatch, 60)
        r = tm.analyze("TCS")
        assert r["extended"] is False and r["extended_short"] is False


class TestAnalyzeDelivery:
    def _run(self, env, monkeypatch, resp=None, exc=None, **ind):
        env.df = _flat_df(60)
        _Ind(monkeypatch, 60, **ind)
        env.delivery = resp
        env.delivery_exc = exc
        return tm.analyze("TCS.NS")

    def _base(self, env, monkeypatch):
        return self._run(env, monkeypatch, resp=_Resp(404))["technical_score"]

    def test_high_delivery_adds_3(self, env, monkeypatch):
        base = self._base(env, monkeypatch)
        tm._mem_tech.clear()
        r = self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 65.4, "source": "nse_bhavcopy"}))
        assert r["delivery_pct"] == 65.4 and r["delivery_source"] == "nse_bhavcopy"
        assert "High delivery 65% (nse_bhavcopy)" in _reasons(r)
        assert r["technical_score"] == base + 3

    def test_low_delivery_subtracts_2(self, env, monkeypatch):
        base = self._base(env, monkeypatch)
        tm._mem_tech.clear()
        r = self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 25.0, "source": "x"}))
        assert "Low delivery 25% (x)" in _reasons(r)
        assert r["technical_score"] == base - 2

    def test_mid_delivery_recorded_without_nudge(self, env, monkeypatch):
        base = self._base(env, monkeypatch)
        tm._mem_tech.clear()
        r = self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 45.0}))
        assert r["delivery_pct"] == 45.0 and r["delivery_source"] is None
        assert "delivery" not in _reasons(r).lower()
        assert r["technical_score"] == base

    def test_boundaries_60_and_30_inclusive(self, env, monkeypatch):
        assert "High delivery 60% (nse)" in _reasons(
            self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 60})))
        tm._mem_tech.clear()
        assert "Low delivery 30% (nse)" in _reasons(
            self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 30})))

    def test_missing_source_label_defaults_to_nse(self, env, monkeypatch):
        r = self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": 70}))
        assert "(nse)" in _reasons(r) and r["delivery_source"] is None

    def test_null_delivery_pct_ignored(self, env, monkeypatch):
        r = self._run(env, monkeypatch, resp=_Resp(200, {"delivery_pct": None, "source": "x"}))
        assert r["delivery_pct"] is None and r["delivery_source"] is None

    def test_non_200_ignored(self, env, monkeypatch):
        r = self._run(env, monkeypatch, resp=_Resp(500, {"delivery_pct": 90}))
        assert r["delivery_pct"] is None

    def test_exception_swallowed(self, env, monkeypatch):
        r = self._run(env, monkeypatch, exc=httpx.ConnectError("no"))
        assert r["delivery_pct"] is None and r["symbol"] == "TCS"

    def test_bad_json_swallowed(self, env, monkeypatch):
        r = self._run(env, monkeypatch, resp=_Resp(200, bad_json=True))
        assert r["delivery_pct"] is None

    def test_url_uses_bare_symbol(self, env, monkeypatch):
        self._run(env, monkeypatch, resp=_Resp(404))
        assert env.delivery_calls[0].endswith("/delivery/TCS")


class TestAnalyzeResultShape:
    def test_full_result_keys_and_rounding(self, env, monkeypatch):
        env.df = _df(80, start=100.0, step=0.7)
        r = tm.analyze("tcs.ns")
        assert set(r) == {
            "symbol", "close", "technical_score", "trend_strength", "support", "resistance",
            "rsi", "adx", "atr", "ema20", "ema50", "ema200", "bb_upper", "bb_lower",
            "volume_surge", "volume_ratio", "delivery_pct", "delivery_source",
            "extended", "extended_short", "data_insufficient", "adaptive_params_used", "reasons",
        }
        assert r["symbol"] == "TCS"
        assert r["close"] == round(r["close"], 2)
        assert isinstance(r["technical_score"], int) and 0 <= r["technical_score"] <= 100
        assert isinstance(r["reasons"], list) and r["reasons"]

    def test_adaptive_params_echoed(self, env):
        env.df = _df(60)
        r = tm.analyze("TCS", rsi_oversold=25.0, rsi_overbought=80.0, extended_1m_pct=0.2,
                       extended_short_pct=0.07, trend_weight=1.2, meanrev_weight=0.8)
        assert r["adaptive_params_used"] == {
            "rsi_oversold": 25.0, "rsi_overbought": 80.0, "extended_1m_pct": 0.2,
            "extended_short_pct": 0.07, "trend_weight": 1.2, "meanrev_weight": 0.8,
        }

    def test_defaults_echoed(self, env):
        env.df = _df(60)
        assert tm.analyze("TCS")["adaptive_params_used"] == {
            "rsi_oversold": 30.0, "rsi_overbought": 70.0, "extended_1m_pct": 0.18,
            "extended_short_pct": 0.05, "trend_weight": 1.0, "meanrev_weight": 1.0,
        }

    @pytest.mark.parametrize("n,insufficient", [(5, True), (29, True), (30, False), (60, False)])
    def test_data_insufficient_threshold(self, env, n, insufficient):
        env.df = _df(n)
        assert tm.analyze("TCS")["data_insufficient"] is insufficient

    def test_score_clamped_to_100(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, rsi=10.0, macd=(1.0, 0.5), prev_macd=(-1.0, 0.0),
             ema=(90.0, 80.0, 70.0), bb=(200.0, 95.0), sr=(99.0, 200.0))
        r = tm.analyze("TCS", trend_weight=5.0, meanrev_weight=5.0)
        assert r["technical_score"] == 100

    def test_score_clamped_to_0(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, rsi=90.0, macd=(-1.0, -0.5), prev_macd=(1.0, 0.0),
             ema=(110.0, 120.0, 130.0), bb=(101.0, 0.0), sr=(50.0, 101.0))
        r = tm.analyze("TCS", trend_weight=5.0, meanrev_weight=5.0)
        assert r["technical_score"] == 0

    def test_score_is_rounded_int(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, rsi=25.0)
        r = tm.analyze("TCS", meanrev_weight=0.55)
        assert isinstance(r["technical_score"], int)

    def test_support_resistance_values_rounded(self, env, monkeypatch):
        env.df = _flat_df(60, close=100.0)
        _Ind(monkeypatch, 60, sr=(50.126, 200.999))
        r = tm.analyze("TCS")
        assert r["support"] == 50.13 and r["resistance"] == 201.0


class TestAnalyzeShortHistoryPaths:
    """Real (un-patched) indicators on frames shorter than each indicator's minimum."""

    @pytest.mark.parametrize("n", [5, 9, 10, 13, 14, 19, 20, 25, 26, 29, 30, 45])
    def test_never_crashes_and_stays_in_range(self, env, n):
        env.df = _df(n, start=100.0, step=0.5)
        r = tm.analyze("TCS")
        assert 0 <= r["technical_score"] <= 100
        assert r["close"] == pytest.approx(100.0 + 0.5 * (n - 1), abs=0.01)

    def test_five_bars_uses_placeholder_indicators(self, env):
        env.df = _df(5)
        r = tm.analyze("TCS")
        reasons = _reasons(r)
        assert r["rsi"] == 50.0 and r["adx"] == 15.0 and r["atr"] == 0.0
        assert "MACD: insufficient data" in reasons
        assert "EMA trend: insufficient data" in reasons
        assert "Bollinger Bands: insufficient data" in reasons

    def test_placeholder_adx_under_20_bars_is_15(self, env):
        env.df = _df(15)
        assert tm.analyze("TCS")["adx"] == 15.0

    def test_placeholder_atr_under_14_bars_is_zero(self, env):
        env.df = _df(10)
        assert tm.analyze("TCS")["atr"] == 0.0


class TestAnalyzeRealIndicators:
    """End-to-end on realistic series with the real indicator maths."""

    def test_strong_uptrend_scores_bullishly(self, env):
        env.df = _df(250, start=100.0, step=1.0)
        r = tm.analyze("TCS")
        text = _reasons(r)
        assert "Bullish EMA stack" in text and "Above 20-day SMA" in text
        assert r["ema20"] > r["ema50"] > r["ema200"]
        assert r["trend_strength"] == "strong"

    def test_uptrend_rsi_reports_50_due_to_nan_quirk(self, env):
        # A relentlessly rising series has no down days → RSI is NaN → reported as 50.
        env.df = _df(250, start=100.0, step=1.0)
        assert tm.analyze("TCS")["rsi"] == 50.0

    def test_strong_downtrend_scores_bearishly(self, env):
        env.df = _df(250, start=400.0, step=-1.0)
        r = tm.analyze("TCS")
        text = _reasons(r)
        assert "Bearish EMA stack" in text and "Below 20-day SMA" in text
        assert r["technical_score"] < 50

    def test_downtrend_rsi_zero_reports_50_quirk(self, env):
        env.df = _df(250, start=400.0, step=-1.0)
        assert tm.analyze("TCS")["rsi"] == 50.0

    def test_noisy_series_produces_plausible_rsi(self, env):
        rng = np.random.default_rng(42)
        closes = 100 + rng.normal(0, 1.0, 250).cumsum()
        closes = np.maximum(closes, 5.0)
        env.df = _df(0, closes=list(closes))
        r = tm.analyze("TCS")
        assert 0 < r["rsi"] < 100
        assert r["atr"] > 0

    def test_atr_ignores_corporate_action_jump(self, env):
        closes = [100.0 + (i % 2) for i in range(60)] + [200.0 + (i % 2) for i in range(60)]
        env.df = _df(0, closes=closes)
        r = tm.analyze("TCS")
        assert r["atr"] < 10  # the 100% jump day would push ATR far higher if not clamped


# ── /sector-strength/{symbol} ─────────────────────────────────────────────────

def _fake_sqlalchemy(monkeypatch, sector_row=None, peer_rows=None, exec_exc=None, connect_exc=None):
    seen = {"queries": [], "urls": [], "kw": []}
    mod = types.ModuleType("sqlalchemy")

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            seen["queries"].append((query, params))
            if exec_exc:
                raise exec_exc
            if "SELECT sector" in query:
                return SimpleNamespace(fetchone=lambda: sector_row)
            return SimpleNamespace(fetchall=lambda: list(peer_rows or []))

    class Engine:
        def connect(self):
            if connect_exc:
                raise connect_exc
            return Conn()

    def create_engine(url, **kw):
        seen["urls"].append(url)
        seen["kw"].append(kw)
        return Engine()

    mod.create_engine = create_engine
    mod.text = lambda s: s
    monkeypatch.setitem(sys.modules, "sqlalchemy", mod)
    return seen


@pytest.fixture()
def fake_rs(monkeypatch):
    """Replace shared_adaptive.relative_strength_vs_sector with a recorder."""
    rec = SimpleNamespace(calls=[], result={"stock_return_10d": 4.2, "passes": True},
                          exc=None, drive=None)

    async def fake(symbol, sector, get_return_fn, get_peers_fn, window_days=10):
        rec.calls.append((symbol, sector))
        if rec.exc:
            raise rec.exc
        if rec.drive:
            await rec.drive(get_return_fn, get_peers_fn)
        return dict(rec.result)

    mod = types.ModuleType("shared_adaptive")
    mod.relative_strength_vs_sector = fake
    monkeypatch.setitem(sys.modules, "shared_adaptive", mod)
    return rec


class TestSectorStrength:
    def test_explicit_sector_skips_db(self, monkeypatch, fake_rs):
        seen = _fake_sqlalchemy(monkeypatch)
        r = run(tm.sector_relative_strength("tcs.ns", sector="  IT  "))
        assert r == {"symbol": "TCS", "sector": "IT", "stock_return_10d": 4.2, "passes": True}
        assert fake_rs.calls == [("TCS", "IT")]
        assert seen["urls"] == []

    def test_sector_looked_up_from_symbol_master(self, monkeypatch, fake_rs):
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
        seen = _fake_sqlalchemy(monkeypatch, sector_row=("Banking",))
        r = run(tm.sector_relative_strength("HDFCBANK"))
        assert r["sector"] == "Banking"
        assert seen["urls"][0] == "postgresql://u:p@h/db"
        assert seen["kw"][0] == {"pool_pre_ping": True, "pool_size": 1, "max_overflow": 0}
        assert seen["queries"][0][1] == {"s": "HDFCBANK"}

    def test_cache_database_url_used_when_database_url_unset(self, monkeypatch, fake_rs):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgresql://c/db")
        seen = _fake_sqlalchemy(monkeypatch, sector_row=("Auto",))
        run(tm.sector_relative_strength("MARUTI"))
        assert seen["urls"][0] == "postgresql://c/db"

    def test_no_db_url_uses_empty_string(self, monkeypatch, fake_rs):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        seen = _fake_sqlalchemy(monkeypatch, sector_row=None)
        r = run(tm.sector_relative_strength("TCS"))
        assert seen["urls"] == [""] and r["sector"] == ""

    def test_no_row_gives_empty_sector(self, monkeypatch, fake_rs):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        _fake_sqlalchemy(monkeypatch, sector_row=None)
        assert run(tm.sector_relative_strength("TCS"))["sector"] == ""

    def test_null_sector_column_gives_empty(self, monkeypatch, fake_rs):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        _fake_sqlalchemy(monkeypatch, sector_row=(None,))
        assert run(tm.sector_relative_strength("TCS"))["sector"] == ""

    def test_db_error_swallowed(self, monkeypatch, fake_rs):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        _fake_sqlalchemy(monkeypatch, connect_exc=RuntimeError("db down"))
        r = run(tm.sector_relative_strength("TCS"))
        assert r["sector"] == "" and r["passes"] is True

    def test_shared_module_error_returned_as_payload(self, monkeypatch, fake_rs):
        fake_rs.exc = RuntimeError("kaboom")
        r = run(tm.sector_relative_strength("TCS", sector="IT"))
        assert r == {"symbol": "TCS", "sector": "IT", "error": "kaboom", "passes": False}

    def test_falls_back_to_shared_adaptive_thresholds_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "shared_adaptive", None)  # ImportError
        called = {}

        async def fake(symbol, sector, get_return_fn, get_peers_fn, window_days=10):
            called["args"] = (symbol, sector)
            return {"passes": True, "via": "adaptive_thresholds"}

        mod = types.ModuleType("adaptive_thresholds")
        mod.relative_strength_vs_sector = fake
        monkeypatch.setitem(sys.modules, "adaptive_thresholds", mod)
        r = run(tm.sector_relative_strength("TCS", sector="IT"))
        assert r["via"] == "adaptive_thresholds" and called["args"] == ("TCS", "IT")

    def test_both_modules_missing_returns_error_payload(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "shared_adaptive", None)
        monkeypatch.setitem(sys.modules, "adaptive_thresholds", None)
        r = run(tm.sector_relative_strength("TCS", sector="IT"))
        assert r["passes"] is False and "error" in r and r["symbol"] == "TCS"

    def test_real_shared_adaptive_no_data_path(self, monkeypatch):
        """No fake: the real shared_adaptive runs with a history that has no data."""
        monkeypatch.delitem(sys.modules, "shared_adaptive", raising=False)
        monkeypatch.setattr(tm, "_fetch_history", lambda s, force=False: None)
        r = run(tm.sector_relative_strength("TCS", sector="IT"))
        assert r["passes"] is False and r["stock_return_10d"] is None and r["note"] == "no data"

    def test_real_shared_adaptive_with_peers(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "shared_adaptive", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        _fake_sqlalchemy(monkeypatch, peer_rows=[("INFY",), ("WIPRO",), ("TCS",)])
        rising = _df(60, start=100.0, step=2.0)
        flat = _df(60, closes=[100.0] * 60)
        monkeypatch.setattr(tm, "_fetch_history",
                            lambda s, force=False: rising if s == "TCS" else flat)
        r = run(tm.sector_relative_strength("TCS", sector="IT"))
        assert r["stock_return_10d"] > 0 and r["peers_in_window"] == 2
        assert r["sector_percentile"] == 100.0

    def test_inner_get_return_paths(self, monkeypatch, fake_rs):
        out = {}
        frames = {
            "GOOD": _df(30, start=100.0, step=1.0),
            "SHORT": _df(8),
            "EMPTY": pd.DataFrame(),
            "NOCLOSE": pd.DataFrame({"Open": [1.0] * 30}),
            "NONE": None,
        }
        monkeypatch.setattr(tm, "_fetch_history", lambda s, force=False: frames[s])

        async def drive(get_return, get_peers):
            out["good"] = await get_return("good.ns", 10)
            out["short"] = await get_return("SHORT", 10)
            out["empty"] = await get_return("EMPTY", 10)
            out["noclose"] = await get_return("NOCLOSE", 10)
            out["none"] = await get_return("NONE", 10)

        fake_rs.drive = drive
        run(tm.sector_relative_strength("TCS", sector="IT"))
        # GOOD closes 100..129; iloc[-1]=129, iloc[-11]=119 → (129/119 - 1)*100
        assert out["good"] == pytest.approx((129 / 119 - 1) * 100)
        assert out["short"] is None and out["empty"] is None
        assert out["noclose"] is None and out["none"] is None

    def test_inner_get_peers_paths(self, monkeypatch, fake_rs):
        out = {}
        monkeypatch.setenv("DATABASE_URL", "postgres://u@h/db")
        seen = _fake_sqlalchemy(monkeypatch, peer_rows=[("INFY",), ("WIPRO",)])

        async def drive(get_return, get_peers):
            out["peers"] = await get_peers("IT")

        fake_rs.drive = drive
        run(tm.sector_relative_strength("TCS", sector="IT"))
        assert out["peers"] == ["INFY", "WIPRO"]
        assert seen["urls"] == ["postgresql://u@h/db"]
        assert seen["queries"][0][1] == {"sec": "IT"}
        assert "LIMIT 50" in seen["queries"][0][0]

    def test_inner_get_peers_db_error_returns_empty(self, monkeypatch, fake_rs):
        out = {}
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        _fake_sqlalchemy(monkeypatch, exec_exc=RuntimeError("db down"))

        async def drive(get_return, get_peers):
            out["peers"] = await get_peers("IT")

        fake_rs.drive = drive
        run(tm.sector_relative_strength("TCS", sector="IT"))
        assert out["peers"] == []

    def test_inner_get_peers_without_db_url(self, monkeypatch, fake_rs):
        out = {}
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        seen = _fake_sqlalchemy(monkeypatch, peer_rows=[])

        async def drive(get_return, get_peers):
            out["peers"] = await get_peers("IT")

        fake_rs.drive = drive
        run(tm.sector_relative_strength("TCS", sector="IT"))
        assert out["peers"] == [] and seen["urls"] == [""]
