"""group279: the gateway's remaining direct yfinance callers ask market-data first and can be switched off.

  - yf_policy.direct_yf_ok / env_on / md_daily_frame
  - movers leftovers (main._get_nifty50_data) honour GATEWAY_DIRECT_YFINANCE_FALLBACK
  - surprise feed: market-data /quotes/bulk first, yfinance only for the rest
  - premarket baselines: market-data /history step, yfinance bulk / per-symbol decline when off
  - IPO history fallback honours the switch

Run from services/api-gateway:  python3 -m pytest tests/test_group279_remaining_yfinance_callers.py -v"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
    import yf_policy
    import surprise_scanner as sc
    import surprise_premarket as pm
    import ipo_scanner as ipo
finally:
    os.environ.update(_saved_env)


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._b = body

    def json(self):
        return self._b


def candles(n, start=100.0, vol=1_000_000):
    return [{"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "open": start, "high": start + 5, "low": start - 5,
             "close": start + (i % 3), "volume": vol} for i in range(n)]


# ── yf_policy ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val,expect", [(None, True), ("", True), ("1", True), ("on", True),
                                         ("0", False), ("false", False), ("No", False), (" OFF ", False)])
def test_direct_yf_ok_reads_the_switch(monkeypatch, val, expect):
    if val is None:
        monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", val)
    assert yf_policy.direct_yf_ok() is expect
    assert gw._direct_yf_ok() is expect            # the gateway helper is the same switch


def test_env_on_default_and_off_values(monkeypatch):
    monkeypatch.delenv("X_SWITCH", raising=False)
    assert yf_policy.env_on("X_SWITCH") is True and yf_policy.env_on("X_SWITCH", False) is False
    monkeypatch.setenv("X_SWITCH", "off")
    assert yf_policy.env_on("X_SWITCH") is False


def test_md_daily_frame_shape_and_params(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"], seen["params"] = url, params
        return Resp(200, {"candles": candles(8)})
    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    df = yf_policy.md_daily_frame("http://md/", "M&M", period="1y")
    assert list(df.columns)[:5] == ["Open", "High", "Low", "Close", "Volume"] and len(df) == 8
    assert isinstance(df.index, pd.DatetimeIndex)
    assert seen["url"] == "http://md/history/M%26M" and seen["params"] == {"interval": "1d", "period": "1y"}
    yf_policy.md_daily_frame("http://md", "TCS", days=30)
    assert seen["params"] == {"interval": "1d", "days": 30}


@pytest.mark.parametrize("resp", [Resp(404, {}), Resp(200, {"candles": []}), Resp(200, None),
                                  Resp(200, {"candles": [{"date": "2026-01-01", "close": None}]}),
                                  Resp(200, {"candles": [{"close": 5}]})])
def test_md_daily_frame_no_data_is_none(monkeypatch, resp):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: resp)
    assert yf_policy.md_daily_frame("http://md", "X", period="1mo") is None


def test_md_daily_frame_never_raises(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert yf_policy.md_daily_frame("http://md", "X", period="1mo") is None
    assert yf_policy.md_daily_frame("", "X", period="1mo") is None


# ── movers leftovers ─────────────────────────────────────────────────────────
def _movers_env(monkeypatch, direct):
    seen = []

    class _T:
        def __init__(self, t):
            seen.append(t)

        def history(self, **k):
            return pd.DataFrame()
    monkeypatch.setattr(gw.yf, "Ticker", _T)
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "open")
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    monkeypatch.setattr(gw, "_redis_set", lambda *a, **k: None)
    monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: f"{s}.NS")
    monkeypatch.setattr(gw, "_movers_rows_from_market_data",
                        lambda syms: [{"symbol": "AAA", "price": 110.0, "change": 10.0, "change_pct": 10.0,
                                       "volume": 1, "high": 110.0, "low": 100.0}])
    if direct is None:
        monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", direct)
    return seen


def test_movers_leftovers_go_to_yfinance_by_default(monkeypatch):
    seen = _movers_env(monkeypatch, None)
    out = gw._get_nifty50_data()
    assert seen == ["BBB.NS"] and [r["symbol"] for r in out] == ["AAA"]


def test_movers_leftovers_stay_out_of_yfinance_when_it_is_off(monkeypatch):
    seen = _movers_env(monkeypatch, "0")
    out = gw._get_nifty50_data()
    assert seen == [] and [r["symbol"] for r in out] == ["AAA"]


# ── IPO history fallback ─────────────────────────────────────────────────────
def _ipo_env(monkeypatch, direct):
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp(503, {}))
    yf = types.ModuleType("yfinance")

    class T:
        def __init__(self, t):
            calls.append(t)

        def history(self, **k):
            return pd.DataFrame({"Close": [1.0, 2.0]})
    yf.Ticker = T
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    sa = types.ModuleType("symbol_aliases")
    sa.resolve_ns_ticker = lambda s: f"{s}.NS"
    monkeypatch.setitem(sys.modules, "symbol_aliases", sa)
    if direct is None:
        monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", direct)
    return calls


def test_ipo_history_falls_back_to_yfinance_by_default(monkeypatch):
    calls = _ipo_env(monkeypatch, None)
    assert ipo._fetch_history("NEWCO", 10) is not None and calls == ["NEWCO.NS"]


def test_ipo_history_does_not_call_yfinance_when_it_is_off(monkeypatch):
    calls = _ipo_env(monkeypatch, "off")
    assert ipo._fetch_history("NEWCO", 10) is None and calls == []


# ── surprise feed ────────────────────────────────────────────────────────────
def _q(sym, px, prev=None, fetched="now", **extra):
    from datetime import datetime, timezone
    row = {"symbol": sym, "price": px, "previous_close": prev, "day_high": px + 1, "day_low": px - 1,
           "volume": 1000, "source": "dhan"}
    row["fetched_at"] = datetime.now(timezone.utc).isoformat() if fetched == "now" else fetched
    row.update(extra)
    return row


class FakeClient:
    """httpx.AsyncClient stand-in: POST /quotes/bulk from `quotes`, GET /quote/{sym} from `singles`."""
    quotes: list = []
    singles: dict = {}
    posts: list = []
    gets: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, timeout=None):
        FakeClient.posts.append((url, list(json["symbols"])))
        want = set(json["symbols"])
        return Resp(200, {"quotes": [q for q in FakeClient.quotes if q["symbol"] in want]})

    async def get(self, url, **kw):
        FakeClient.gets.append(url)
        r = FakeClient.singles.get(url.rsplit("/", 1)[-1])
        return Resp(200, r) if r else Resp(404, {})


@pytest.fixture
def feed(monkeypatch):
    FakeClient.quotes, FakeClient.singles, FakeClient.posts, FakeClient.gets = [], {}, [], []
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sc, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(sc, "_read_surprise_feed_cache", lambda: None)
    written = []
    monkeypatch.setattr(sc, "_write_surprise_feed_cache", lambda payload: written.append(payload))
    monkeypatch.setattr(sc, "MAX_STOCK_PRICE", 0.0)
    monkeypatch.setattr(sc, "SURPRISE_FEED_COOLDOWN_SEC", 0.0)
    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setenv("SURPRISE_FEED_VIA_MARKET_DATA", "1")        # conftest turns it off for the older tests
    monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    sys.modules.pop("surprise_premarket_stub", None)
    downloads = []
    yf = types.ModuleType("yfinance")

    def download(tickers, **kw):
        downloads.append(tickers)
        names = tickers.split()
        cols = {}
        for n in names:
            cols[(n, "Close")] = [99.0, 100.0]
            cols[(n, "High")] = [101.0, 101.0]
            cols[(n, "Low")] = [98.0, 98.0]
            cols[(n, "Volume")] = [5000.0, 6000.0]
        df = pd.DataFrame(cols)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df
    yf.download = download
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    monkeypatch.setitem(sys.modules, "symbol_aliases", None)
    rig = types.SimpleNamespace(downloads=downloads, written=written)
    return rig


def run_feed(**kw):
    return asyncio.run(sc.run_market_aware_surprise_feed(**kw))


def test_feed_rows_from_market_data_shape(feed):
    FakeClient.quotes = [_q("AAA", 110.0, 100.0), _q("BBB", 50.0, None, day_change_pct=2.5)]
    rows = asyncio.run(sc._feed_rows_from_market_data(FakeClient(), "http://md", ["AAA", "BBB", "ZZZ"]))
    by = {r["symbol"]: r for r in rows}
    assert set(by) == {"AAA", "BBB"}
    a = by["AAA"]
    assert (a["price"], a["cmp"], a["previous_close"], a["day_change_pct"]) == (110.0, 110.0, 100.0, 10.0)
    assert (a["day_high"], a["day_low"], a["volume"], a["source"]) == (111.0, 109.0, 1000, "dhan")
    assert by["BBB"]["day_change_pct"] == 2.5 and "previous_close" not in by["BBB"]


def test_feed_rows_skip_stale_quotes_only_while_the_market_is_open(feed, monkeypatch):
    FakeClient.quotes = [_q("OLD", 10.0, 9.0, fetched="2020-01-01T00:00:00")]
    assert asyncio.run(sc._feed_rows_from_market_data(FakeClient(), "http://md", ["OLD"])) == []
    monkeypatch.setattr(sc, "is_market_open_ist", lambda: False)
    assert [r["symbol"] for r in asyncio.run(sc._feed_rows_from_market_data(FakeClient(), "http://md", ["OLD"]))] == ["OLD"]


def test_feed_rows_respect_the_price_cap_and_never_raise(feed, monkeypatch):
    monkeypatch.setattr(sc, "MAX_STOCK_PRICE", 100.0)
    FakeClient.quotes = [_q("CHEAP", 50.0, 49.0), _q("DEAR", 500.0, 490.0)]
    rows = asyncio.run(sc._feed_rows_from_market_data(FakeClient(), "http://md", ["CHEAP", "DEAR"]))
    assert [r["symbol"] for r in rows] == ["CHEAP"]

    class Boom(FakeClient):
        async def post(self, *a, **k):
            raise RuntimeError("down")
    assert asyncio.run(sc._feed_rows_from_market_data(Boom(), "http://md", ["CHEAP"])) == []


def test_feed_market_data_prices_everything_and_yfinance_is_not_called(feed):
    FakeClient.quotes = [_q("AAA", 110.0, 100.0), _q("BBB", 50.0, 49.0)]
    out = run_feed(symbols=["AAA", "BBB"], market_data_url="http://md", force=True)
    assert out["status"] == "success" and out["count"] == 2
    assert feed.downloads == [] and FakeClient.gets == []
    assert {r["symbol"] for r in out["data"]} == {"AAA", "BBB"}


def test_feed_only_sends_the_unpriced_symbols_to_yfinance(feed):
    FakeClient.quotes = [_q("AAA", 110.0, 100.0)]
    out = run_feed(symbols=["AAA", "BBB"], market_data_url="http://md", force=True)
    assert feed.downloads == ["BBB.NS"]
    by = {r["symbol"]: r for r in out["data"]}
    assert by["AAA"]["source"] == "dhan" and by["BBB"]["source"] == "yahoo_bulk"


def test_feed_with_direct_yfinance_off_uses_the_per_symbol_quote_only(feed, monkeypatch):
    monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", "0")
    FakeClient.quotes = [_q("AAA", 110.0, 100.0)]
    FakeClient.singles = {"BBB": {"price": 77.0, "source": "angelone"}}
    out = run_feed(symbols=["AAA", "BBB", "CCC"], market_data_url="http://md", force=True)
    assert feed.downloads == []
    assert FakeClient.gets == ["http://md/quote/BBB", "http://md/quote/CCC"]
    assert {r["symbol"] for r in out["data"]} == {"AAA", "BBB"}


def test_feed_old_order_is_restored_by_the_switch(feed, monkeypatch):
    monkeypatch.setenv("SURPRISE_FEED_VIA_MARKET_DATA", "0")
    FakeClient.quotes = [_q("AAA", 110.0, 100.0)]
    run_feed(symbols=["AAA"], market_data_url="http://md", force=True)
    assert FakeClient.posts == [] and feed.downloads == ["AAA.NS"]


def test_feed_market_data_failure_falls_back_to_yfinance(feed, monkeypatch):
    class Down(FakeClient):
        async def post(self, *a, **k):
            raise RuntimeError("down")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Down)
    out = run_feed(symbols=["AAA"], market_data_url="http://md", force=True)
    assert feed.downloads == ["AAA.NS"] and out["count"] == 1


# ── premarket baselines ──────────────────────────────────────────────────────
def frame(n=60, high=120.0, close=100.0, vol=1_000_000):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"Open": close, "High": [high] + [close + 5] * (n - 1), "Low": close - 5, "Close": close,
                         "Volume": vol}, index=idx)


@pytest.fixture
def md_hist(monkeypatch):
    monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    monkeypatch.setenv("PREMARKET_BASELINES_VIA_MARKET_DATA", "1")
    monkeypatch.setenv("MARKET_DATA_URL", "http://md")
    monkeypatch.setattr(pm, "premarket_stop_requested", lambda: False)
    calls = []
    frames = {}

    def fake(url, sym, period=None, days=None, timeout=15.0):
        calls.append((url, sym, period))
        return frames.get(sym)
    monkeypatch.setattr(yf_policy, "md_daily_frame", fake)
    monkeypatch.setitem(sys.modules, "symbol_aliases", None)
    return types.SimpleNamespace(calls=calls, frames=frames)


def test_baseline_row_from_frame_matches_the_yfinance_maths():
    row = pm._baseline_row_from_frame("ABC", frame())
    assert row["symbol"] == "ABC" and row["prev_close"] == 100.0 and row["high_52w"] == 120.0
    assert row["dist_52w_pct"] == round((120.0 - 100.0) / 120.0 * 100, 2)
    assert row["daily_atr"] == 10.0 and row["avg_15m_volume"] == 40000 and row["is_liquid"] is True
    assert row["sector"] is None


@pytest.mark.parametrize("fr", [None, frame(4), frame(60, high=0.0, close=0.0)])
def test_baseline_row_from_frame_too_little_data_is_none(fr):
    assert pm._baseline_row_from_frame("ABC", fr) is None


def test_market_data_baselines_rows_and_remaining(md_hist):
    md_hist.frames.update({"AAA": frame(), "BBB": frame(3)})
    rows, remaining = pm.bulk_baselines_from_market_data(["AAA", "BBB", "CCC", "NIFTY50"])
    assert [r["symbol"] for r in rows] == ["AAA"]
    assert sorted(remaining) == ["BBB", "CCC"]                      # index symbol dropped, like the yfinance path
    assert {c[0] for c in md_hist.calls} == {"http://md"} and {c[2] for c in md_hist.calls} == {"1y"}


def test_market_data_baselines_switch_off_leaves_everything(md_hist, monkeypatch):
    monkeypatch.setenv("PREMARKET_BASELINES_VIA_MARKET_DATA", "0")
    md_hist.frames["AAA"] = frame()
    assert pm.bulk_baselines_from_market_data(["AAA"]) == ([], ["AAA"]) and md_hist.calls == []


def test_market_data_baselines_stop_request_and_budget_leave_the_rest(md_hist, monkeypatch):
    md_hist.frames.update({"AAA": frame(), "BBB": frame()})
    monkeypatch.setattr(pm, "premarket_stop_requested", lambda: True)
    rows, remaining = pm.bulk_baselines_from_market_data(["AAA", "BBB"])
    assert rows == [] and sorted(remaining) == ["AAA", "BBB"] and md_hist.calls == []
    monkeypatch.setattr(pm, "premarket_stop_requested", lambda: False)
    rows, remaining = pm.bulk_baselines_from_market_data(["AAA", "BBB"], budget_s=-1)
    assert rows == [] and sorted(remaining) == ["AAA", "BBB"]


def test_market_data_baselines_never_raise(md_hist, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pm, "_baseline_row_from_frame", boom)
    md_hist.frames["AAA"] = frame()
    assert pm.bulk_baselines_from_market_data(["AAA"]) == ([], ["AAA"])


def test_yfinance_bulk_declines_when_direct_is_off_and_keeps_the_symbols(monkeypatch):
    monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", "0")
    monkeypatch.setitem(sys.modules, "symbol_aliases", None)
    yf = types.ModuleType("yfinance")
    called = []
    yf.download = lambda *a, **k: called.append(a) or None
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    rows, remaining = pm.bulk_baselines_from_yfinance(["AAA", "BBB", "NIFTY50"])
    assert rows == [] and remaining == ["AAA", "BBB"] and called == []


def test_per_symbol_baseline_declines_when_direct_is_off(monkeypatch):
    monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", "0")
    yf = types.ModuleType("yfinance")
    called = []
    yf.Ticker = lambda *a, **k: called.append(a) or None
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    assert pm.compute_baseline_for_symbol("AAA") is None and called == []
