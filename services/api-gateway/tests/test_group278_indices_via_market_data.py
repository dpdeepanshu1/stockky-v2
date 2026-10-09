"""group278: /market/indices reads ^NSEI / ^BSESN from market-data /history first (Dhan -> AngelOne -> yfinance is
decided there); direct yfinance is only the last resort and can be switched off.
Run from services/api-gateway:  python3 -m pytest tests/test_group278_indices_via_market_data.py -v"""
from __future__ import annotations

import os

import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._b = body

    def json(self):
        return self._b


def candles(n, start=22000.0):
    return [{"date": f"2026-10-0{i + 1}", "open": start + i, "high": start + i + 5, "low": start + i - 5,
             "close": start + i + 2, "volume": 0} for i in range(n)]


@pytest.fixture
def md_on(monkeypatch):
    monkeypatch.setenv("INDICES_VIA_MARKET_DATA", "1")


def test_frame_has_yfinance_shape(monkeypatch, md_on):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"], seen["params"] = url, params
        return Resp(200, {"candles": candles(5)})
    monkeypatch.setattr(gw.httpx, "get", fake_get)
    df = gw._md_index_frame("^NSEI", "5d")
    assert list(df.columns)[:4] == ["Open", "High", "Low", "Close"] and len(df) == 5
    assert "%5ENSEI" in seen["url"] and seen["params"]["period"] == "5d"
    assert isinstance(df.index, pd.DatetimeIndex)


def test_period_1d_is_the_latest_session_only(monkeypatch, md_on):
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: Resp(200, {"candles": candles(5)}))
    df = gw._md_index_frame("^NSEI", "1d")
    assert len(df) == 1 and float(df["Close"].iloc[0]) == 22006.0


@pytest.mark.parametrize("resp", [Resp(404, {}), Resp(200, {"candles": []}), Resp(200, None),
                                  Resp(200, {"candles": [{"date": "2026-10-01", "close": None}]})])
def test_no_data_is_none(monkeypatch, resp):
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: resp)
    assert gw._md_index_frame("^NSEI", "1d") is None


def test_network_error_is_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(gw.httpx, "get", boom)
    assert gw._md_index_frame("^NSEI", "1d") is None


class YF:
    calls = []

    def __init__(self, sym):
        self.sym = sym

    def history(self, period="1d", **kw):
        YF.calls.append((self.sym, period))
        return pd.DataFrame({"Open": [1.0], "Close": [2.0]})


def test_market_data_answer_wins_and_yfinance_is_not_called(monkeypatch, md_on):
    YF.calls = []
    monkeypatch.setattr(gw.yf, "Ticker", YF)
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: Resp(200, {"candles": candles(5)}))
    df = gw._IndexTicker("^NSEI").history(period="1d")
    assert float(df["Close"].iloc[0]) == 22006.0 and YF.calls == []


def test_falls_back_to_yfinance_when_market_data_has_nothing(monkeypatch, md_on):
    YF.calls = []
    monkeypatch.setattr(gw.yf, "Ticker", YF)
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: Resp(503, {}))
    df = gw._IndexTicker("^BSESN").history(period="5d")
    assert float(df["Close"].iloc[0]) == 2.0 and YF.calls == [("^BSESN", "5d")]


def test_no_direct_yfinance_when_switched_off(monkeypatch, md_on):
    YF.calls = []
    monkeypatch.setattr(gw.yf, "Ticker", YF)
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: Resp(503, {}))
    monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", "0")
    assert gw._IndexTicker("^NSEI").history(period="1d").empty and YF.calls == []


def test_market_data_read_can_be_turned_off(monkeypatch):
    YF.calls = []
    monkeypatch.setattr(gw.yf, "Ticker", YF)
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call expected")))
    gw._IndexTicker("^NSEI").history(period="1d")      # conftest default INDICES_VIA_MARKET_DATA=0
    assert YF.calls == [("^NSEI", "1d")]


@pytest.mark.parametrize("raw,want", [(None, True), ("", True), ("1", True), ("0", False), ("off", False),
                                      ("False", False)])
def test_direct_yf_switch(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raw)
    assert gw._direct_yf_ok() is want
