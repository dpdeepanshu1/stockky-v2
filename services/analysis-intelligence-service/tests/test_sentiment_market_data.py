"""group270: sentiment asks market-data-service (Dhan -> AngelOne -> yfinance there) before yfinance.

Run from services/analysis-intelligence-service:   python3 -m pytest tests/test_sentiment_market_data.py -v
"""
from __future__ import annotations
import os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sentiment"))

import numpy as _np          # noqa: F401
import pandas as _pd
import pytest

if "yfinance" not in sys.modules:
    _stub = types.ModuleType("yfinance")
    _stub.set_session = lambda s: None
    _stub.shared = types.SimpleNamespace(_session=None)

    class _T:
        def __init__(self, sym): pass
        def history(self, **kw): return _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})
    _stub.Ticker = _T
    _stub.download = lambda **kw: _pd.DataFrame()
    sys.modules["yfinance"] = _stub

import main as sm   # sentiment/main.py
_REAL_MD_GET_JSON = sm._md_get_json     # the tests below replace sm._md_get_json with fakes


@pytest.fixture(autouse=True)
def md_on(monkeypatch):
    monkeypatch.setenv("SENTIMENT_USE_MARKET_DATA", "1")
    monkeypatch.setattr(sm, "MARKET_DATA_URL", "http://md.test")
    sm._cache["data"] = None
    sm._cache["timestamp"] = None
    yield


def _install(monkeypatch, routes):
    """routes: {path-prefix: json-or-None}. Records every path asked."""
    seen = []

    class R:
        def __init__(self, body): self.status_code, self._b = (200 if body is not None else 503), body
        def json(self): return self._b

    def fake(path, timeout=10.0):
        seen.append(path)
        for prefix, body in routes.items():
            if path.startswith(prefix):
                return body
        return None
    monkeypatch.setattr(sm, "_md_get_json", fake)
    return seen


def _bars(n, start=100.0, step=1.0):
    return [{"date": f"2026-09-{i + 1:02d} 00:00", "open": start + i * step, "high": start + i * step + 2,
             "low": start + i * step - 2, "close": start + i * step, "volume": 1000 + i} for i in range(n)]


class TestIndexFromMarketData:
    def test_quote_is_used_when_complete(self, monkeypatch):
        seen = _install(monkeypatch, {"/quote/^NSEI": {"price": 24100.0, "previous_close": 24000.0,
                                                         "day_high": 24150.0, "day_low": 23950.0, "volume": 5}})
        i = sm._index_from_market_data("^NSEI", "NIFTY 50")
        assert i.current == 24100.0 and i.previous_close == 24000.0 and i.change == 100.0
        assert i.change_percent == round(100 / 24000 * 100, 2) and i.high == 24150.0 and i.volume == 5
        assert seen == ["/quote/^NSEI"]                      # no history call needed

    def test_history_used_when_quote_has_no_previous_close(self, monkeypatch):
        seen = _install(monkeypatch, {"/quote/": {"price": 24100.0},
                                      "/history/^NSEI": {"candles": _bars(5)}})
        i = sm._index_from_market_data("^NSEI", "NIFTY 50")
        assert i.current == 104.0 and i.previous_close == 103.0
        assert seen[0].startswith("/quote/") and seen[1].startswith("/history/^NSEI?period=1mo")

    def test_none_when_nothing_usable(self, monkeypatch):
        _install(monkeypatch, {})
        assert sm._index_from_market_data("^NSEI", "NIFTY 50") is None
        _install(monkeypatch, {"/history/": {"candles": _bars(1)}})
        assert sm._index_from_market_data("^NSEI", "NIFTY 50") is None

    def test_zero_closes_in_history_are_ignored(self, monkeypatch):
        bad = _bars(3)
        bad[-1]["close"] = 0
        _install(monkeypatch, {"/history/": {"candles": bad}})
        i = sm._index_from_market_data("^NSEI", "NIFTY 50")
        assert i.current == 101.0 and i.previous_close == 100.0

    def test_cmp_key_accepted(self, monkeypatch):
        _install(monkeypatch, {"/quote/": {"cmp": 50.0, "previous_close": 40.0}})
        assert sm._index_from_market_data("^BSESN", "SENSEX").current == 50.0

    def test_symbol_is_url_encoded_but_caret_kept(self, monkeypatch):
        seen = _install(monkeypatch, {"/quote/": {"price": 1.0, "previous_close": 1.0}})
        sm._index_from_market_data("^NSEI", "N")
        assert seen[0] == "/quote/^NSEI"


class TestBatch:
    def test_all_from_market_data_never_touches_yfinance(self, monkeypatch):
        _install(monkeypatch, {"/quote/": {"price": 110.0, "previous_close": 100.0}})
        monkeypatch.setattr(sm.yf, "download", lambda **k: (_ for _ in ()).throw(AssertionError("yfinance used")))
        out = sm.fetch_indices_batch(sm.INDEX_SYMBOLS)
        assert set(out) == {"NIFTY 50", "SENSEX"} and out["NIFTY 50"].change_percent == 10.0

    def test_partial_answer_sends_only_the_missing_index_to_yfinance(self, monkeypatch):
        _install(monkeypatch, {"/quote/^NSEI": {"price": 110.0, "previous_close": 100.0}})
        asked = {}

        def fake_download(**kw):
            asked["tickers"] = kw["tickers"]
            return _pd.DataFrame()
        monkeypatch.setattr(sm.yf, "download", fake_download)
        monkeypatch.setattr(sm, "fetch_individual_ticker", lambda sym, name, max_retries=3: None)
        out = sm.fetch_indices_batch(sm.INDEX_SYMBOLS)
        assert "NIFTY 50" in out and asked["tickers"] == ["^BSESN"]

    def test_flag_off_restores_direct_yfinance(self, monkeypatch):
        monkeypatch.setenv("SENTIMENT_USE_MARKET_DATA", "0")
        seen = _install(monkeypatch, {"/quote/": {"price": 1.0, "previous_close": 1.0}})
        monkeypatch.setattr(sm.yf, "download", lambda **k: _pd.DataFrame())
        monkeypatch.setattr(sm, "fetch_individual_ticker", lambda sym, name, max_retries=3: None)
        assert sm.fetch_indices_batch(sm.INDEX_SYMBOLS) == {}
        assert seen == []


class TestNiftyHistory:
    def test_market_data_bars_become_a_frame(self, monkeypatch):
        _install(monkeypatch, {"/history/%5ENSEI": {"candles": _bars(30)}})
        df = sm._nifty_hist("6d", 6)
        assert list(df.columns) == ["High", "Low", "Close"] and len(df) == 6 and df["Close"].iloc[-1] == 129.0

    def test_too_few_bars_fall_back_to_yfinance(self, monkeypatch):
        _install(monkeypatch, {"/history/%5ENSEI": {"candles": _bars(3)}})
        sentinel = _pd.DataFrame({"Close": [1.0] * 6, "High": [1.0] * 6, "Low": [1.0] * 6})
        monkeypatch.setattr(sm.yf, "Ticker", lambda s: types.SimpleNamespace(history=lambda **k: sentinel))
        assert sm._nifty_hist("6d", 6) is sentinel

    def test_flag_off_goes_straight_to_yfinance(self, monkeypatch):
        monkeypatch.setenv("SENTIMENT_USE_MARKET_DATA", "0")
        seen = _install(monkeypatch, {"/history/": {"candles": _bars(30)}})
        sentinel = _pd.DataFrame({"Close": [1.0], "High": [1.0], "Low": [1.0]})
        monkeypatch.setattr(sm.yf, "Ticker", lambda s: types.SimpleNamespace(history=lambda **k: sentinel))
        assert sm._nifty_hist("1mo", 22) is sentinel and seen == []

    def test_score_uses_market_data_momentum_and_volatility(self, monkeypatch):
        _install(monkeypatch, {"/history/%5ENSEI": {"candles": _bars(30, 24000.0, 50.0)}})
        monkeypatch.setattr(sm.yf, "Ticker", lambda s: (_ for _ in ()).throw(AssertionError("yfinance used")))
        idx = {"NIFTY 50": sm.IndexData(symbol="^NSEI", name="NIFTY 50", current=1, previous_close=1, change=0,
                                        change_percent=0.0, high=1, low=1, volume=1, timestamp=sm.datetime.now())}
        score = sm.compute_market_score(idx)
        assert 0 <= score <= 100 and score > 50          # a steady up-trend lifts the neutral 50


class TestMdGetJson:
    def test_real_function_paths(self, monkeypatch):
        real = types.SimpleNamespace(_md_get_json=_REAL_MD_GET_JSON, httpx=sm.httpx)

        class R:
            def __init__(self, code, body=None): self.status_code, self._b = code, body
            def json(self): return self._b
        monkeypatch.setitem(sys.modules, "md_guard", types.SimpleNamespace(md_get=lambda url, **k: R(200, {"ok": 1})))
        assert real._md_get_json("/p") == {"ok": 1}
        monkeypatch.setitem(sys.modules, "md_guard", types.SimpleNamespace(md_get=lambda url, **k: R(503)))
        assert real._md_get_json("/p") is None
        monkeypatch.setitem(sys.modules, "md_guard",
                            types.SimpleNamespace(md_get=lambda url, **k: (_ for _ in ()).throw(RuntimeError("x"))))
        assert real._md_get_json("/p") is None
        monkeypatch.delitem(sys.modules, "md_guard", raising=False)
        monkeypatch.setattr(real.httpx, "get", lambda url, **k: R(200, {"plain": 1}))
        assert real._md_get_json("/p") == {"plain": 1}
