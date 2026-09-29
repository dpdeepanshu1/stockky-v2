"""
tests/test_event_main.py — coverage for event/main.py

No network, no Redis. yfinance's Ticker, feedparser.parse and every news source
are faked; the module-level memory cache is reset around each test; the optional
rate_limit_report import is stubbed. Route functions are called directly
(FastAPI's @app.get returns the original function), so no TestClient is needed.
Route *ordering* is checked through Starlette's own matcher instead.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_event_main.py -v
"""
from __future__ import annotations

import importlib.util
import os
import runpy
import sys
import time
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_EVENT_MAIN = os.path.join(_SERVICE, "event", "main.py")
sys.path.insert(0, _SERVICE)                        # rate_limit_report
sys.path.insert(0, os.path.join(_SERVICE, "event"))  # event/main.py + event_depth

import numpy as np
import pandas as pd
import pytest
from starlette.responses import JSONResponse
from starlette.routing import Match

import main as em  # noqa: E402  (event/main.py)


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Isolate every module-level cache / flag for each test."""
    for d in (em._yf_cache, em._company_name_cache, em._yf_ticker_cache, em._mem, em._mem_exp):
        d.clear()
    monkeypatch.setattr(em, "_redis", None)
    monkeypatch.setattr(em, "_yf_rate_limited_until", 0.0)
    yield
    for d in (em._yf_cache, em._company_name_cache, em._yf_ticker_cache, em._mem, em._mem_exp):
        d.clear()


@pytest.fixture(autouse=True)
def rl(monkeypatch):
    """Stub rate_limit_report so nothing real is reported; records calls."""
    calls = []
    fake = types.ModuleType("rate_limit_report")
    fake.report_if_rate_limited = lambda exc, **kw: calls.append((exc, kw))
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    return calls


class FakeRedis:
    def __init__(self, value=None, fail_get=False, fail_set=False):
        self.value = value
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.setex_calls = []
        self.set_calls = []

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis down")
        return self.value

    def setex(self, key, ttl, data):
        if self.fail_set:
            raise RuntimeError("redis down")
        self.setex_calls.append((key, ttl, data))

    def set(self, key, data):
        if self.fail_set:
            raise RuntimeError("redis down")
        self.set_calls.append((key, data))


class _T:
    """Fake yfinance Ticker: whatever attributes the test sets."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Boom:
    """Fake Ticker where ANY attribute access raises."""

    def __init__(self, exc):
        self._exc = exc

    def __getattr__(self, name):
        raise self._exc


def _pp(days_ago):
    """feedparser-style published_parsed struct_time."""
    return (datetime.utcnow() - timedelta(days=days_ago)).timetuple()


def _iso(days_from_now):
    return (datetime.utcnow() + timedelta(days=days_from_now)).date().isoformat()


_UNSET = object()


def _entry(title="Zenith Widgets posts profit", link="http://x/1", days_ago=1,
           desc=None, source=_UNSET, pp=_UNSET):
    e = SimpleNamespace(title=title, link=link)
    e.published_parsed = _pp(days_ago) if pp is _UNSET else pp
    if desc is not None:
        e.description = desc
    if source is not _UNSET:
        e.source = source
    return e


def _feed(entries, bozo=False):
    return SimpleNamespace(entries=list(entries), bozo=bozo)


@pytest.fixture()
def company(monkeypatch):
    """Company name Zenith Widgets → keywords: zenith, widgets, zwid, ..."""
    monkeypatch.setattr(em, "_get_company_name", lambda s: "Zenith Widgets")


def _wire(monkeypatch, *, earnings=None, divs=None, splits=None, ins=None, ud=None,
          ih=None, eh=None, news=()):
    """Replace every data source _fetch_events reads."""
    monkeypatch.setattr(em, "_get_earnings_dates", lambda s, limit=1: earnings)
    monkeypatch.setattr(em, "_get_dividends", lambda s: divs)
    monkeypatch.setattr(em, "_get_splits", lambda s: splits)
    monkeypatch.setattr(em, "_get_insider_transactions", lambda s: ins)
    monkeypatch.setattr(em, "_get_upgrades_downgrades", lambda s: ud)
    monkeypatch.setattr(em, "_get_institutional_holders", lambda s: ih)
    monkeypatch.setattr(em, "_get_earnings_history", lambda s: eh)
    monkeypatch.setattr(em, "_fetch_news_from_multiple_sources", lambda s, max_total=15: list(news))


def _reload_with(monkeypatch, *, block=(), env=None, extra_modules=None):
    """Exec event/main.py fresh under controlled import conditions."""
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    for name in block:
        monkeypatch.setitem(sys.modules, name, None)  # forces ImportError
    for name, mod in (extra_modules or {}).items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("event_main_fresh", _EVENT_MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── module import / optional dependencies ─────────────────────────────────────

class TestImport:
    def test_constants_and_metadata(self):
        assert em.app.version == "0.4.4"
        assert em.EVENT_CACHE_TTL == 4 * 3600
        assert em.EMPTY_NEWS_CACHE_TTL == 3600
        assert em.EVENT_FALLBACK_TTL == 30 * 24 * 3600

    def test_event_depth_missing_disables_enrichment(self, monkeypatch):
        mod = _reload_with(monkeypatch, block=("event_depth",))
        assert mod.enrich_events is None

    def test_event_depth_present_by_default(self):
        assert callable(em.enrich_events)

    def test_redis_off_by_default(self, monkeypatch):
        monkeypatch.delenv("USE_REDIS", raising=False)
        mod = _reload_with(monkeypatch)
        assert mod._redis is None

    def test_redis_enabled_and_pings(self, monkeypatch):
        seen = {}

        class R:
            def __init__(self, url, token):
                seen["init"] = (url, token)

            def ping(self):
                seen["ping"] = True

        fake = types.ModuleType("upstash_redis")
        fake.Redis = R
        mod = _reload_with(
            monkeypatch,
            env={"USE_REDIS": "yes", "UPSTASH_REDIS_REST_URL": "https://u", "UPSTASH_REDIS_REST_TOKEN": "t"},
            extra_modules={"upstash_redis": fake},
        )
        assert isinstance(mod._redis, R)
        assert seen == {"init": ("https://u", "t"), "ping": True}

    def test_redis_ping_failure_falls_back_to_memory(self, monkeypatch):
        class R:
            def __init__(self, url, token):
                pass

            def ping(self):
                raise RuntimeError("no route")

        fake = types.ModuleType("upstash_redis")
        fake.Redis = R
        mod = _reload_with(
            monkeypatch,
            env={"USE_REDIS": "1", "UPSTASH_REDIS_REST_URL": "https://u", "UPSTASH_REDIS_REST_TOKEN": "t"},
            extra_modules={"upstash_redis": fake},
        )
        assert mod._redis is None

    def test_use_redis_without_credentials_stays_memory(self, monkeypatch):
        monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
        monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
        mod = _reload_with(monkeypatch, env={"USE_REDIS": "true"})
        assert mod._redis is None


class TestMainBlock:
    """`if __name__ == "__main__"` starts uvicorn on $PORT (default 8006)."""

    def _run(self, monkeypatch):
        started = []
        fake = types.ModuleType("uvicorn")
        fake.run = lambda *a, **k: started.append((a, k))
        monkeypatch.setitem(sys.modules, "uvicorn", fake)
        runpy.run_path(_EVENT_MAIN, run_name="__main__")
        return started

    def test_port_from_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "9123")
        assert self._run(monkeypatch) == [(("main:app",), {"host": "0.0.0.0", "port": 9123, "reload": True})]

    def test_port_default(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        assert self._run(monkeypatch)[0][1]["port"] == 8006


# ── cached_yf decorator ───────────────────────────────────────────────────────

class TestCachedYf:
    def _make(self):
        calls = []

        @em.cached_yf("m")
        def fn(symbol, x=1):
            calls.append((symbol, x))
            return {"v": len(calls)}

        return fn, calls

    def test_miss_then_hit(self):
        fn, calls = self._make()
        assert fn("A.NS") == {"v": 1}
        assert fn("A.NS") == {"v": 1}
        assert calls == [("A.NS", 1)]
        assert "A.NS:m" in em._yf_cache

    def test_expired_entry_is_dropped_and_refetched(self):
        fn, calls = self._make()
        fn("A.NS")
        em._yf_cache["A.NS:m"]["timestamp"] -= em.CACHE_TTL_SECONDS + 1
        assert fn("A.NS") == {"v": 2}
        assert len(calls) == 2

    def test_none_results_are_cached_too(self):
        n = []

        @em.cached_yf("none")
        def fn(symbol):
            n.append(1)
            return None

        assert fn("A") is None
        assert fn("A") is None
        assert len(n) == 1

    def test_keys_are_per_symbol_and_method(self):
        fn, calls = self._make()
        fn("A.NS")
        fn("B.NS")
        assert len(calls) == 2


# ── memory / Redis cache ──────────────────────────────────────────────────────

class TestRedisGetSet:
    def test_set_get_roundtrip_in_memory(self):
        em._redis_set("k", {"a": 1}, ttl=60)
        assert em._redis_get("k") == {"a": 1}
        assert em._mem_exp["k"] > time.time()

    def test_set_without_ttl_never_expires(self):
        em._redis_set("k", [1])
        assert em._mem_exp["k"] is None
        assert em._redis_get("k") == [1]

    def test_expired_memory_entry_with_no_redis_is_none(self):
        em._redis_set("k", 1, ttl=60)
        em._mem_exp["k"] = time.time() - 1
        assert em._redis_get("k") is None

    def test_missing_key_no_redis(self):
        assert em._redis_get("nope") is None

    def test_redis_hit_string_is_parsed_and_memoised(self, monkeypatch):
        r = FakeRedis(value='{"x": 5}')
        monkeypatch.setattr(em, "_redis", r)
        assert em._redis_get("k") == {"x": 5}
        r.value = None  # second read served from memory
        assert em._redis_get("k") == {"x": 5}

    def test_redis_hit_bytes_is_parsed(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(value=b'[1, 2]'))
        assert em._redis_get("k") == [1, 2]

    def test_redis_hit_already_decoded(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(value={"y": 1}))
        assert em._redis_get("k") == {"y": 1}

    def test_redis_miss_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(value=None))
        assert em._redis_get("k") is None

    def test_redis_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(fail_get=True))
        assert em._redis_get("k") is None

    def test_redis_bad_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(value="not json"))
        assert em._redis_get("k") is None

    def test_set_writes_through_with_setex_when_ttl(self, monkeypatch):
        r = FakeRedis()
        monkeypatch.setattr(em, "_redis", r)
        em._redis_set("k", {"d": datetime(2026, 1, 1)}, ttl=30)
        assert r.setex_calls == [("k", 30, '{"d": "2026-01-01 00:00:00"}')]
        assert r.set_calls == []

    def test_set_writes_through_with_set_when_no_ttl(self, monkeypatch):
        r = FakeRedis()
        monkeypatch.setattr(em, "_redis", r)
        em._redis_set("k", {"a": 1})
        assert r.set_calls == [("k", '{"a": 1}')]
        assert r.setex_calls == []

    def test_set_redis_failure_is_swallowed_but_memory_kept(self, monkeypatch):
        monkeypatch.setattr(em, "_redis", FakeRedis(fail_set=True))
        em._redis_set("k", {"a": 1}, ttl=5)
        assert em._redis_get("k") == {"a": 1}

    def test_memory_eviction_drops_oldest_400(self):
        for i in range(4000):
            em._mem[f"k{i}"] = i
            em._mem_exp[f"k{i}"] = None
        em._redis_set("new", 1)  # 4001 entries → evict first 400
        assert len(em._mem) == 3601
        assert "k0" not in em._mem and "k0" not in em._mem_exp
        assert "k400" in em._mem
        assert "new" in em._mem


# ── state helpers ─────────────────────────────────────────────────────────────

class TestState:
    def test_default_state(self):
        st = em._load_state()
        assert st == {"subscriptions": [], "last_known": {}, "subscription_meta": {}}

    def test_legacy_state_is_backfilled_as_user(self):
        em._save_state({"subscriptions": ["A.NS", "B.NS"], "last_known": {}})
        st = em._load_state()
        assert st["subscription_meta"] == {
            "A.NS": {"source": "user", "added_at": None},
            "B.NS": {"source": "user", "added_at": None},
        }

    def test_existing_meta_is_preserved(self):
        em._save_state({
            "subscriptions": ["A.NS"], "last_known": {},
            "subscription_meta": {"A.NS": {"source": "auto", "added_at": "t"}},
        })
        assert em._load_state()["subscription_meta"]["A.NS"]["source"] == "auto"

    def test_save_state_persists_without_ttl(self):
        em._save_state({"subscriptions": [], "last_known": {}})
        assert em._mem_exp[em.STATE_KEY] is None


class TestNormalizeAndSafeFloat:
    @pytest.mark.parametrize("raw,expected", [
        ("tcs", "TCS.NS"),
        ("  infy ", "INFY.NS"),
        ("RELIANCE.NS", "RELIANCE.NS"),
        ("500325.bo", "500325.BO"),
    ])
    def test_normalize(self, raw, expected):
        assert em._normalize(raw) == expected

    @pytest.mark.parametrize("val,expected", [
        (1, 1.0), ("2.5", 2.5), (np.float64(3.0), 3.0),
        (None, None), ("abc", None), (float("nan"), None),
        (float("inf"), None), (float("-inf"), None), ([], None),
    ])
    def test_safe_float(self, val, expected):
        assert em._safe_float(val) == expected


# ── yfinance helpers ──────────────────────────────────────────────────────────

class TestRateLimitFlag:
    def test_not_limited_initially(self):
        assert em._yf_is_rate_limited() is False

    @pytest.mark.parametrize("msg", [
        "HTTP Error 429", "Too Many Requests", "Rate limited. Try after a while",
    ])
    def test_429_like_errors_set_cooldown(self, msg, rl):
        em._yf_mark_rate_limited(RuntimeError(msg))
        assert em._yf_is_rate_limited() is True
        assert em._yf_rate_limited_until == pytest.approx(time.time() + em._YF_COOLDOWN_SEC, abs=5)
        assert len(rl) == 1
        assert rl[0][1] == {"provider": "market_data", "path": "event/yfinance"}

    @pytest.mark.parametrize("err", [None, "", RuntimeError("boom"), "some other failure"])
    def test_other_errors_do_not_set_cooldown(self, err, rl):
        em._yf_mark_rate_limited(err)
        assert em._yf_is_rate_limited() is False
        assert rl == []

    def test_cooldown_never_shortens(self, monkeypatch):
        em._yf_rate_limited_until = time.time() + 10_000
        before = em._yf_rate_limited_until
        em._yf_mark_rate_limited("429")
        assert em._yf_rate_limited_until == before

    def test_reporter_import_failure_is_ignored(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rate_limit_report", None)  # ImportError
        em._yf_mark_rate_limited("429")
        assert em._yf_is_rate_limited() is True

    def test_reporter_exception_is_ignored(self, monkeypatch):
        fake = types.ModuleType("rate_limit_report")

        def bad(*a, **k):
            raise RuntimeError("x")

        fake.report_if_rate_limited = bad
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        em._yf_mark_rate_limited("429")
        assert em._yf_is_rate_limited() is True


class TestGetTicker:
    def test_creates_sets_tz_and_reuses(self, monkeypatch):
        made = []

        class Tk:
            def __init__(self, symbol):
                made.append(symbol)

        monkeypatch.setattr(em.yf, "Ticker", Tk)
        t1 = em._get_ticker("A.NS")
        t2 = em._get_ticker("A.NS")
        assert t1 is t2
        assert made == ["A.NS"]
        assert t1._tz == "Asia/Kolkata"

    def test_tz_assignment_failure_is_ignored(self, monkeypatch):
        class Tk:
            def __init__(self, symbol):
                pass

            @property
            def _tz(self):
                return None

        monkeypatch.setattr(em.yf, "Ticker", Tk)
        assert isinstance(em._get_ticker("B.NS"), Tk)


class TestGetCompanyName:
    def test_cached_name_short_circuits(self, monkeypatch):
        em._company_name_cache["A.NS"] = "Cached Co"
        monkeypatch.setattr(em, "_get_ticker", lambda s: pytest.fail("should not be called"))
        assert em._get_company_name("A.NS") == "Cached Co"

    def test_long_name_preferred(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(info={"longName": "Long Ltd", "shortName": "Short"}))
        assert em._get_company_name("A.NS") == "Long Ltd"
        assert em._company_name_cache["A.NS"] == "Long Ltd"

    def test_short_name_when_no_long(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(info={"shortName": "Short"}))
        assert em._get_company_name("A.NS") == "Short"

    @pytest.mark.parametrize("info", [{}, None])
    def test_fallback_to_symbol_without_suffix(self, monkeypatch, info):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(info=info))
        assert em._get_company_name("ABC.NS") == "ABC"
        assert em._get_company_name("XYZ.BO") == "XYZ"

    def test_exception_falls_back_and_marks_rate_limit(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("429 Too Many Requests")))
        assert em._get_company_name("ABC.NS") == "ABC"
        assert em._yf_is_rate_limited() is True

    def test_non_rate_limit_exception_falls_back_quietly(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("boom")))
        assert em._get_company_name("ABC.NS") == "ABC"
        assert em._yf_is_rate_limited() is False

    def test_rate_limited_returns_fallback_without_calling_yfinance(self, monkeypatch):
        em._yf_rate_limited_until = time.time() + 1000
        monkeypatch.setattr(em, "_get_ticker", lambda s: pytest.fail("should not be called"))
        assert em._get_company_name("ABC.NS") == "ABC"

    def test_QUIRK_rate_limit_fallback_is_cached_for_the_process_lifetime(self, monkeypatch):
        """A transient cool-down caches the bare ticker as the 'company name' with no
        expiry (COMPANY_NAME_CACHE_TTL is defined but never used), so Google News keeps
        being queried by ticker even after yfinance recovers."""
        em._yf_rate_limited_until = time.time() + 1000
        assert em._get_company_name("ABC.NS") == "ABC"
        em._yf_rate_limited_until = 0.0
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(info={"longName": "Real Name Ltd"}))
        assert em._get_company_name("ABC.NS") == "ABC"


class TestYfWrappers:
    """Each _get_* wrapper: happy path + swallowed exception (returns None / [])."""

    @pytest.mark.parametrize("fn,attr,call", [
        ("_get_dividends", "dividends", ()),
        ("_get_splits", "splits", ()),
        ("_get_insider_transactions", "insider_transactions", ()),
        ("_get_upgrades_downgrades", "upgrades_downgrades", ()),
        ("_get_institutional_holders", "institutional_holders", ()),
    ])
    def test_property_wrappers(self, monkeypatch, fn, attr, call):
        sentinel = object()
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(**{attr: sentinel}))
        assert getattr(em, fn)("A.NS", *call) is sentinel
        em._yf_cache.clear()
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("x")))
        assert getattr(em, fn)("A.NS", *call) is None

    def test_earnings_dates_passes_limit(self, monkeypatch):
        seen = {}

        def get_earnings_dates(limit):
            seen["limit"] = limit
            return "df"

        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(get_earnings_dates=get_earnings_dates))
        assert em._get_earnings_dates("A.NS", limit=4) == "df"
        assert seen["limit"] == 4

    def test_earnings_dates_exception(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("x")))
        assert em._get_earnings_dates("A.NS") is None


class TestGetEarningsHistory:
    def test_rate_limited_returns_none(self, monkeypatch):
        em._yf_rate_limited_until = time.time() + 1000
        monkeypatch.setattr(em, "_get_ticker", lambda s: pytest.fail("no yfinance while limited"))
        assert em._get_earnings_history("A.NS") is None

    def test_attribute_used_when_present(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(earnings_history="EH"))
        assert em._get_earnings_history("A.NS") == "EH"

    def test_falls_back_to_get_earnings_history_method(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(get_earnings_history=lambda: "EH2"))
        assert em._get_earnings_history("A.NS") == "EH2"

    def test_none_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T())
        assert em._get_earnings_history("A.NS") is None

    def test_none_when_method_not_callable(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(get_earnings_history="nope"))
        assert em._get_earnings_history("A.NS") is None

    def test_none_when_method_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(get_earnings_history=lambda: None))
        assert em._get_earnings_history("A.NS") is None

    def test_exception_marks_rate_limit_when_429(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("Too Many Requests")))
        assert em._get_earnings_history("A.NS") is None
        assert em._yf_is_rate_limited() is True

    def test_other_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("boom")))
        assert em._get_earnings_history("A.NS") is None
        assert em._yf_is_rate_limited() is False


class TestGetNews:
    def test_returns_list(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(news=[{"title": "x"}]))
        assert em._get_news("A.NS") == [{"title": "x"}]

    @pytest.mark.parametrize("val", [None, []])
    def test_empty_becomes_list(self, monkeypatch, val):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _T(news=val))
        assert em._get_news("A.NS") == []

    @pytest.mark.parametrize("msg", ["Expecting value: line 1 column 1 (char 0)", "line 1 column 1"])
    def test_empty_body_error_is_quiet(self, monkeypatch, msg):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(ValueError(msg)))
        assert em._get_news("A.NS") == []

    def test_other_error_returns_empty(self, monkeypatch):
        monkeypatch.setattr(em, "_get_ticker", lambda s: _Boom(RuntimeError("boom")))
        assert em._get_news("A.NS") == []


# ── keywords / classification / summary ───────────────────────────────────────

class TestKeywords:
    def test_company_base_alias_and_parts(self, monkeypatch):
        monkeypatch.setattr(em, "_get_company_name", lambda s: "Physics Wallah Ltd")
        keys = set(em._get_keywords("PWL.NS"))
        assert {"Physics Wallah Ltd", "PWL", "pwl", "physics wallah ltd"} <= keys
        assert {"Physics Wallah", "physics wallah", "PW Edtech", "pw edtech"} <= keys
        assert {"physics", "wallah", "ltd"} <= keys

    def test_short_parts_dropped_and_ampersand_split(self, monkeypatch):
        monkeypatch.setattr(em, "_get_company_name", lambda s: "A&B Co Industries")
        keys = set(em._get_keywords("ABC.BO"))
        assert "industries" in keys
        assert "co" not in keys and "a" not in keys and "b" not in keys

    def test_symbol_without_alias(self, monkeypatch):
        monkeypatch.setattr(em, "_get_company_name", lambda s: "Zenith Widgets")
        assert {"ZWID", "zwid", "zenith", "widgets"} <= set(em._get_keywords("ZWID.NS"))


class TestClassify:
    @pytest.mark.parametrize("title,expected", [
        ("Acme Q3 earnings beat", "results"),
        ("Acme reports profit jumps", "results"),
        ("Acme board meeting on Friday", "board_meeting"),
        ("Acme AGM notice", "board_meeting"),
        ("Bulk deal: fund buys Acme", "bulk_block"),
        ("Promoter buying at Acme", "insider"),
        ("Acme declares interim dividend", "dividend"),
        ("Acme announces bonus issue", "corporate_action"),
        ("Acme raises guidance", "guidance"),
        ("Acme shares open flat", None),
        ("", None),
        (None, None),
    ])
    def test_categories(self, title, expected):
        assert em._classify_event_title(title) == expected

    def test_first_matching_group_wins(self):
        # 'results' is checked before 'board_meeting'
        assert em._classify_event_title("Board meeting to consider results") == "results"

    def test_QUIRK_keywords_are_plain_substrings(self):
        """No word boundaries: 'egm' in 'segment', 'agm' in 'Magma', 'mou' in 'amount'."""
        assert em._classify_event_title("Segment update") == "board_meeting"
        assert em._classify_event_title("Magma share price") == "board_meeting"
        assert em._classify_event_title("Amount raised") == "guidance"


class TestSummarize:
    def test_empty(self):
        assert em._summarize_events({"symbol": "ACME.NS"}) == \
            "No major corporate events detected for ACME in the recent window."

    def test_empty_bo_symbol(self):
        assert "for ACME in" in em._summarize_events({"symbol": "ACME.BO"})

    def test_missing_symbol(self):
        assert em._summarize_events({}) == "No major corporate events detected for  in the recent window."

    def test_next_earnings(self):
        s = em._summarize_events({"symbol": "A.NS", "next_earnings_date": "2026-10-20"})
        assert s == "📅 Next results/earnings: 2026-10-20"

    @pytest.mark.parametrize("pct,word,mag", [(12.34, "beat", "12.3"), (-5.0, "missed", "5.0"), (0, "missed", "0.0")])
    def test_earnings_surprise(self, pct, word, mag):
        s = em._summarize_events({"symbol": "A.NS", "earnings_surprise": {"surprise_pct": pct}})
        assert s == f"📊 Latest earnings {word} estimates by {mag}%"

    def test_surprise_without_pct_ignored(self):
        s = em._summarize_events({"symbol": "A.NS", "earnings_surprise": {"surprise_pct": None}})
        assert s.startswith("No major")

    def test_insider_buys_and_sells(self):
        s = em._summarize_events({"symbol": "A.NS", "recent_insider_transactions": [
            {"transaction": "Purchase"}, {"transaction": "Buy"}, {"transaction": "Sale"},
            {"transaction": "Sell"}, {"transaction": None}, {"transaction": "Gift"},
        ]})
        assert "🟢 Insider/promoter buying (2 txn(s))" in s
        assert "🔴 Insider/promoter selling (2 txn(s))" in s

    def test_insider_neutral_only_adds_nothing(self):
        s = em._summarize_events({"symbol": "A.NS", "recent_insider_transactions": [{"transaction": "Gift"}]})
        assert s.startswith("No major")

    def test_bulk_with_side(self):
        s = em._summarize_events({"symbol": "A.NS", "bulk_deals": [{"side": "BUY"}, {}]})
        assert s == "📦 Bulk/block deal(s): 2 — BUY"

    def test_bulk_with_transaction_key(self):
        s = em._summarize_events({"symbol": "A.NS", "bulk_deals": [{"transaction": "SELL"}]})
        assert s == "📦 Bulk/block deal(s): 1 — SELL"

    def test_bulk_without_side_or_non_dict(self):
        assert em._summarize_events({"symbol": "A.NS", "bulk_deals": [{}]}) == "📦 Bulk/block deal(s): 1"
        assert em._summarize_events({"symbol": "A.NS", "bulk_deals": ["x"]}) == "📦 Bulk/block deal(s): 1"

    def test_last_dividend(self):
        s = em._summarize_events({"symbol": "A.NS", "last_dividend": {"amount": 2.5, "date": "2026-07-01"}})
        assert s == "💰 Last dividend: 2.5 on 2026-07-01"

    def test_news_categories_in_priority_order_with_labels(self):
        news = [
            {"title": "Acme raises guidance for FY27"},
            {"title": "Acme announces bonus issue"},
            {"title": "Acme declares dividend"},
            {"title": "Acme board meeting on 5th"},
            {"title": "Promoter buying at Acme"},
            {"title": "Bulk deal in Acme"},
            {"title": "Acme Q2 results out"},
            {"title": "Acme second results story"},
            {"title": None},
            {"title": "Nothing relevant here"},
        ]
        s = em._summarize_events({"symbol": "A.NS", "recent_news": news})
        assert s == " | ".join([
            "Results: Acme Q2 results out",
            "Bulk/Block: Bulk deal in Acme",
            "Insider: Promoter buying at Acme",
            "Board: Acme board meeting on 5th",
            "Dividend: Acme declares dividend",
            "Corporate action: Acme announces bonus issue",
            "Guidance/Orders: Acme raises guidance for FY27",
        ])

    def test_news_title_truncated_to_100_chars(self):
        long_title = "Acme results " + "x" * 200
        s = em._summarize_events({"symbol": "A.NS", "recent_news": [{"title": long_title}]})
        assert s == "Results: " + long_title[:100]

    def test_all_sections_joined(self):
        s = em._summarize_events({
            "symbol": "A.NS", "next_earnings_date": "2026-10-20",
            "earnings_surprise": {"surprise_pct": 4.0},
            "last_dividend": {"amount": 1, "date": "d"},
        })
        assert s.count(" | ") == 2


# ── news sources ──────────────────────────────────────────────────────────────

class TestGoogleNews:
    URL_PREFIX = "https://news.google.com/rss/search?q="

    def _patch(self, monkeypatch, feed):
        seen = {}

        def parse(url):
            seen["url"] = url
            return feed

        monkeypatch.setattr(em.feedparser, "parse", parse)
        return seen

    def test_builds_query_and_maps_entries(self, monkeypatch, company):
        seen = self._patch(monkeypatch, _feed([
            _entry("A", "http://a", days_ago=1, source=SimpleNamespace(title="Reuters")),
        ]))
        items = em._fetch_google_news("ZWID.NS")
        assert seen["url"].startswith(self.URL_PREFIX + "Zenith%20Widgets&hl=en-IN")
        assert len(items) == 1
        assert items[0]["title"] == "A" and items[0]["url"] == "http://a"
        assert items[0]["publisher"] == "Reuters"
        assert items[0]["published"] is not None

    def test_publisher_defaults_when_no_source(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry("A")]))
        assert em._fetch_google_news("ZWID.NS")[0]["publisher"] == "Google News"

    def test_publisher_none_when_source_has_no_title(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry("A", source=SimpleNamespace())]))
        assert em._fetch_google_news("ZWID.NS")[0]["publisher"] is None

    def test_missing_published_date_is_kept_with_none(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry("A", pp=None)]))
        assert em._fetch_google_news("ZWID.NS")[0]["published"] is None

    def test_entry_without_published_attribute(self, monkeypatch, company):
        e = SimpleNamespace(title="A", link="http://a")
        self._patch(monkeypatch, _feed([e]))
        assert em._fetch_google_news("ZWID.NS")[0]["published"] is None

    def test_entries_older_than_30_days_dropped(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry("old", days_ago=45), _entry("new", days_ago=2)]))
        assert [i["title"] for i in em._fetch_google_news("ZWID.NS")] == ["new"]

    def test_max_items_limits_entries_read(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry(f"t{i}") for i in range(10)]))
        assert len(em._fetch_google_news("ZWID.NS", max_items=3)) == 3

    def test_bozo_empty_feed_returns_empty(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([], bozo=True))
        assert em._fetch_google_news("ZWID.NS") == []

    def test_bozo_with_entries_still_used(self, monkeypatch, company):
        self._patch(monkeypatch, _feed([_entry("A")], bozo=True))
        assert len(em._fetch_google_news("ZWID.NS")) == 1

    def test_exception_returns_empty(self, monkeypatch, company):
        def boom(url):
            raise RuntimeError("dns")

        monkeypatch.setattr(em.feedparser, "parse", boom)
        assert em._fetch_google_news("ZWID.NS") == []


PORTALS = [
    (em._fetch_moneycontrol_news, "Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    (em._fetch_economic_times, "Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    (em._fetch_cnbc_tv18, "CNBC TV18", "https://www.cnbctv18.com/feed/"),
]


@pytest.mark.parametrize("fetch,publisher,url", PORTALS, ids=[p[1] for p in PORTALS])
class TestPortalFeeds:
    def _patch(self, monkeypatch, feed):
        seen = {}

        def parse(u):
            seen["url"] = u
            return feed

        monkeypatch.setattr(em.feedparser, "parse", parse)
        return seen

    def test_matches_on_title_and_maps_fields(self, monkeypatch, company, fetch, publisher, url):
        seen = self._patch(monkeypatch, _feed([
            _entry("Zenith Widgets posts profit", "http://z", days_ago=1),
            _entry("Unrelated market wrap", "http://u"),
        ]))
        items = fetch("ZWID.NS")
        assert seen["url"] == url
        assert len(items) == 1
        assert items[0]["title"] == "Zenith Widgets posts profit"
        assert items[0]["publisher"] == publisher
        assert items[0]["url"] == "http://z"
        assert items[0]["published"] is not None

    def test_matches_on_description(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([_entry("Market wrap", desc="Shares of ZWID rallied")]))
        assert len(fetch("ZWID.NS")) == 1

    def test_no_description_attribute_ok(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([_entry("Widgets rally", desc=None)]))
        assert len(fetch("ZWID.NS")) == 1

    def test_old_entries_dropped(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([_entry("Zenith old", days_ago=40), _entry("Zenith new", days_ago=1)]))
        assert [i["title"] for i in fetch("ZWID.NS")] == ["Zenith new"]

    def test_undated_entry_kept_with_none(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([_entry("Zenith undated", pp=None)]))
        assert fetch("ZWID.NS")[0]["published"] is None

    def test_entry_without_published_attribute_kept(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([SimpleNamespace(title="Zenith bare", link="http://b")]))
        assert fetch("ZWID.NS")[0]["published"] is None

    def test_stops_at_max_items(self, monkeypatch, company, fetch, publisher, url):
        self._patch(monkeypatch, _feed([_entry(f"Zenith {i}") for i in range(10)]))
        assert len(fetch("ZWID.NS", max_items=2)) == 2

    def test_only_first_50_entries_scanned(self, monkeypatch, company, fetch, publisher, url):
        entries = [_entry(f"noise {i}") for i in range(50)] + [_entry("Zenith late")]
        self._patch(monkeypatch, _feed(entries))
        assert fetch("ZWID.NS") == []

    def test_exception_returns_empty(self, monkeypatch, company, fetch, publisher, url):
        def boom(u):
            raise RuntimeError("net")

        monkeypatch.setattr(em.feedparser, "parse", boom)
        assert fetch("ZWID.NS") == []


class TestYfNews:
    def test_empty(self, monkeypatch):
        monkeypatch.setattr(em, "_get_news", lambda s: [])
        assert em._fetch_yf_news("A.NS") == []

    def test_new_style_content_payload(self, monkeypatch):
        monkeypatch.setattr(em, "_get_news", lambda s: [{"content": {
            "title": "New T", "provider": {"displayName": "Yahoo"},
            "pubDate": "2026-09-20T10:00:00Z", "canonicalUrl": {"url": "http://new"},
        }}])
        assert em._fetch_yf_news("A.NS") == [{
            "title": "New T", "publisher": "Yahoo",
            "published": "2026-09-20T10:00:00Z", "url": "http://new",
        }]

    def test_old_style_payload(self, monkeypatch):
        monkeypatch.setattr(em, "_get_news", lambda s: [{
            "title": "Old T", "publisher": "Reuters", "providerPublishTime": 1700000000, "link": "http://old",
        }])
        assert em._fetch_yf_news("A.NS") == [{
            "title": "Old T", "publisher": "Reuters", "published": "1700000000", "url": "http://old",
        }]

    def test_null_provider_and_url_fall_back(self, monkeypatch):
        monkeypatch.setattr(em, "_get_news", lambda s: [{
            "content": {"title": "T", "provider": None, "canonicalUrl": None},
            "publisher": "Pub", "link": "http://l",
        }])
        item = em._fetch_yf_news("A.NS")[0]
        assert item["publisher"] == "Pub" and item["url"] == "http://l"
        assert item["published"] == ""  # str("") when no providerPublishTime

    def test_only_first_five(self, monkeypatch):
        monkeypatch.setattr(em, "_get_news", lambda s: [{"title": f"t{i}"} for i in range(9)])
        assert len(em._fetch_yf_news("A.NS")) == 5


class TestMultiSource:
    SOURCES = ["_fetch_yf_news", "_fetch_google_news", "_fetch_moneycontrol_news",
               "_fetch_economic_times", "_fetch_cnbc_tv18"]

    def _set(self, monkeypatch, **by_name):
        for name in self.SOURCES:
            data = by_name.get(name, [])
            monkeypatch.setattr(em, name, lambda s, *a, _d=data, **k: list(_d))

    def test_all_empty(self, monkeypatch):
        self._set(monkeypatch)
        assert em._fetch_news_from_multiple_sources("A.NS") == []

    def test_merge_dedupe_sort_and_cap(self, monkeypatch):
        self._set(
            monkeypatch,
            _fetch_yf_news=[{"title": "Alpha", "published": "2026-09-20T10:00:00"}],
            _fetch_google_news=[
                {"title": "  ALPHA  ", "published": "2026-09-25T10:00:00"},  # dup (case/space)
                {"title": "Beta", "published": "2026-09-27T10:00:00"},
            ],
            _fetch_moneycontrol_news=[{"title": "Gamma", "published": None}],
            _fetch_economic_times=[{"title": "Delta", "published": "2026-09-21T10:00:00"}],
            _fetch_cnbc_tv18=[{"title": "Eps", "published": "2026-09-22T10:00:00"}],
        )
        out = em._fetch_news_from_multiple_sources("A.NS")
        # first occurrence of the duplicate wins; newest first; undated last
        assert [n["title"] for n in out] == ["Beta", "Eps", "Delta", "Alpha", "Gamma"]
        capped = em._fetch_news_from_multiple_sources("A.NS", max_total=2)
        assert [n["title"] for n in capped] == ["Beta", "Eps"]

    def test_source_arguments(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(em, "_fetch_yf_news", lambda s: [])
        monkeypatch.setattr(em, "_fetch_google_news", lambda s, max_items: seen.setdefault("g", max_items) and [])
        monkeypatch.setattr(em, "_fetch_moneycontrol_news", lambda s, max_items: seen.setdefault("m", max_items) and [])
        monkeypatch.setattr(em, "_fetch_economic_times", lambda s, max_items: seen.setdefault("e", max_items) and [])
        monkeypatch.setattr(em, "_fetch_cnbc_tv18", lambda s, max_items: seen.setdefault("c", max_items) and [])
        em._fetch_news_from_multiple_sources("A.NS")
        assert seen == {"g": 8, "m": 5, "e": 5, "c": 5}


# ── _fetch_events ─────────────────────────────────────────────────────────────

def _earn_df():
    return pd.DataFrame({"EPS Estimate": [1.0]}, index=pd.DatetimeIndex(["2026-10-20"]))


def _div_series():
    return pd.Series([1.5, 2.5], index=pd.DatetimeIndex(["2026-01-01", "2026-07-01"]))


def _split_series():
    return pd.Series([2.0], index=pd.DatetimeIndex(["2026-03-01"]))


def _ins_df(n=1):
    return pd.DataFrame({
        "Start Date": [f"2026-09-{10 + i}" for i in range(n)],
        "Insider": [f"Person {i}" for i in range(n)],
        "Transaction": ["Purchase"] * n,
        "Shares": [100 + i for i in range(n)],
        "Value": [1000.0 + i for i in range(n)],
    })


def _ud_df(n=1):
    idx = pd.DatetimeIndex([f"2026-09-{10 + i}" for i in range(n)])
    return pd.DataFrame({
        "Firm": [f"Firm {i}" for i in range(n)],
        "ToGrade": ["Buy"] * n, "FromGrade": ["Hold"] * n, "Action": ["up"] * n,
    }, index=idx)


def _ih_df(n=1):
    return pd.DataFrame({
        "Holder": [f"Fund {i}" for i in range(n)],
        "Shares": [1000 * (i + 1) for i in range(n)],
        "% Out": [0.5 + i for i in range(n)],
    })


def _eh_df(actual, estimate):
    return pd.DataFrame({"actual": [actual], "estimate": [estimate]}, index=pd.DatetimeIndex(["2026-07-01"]))


class TestFetchEventsCache:
    def test_cache_hit_skips_all_sources(self, monkeypatch):
        cached = {"symbol": "ZWID.NS", "recent_news": [{"title": "x"}], "cached": True}
        em._redis_set(f"{em.EVENT_CACHE_PREFIX}ZWID.NS", cached, ttl=100)
        for name in ("_get_earnings_dates", "_get_dividends", "_fetch_news_from_multiple_sources"):
            monkeypatch.setattr(em, name, lambda *a, **k: pytest.fail("cache should short-circuit"))
        assert em._fetch_events("zwid") == cached

    def test_cache_hit_with_no_news_still_returned(self, monkeypatch):
        cached = {"symbol": "ZWID.NS", "recent_news": None}
        em._redis_set(f"{em.EVENT_CACHE_PREFIX}ZWID.NS", cached, ttl=100)
        monkeypatch.setattr(em, "_get_earnings_dates", lambda *a, **k: pytest.fail("no fetch"))
        assert em._fetch_events("ZWID.NS") is cached

    def test_force_bypasses_cache(self, monkeypatch):
        em._redis_set(f"{em.EVENT_CACHE_PREFIX}ZWID.NS", {"symbol": "ZWID.NS", "old": True}, ttl=100)
        _wire(monkeypatch, divs=_div_series())
        res = em._fetch_events("ZWID.NS", force=True)
        assert "old" not in res and res["last_dividend"]["amount"] == 2.5

    def test_rate_limited_still_fetches(self, monkeypatch):
        em._yf_rate_limited_until = time.time() + 1000
        _wire(monkeypatch, news=[{"title": "Some news", "published": "2026-09-20", "publisher": "P", "url": "u"}])
        assert em._fetch_events("ZWID.NS")["recent_news"]


class TestFetchEventsData:
    def test_full_payload(self, monkeypatch):
        news = [
            {"title": "Zenith Q2 results announced", "published": "2026-09-27", "publisher": "P1", "url": "u1"},
            {"title": "Bulk deal in Zenith", "published": "2026-09-26", "publisher": "P2", "url": "u2"},
            {"title": "Zenith shares open flat", "published": "2026-09-25", "publisher": "P3", "url": "u3"},
        ]
        _wire(monkeypatch, earnings=_earn_df(), divs=_div_series(), splits=_split_series(),
              ins=_ins_df(), ud=_ud_df(), ih=_ih_df(), eh=_eh_df(1.1, 1.0), news=news)
        res = em._fetch_events("zwid")
        assert res["symbol"] == "ZWID.NS"
        assert res["next_earnings_date"] == "2026-10-20"
        assert res["last_dividend"] == {"date": "2026-07-01", "amount": 2.5}
        assert res["last_split"] == {"date": "2026-03-01", "ratio": 2.0}
        assert res["recent_insider_transactions"] == [{
            "date": "2026-09-10", "insider": "Person 0", "transaction": "Purchase",
            "shares": 100, "value": 1000.0,
        }]
        assert res["recent_analyst_actions"] == [{
            "date": "2026-09-10", "firm": "Firm 0", "to_grade": "Buy", "from_grade": "Hold", "action": "up",
        }]
        assert res["institutional_holders"] == [{"holder": "Fund 0", "shares": 1000, "pct_held": 0.5}]
        assert res["earnings_surprise"]["surprise_pct"] == 10.0
        assert res["earnings_surprise"]["actual"] == 1.1 and res["earnings_surprise"]["estimate"] == 1.0
        assert res["cached"] is False
        assert res["fii_dii_net_flow"] is None
        assert len(res["recent_news"]) == 3
        # classification: only categorised titles are listed; uncategorised are 'general' but excluded
        assert [e["event_type"] for e in res["classified_events"]] == ["results", "bulk_block"]
        assert res["bulk_deals"] == [{
            "title": "Bulk deal in Zenith", "published": "2026-09-26", "url": "u2", "source": "P2",
        }]
        assert isinstance(res["summary"], str) and res["summary"]
        datetime.fromisoformat(res["checked_at"])

    def test_enrich_events_adds_score_fields(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series())
        res = em._fetch_events("ZWID.NS")
        assert "recent_event_score" in res and "event_summary" in res

    def test_row_limits(self, monkeypatch):
        _wire(monkeypatch, ins=_ins_df(6), ud=_ud_df(6), ih=_ih_df(8))
        res = em._fetch_events("ZWID.NS")
        assert len(res["recent_insider_transactions"]) == 3
        assert len(res["recent_analyst_actions"]) == 3
        assert len(res["institutional_holders"]) == 5
        # analysts newest first
        assert res["recent_analyst_actions"][0]["date"] == "2026-09-15"

    def test_empty_frames_are_skipped(self, monkeypatch):
        empty = pd.DataFrame()
        _wire(monkeypatch, earnings=empty, divs=pd.Series(dtype=float), splits=pd.Series(dtype=float),
              ins=empty, ud=empty, ih=empty, eh=empty)
        res = em._fetch_events("ZWID.NS")
        assert res["next_earnings_date"] is None and res["last_dividend"] is None
        assert res["last_split"] is None and res["recent_insider_transactions"] == []
        assert res["recent_analyst_actions"] == [] and res["institutional_holders"] == []
        assert res["earnings_surprise"] is None

    def test_bad_index_types_are_swallowed(self, monkeypatch):
        """Non-Timestamp indexes make .date() fail; each block swallows its own error."""
        _wire(monkeypatch,
              earnings=pd.DataFrame({"x": [1]}, index=[0]),
              divs=pd.Series([1.0], index=[0]),
              splits=pd.Series([2.0], index=[0]))
        res = em._fetch_events("ZWID.NS")
        assert res["next_earnings_date"] is None
        assert res["last_dividend"] is None
        assert res["last_split"] is None

    def test_insider_shares_edge_cases(self, monkeypatch):
        ins = pd.DataFrame({
            "Start Date": ["", "2026-09-01", "2026-09-02"],
            "Insider": ["A", "B", "C"], "Transaction": ["Sale", "Sale", "Sale"],
            "Shares": [0, np.nan, 50], "Value": [np.nan, 5.0, 6.0],
        }, index=["r0", "r1", "r2"])
        _wire(monkeypatch, ins=ins)
        rows = em._fetch_events("ZWID.NS")["recent_insider_transactions"]
        assert rows[0]["date"] == "r0"          # empty Start Date → row label
        assert rows[0]["shares"] is None        # zero shares → None
        assert rows[0]["value"] is None         # NaN value → None
        assert rows[1]["shares"] is None        # NaN shares → None
        assert rows[2]["shares"] == 50

    def test_insider_without_shares_column(self, monkeypatch):
        ins = pd.DataFrame({"Insider": ["A"], "Transaction": ["Buy"]})
        _wire(monkeypatch, ins=ins)
        assert em._fetch_events("ZWID.NS")["recent_insider_transactions"][0]["shares"] is None

    def test_insider_row_error_keeps_earlier_rows(self, monkeypatch):
        ins = pd.DataFrame({
            "Start Date": ["d0", "d1"], "Insider": ["A", "B"], "Transaction": ["Buy", "Buy"],
            "Shares": [10, "5.5"], "Value": [1.0, 2.0],   # "5.5" → int() raises
        })
        _wire(monkeypatch, ins=ins)
        rows = em._fetch_events("ZWID.NS")["recent_insider_transactions"]
        assert [r["insider"] for r in rows] == ["A"]

    def test_analyst_non_datetime_index_uses_str(self, monkeypatch):
        ud = pd.DataFrame({"Firm": ["F"], "ToGrade": ["Buy"], "FromGrade": [""], "Action": ["init"]}, index=["label"])
        _wire(monkeypatch, ud=ud)
        assert em._fetch_events("ZWID.NS")["recent_analyst_actions"][0]["date"] == "label"

    def test_analyst_unsortable_index_is_swallowed(self, monkeypatch):
        idx = pd.Index([pd.Timestamp("2026-09-01"), "junk"], dtype=object)
        ud = pd.DataFrame({"Firm": ["A", "B"], "ToGrade": ["x", "y"], "FromGrade": ["", ""], "Action": ["a", "b"]}, index=idx)
        _wire(monkeypatch, ud=ud)
        assert em._fetch_events("ZWID.NS")["recent_analyst_actions"] == []

    def test_holder_shares_edge_cases_and_error(self, monkeypatch):
        ih = pd.DataFrame({"Holder": ["A", "B"], "Shares": [np.nan, 10], "% Out": [np.nan, 1.0]})
        _wire(monkeypatch, ih=ih)
        rows = em._fetch_events("ZWID.NS")["institutional_holders"]
        assert rows[0] == {"holder": "A", "shares": None, "pct_held": None}
        assert rows[1]["shares"] == 10

        em._mem.clear()
        bad = pd.DataFrame({"Holder": ["A", "B"], "Shares": [7, "5.5"], "% Out": [1.0, 2.0]})
        _wire(monkeypatch, ih=bad)
        assert [r["holder"] for r in em._fetch_events("ZWID.NS", force=True)["institutional_holders"]] == ["A"]

    def test_holder_without_shares_column(self, monkeypatch):
        _wire(monkeypatch, ih=pd.DataFrame({"Holder": ["A"]}))
        assert em._fetch_events("ZWID.NS")["institutional_holders"][0]["shares"] is None


class TestFetchEventsEarningsSurprise:
    def test_beat_and_miss_rounding(self, monkeypatch):
        _wire(monkeypatch, eh=_eh_df(1.0, 3.0))
        s = em._fetch_events("ZWID.NS")["earnings_surprise"]
        assert s["surprise_pct"] == -66.67
        assert s["date"] == "2026-07-01 00:00:00"

    def test_zero_estimate_skipped(self, monkeypatch):
        _wire(monkeypatch, eh=_eh_df(1.0, 0.0))
        assert em._fetch_events("ZWID.NS")["earnings_surprise"] is None

    def test_missing_actual_skipped(self, monkeypatch):
        _wire(monkeypatch, eh=_eh_df(None, 2.0))
        assert em._fetch_events("ZWID.NS")["earnings_surprise"] is None

    def test_missing_columns_skipped(self, monkeypatch):
        _wire(monkeypatch, eh=pd.DataFrame({"other": [1]}, index=pd.DatetimeIndex(["2026-07-01"])))
        assert em._fetch_events("ZWID.NS")["earnings_surprise"] is None

    def test_non_numeric_values_swallowed(self, monkeypatch):
        _wire(monkeypatch, eh=_eh_df("a", "b"))
        assert em._fetch_events("ZWID.NS")["earnings_surprise"] is None

    def test_QUIRK_nan_actual_and_estimate_produce_nan_surprise(self, monkeypatch):
        """NaN is not None and not == 0, so it passes the guards: surprise_pct becomes NaN,
        the summary reads 'missed estimates by nan%', and the payload can't be rendered by
        FastAPI (JSONResponse uses allow_nan=False) → HTTP 500 for that symbol."""
        _wire(monkeypatch, eh=_eh_df(np.nan, np.nan))
        res = em._fetch_events("ZWID.NS")
        assert res["earnings_surprise"]["actual"] is None
        assert res["earnings_surprise"]["estimate"] is None
        assert np.isnan(res["earnings_surprise"]["surprise_pct"])
        assert "missed estimates by nan%" in res["summary"]
        with pytest.raises(ValueError):
            JSONResponse(res)


class TestFetchEventsCachingAndFallback:
    NEWS = [{"title": "Zenith update", "published": "2026-09-20", "publisher": "P", "url": "u"}]

    def test_real_data_with_news_caches_long_ttl_and_fallback(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series(), news=self.NEWS)
        res = em._fetch_events("ZWID.NS")
        key = f"{em.EVENT_CACHE_PREFIX}ZWID.NS"
        assert res["cached"] is False
        assert em._mem[key]["cached"] is True
        assert em._mem_exp[key] == pytest.approx(time.time() + em.EVENT_CACHE_TTL, abs=5)
        fb = f"{em.EVENT_FALLBACK_PREFIX}ZWID.NS"
        assert em._mem[fb]["cached"] is False
        assert em._mem_exp[fb] == pytest.approx(time.time() + em.EVENT_FALLBACK_TTL, abs=5)

    def test_real_data_without_news_uses_short_ttl(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series())
        em._fetch_events("ZWID.NS")
        key = f"{em.EVENT_CACHE_PREFIX}ZWID.NS"
        assert em._mem_exp[key] == pytest.approx(time.time() + em.EMPTY_NEWS_CACHE_TTL, abs=5)

    def test_second_call_served_from_cache(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series(), news=self.NEWS)
        em._fetch_events("ZWID.NS")
        monkeypatch.setattr(em, "_get_dividends", lambda s: pytest.fail("cached"))
        assert em._fetch_events("ZWID.NS")["cached"] is True

    def test_empty_live_fetch_serves_stale_fallback(self, monkeypatch):
        fb = f"{em.EVENT_FALLBACK_PREFIX}ZWID.NS"
        em._redis_set(fb, {"symbol": "ZWID.NS", "last_dividend": {"date": "d", "amount": 1}}, ttl=1000)
        _wire(monkeypatch)
        res = em._fetch_events("ZWID.NS")
        assert res["cached"] is True and res["stale"] is True
        assert res["last_dividend"]["amount"] == 1
        key = f"{em.EVENT_CACHE_PREFIX}ZWID.NS"
        assert em._mem[key]["stale"] is True
        assert em._mem_exp[key] == pytest.approx(time.time() + 900, abs=5)

    def test_empty_live_fetch_no_fallback_returns_empty_result_uncached(self, monkeypatch):
        _wire(monkeypatch)
        res = em._fetch_events("ZWID.NS")
        assert res["cached"] is False
        assert res["recent_news"] == [] and res["classified_events"] == []
        assert f"{em.EVENT_CACHE_PREFIX}ZWID.NS" not in em._mem
        assert f"{em.EVENT_FALLBACK_PREFIX}ZWID.NS" not in em._mem


class TestFetchEventsEnrichment:
    def test_enrich_events_result_is_used(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series())
        monkeypatch.setattr(em, "enrich_events", lambda r, symbol: {**r, "tag": symbol})
        assert em._fetch_events("ZWID.NS")["tag"] == "ZWID.NS"

    def test_enrich_events_failure_keeps_base_result(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series())

        def boom(r, symbol):
            raise RuntimeError("enrich broke")

        monkeypatch.setattr(em, "enrich_events", boom)
        res = em._fetch_events("ZWID.NS")
        assert res["last_dividend"]["amount"] == 2.5 and "tag" not in res

    def test_enrich_events_absent(self, monkeypatch):
        _wire(monkeypatch, divs=_div_series())
        monkeypatch.setattr(em, "enrich_events", None)
        assert "recent_event_score" not in em._fetch_events("ZWID.NS")


# ── _diff_events ──────────────────────────────────────────────────────────────

class TestDiffEvents:
    def test_identical_snapshots(self):
        snap = {"next_earnings_date": "d", "last_dividend": {"date": "1", "amount": 1},
                "recent_analyst_actions": [{"date": "1", "firm": "F"}]}
        assert em._diff_events(snap, dict(snap)) == []

    def test_empty_vs_empty(self):
        assert em._diff_events({}, {}) == []

    def test_earnings_date_change(self):
        out = em._diff_events({"next_earnings_date": "a"}, {"next_earnings_date": "b"})
        assert out == ["Earnings date: a → b"]

    def test_first_snapshot_reports_everything_as_new(self):
        out = em._diff_events({}, {"next_earnings_date": "2026-10-20"})
        assert out == ["Earnings date: None → 2026-10-20"]

    def test_new_dividend(self):
        out = em._diff_events({"last_dividend": {"date": "1"}}, {"last_dividend": {"date": "2", "amount": 3.0}})
        assert out == ["New dividend declared: ₹3.0 on 2"]

    def test_dividend_without_current_date_ignored(self):
        assert em._diff_events({"last_dividend": {"date": "1"}}, {"last_dividend": None}) == []

    def test_new_split(self):
        out = em._diff_events({}, {"last_split": {"date": "d", "ratio": 2.0}})
        assert out == ["Stock split: 2.0:1 on d"]

    def test_split_without_current_date_ignored(self):
        assert em._diff_events({"last_split": {"date": "d"}}, {}) == []

    def test_new_analyst_action_only(self):
        prev = {"recent_analyst_actions": [{"date": "1", "firm": "A"}]}
        cur = {"recent_analyst_actions": [
            {"date": "1", "firm": "A", "action": "up", "to_grade": "Buy"},
            {"date": "2", "firm": "B", "action": "init", "to_grade": "Sell"},
        ]}
        assert em._diff_events(prev, cur) == ["Analyst: B init → Sell"]

    def test_new_insider_transaction_only(self):
        prev = {"recent_insider_transactions": [{"date": "1", "insider": "X"}]}
        cur = {"recent_insider_transactions": [
            {"date": "1", "insider": "X", "transaction": "Buy", "shares": 1},
            {"date": "2", "insider": "Y", "transaction": "Sale", "shares": 50},
        ]}
        assert em._diff_events(prev, cur) == ["Insider Sale: Y — 50 shares"]

    def test_surprise_change(self):
        out = em._diff_events({"earnings_surprise": {"surprise_pct": 1.0}}, {"earnings_surprise": {"surprise_pct": 2.5}})
        assert out == ["Earnings surprise: 2.5%"]

    def test_surprise_unchanged_or_absent(self):
        same = {"earnings_surprise": {"surprise_pct": 1.0}}
        assert em._diff_events(same, dict(same)) == []
        assert em._diff_events({"earnings_surprise": None}, {"earnings_surprise": None}) == []

    def test_bulk_count_change(self):
        assert em._diff_events({"bulk_deals": []}, {"bulk_deals": [{}]}) == ["Bulk/Block deal detected"]
        assert em._diff_events({"bulk_deals": [{}]}, {"bulk_deals": [{}]}) == []

    def test_new_institutional_holder(self):
        out = em._diff_events({}, {"institutional_holders": [{"holder": "Fund A", "shares": 12345}]})
        assert out == ["New institutional holder: Fund A — 12,345 shares"]

    def test_new_holder_without_shares_ignored(self):
        assert em._diff_events({}, {"institutional_holders": [{"holder": "F", "shares": None}]}) == []
        assert em._diff_events({}, {"institutional_holders": [{"holder": "F", "shares": 0}]}) == []

    def test_holder_without_name_ignored(self):
        assert em._diff_events({}, {"institutional_holders": [{"shares": 5}]}) == []
        assert em._diff_events({"institutional_holders": [{"shares": 5}]}, {}) == []

    def test_holder_increase_over_five_percent(self):
        prev = {"institutional_holders": [{"holder": "F", "shares": 1000}]}
        cur = {"institutional_holders": [{"holder": "F", "shares": 1100}]}
        assert em._diff_events(prev, cur) == ["F increased holding by 10.0% (1,000 → 1,100 shares)"]

    def test_holder_small_change_or_decrease_ignored(self):
        prev = {"institutional_holders": [{"holder": "F", "shares": 1000}]}
        assert em._diff_events(prev, {"institutional_holders": [{"holder": "F", "shares": 1050}]}) == []
        assert em._diff_events(prev, {"institutional_holders": [{"holder": "F", "shares": 500}]}) == []

    def test_holder_with_missing_previous_shares_ignored(self):
        prev = {"institutional_holders": [{"holder": "F", "shares": None}]}
        cur = {"institutional_holders": [{"holder": "F", "shares": 500}]}
        assert em._diff_events(prev, cur) == []

    def test_holder_current_shares_missing_ignored(self):
        prev = {"institutional_holders": [{"holder": "F", "shares": 1000}]}
        cur = {"institutional_holders": [{"holder": "F", "shares": None}]}
        assert em._diff_events(prev, cur) == []


# ── routes ────────────────────────────────────────────────────────────────────

class TestSimpleRoutes:
    def test_root(self):
        r = em.root()
        assert r["service"] == "Stockky Event Tracker Service"
        assert r["version"] == "0.4.4" and r["status"] == "running"
        assert "/events/raw-feed?hours=24" in r["endpoints"]

    def test_health_without_and_with_redis(self, monkeypatch):
        assert em.health() == {"status": "ok", "service": "event-tracker-service", "redis": False}
        monkeypatch.setattr(em, "_redis", object())
        assert em.health()["redis"] is True

    def test_get_events_delegates(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(em, "_fetch_events", lambda s, force=False: seen.update(s=s, f=force) or {"ok": 1})
        assert em.get_events("tcs", force=True) == {"ok": 1}
        assert seen == {"s": "tcs", "f": True}
        em.get_events("tcs")
        assert seen["f"] is False


class TestRouteOrdering:
    @staticmethod
    def _matching_endpoint(path):
        scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
        for route in em.app.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route.endpoint
        return None

    def test_normal_routes_resolve(self):
        assert self._matching_endpoint("/events/TCS") is em.get_events
        assert self._matching_endpoint("/events/TCS/categorized") is em.get_events_categorized
        assert self._matching_endpoint("/subscriptions") is em.list_subscriptions
        assert self._matching_endpoint("/check") is em.check_for_changes

    def test_QUIRK_raw_feed_is_shadowed_by_symbol_route(self):
        """/events/{symbol} is registered BEFORE /events/raw-feed, so an HTTP GET to
        /events/raw-feed is answered by get_events(symbol="raw-feed"), and raw_feed()
        is unreachable. Fix = register raw-feed first. Pinned, not changed."""
        assert self._matching_endpoint("/events/raw-feed") is em.get_events
        assert self._matching_endpoint("/events/raw-feed") is not em.raw_feed


def _current(**over):
    base = {
        "symbol": "ZWID.NS", "next_earnings_date": None, "last_dividend": None, "last_split": None,
        "recent_analyst_actions": [], "recent_insider_transactions": [], "earnings_surprise": None,
        "institutional_holders": [], "checked_at": "2026-09-29T00:00:00", "summary": "S",
    }
    base.update(over)
    return base


class TestCategorized:
    def _go(self, monkeypatch, current, symbol="zwid", enrich=None, **kw):
        monkeypatch.setattr(em, "_fetch_events", lambda s, force=False: current)
        monkeypatch.setattr(em, "enrich_events", enrich)
        return em.get_events_categorized(symbol, **kw)

    def test_normalizes_symbol_and_basic_shape(self, monkeypatch):
        out = self._go(monkeypatch, _current())
        assert out["symbol"] == "ZWID.NS"
        assert out["upcoming"] == [] and out["recent"] == [] and out["recent_changes"] == []
        assert out["summary"] == "S" and out["event_summary"] == "S"
        assert out["checked_at"] == "2026-09-29T00:00:00"
        assert out["institutional_holders"] == []
        assert out["recent_event_score"] is None and out["has_positive_catalyst"] is None

    def test_summary_falls_back_to_event_summary(self, monkeypatch):
        cur = _current(summary=None, event_summary="ES")
        out = self._go(monkeypatch, cur)
        assert out["summary"] == "ES" and out["event_summary"] == "ES"

    def test_future_earnings_is_upcoming(self, monkeypatch):
        d = _iso(10)
        out = self._go(monkeypatch, _current(next_earnings_date=d))
        assert out["upcoming"] == [{"type": "earnings_date", "date": d, "description": f"Next earnings: {d}"}]
        assert out["recent"] == []

    def test_past_earnings_is_recent(self, monkeypatch):
        d = _iso(-10)
        out = self._go(monkeypatch, _current(next_earnings_date=d))
        assert out["upcoming"] == [] and out["recent"][0]["type"] == "earnings_date"

    def test_z_suffix_handled(self, monkeypatch):
        out = self._go(monkeypatch, _current(next_earnings_date=_iso(10) + "T00:00:00Z"))
        assert len(out["upcoming"]) == 1

    def test_unparseable_earnings_date_defaults_to_upcoming(self, monkeypatch):
        out = self._go(monkeypatch, _current(next_earnings_date="soon"))
        assert len(out["upcoming"]) == 1 and out["upcoming"][0]["date"] == "soon"

    def test_all_recent_kinds_sorted_newest_first(self, monkeypatch):
        cur = _current(
            last_dividend={"date": "2026-07-01", "amount": 2.5},
            last_split={"date": "2026-03-01", "ratio": 2.0},
            recent_analyst_actions=[{"date": "2026-09-10", "firm": "F", "action": "up", "to_grade": "Buy"}],
            recent_insider_transactions=[{"date": "2026-09-20", "insider": "P", "transaction": "Buy", "shares": 5}],
            earnings_surprise={"date": "2026-08-01", "surprise_pct": 4.5},
        )
        out = self._go(monkeypatch, cur)
        assert [r["type"] for r in out["recent"]] == ["insider", "analyst", "earnings_surprise", "dividend", "split"]
        by_type = {r["type"]: r["description"] for r in out["recent"]}
        assert by_type["dividend"] == "Dividend of ₹2.5 declared"
        assert by_type["split"] == "2.0:1 stock split"
        assert by_type["analyst"] == "F: up → Buy"
        assert by_type["insider"] == "Insider Buy: P — 5 shares"
        assert by_type["earnings_surprise"] == "Earnings surprise: 4.5% vs estimate"

    def test_entries_without_dates_sort_last(self, monkeypatch):
        cur = _current(
            last_dividend={"date": "2026-07-01", "amount": 1},
            recent_analyst_actions=[{"firm": "F", "action": "a", "to_grade": "b"}],
        )
        out = self._go(monkeypatch, cur)
        assert [r["type"] for r in out["recent"]] == ["dividend", "analyst"]

    def test_items_without_date_are_skipped_for_dividend_split_surprise(self, monkeypatch):
        cur = _current(last_dividend={"amount": 1}, last_split={"ratio": 2},
                       earnings_surprise={"surprise_pct": 1.0})
        assert self._go(monkeypatch, cur)["recent"] == []

    def test_recent_changes_against_previous_snapshot(self, monkeypatch):
        em._save_state({"subscriptions": [], "last_known": {"ZWID.NS": _current(next_earnings_date="2026-01-01")}})
        out = self._go(monkeypatch, _current(next_earnings_date=_iso(30)))
        assert out["recent_changes"] == [f"Earnings date: 2026-01-01 → {_iso(30)}"]

    def test_no_previous_snapshot_no_changes(self, monkeypatch):
        assert self._go(monkeypatch, _current(next_earnings_date=_iso(30)))["recent_changes"] == []

    def test_enrich_events_merges_and_restores_lists(self, monkeypatch):
        cur = _current(next_earnings_date=_iso(10))

        def enrich(d, symbol):
            assert symbol == "ZWID.NS"
            return {**d, "recent_event_score": 77, "upcoming": "CLOBBERED", "recent": "CLOBBERED",
                    "recent_changes": "CLOBBERED"}

        out = self._go(monkeypatch, cur, enrich=enrich)
        assert out["recent_event_score"] == 77
        assert isinstance(out["upcoming"], list) and isinstance(out["recent"], list)
        assert out["recent_changes"] == []

    def test_enrich_events_failure_returns_unenriched(self, monkeypatch):
        def boom(d, symbol):
            raise RuntimeError("x")

        out = self._go(monkeypatch, _current(), enrich=boom)
        assert out["summary"] == "S"

    def test_force_flag_forwarded(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(em, "_fetch_events", lambda s, force=False: seen.update(f=force) or _current())
        monkeypatch.setattr(em, "enrich_events", None)
        em.get_events_categorized("zwid", force=True)
        assert seen["f"] is True


class TestSubscriptions:
    def test_subscribe_normalizes_sorts_and_tags(self):
        r = em.subscribe(em.SubscribeRequest(symbols=["tcs", "infy", "TCS"]))
        assert r == {"subscriptions": ["INFY.NS", "TCS.NS"]}
        meta = em._load_state()["subscription_meta"]
        assert meta["TCS.NS"]["source"] == "user" and meta["TCS.NS"]["added_at"]

    def test_subscribe_auto_source(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="auto"))
        assert em._load_state()["subscription_meta"]["TCS.NS"]["source"] == "auto"

    def test_auto_never_downgrades_user(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="user"))
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="auto"))
        assert em._load_state()["subscription_meta"]["TCS.NS"]["source"] == "user"

    def test_user_upgrades_auto(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="auto"))
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="user"))
        assert em._load_state()["subscription_meta"]["TCS.NS"]["source"] == "user"

    def test_auto_resubscribe_keeps_original_meta(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="auto"))
        before = dict(em._load_state()["subscription_meta"]["TCS.NS"])
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="auto"))
        assert em._load_state()["subscription_meta"]["TCS.NS"] == before

    def test_unsubscribe_removes_and_reports(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs", "infy"]))
        r = em.unsubscribe(em.UnsubscribeRequest(symbols=["tcs", "wipro"]))
        assert r == {"subscriptions": ["INFY.NS"], "removed": ["TCS.NS"]}
        assert "TCS.NS" not in em._load_state()["subscription_meta"]

    def test_unsubscribe_only_source_filters(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="user"))
        em.subscribe(em.SubscribeRequest(symbols=["infy"], source="auto"))
        r = em.unsubscribe(em.UnsubscribeRequest(symbols=["tcs", "infy"], only_source="auto"))
        assert r == {"subscriptions": ["TCS.NS"], "removed": ["INFY.NS"]}

    def test_unsubscribe_without_meta_treated_as_user(self):
        em._save_state({"subscriptions": ["OLD.NS"], "last_known": {}, "subscription_meta": {}})
        # _load_state backfills meta; force it missing again to hit the .get default
        state = em._mem[em.STATE_KEY]
        state["subscription_meta"] = {}
        real_load = em._load_state
        em._load_state = lambda: state
        try:
            r = em.unsubscribe(em.UnsubscribeRequest(symbols=["old"], only_source="auto"))
        finally:
            em._load_state = real_load
        assert r["removed"] == []  # missing meta counts as "user", so an auto-only prune skips it

    def test_list_subscriptions_all_and_by_source(self):
        em.subscribe(em.SubscribeRequest(symbols=["tcs"], source="user"))
        em.subscribe(em.SubscribeRequest(symbols=["infy"], source="auto"))
        assert em.list_subscriptions() == {"subscriptions": ["INFY.NS", "TCS.NS"]}
        assert em.list_subscriptions(source="auto") == {"subscriptions": ["INFY.NS"]}
        assert em.list_subscriptions(source="user") == {"subscriptions": ["TCS.NS"]}
        assert em.list_subscriptions(source="nope") == {"subscriptions": []}


class TestRawFeed:
    def _seed(self, subs, cache):
        em._save_state({"subscriptions": subs, "last_known": {}})
        for sym, news in cache.items():
            em._redis_set(f"{em.EVENT_CACHE_PREFIX}{sym}", {"recent_news": news}, ttl=100)

    def test_no_subscriptions(self):
        out = em.raw_feed()
        assert out["items"] == [] and out["hours"] == 24
        datetime.fromisoformat(out["checked_at"])

    def test_symbols_without_cache_skipped(self):
        self._seed(["A.NS", "B.NS"], {})
        assert em.raw_feed()["items"] == []

    def test_recent_items_included_old_excluded(self):
        recent = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        old = (datetime.utcnow() - timedelta(hours=60)).isoformat()
        self._seed(["A.NS"], {"A.NS": [
            {"title": "fresh", "published": recent, "publisher": "P"},
            {"title": "stale", "published": old, "publisher": "P"},
        ]})
        items = em.raw_feed(hours=24)["items"]
        assert items == [{"symbol": "A.NS", "headline": "fresh", "price": None, "ts": recent, "publisher": "P"}]

    def test_hours_widens_window(self):
        old = (datetime.utcnow() - timedelta(hours=60)).isoformat()
        self._seed(["A.NS"], {"A.NS": [{"title": "stale", "published": old}]})
        out = em.raw_feed(hours=72)
        assert out["hours"] == 72 and len(out["items"]) == 1

    def test_z_suffix_parsed(self):
        old = (datetime.utcnow() - timedelta(hours=60)).isoformat() + "Z"
        self._seed(["A.NS"], {"A.NS": [{"title": "stale", "published": old}]})
        assert em.raw_feed(hours=24)["items"] == []

    def test_undated_and_unparseable_items_are_kept(self):
        self._seed(["A.NS"], {"A.NS": [
            {"title": "undated", "published": None},
            {"title": "epoch", "published": "1700000000"},
            {"title": "aware", "published": "2020-01-01T00:00:00+05:30"},  # aware vs naive → TypeError
        ]})
        assert [i["headline"] for i in em.raw_feed()["items"]] == ["undated", "epoch", "aware"]

    def test_null_news_list(self):
        self._seed(["A.NS"], {"A.NS": None})
        assert em.raw_feed()["items"] == []

    def test_items_from_multiple_symbols(self):
        ts = datetime.utcnow().isoformat()
        self._seed(["A.NS", "B.NS"], {"A.NS": [{"title": "a", "published": ts}], "B.NS": [{"title": "b", "published": ts}]})
        assert [i["symbol"] for i in em.raw_feed()["items"]] == ["A.NS", "B.NS"]


class TestCheck:
    def test_no_subscriptions(self):
        out = em.check_for_changes()
        assert out["checked"] == 0 and out["changes"] == []
        datetime.fromisoformat(out["checked_at"])

    def test_first_check_reports_and_stores_snapshot_then_quiet(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(em.time, "sleep", lambda s: sleeps.append(s))
        snaps = {
            "A.NS": _current(symbol="A.NS", next_earnings_date="2026-10-20"),
            "B.NS": _current(symbol="B.NS"),
        }
        monkeypatch.setattr(em, "_fetch_events", lambda s, force=False: snaps[s])
        em.subscribe(em.SubscribeRequest(symbols=["a", "b"]))

        first = em.check_for_changes()
        assert first["checked"] == 2
        assert [c["symbol"] for c in first["changes"]] == ["A.NS"]
        assert first["changes"][0]["changes"] == ["Earnings date: None → 2026-10-20"]
        assert first["changes"][0]["current"] is snaps["A.NS"]
        assert sleeps == [1]  # only between symbols, not before the first
        assert set(em._load_state()["last_known"]) == {"A.NS", "B.NS"}

        second = em.check_for_changes()
        assert second["changes"] == []

    def test_detects_change_on_later_check(self, monkeypatch):
        monkeypatch.setattr(em.time, "sleep", lambda s: None)
        box = {"cur": _current(symbol="A.NS", next_earnings_date="2026-10-20")}
        monkeypatch.setattr(em, "_fetch_events", lambda s, force=False: box["cur"])
        em.subscribe(em.SubscribeRequest(symbols=["a"]))
        em.check_for_changes()
        box["cur"] = _current(symbol="A.NS", next_earnings_date="2026-11-01")
        out = em.check_for_changes()
        assert out["changes"][0]["changes"] == ["Earnings date: 2026-10-20 → 2026-11-01"]


class TestSymbolsWithEvents:
    def _seed(self, subs, events):
        em._save_state({"subscriptions": subs, "last_known": {}})
        for sym, ev in events.items():
            em._redis_set(f"{em.EVENT_CACHE_PREFIX}{sym}", ev, ttl=100)

    def test_cached_list_returned(self):
        em._redis_set(em.EVENTS_LIST_CACHE_KEY, ["X.NS"], ttl=100)
        assert em.symbols_with_events() == {"symbols": ["X.NS"]}

    def test_non_list_cache_is_ignored(self):
        em._redis_set(em.EVENTS_LIST_CACHE_KEY, {"not": "a list"}, ttl=100)
        assert em.symbols_with_events() == {"symbols": []}

    def test_no_subscriptions_caches_empty(self):
        assert em.symbols_with_events() == {"symbols": []}
        assert em._mem[em.EVENTS_LIST_CACHE_KEY] == []
        assert em._mem_exp[em.EVENTS_LIST_CACHE_KEY] == pytest.approx(time.time() + em.EVENTS_LIST_CACHE_TTL, abs=5)

    def test_matches_on_earnings_dividend_and_split(self):
        soon = _iso(2)
        self._seed(["E.NS", "D.NS", "S.NS", "FAR.NS", "NONE.NS"], {
            "E.NS": {"next_earnings_date": soon},
            "D.NS": {"last_dividend": {"date": soon}},
            "S.NS": {"last_split": {"date": soon}},
            "FAR.NS": {"next_earnings_date": _iso(30), "last_dividend": {"date": _iso(40)},
                       "last_split": {"date": _iso(-3)}},
            "NONE.NS": {},
        })
        out = em.symbols_with_events(days_ahead=7)
        assert out == {"symbols": ["D.NS", "E.NS", "S.NS"]}
        assert em._mem[em.EVENTS_LIST_CACHE_KEY] == ["D.NS", "E.NS", "S.NS"]

    def test_days_ahead_controls_window(self):
        self._seed(["A.NS"], {"A.NS": {"next_earnings_date": _iso(20)}})
        assert em.symbols_with_events(days_ahead=7) == {"symbols": []}
        em._mem.pop(em.EVENTS_LIST_CACHE_KEY)
        assert em.symbols_with_events(days_ahead=30) == {"symbols": ["A.NS"]}

    def test_symbols_without_cached_events_skipped(self):
        self._seed(["A.NS"], {})
        assert em.symbols_with_events() == {"symbols": []}

    def test_unparseable_dates_are_skipped_and_later_fields_still_checked(self):
        self._seed(["A.NS", "B.NS", "C.NS"], {
            "A.NS": {"next_earnings_date": "soon", "last_dividend": {"date": _iso(1)}},
            "B.NS": {"last_dividend": {"date": "junk"}, "last_split": {"date": _iso(1)}},
            "C.NS": {"last_split": {"date": "junk"}},
        })
        assert em.symbols_with_events() == {"symbols": ["A.NS", "B.NS"]}

    def test_non_string_dates_are_skipped(self):
        self._seed(["A.NS"], {"A.NS": {"next_earnings_date": 12345}})
        assert em.symbols_with_events() == {"symbols": []}

    def test_result_is_cached_for_next_call(self):
        self._seed(["A.NS"], {"A.NS": {"next_earnings_date": _iso(2)}})
        first = em.symbols_with_events()
        em._mem.pop(f"{em.EVENT_CACHE_PREFIX}A.NS")
        assert em.symbols_with_events() == first
