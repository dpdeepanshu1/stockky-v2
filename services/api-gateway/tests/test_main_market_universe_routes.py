"""tests/test_main_market_universe_routes.py — coverage for api-gateway/main.py, slice 8 (lines 6036-6453)

Pass 66. The market-overview routes and the scan-universe routes:

* `GET /market/session`, `/market/top-gainers`, `/market/top-losers`, `/market/most-active`;
* `GET /market/trending` (market-data quote -> yfinance fallback, the 10-symbol cap, the 20 s budget);
* `GET /market/indices` (cache hit, live build, mood bands, the stale / fallback degrade paths);
* `_movers_with_deadline` and `GET /scan/universe` (warm / stale / cold caches, the build deadline);
* `GET /universe` + `/api/universe` (Neon feed -> training universe -> local builder);
* `DELETE /scan/universe/cache`.

Everything downstream is faked: the session-phase / holiday helpers, the nifty-50 and momentum helpers,
the sync `httpx.get`, `httpx.AsyncClient`, `yf.Ticker` (tiny DataFrames), the kv cache, the stale-fallback
cache, the universe builder and the price/equity filters. Nothing touches the network or a database.
Findings are pinned as current behaviour and marked ``NOT FIXED``; fixed or by-design ones are labelled as such.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_market_universe_routes.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


class KV:
    """Dict-backed stand-in for the gateway's `_redis_get` / `_redis_set`."""

    def __init__(self):
        self.store = {}
        self.sets = []          # (key, value, ttl)

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


@pytest.fixture
def client():
    return TestClient(gw.app, raise_server_exceptions=False)


class _AsyncioProxy:
    """Stands in for `gw.asyncio`: everything real except the named overrides."""

    def __init__(self, **overrides):
        self._o = overrides

    def __getattr__(self, name):
        if name in self._o:
            return self._o[name]
        return getattr(asyncio, name)


# ═════════════════════════════════════════════════════════════════════════════
# GET /market/session
# ═════════════════════════════════════════════════════════════════════════════

def _fixed_datetime(year, month, day, hour=11, minute=0):
    fixed = datetime(year, month, day, hour, minute, tzinfo=gw.IST)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return _DT, fixed


@pytest.fixture
def session_env(monkeypatch):
    env = SimpleNamespace(phase="open", holidays=set())

    def setup(year, month, day, phase, holidays=()):
        dt_cls, fixed = _fixed_datetime(year, month, day)
        monkeypatch.setattr(gw, "datetime", dt_cls)
        monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: phase)
        monkeypatch.setattr(gw, "is_nse_holiday", lambda d: d in set(holidays))
        return fixed

    env.setup = setup
    return env


# 2026-09-30 is a Wednesday, 2026-10-03 a Saturday.
class TestMarketSession:
    def test_open_weekday(self, session_env, client):
        fixed = session_env.setup(2026, 9, 30, "open")
        body = client.get("/market/session").json()
        assert body == {
            "phase": "open",
            "is_open": True,
            "is_market_day": True,
            "is_holiday": False,
            "now_ist": fixed.isoformat(),
            "session_window": "09:15–15:30 IST Mon–Fri (ex holidays)",
            "quote_polling": True,
        }

    @pytest.mark.parametrize("phase,polling", [
        ("preopen", True), ("open", True), ("post", True),
        ("closed", False), ("holiday", False),
    ])
    def test_quote_polling_only_in_the_live_phases(self, session_env, phase, polling):
        session_env.setup(2026, 9, 30, phase)
        assert gw.market_session()["quote_polling"] is polling

    def test_is_open_only_for_the_open_phase(self, session_env):
        session_env.setup(2026, 9, 30, "post")
        assert gw.market_session()["is_open"] is False

    def test_closed_phase_on_a_normal_weekday_is_still_a_market_day(self, session_env):
        session_env.setup(2026, 9, 30, "closed")          # after hours on a Wednesday
        out = gw.market_session()
        assert out["is_market_day"] is True and out["is_holiday"] is False

    def test_closed_phase_on_a_weekend_is_not_a_market_day(self, session_env):
        session_env.setup(2026, 10, 3, "closed")          # Saturday
        assert gw.market_session()["is_market_day"] is False

    def test_closed_phase_on_a_weekday_holiday_is_not_a_market_day(self, session_env):
        session_env.setup(2026, 9, 30, "closed", holidays=[datetime(2026, 9, 30).date()])
        assert gw.market_session()["is_market_day"] is False

    def test_holiday_phase_on_a_holiday(self, session_env):
        session_env.setup(2026, 9, 30, "holiday", holidays=[datetime(2026, 9, 30).date()])
        out = gw.market_session()
        assert out["is_holiday"] is True and out["is_market_day"] is False
        assert out["is_open"] is False and out["quote_polling"] is False

    def test_holiday_phase_contradicting_the_calendar_is_not_a_market_day(self, session_env):
        # FIXED: the phase helper is authoritative for "holiday", so the flags agree.
        session_env.setup(2026, 9, 30, "holiday", holidays=[])
        out = gw.market_session()
        assert out["is_holiday"] is True and out["is_market_day"] is False

    def test_route_is_registered(self, session_env, client):
        session_env.setup(2026, 9, 30, "open")
        r = client.get("/market/session")
        assert r.status_code == 200 and r.json()["phase"] == "open"


# ═════════════════════════════════════════════════════════════════════════════
# /market/top-gainers, /top-losers, /most-active
# ═════════════════════════════════════════════════════════════════════════════

def _nifty_rows(n=12):
    return [{"symbol": f"S{i:02d}", "change_pct": float(i - 6), "volume": 1000 * (i + 1)} for i in range(n)]


class TestNiftyMovers:
    @pytest.fixture(autouse=True)
    def _data(self, monkeypatch):
        self.rows = _nifty_rows()
        monkeypatch.setattr(gw, "_get_nifty50_data", lambda: list(self.rows))

    def test_top_gainers_are_sorted_desc_and_capped_at_ten(self):
        out = gw.market_top_gainers()
        assert out["count"] == 10
        pcts = [r["change_pct"] for r in out["data"]]
        assert pcts == sorted(pcts, reverse=True) and pcts[0] == 5.0 and pcts[-1] == -4.0

    def test_top_losers_are_sorted_asc_and_capped_at_ten(self):
        out = gw.market_top_losers()
        assert out["count"] == 10
        pcts = [r["change_pct"] for r in out["data"]]
        assert pcts == sorted(pcts) and pcts[0] == -6.0 and pcts[-1] == 3.0

    def test_most_active_is_sorted_by_volume_desc(self):
        out = gw.market_most_active()
        assert out["count"] == 10
        vols = [r["volume"] for r in out["data"]]
        assert vols == sorted(vols, reverse=True) and vols[0] == 12000

    def test_fewer_than_ten_rows_are_all_returned(self):
        self.rows[:] = _nifty_rows(3)
        for fn in (gw.market_top_gainers, gw.market_top_losers, gw.market_most_active):
            assert fn()["count"] == 3

    def test_empty_data(self):
        self.rows[:] = []
        for fn in (gw.market_top_gainers, gw.market_top_losers, gw.market_most_active):
            assert fn() == {"data": [], "count": 0}

    def test_routes_are_registered(self, client):
        assert client.get("/market/top-gainers").json()["count"] == 10
        assert client.get("/market/top-losers").json()["count"] == 10
        assert client.get("/market/most-active").json()["count"] == 10

    def test_a_row_missing_the_sort_key_is_skipped(self, client):
        # FIXED: a malformed row is skipped instead of taking the endpoint down.
        self.rows.append({"symbol": "BAD"})
        for path in ("/market/top-gainers", "/market/top-losers", "/market/most-active"):
            r = client.get(path)
            assert r.status_code == 200
            assert "BAD" not in [x["symbol"] for x in r.json()["data"]]


# ═════════════════════════════════════════════════════════════════════════════
# GET /market/trending
# ═════════════════════════════════════════════════════════════════════════════

class FakeGetResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code = status
        self._data = data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._data


class TrendEnv:
    def __init__(self):
        self.movers = []
        self.news = []
        self.quotes = {}            # symbol -> FakeGetResp | Exception
        self.quote_timeouts = []
        self.tickers = {}           # resolved yf ticker -> DataFrame | Exception
        self.resolve = lambda sym: sym + ".NS"
        self.resolved = []
        self.history_calls = []
        self.bulk_posts = []


@pytest.fixture
def tenv(monkeypatch):
    env = TrendEnv()
    monkeypatch.setattr(gw, "_get_momentum_movers", lambda: list(env.movers)
                        if not isinstance(env.movers, Exception) else (_ for _ in ()).throw(env.movers))
    monkeypatch.setattr(gw, "_get_news_mentioned_symbols", lambda: list(env.news))

    def http_get(url, timeout=None, **kw):
        env.quote_timeouts.append(timeout)
        sym = url.rsplit("/", 1)[1]
        out = env.quotes.get(sym)
        if out is None:
            raise httpx.ConnectError("no quote for " + sym)
        if isinstance(out, Exception):
            raise out
        return out

    def resolve(sym):
        env.resolved.append(sym)
        return env.resolve(sym)

    class FakeTicker:
        def __init__(self, name):
            self.name = name

        def history(self, period=None):
            env.history_calls.append((self.name, period))
            out = env.tickers[self.name]
            if isinstance(out, Exception):
                raise out
            return out

    def http_post(url, json=None, timeout=None, **kw):
        """group233: /market/trending prices its (<=10) symbols with ONE POST /quotes/bulk. The per-symbol
        `env.quotes` entries are turned into bulk rows: a 200 dict body is a row, anything else leaves it out."""
        env.quote_timeouts.append(timeout)
        env.bulk_posts.append((url, list(json["symbols"])))
        rows = []
        for sym in json["symbols"]:
            out = env.quotes.get(sym)
            if out is None or isinstance(out, Exception) or out.status_code != 200:
                continue
            try:
                body = out.json()
            except Exception:
                continue
            if isinstance(body, dict):
                rows.append(dict(body, symbol=sym))
        return FakeGetResp(200, {"quotes": rows})

    monkeypatch.setattr(gw.httpx, "get", http_get)
    monkeypatch.setattr(gw.httpx, "post", http_post)
    monkeypatch.setattr(gw, "resolve_ns_ticker", resolve)
    monkeypatch.setattr(gw.yf, "Ticker", FakeTicker)
    return env


def _ohlc(open_, close):
    return pd.DataFrame({"Open": [open_], "Close": [close]})


class TestMarketTrendingQuotes:
    def test_quote_with_previous_close_gives_change_and_percent(self, tenv):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"price": 110.0, "previous_close": 100.0})
        out = _run(gw.market_trending())
        assert out == {"data": [{"symbol": "AAA", "price": 110.0, "change": 10.0, "change_pct": 10.0}],
                       "count": 1}
        assert tenv.quote_timeouts == [8]                       # group233: one bulk POST, not one GET per symbol
        assert tenv.bulk_posts == [(f"{gw.MARKET_DATA_URL.rstrip('/')}/quotes/bulk", ["AAA"])]
        assert tenv.resolved == [] and tenv.history_calls == []

    def test_cmp_is_accepted_when_price_is_missing(self, tenv):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"cmp": 50.5, "previous_close": 50.0})
        row = _run(gw.market_trending())["data"][0]
        assert row["price"] == 50.5 and row["change"] == 0.5 and row["change_pct"] == 1.0

    def test_day_change_pct_is_used_when_there_is_no_previous_close(self, tenv):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"price": 200.0, "day_change_pct": 2.5})
        row = _run(gw.market_trending())["data"][0]
        assert row["change_pct"] == 2.5 and row["change"] == 5.0

    def test_zero_previous_close_falls_back_to_day_change_pct(self, tenv):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"price": 200.0, "previous_close": 0, "day_change_pct": -1.0})
        row = _run(gw.market_trending())["data"][0]
        assert row["change_pct"] == -1.0 and row["change"] == -2.0

    def test_price_only_quote_has_no_change_fields(self, tenv):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"price": 75.0})
        row = _run(gw.market_trending())["data"][0]
        assert row == {"symbol": "AAA", "price": 75.0, "change": None, "change_pct": None}


class TestMarketTrendingYfinanceFallback:
    @pytest.mark.parametrize("quote", [
        FakeGetResp(503, {"price": 10.0}),                       # non-200
        FakeGetResp(200, {"price": 0, "previous_close": 5}),     # non-positive price
        FakeGetResp(200, {}),                                    # empty payload
        FakeGetResp(200, None),                                  # `resp.json() or {}` guard
        FakeGetResp(200, json_raises=True),                      # bad json
        httpx.ReadTimeout("slow"),                               # transport failure
        None,                                                    # no route -> ConnectError
    ])
    def test_unusable_quote_falls_back_to_yfinance(self, tenv, quote):
        tenv.movers = ["AAA"]
        if quote is not None:
            tenv.quotes["AAA"] = quote
        tenv.tickers["AAA.NS"] = _ohlc(100.0, 103.0)
        out = _run(gw.market_trending())
        assert out["data"] == [{"symbol": "AAA", "price": 103.0, "change": 3.0, "change_pct": 3.0}]
        assert tenv.history_calls == [("AAA.NS", "1d")]

    def test_unresolvable_symbol_is_skipped(self, tenv):
        tenv.movers = ["AAA", "BBB"]
        tenv.resolve = lambda sym: None if sym == "AAA" else sym + ".NS"
        tenv.tickers["BBB.NS"] = _ohlc(10.0, 11.0)
        out = _run(gw.market_trending())
        assert [r["symbol"] for r in out["data"]] == ["BBB"] and out["count"] == 1

    def test_empty_history_is_skipped(self, tenv):
        tenv.movers = ["AAA"]
        tenv.tickers["AAA.NS"] = pd.DataFrame({"Open": [], "Close": []})
        assert _run(gw.market_trending()) == {"data": [], "count": 0}

    def test_yfinance_failure_skips_only_that_symbol(self, tenv):
        tenv.movers = ["AAA", "BBB"]
        tenv.tickers["AAA.NS"] = RuntimeError("yahoo down")
        tenv.tickers["BBB.NS"] = _ohlc(20.0, 22.0)
        out = _run(gw.market_trending())
        assert [r["symbol"] for r in out["data"]] == ["BBB"]

    def test_zero_open_price_gives_a_null_change_pct(self, tenv, client):
        # FIXED: a zero Open no longer produces inf (which FastAPI cannot JSON-encode).
        tenv.movers = ["AAA"]
        tenv.tickers["AAA.NS"] = _ohlc(0.0, 5.0)
        out = _run(gw.market_trending())
        assert out["count"] == 1 and out["data"][0]["change_pct"] is None
        assert client.get("/market/trending").status_code == 200


class TestMarketTrendingSelection:
    def test_movers_and_news_are_deduplicated(self, tenv):
        tenv.movers = ["AAA", "BBB"]
        tenv.news = ["BBB", "CCC"]
        for s in ("AAA", "BBB", "CCC"):
            tenv.quotes[s] = FakeGetResp(200, {"price": 10.0})
        out = _run(gw.market_trending())
        assert sorted(r["symbol"] for r in out["data"]) == ["AAA", "BBB", "CCC"]

    def test_result_is_capped_at_ten_symbols(self, tenv):
        tenv.movers = [f"M{i:02d}" for i in range(8)]
        tenv.news = [f"N{i:02d}" for i in range(8)]
        for s in tenv.movers + tenv.news:
            tenv.quotes[s] = FakeGetResp(200, {"price": 10.0})
        out = _run(gw.market_trending())
        assert out["count"] == 10 and len(out["data"]) == 10

    def test_no_candidates_returns_an_empty_list(self, tenv):
        assert _run(gw.market_trending()) == {"data": [], "count": 0}

    def test_route_is_registered(self, tenv, client):
        tenv.movers = ["AAA"]
        tenv.quotes["AAA"] = FakeGetResp(200, {"price": 10.0})
        assert client.get("/market/trending").json()["count"] == 1


class TestMarketTrendingDegrade:
    def test_helper_failure_degrades_to_empty(self, tenv, caplog):
        tenv.movers = RuntimeError("movers exploded")
        with caplog.at_level("WARNING", logger=gw.logger.name):
            out = _run(gw.market_trending())
        assert out == {"data": [], "count": 0}
        assert "market/trending failed: movers exploded" in caplog.text

    def test_twenty_second_budget_returns_empty(self, tenv, monkeypatch, caplog):
        seen = {}

        async def fake_wait_for(aw, timeout):
            seen["timeout"] = timeout
            aw.cancel()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(gw, "asyncio", _AsyncioProxy(wait_for=fake_wait_for))
        with caplog.at_level("WARNING", logger=gw.logger.name):
            out = _run(gw.market_trending())
        assert seen["timeout"] == 20.0
        assert out == {"data": [], "count": 0}
        assert "market/trending hit 20 s budget" in caplog.text


# ═════════════════════════════════════════════════════════════════════════════
# GET /market/indices
# ═════════════════════════════════════════════════════════════════════════════

NO_CACHE = {
    "cache-control": "no-cache, no-store, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


def _frame(open_, close):
    return pd.DataFrame({"Open": open_, "Close": close})


class IndexEnv:
    def __init__(self):
        self.frames = {}           # "^NSEI" / "^BSESN" -> DataFrame | Exception
        self.calls = []


@pytest.fixture
def ienv(monkeypatch, kv):
    env = IndexEnv()

    class FakeTicker:
        def __init__(self, name):
            self.name = name
            env.calls.append(name)

        def history(self, period=None):
            out = env.frames[self.name]
            if isinstance(out, Exception):
                raise out
            return out

    monkeypatch.setattr(gw.yf, "Ticker", FakeTicker)
    return env


def _set_move(env, nifty_pct, sensex_pct, base=20000.0):
    """Two-row frames: previous close = Close[0] = base, last close = base * (1 + pct)."""
    env.frames["^NSEI"] = _frame([base, base], [base, base * (1 + nifty_pct / 100)])
    env.frames["^BSESN"] = _frame([base, base], [base, base * (1 + sensex_pct / 100)])


class TestIndicesCache:
    def test_cache_hit_is_served_with_a_fresh_timestamp_and_no_yfinance(self, ienv, kv, client):
        kv.store[gw.INDICES_CACHE_KEY] = {"nifty": {"price": 1}, "fetched_at": "old"}
        r = client.get("/market/indices")
        assert r.status_code == 200
        body = r.json()
        assert body["nifty"] == {"price": 1} and body["fetched_at"] != "old"
        for k, v in NO_CACHE.items():
            assert r.headers[k] == v
        assert ienv.calls == [] and kv.sets == []

    def test_force_refresh_bypasses_the_cache(self, ienv, kv):
        kv.store[gw.INDICES_CACHE_KEY] = {"nifty": {"price": 1}}
        _set_move(ienv, 0.0, 0.0)
        body = json.loads(gw.get_market_indices(force_refresh=True).body)
        assert body["nifty"]["price"] == 20000.0 and ienv.calls == ["^NSEI", "^BSESN"]

    @pytest.mark.parametrize("cached", ["", [], "text", {}, None])
    def test_unusable_cache_values_fall_through_to_a_live_fetch(self, ienv, kv, cached):
        if cached is not None:
            kv.store[gw.INDICES_CACHE_KEY] = cached
        _set_move(ienv, 0.0, 0.0)
        body = json.loads(gw.get_market_indices().body)
        assert body["market_mood"] == "NEUTRAL" and ienv.calls == ["^NSEI", "^BSESN"]


class TestIndicesLive:
    def test_success_shape_caches_and_headers(self, ienv, kv):
        _set_move(ienv, 0.6, 0.4)
        resp = gw.get_market_indices()
        body = json.loads(resp.body)
        assert body["nifty"] == {"price": 20120.0, "change": 120.0, "change_pct": 0.6}
        assert body["sensex"] == {"price": 20080.0, "change": 80.0, "change_pct": 0.4}
        assert body["market_score"] == 67 and body["market_mood"] == "BULLISH"
        assert body["fetched_at"].endswith(("AM", "PM"))
        assert "stale" not in body and "fallback" not in body
        for k, v in NO_CACHE.items():
            assert resp.headers[k] == v
        assert (gw.INDICES_CACHE_KEY, body, 300) in kv.sets
        assert (gw.INDICES_LAST_KNOWN, body, 86400) in kv.sets

    @pytest.mark.parametrize("nifty,sensex,score,mood", [
        (1.5, 1.5, 100, "BULLISH"),
        (3.0, 3.0, 100, "BULLISH"),       # clamped at 100
        (0.6, 0.6, 70, "BULLISH"),
        (0.15, 0.15, 55, "NEUTRAL"),
        (0.0, 0.0, 50, "NEUTRAL"),
        (-0.15, -0.15, 45, "NEUTRAL"),
        (-0.6, -0.6, 30, "BEARISH"),
        (-1.5, -1.5, 0, "BEARISH"),
        (-4.0, -4.0, 0, "BEARISH"),       # clamped at 0
    ])
    def test_mood_bands_and_clamping(self, ienv, nifty, sensex, score, mood):
        _set_move(ienv, nifty, sensex)
        body = json.loads(gw.get_market_indices().body)
        assert body["market_score"] == score and body["market_mood"] == mood

    def test_score_averages_the_two_indices(self, ienv):
        _set_move(ienv, 1.5, -1.5)
        assert json.loads(gw.get_market_indices().body)["market_score"] == 50

    def test_single_row_frames_use_the_open_as_previous_close(self, ienv):
        ienv.frames["^NSEI"] = _frame([100.0], [101.0])
        ienv.frames["^BSESN"] = _frame([200.0], [198.0])
        body = json.loads(gw.get_market_indices().body)
        assert body["nifty"] == {"price": 101.0, "change": 1.0, "change_pct": 1.0}
        assert body["sensex"] == {"price": 198.0, "change": -2.0, "change_pct": -1.0}

    def test_route_is_registered(self, ienv, client):
        _set_move(ienv, 0.0, 0.0)
        r = client.get("/market/indices", params={"force_refresh": "true"})
        assert r.status_code == 200 and r.json()["market_mood"] == "NEUTRAL"


class TestIndicesDegrade:
    @pytest.mark.parametrize("empty", ["^NSEI", "^BSESN"])
    def test_empty_history_degrades_instead_of_returning_503(self, ienv, kv, empty):
        # By design (relabelled from NOT FIXED): the 503 raised for empty index data is caught by the
        # function's own `except Exception`, so callers get the fallback payload with HTTP 200 and the
        # `fallback`/`stale` flags say it is degraded. A banner keeps working instead of erroring.
        _set_move(ienv, 0.0, 0.0)
        ienv.frames[empty] = pd.DataFrame({"Open": [], "Close": []})
        resp = gw.get_market_indices()
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["fallback"] is True and body["stale"] is True

    def test_failure_serves_last_known_marked_stale(self, ienv, kv):
        last = {"nifty": {"price": 1}, "market_mood": "BULLISH", "market_score": 80, "fetched_at": "old"}
        kv.store[gw.INDICES_LAST_KNOWN] = dict(last)
        ienv.frames["^NSEI"] = RuntimeError("yahoo down")
        resp = gw.get_market_indices()
        body = json.loads(resp.body)
        assert body["stale"] is True and body["fetched_at"] != "old"
        assert body["nifty"] == {"price": 1} and "fallback" not in body
        assert (gw.INDICES_CACHE_KEY, body, 60) in kv.sets
        for k, v in NO_CACHE.items():
            assert resp.headers[k] == v
        # the 24 h last-known copy is not re-written on this path
        assert all(s[0] != gw.INDICES_LAST_KNOWN for s in kv.sets)

    @pytest.mark.parametrize("bad_last", [None, {}, "junk", []])
    def test_failure_without_a_usable_last_known_serves_the_zero_fallback(self, ienv, kv, bad_last):
        if bad_last is not None:
            kv.store[gw.INDICES_LAST_KNOWN] = bad_last
        ienv.frames["^NSEI"] = RuntimeError("yahoo down")
        resp = gw.get_market_indices()
        body = json.loads(resp.body)
        assert body["nifty"] == {"price": 0, "change": 0, "change_pct": 0}
        assert body["sensex"] == {"price": 0, "change": 0, "change_pct": 0}
        assert body["market_mood"] == "NEUTRAL" and body["market_score"] == 50
        assert body["stale"] is True and body["fallback"] is True
        assert (gw.INDICES_CACHE_KEY, body, 60) in kv.sets
        # FIXED: the zero fallback is short-lived only; it never reaches the 24 h last-known key.
        assert all(k != gw.INDICES_LAST_KNOWN for k, _, _ in kv.sets)

    def test_second_index_failure_also_degrades(self, ienv, kv):
        ienv.frames["^NSEI"] = _frame([1.0], [1.0])
        ienv.frames["^BSESN"] = RuntimeError("sensex down")
        assert json.loads(gw.get_market_indices().body)["fallback"] is True

    def test_fallback_does_not_write_the_last_known_key(self, ienv, kv):
        # FIXED: the zero fallback used to be written to the 24 h key, so the next outage served zeros.
        ienv.frames["^NSEI"] = RuntimeError("down")
        gw.get_market_indices()
        assert gw.INDICES_LAST_KNOWN not in kv.store

    def test_zero_previous_close_yields_a_zero_pct_and_clean_caches(self, ienv, kv, client):
        # FIXED: a zero previous close gives change_pct 0.0 (via _safe_pct) instead of inf, so nothing
        # poisoned is cached and the route answers 200.
        ienv.frames["^NSEI"] = _frame([0.0, 0.0], [0.0, 5.0])
        ienv.frames["^BSESN"] = _frame([0.0, 0.0], [0.0, 5.0])
        r = client.get("/market/indices", params={"force_refresh": "true"})
        assert r.status_code == 200
        assert r.json()["nifty"]["change_pct"] == 0.0
        assert kv.store[gw.INDICES_LAST_KNOWN]["nifty"]["change_pct"] == 0.0
        assert kv.store[gw.INDICES_CACHE_KEY]["nifty"]["change_pct"] == 0.0
        assert client.get("/market/indices").status_code == 200

    def test_a_non_finite_cache_entry_is_ignored_and_rebuilt(self, ienv, kv, client):
        kv.store[gw.INDICES_CACHE_KEY] = {"nifty": {"change_pct": float("inf")}, "sensex": {}}
        ienv.frames["^NSEI"] = _frame([100.0, 110.0], [100.0, 110.0])
        ienv.frames["^BSESN"] = _frame([100.0, 110.0], [100.0, 110.0])
        assert client.get("/market/indices").status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# _movers_with_deadline
# ═════════════════════════════════════════════════════════════════════════════

class TestMoversWithDeadline:
    def test_ready_in_time(self, monkeypatch):
        monkeypatch.setattr(gw, "_get_momentum_movers", lambda: ["AAA", "BBB"])
        assert _run(gw._movers_with_deadline()) == (["AAA", "BBB"], False)

    def test_helper_failure_returns_empty_partial(self, monkeypatch, caplog):
        def boom():
            raise RuntimeError("nse down")

        monkeypatch.setattr(gw, "_get_momentum_movers", boom)
        with caplog.at_level("WARNING", logger=gw.logger.name):
            assert _run(gw._movers_with_deadline()) == ([], True)
        assert "momentum movers failed: nse down" in caplog.text

    def test_deadline_returns_partial_and_the_task_keeps_running(self, monkeypatch, caplog):
        release = threading.Event()
        finished = []

        def slow():
            release.wait(5)
            finished.append(True)
            return ["LATE"]

        monkeypatch.setattr(gw, "_get_momentum_movers", slow)
        monkeypatch.setattr(gw, "SCAN_UNIVERSE_MOVERS_DEADLINE_S", 0.05)

        async def go():
            with caplog.at_level("WARNING", logger=gw.logger.name):
                out = await gw._movers_with_deadline()
            release.set()
            await asyncio.sleep(0.2)      # let the background thread + done-callback finish
            return out

        assert _run(go()) == ([], True)
        assert finished == [True]
        assert "momentum movers not ready within" in caplog.text

    def test_background_failure_after_the_deadline_is_consumed_quietly(self, monkeypatch):
        release = threading.Event()

        def slow_boom():
            release.wait(5)
            raise RuntimeError("late failure")

        monkeypatch.setattr(gw, "_get_momentum_movers", slow_boom)
        monkeypatch.setattr(gw, "SCAN_UNIVERSE_MOVERS_DEADLINE_S", 0.05)
        loop_errors = []

        async def go():
            asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
            out = await gw._movers_with_deadline()
            release.set()
            await asyncio.sleep(0.2)
            return out

        assert _run(go()) == ([], True)
        assert loop_errors == []          # the done-callback retrieved the exception


# ═════════════════════════════════════════════════════════════════════════════
# GET /scan/universe
# ═════════════════════════════════════════════════════════════════════════════

def _syms(n, prefix="S"):
    return [f"{prefix}{i:03d}" for i in range(n)]


class FakeStaleCache:
    def __init__(self, stale=None, raises=False):
        self.stale = stale
        self.raises = raises
        self.calls = []

    def get_stale(self, key):
        self.calls.append(key)
        if self.raises:
            raise RuntimeError("neon down")
        return self.stale


class UEnv:
    def __init__(self):
        self.build_result = _syms(60, "B")
        self.build_delay = 0.0
        self.build_calls = 0
        self.build_raises = None
        self.movers = (["MOVER"], False)
        self.movers_calls = 0
        self.searched = []
        self.searched_raises = False
        self.filter_equities = lambda s: list(s)
        self.filter_price = lambda s: list(s)


@pytest.fixture
def uenv(monkeypatch, kv):
    env = UEnv()

    def build():
        env.build_calls += 1
        if env.build_delay:
            time.sleep(env.build_delay)
        if env.build_raises:
            raise env.build_raises
        return list(env.build_result)

    async def movers():
        env.movers_calls += 1
        return env.movers

    def load_searched():
        if env.searched_raises:
            raise RuntimeError("searched down")
        return list(env.searched)

    monkeypatch.setattr(gw, "_build_scan_universe", build)
    monkeypatch.setattr(gw, "_movers_with_deadline", movers)
    monkeypatch.setattr(gw, "_load_searched", load_searched)
    monkeypatch.setattr(gw, "_filter_equities", lambda s: env.filter_equities(s))
    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", lambda s: env.filter_price(s))
    monkeypatch.setattr(gw, "_kv_cache", None)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_BUILD_DEADLINE_S", 5.0)
    return env


def _universe(**kw):
    async def go():
        out = await gw.get_scan_universe(**kw)
        await asyncio.sleep(0.05)       # let a fire-and-forget background rebuild task run
        return out
    return _run(go())


class TestScanUniverseColdBuild:
    def test_build_within_the_deadline(self, uenv):
        uenv.searched = ["B001", "NOPE"]
        out = _universe()
        assert out == {
            "total": 60,
            "symbols": uenv.build_result,
            "searched_symbols_included": ["B001"],
            "momentum_movers": ["MOVER"],
            "momentum_movers_partial": False,
            "max_price": 5000.0,
        }
        assert uenv.build_calls == 1

    def test_partial_movers_are_flagged(self, uenv):
        uenv.movers = ([], True)
        out = _universe()
        assert out["momentum_movers"] == [] and out["momentum_movers_partial"] is True

    def test_cached_false_ignores_a_warm_cache(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(80, "C")
        out = _universe(cached=False)
        assert out["symbols"] == uenv.build_result and "cached" not in out

    def test_searched_lookup_failure_is_degraded_to_empty(self, uenv):
        # FIXED: a failing searched-list read no longer fails an endpoint whose universe built fine.
        uenv.searched_raises = True
        _universe()

    def test_route_is_registered(self, uenv, client):
        r = client.get("/scan/universe")
        assert r.status_code == 200 and r.json()["total"] == 60


class TestScanUniverseCachedWarm:
    def test_warm_live_cache_is_served_filtered(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(70, "W")
        uenv.searched = ["W001", "ZZZ"]
        seen = {}
        uenv.filter_equities = lambda s: seen.setdefault("eq", list(s)) and [x for x in s if x != "W000"]
        out = _universe(cached=True)
        assert out["cached"] is True and "stale" not in out
        assert out["total"] == 69 and "W000" not in out["symbols"]
        assert out["searched_symbols_included"] == ["W001"]
        assert out["momentum_movers"] == ["MOVER"] and out["momentum_movers_partial"] is False
        assert out["max_price"] == 5000.0
        assert uenv.build_calls == 0

    def test_both_filters_are_applied_in_order(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(70, "W")
        order = []
        uenv.filter_equities = lambda s: (order.append("equities"), list(s))[1]
        uenv.filter_price = lambda s: (order.append("price"), list(s))[1]
        _universe(cached=True)
        assert order == ["equities", "price"]

    def test_partial_movers_flag_is_passed_through(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(70, "W")
        uenv.movers = ([], True)
        assert _universe(cached=True)["momentum_movers_partial"] is True

    def test_warm_cache_over_the_price_filter_below_50_falls_through_to_a_build(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(70, "W")
        uenv.filter_price = lambda s: list(s)[:10]
        out = _universe(cached=True)
        assert out["symbols"] == uenv.build_result and uenv.build_calls == 1

    @pytest.mark.parametrize("live", [None, [], "junk", {"a": 1}])
    def test_unusable_live_value_falls_through(self, uenv, kv, live):
        if live is not None:
            kv.store[gw.SCAN_UNIVERSE_KEY] = live
        out = _universe(cached=True)
        assert out["symbols"] == uenv.build_result and uenv.build_calls == 1

    def test_route_passes_the_cached_flag(self, uenv, kv, client):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(70, "W")
        body = client.get("/scan/universe", params={"cached": "true"}).json()
        assert body["cached"] is True and uenv.build_calls == 0


class TestScanUniverseCachedStale:
    def test_stale_fallback_is_served_and_the_live_key_repopulated(self, uenv, kv, monkeypatch):
        stale = _syms(65, "T")
        cache = FakeStaleCache(stale)
        monkeypatch.setattr(gw, "_kv_cache", cache)
        uenv.searched = ["T001"]
        out = _universe(cached=True)
        assert cache.calls == [gw.SCAN_UNIVERSE_STALE_KEY]
        assert out["cached"] is True and out["stale"] is True and out["total"] == 65
        assert out["searched_symbols_included"] == ["T001"]
        assert (gw.SCAN_UNIVERSE_KEY, stale, 300) in kv.sets
        assert uenv.build_calls == 1                      # the background rebuild was scheduled and ran

    def test_filters_apply_to_the_stale_copy(self, uenv, kv, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(_syms(65, "T")))
        uenv.filter_equities = lambda s: [x for x in s if x != "T000"]
        out = _universe(cached=True)
        assert out["total"] == 64 and "T000" not in out["symbols"]
        assert kv.sets[0][1] == out["symbols"]            # the repopulated live key holds the filtered list

    def test_failure_to_schedule_the_rebuild_is_swallowed(self, uenv, kv, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(_syms(65, "T")))

        def boom(coro):
            coro.close()
            raise RuntimeError("no loop")

        monkeypatch.setattr(gw, "asyncio", _AsyncioProxy(create_task=boom))
        out = _universe(cached=True)
        assert out["stale"] is True and uenv.build_calls == 0

    @pytest.mark.parametrize("stale", [None, [], "junk", _syms(49, "T")])
    def test_unusable_or_short_stale_copy_falls_through_to_a_build(self, uenv, kv, monkeypatch, stale):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(stale))
        out = _universe(cached=True)
        assert out["symbols"] == uenv.build_result and "stale" not in out
        assert uenv.build_calls == 1

    def test_stale_copy_below_50_after_filtering_falls_through_to_a_build(self, uenv, kv, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(_syms(65, "T")))
        uenv.filter_price = lambda s: list(s)[:20]
        out = _universe(cached=True)
        assert out["symbols"] == uenv.build_result and "stale" not in out
        assert all(s[0] != gw.SCAN_UNIVERSE_KEY for s in kv.sets)       # live key was NOT repopulated

    def test_stale_read_failure_falls_through_to_a_build(self, uenv, kv, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(raises=True))
        out = _universe(cached=True)
        assert out["symbols"] == uenv.build_result and uenv.build_calls == 1

    def test_no_stale_cache_at_all_falls_through_to_a_build(self, uenv, kv):
        out = _universe(cached=True)          # `_kv_cache` is None here
        assert out["symbols"] == uenv.build_result and "cached" not in out


class TestScanUniverseDeadline:
    @pytest.fixture(autouse=True)
    def _slow_build(self, uenv, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_UNIVERSE_BUILD_DEADLINE_S", 0.05)
        uenv.build_delay = 0.25

    def test_redis_universe_is_served_when_the_build_is_too_slow(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")
        out = _run(gw.get_scan_universe())
        assert out["deadline_exceeded"] is True and out["stale"] is True and out["cached"] is True
        assert out["total"] == 60 and out["symbols"][0] == "R000"
        assert out["searched_symbols_included"] == []
        assert out["momentum_movers"] == ["MOVER"] and out["max_price"] == 5000.0

    def test_fallback_is_filtered(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")
        uenv.filter_equities = lambda s: [x for x in s if x != "R000"]
        out = _run(gw.get_scan_universe())
        assert out["total"] == 59 and out["deadline_exceeded"] is True

    def test_neon_stale_copy_is_used_when_redis_is_short(self, uenv, kv, monkeypatch):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(10, "R")            # < 50 -> not good enough
        cache = FakeStaleCache(_syms(70, "N"))
        monkeypatch.setattr(gw, "_kv_cache", cache)
        out = _run(gw.get_scan_universe())
        assert cache.calls == [gw.SCAN_UNIVERSE_STALE_KEY]
        assert out["deadline_exceeded"] is True and out["symbols"][0] == "N000"

    def test_redis_hit_skips_the_neon_lookup(self, uenv, kv, monkeypatch):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")
        cache = FakeStaleCache(_syms(70, "N"))
        monkeypatch.setattr(gw, "_kv_cache", cache)
        _run(gw.get_scan_universe())
        assert cache.calls == []

    def test_no_cached_universe_waits_for_the_build(self, uenv, kv, caplog):
        with caplog.at_level("WARNING", logger=gw.logger.name):
            out = _run(gw.get_scan_universe())
        assert out["total"] == 60 and "deadline_exceeded" not in out and "cached" not in out
        assert out["symbols"] == uenv.build_result
        assert "no cached universe exists — waiting for the build" in caplog.text
        assert uenv.build_calls == 1

    def test_stale_copy_below_50_also_waits_for_the_build(self, uenv, kv, monkeypatch):
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(_syms(10, "N")))
        out = _run(gw.get_scan_universe())
        assert "deadline_exceeded" not in out and out["symbols"] == uenv.build_result

    def test_fallback_lookup_failure_waits_for_the_build(self, uenv, kv, monkeypatch):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(10, "R")
        monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(raises=True))
        out = _run(gw.get_scan_universe())
        assert "deadline_exceeded" not in out and out["symbols"] == uenv.build_result

    def test_fallback_below_50_after_filtering_is_not_served(self, uenv, kv):
        # FIXED: the >= 50 gate now holds AFTER the price/equity filters, so a shrunken fallback
        # is not served as a deadline response.
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")
        uenv.filter_price = lambda s: list(s)[:5]
        out = _run(gw.get_scan_universe())
        assert not out.get("deadline_exceeded")

    def test_fallback_filter_failure_is_not_served(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")

        def boom(s):
            raise RuntimeError("filter down")

        uenv.filter_price = boom
        out = _run(gw.get_scan_universe())
        assert not out.get("deadline_exceeded")

    def test_build_failure_after_the_deadline_propagates(self, uenv, kv):
        uenv.build_raises = RuntimeError("build blew up")
        with pytest.raises(RuntimeError, match="build blew up"):
            _run(gw.get_scan_universe())

    def test_build_keeps_running_in_the_background_after_a_fallback_response(self, uenv, kv):
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "R")

        async def go():
            out = await gw.get_scan_universe()
            assert uenv.build_calls == 1
            await asyncio.sleep(0.4)        # the shielded build thread is not cancelled by the timeout
            return out

        assert _run(go())["deadline_exceeded"] is True


# ═════════════════════════════════════════════════════════════════════════════
# GET /universe and /api/universe
# ═════════════════════════════════════════════════════════════════════════════

class FakeAsyncClient:
    script = None               # dict: status/body/raises/urls

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None, **kw):
        s = FakeAsyncClient.script
        s["urls"].append((url, timeout))
        if s.get("raises"):
            raise s["raises"]
        return SimpleNamespace(status_code=s.get("status", 200), json=lambda: s.get("body"))


@pytest.fixture
def unv(monkeypatch):
    env = SimpleNamespace(
        neon=["NEON1", "NEON2"],
        neon_raises=None,
        neon_args=[],
        built=["BUILT1"],
        build_raises=None,
        build_calls=0,
        filtered_in=[],
    )

    def neon(max_price=None):
        env.neon_args.append(max_price)
        if env.neon_raises:
            raise env.neon_raises
        return env.neon

    def build():
        env.build_calls += 1
        if env.build_raises:
            raise env.build_raises
        return env.built

    def price_filter(symbols):
        env.filtered_in.append(list(symbols))
        return [s for s in symbols if s != "DROP"]

    FakeAsyncClient.script = {"urls": [], "status": 200, "body": None}
    monkeypatch.setattr(data_feed, "list_feed_symbols_from_neon_under_max_price", neon)
    monkeypatch.setattr(gw, "_build_scan_universe", build)
    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", price_filter)
    monkeypatch.setattr(gw.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
    monkeypatch.setenv("DECISION_URL", "http://dec.local/decision")
    return env


class TestUniverseNeonFeed:
    def test_neon_symbols_are_used_and_filtered(self, unv):
        unv.neon = ["NEON1", "DROP", "NEON2"]
        out = _run(gw.get_universe())
        assert out == ["NEON1", "NEON2"]
        assert unv.neon_args == [5000.0]
        assert unv.filtered_in == [["NEON1", "DROP", "NEON2"]]
        assert FakeAsyncClient.script["urls"] == [] and unv.build_calls == 0

    @pytest.mark.parametrize("path", ["/universe", "/api/universe"])
    def test_both_paths_serve_the_same_list(self, unv, client, path):
        assert client.get(path).json() == ["NEON1", "NEON2"]

    def test_none_from_neon_counts_as_empty(self, unv):
        unv.neon = None
        FakeAsyncClient.script["body"] = ["TRAIN1"]
        assert _run(gw.get_universe()) == ["TRAIN1"]

    def test_neon_failure_falls_back_to_the_training_universe(self, unv):
        unv.neon_raises = RuntimeError("neon down")
        FakeAsyncClient.script["body"] = ["TRAIN1"]
        assert _run(gw.get_universe()) == ["TRAIN1"]


class TestUniverseTrainingFallback:
    @pytest.fixture(autouse=True)
    def _neon_empty(self, unv):
        unv.neon = []

    def test_url_strips_the_decision_suffix(self, unv):
        FakeAsyncClient.script["body"] = ["TRAIN1"]
        _run(gw.get_universe())
        assert FakeAsyncClient.script["urls"] == [("http://dec.local/training/universe", 10.0)]

    def test_url_without_a_decision_suffix_is_kept_and_trailing_slash_trimmed(self, unv, monkeypatch):
        monkeypatch.setenv("DECISION_URL", "http://other.local/")
        FakeAsyncClient.script["body"] = ["TRAIN1"]
        _run(gw.get_universe())
        assert FakeAsyncClient.script["urls"][0][0] == "http://other.local/training/universe"

    def test_list_body_is_stringified(self, unv):
        FakeAsyncClient.script["body"] = ["A", 7]
        assert _run(gw.get_universe()) == ["A", "7"]

    def test_dict_body_uses_symbols_first(self, unv):
        FakeAsyncClient.script["body"] = {"symbols": ["SYM1"], "universe": ["UNI1"]}
        assert _run(gw.get_universe()) == ["SYM1"]

    def test_dict_body_falls_back_to_universe_key(self, unv):
        FakeAsyncClient.script["body"] = {"universe": ["UNI1"]}
        assert _run(gw.get_universe()) == ["UNI1"]

    def test_dict_body_without_either_key_falls_through_to_the_local_builder(self, unv):
        FakeAsyncClient.script["body"] = {"other": 1}
        assert _run(gw.get_universe()) == ["BUILT1"] and unv.build_calls == 1

    def test_unexpected_body_type_falls_through_to_the_local_builder(self, unv):
        FakeAsyncClient.script["body"] = "garbage"
        assert _run(gw.get_universe()) == ["BUILT1"]

    def test_non_200_falls_through_to_the_local_builder(self, unv):
        FakeAsyncClient.script.update(status=503, body=["TRAIN1"])
        assert _run(gw.get_universe()) == ["BUILT1"]

    def test_transport_failure_falls_through_to_the_local_builder(self, unv):
        FakeAsyncClient.script["raises"] = httpx.ConnectError("refused")
        assert _run(gw.get_universe()) == ["BUILT1"]

    def test_partial_results_from_a_failing_body_are_discarded(self, unv):
        # a dict body whose "symbols" is not iterable raises mid-parse; symbols resets to []
        FakeAsyncClient.script["body"] = {"symbols": 5}
        assert _run(gw.get_universe()) == ["BUILT1"]


class TestUniverseLocalBuilder:
    @pytest.fixture(autouse=True)
    def _nothing_upstream(self, unv):
        unv.neon = []
        FakeAsyncClient.script["body"] = []

    def test_builder_result_is_filtered(self, unv):
        unv.built = ["BUILT1", "DROP"]
        assert _run(gw.get_universe()) == ["BUILT1"]
        assert unv.filtered_in == [["BUILT1", "DROP"]]

    def test_builder_failure_returns_an_empty_list(self, unv):
        unv.build_raises = RuntimeError("build failed")
        assert _run(gw.get_universe()) == []
        assert unv.filtered_in == [[]]

    def test_price_gate_runs_exactly_once_on_the_final_list(self, unv):
        _run(gw.get_universe())
        assert len(unv.filtered_in) == 1


# ═════════════════════════════════════════════════════════════════════════════
# DELETE /scan/universe/cache
# ═════════════════════════════════════════════════════════════════════════════

class FakeRedis:
    def __init__(self, raises=False):
        self.deleted = []
        self.raises = raises

    def delete(self, key):
        if self.raises:
            raise RuntimeError("redis down")
        self.deleted.append(key)


class TestClearUniverseCache:
    MESSAGE = "Scan universe cache cleared — will rebuild on next scan"

    def test_redis_key_is_deleted(self, monkeypatch):
        r = FakeRedis()
        monkeypatch.setattr(gw, "_redis", r)
        out = gw.clear_universe_cache()
        assert out["message"] == self.MESSAGE and "redis" in out["cleared"]
        assert r.deleted == [gw.SCAN_UNIVERSE_KEY]

    def test_redis_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(gw, "_redis", FakeRedis(raises=True))
        out = gw.clear_universe_cache()
        assert out["message"] == self.MESSAGE and "redis" not in out["cleared"]
        assert out["errors"] == ["redis: redis down"]

    def test_without_redis_the_kv_and_memory_layers_are_still_cleared(self, monkeypatch, kv):
        # FIXED: with Redis off (the default) the kv / in-process layers are cleared and reported.
        monkeypatch.setattr(gw, "_redis", None)
        kv.store[gw.SCAN_UNIVERSE_KEY] = _syms(60, "C")
        out = gw.clear_universe_cache()
        assert out["message"] == self.MESSAGE and "memory" in out["cleared"]

    def test_route_is_registered(self, monkeypatch, client):
        r = FakeRedis()
        monkeypatch.setattr(gw, "_redis", r)
        resp = client.delete("/scan/universe/cache")
        assert resp.status_code == 200 and resp.json()["message"] == self.MESSAGE
        assert r.deleted == [gw.SCAN_UNIVERSE_KEY]


class TestPass82MarketHelpers:
    def test_safe_pct_guards(self):
        assert gw._safe_pct(5, 100) == 5.0
        assert gw._safe_pct("x", 100) == 0.0 and gw._safe_pct(5, None) == 0.0
        assert gw._safe_pct(5, 0) == 0.0 and gw._safe_pct(5, -3) == 0.0
        assert gw._safe_pct(float("inf"), 100) == 0.0 and gw._safe_pct(5, float("nan")) == 0.0
        assert gw._safe_pct(1e308, 1e-300) == 0.0          # finite inputs, overflowing result

    def test_json_finite_walks_nested_containers(self):
        assert gw._json_finite({"a": [1.0, (2.0, {"b": 3.0})], "c": "x"}) is True
        assert gw._json_finite({"a": [1.0, {"b": float("nan")}]}) is False
        assert gw._json_finite([float("inf")]) is False and gw._json_finite(1.5) is True

    def test_rank_market_rows_skips_malformed_rows(self):
        rows = [{"v": 1}, {"v": 3}, "junk", None, {"v": "x"}, {"v": float("inf")}, {"w": 9}, {"v": 2}]
        assert [r["v"] for r in gw._rank_market_rows(rows, "v", True)] == [3, 2, 1]
        assert [r["v"] for r in gw._rank_market_rows(rows, "v", False, n=2)] == [1, 2]
        assert gw._rank_market_rows(None, "v", True) == []


class TestPass82IndicesAndUniverse:
    def test_non_finite_index_values_take_the_degrade_path(self, ienv, kv, client):
        ienv.frames["^NSEI"] = _frame([100.0, 100.0], [100.0, float("inf")])
        ienv.frames["^BSESN"] = _frame([100.0, 100.0], [100.0, 105.0])
        r = client.get("/market/indices", params={"force_refresh": "true"})
        assert r.status_code == 200 and r.json().get("fallback") is True
        assert all(k != gw.INDICES_LAST_KNOWN for k, _, _ in kv.sets)


class TestPass82ClearCacheFailures:
    def test_kv_delete_failure_is_reported_but_other_layers_clear(self, monkeypatch):
        class BadKV:
            def delete(self, key):
                raise RuntimeError("kv down")

        monkeypatch.setattr(gw, "_kv_cache", BadKV())
        monkeypatch.setattr(gw, "_redis", None)
        out = gw.clear_universe_cache()
        assert out["cleared"] == ["memory"] and out["errors"] == ["kv: kv down"]

    def test_nothing_clearable_reports_failure(self, monkeypatch):
        class BadMem(dict):
            def pop(self, *a, **k):
                raise RuntimeError("mem down")

        monkeypatch.setattr(gw, "_kv_cache", None)
        monkeypatch.setattr(gw, "_redis", None)
        monkeypatch.setattr(gw, "_mem_kv", BadMem())
        out = gw.clear_universe_cache()
        assert out["ok"] is False and out["cleared"] == [] and out["errors"] == ["memory: mem down"]
