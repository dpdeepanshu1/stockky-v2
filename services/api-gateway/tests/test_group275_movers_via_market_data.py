"""group275: api-gateway market movers are priced through market-data /quotes/bulk first; yfinance only for the rest."""
from datetime import datetime, timezone

import pytest

import main as gw


def _q(sym, px, prev, **kw):
    r = {"symbol": f"{sym}.NS", "price": px, "previous_close": prev, "day_high": px + 1, "day_low": px - 1,
         "volume": 500, "fetched_at": datetime.now(timezone.utc).isoformat()}
    r.update(kw)
    return r


class _Resp:
    def __init__(self, quotes, status=200):
        self.status_code = status
        self._q = quotes

    def json(self):
        return {"quotes": self._q}


@pytest.fixture()
def post(monkeypatch):
    class P:
        calls = []
        quotes = []
        status = 200
        exc = None
    p = P()
    p.calls = []

    def fake(url, json=None, timeout=None):
        p.calls.append((url, list(json["symbols"])))
        if p.exc:
            raise p.exc
        wanted = set(json["symbols"])
        return _Resp([q for q in p.quotes if q["symbol"].replace(".NS", "") in wanted], p.status)
    monkeypatch.setattr(gw.httpx, "post", fake)
    monkeypatch.delenv("GATEWAY_MOVERS_VIA_MARKET_DATA", raising=False)
    return p


def test_rows_have_the_shape_the_yfinance_path_produced(post):
    post.quotes = [_q("TCS", 110.0, 100.0)]
    out = gw._movers_rows_from_market_data(["TCS", "INFY"])
    assert out == [{"symbol": "TCS", "price": 110.0, "change": 10.0, "change_pct": 10.0,
                    "volume": 500, "high": 111.0, "low": 109.0}]


def test_chunks_follow_the_bulk_chunk_setting(post, monkeypatch):
    monkeypatch.setenv("GATEWAY_BULK_QUOTE_CHUNK", "2")
    gw._movers_rows_from_market_data(["A", "B", "C", "D", "E"])
    assert [len(c[1]) for c in post.calls] == [2, 2, 1]


def test_stale_or_prevcloseless_rows_are_left_for_yfinance(post):
    post.quotes = [_q("OLD", 10.0, 9.0, fetched_at="2020-01-01T00:00:00"),
                   _q("NOPREV", 10.0, None), _q("OK", 10.0, 9.0)]
    assert [r["symbol"] for r in gw._movers_rows_from_market_data(["OLD", "NOPREV", "OK"])] == ["OK"]


def test_failures_never_raise(post):
    post.exc = RuntimeError("down")
    assert gw._movers_rows_from_market_data(["A"]) == []
    post.exc = None
    post.status = 503
    assert gw._movers_rows_from_market_data(["A"]) == []


def test_switch_off_makes_no_call(post, monkeypatch):
    monkeypatch.setenv("GATEWAY_MOVERS_VIA_MARKET_DATA", "0")
    assert gw._movers_rows_from_market_data(["A"]) == [] and post.calls == []


def test_get_nifty50_data_only_sends_the_unpriced_symbols_to_yfinance(post, monkeypatch):
    post.quotes = [_q("AAA", 110.0, 100.0)]
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "open")   # group279: was wall-clock dependent
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    monkeypatch.setattr(gw, "_redis_set", lambda *a, **k: None)
    seen = []

    class _T:
        def __init__(self, t):
            seen.append(t)

        def history(self, **k):
            import pandas as pd
            return pd.DataFrame()
    monkeypatch.setattr(gw.yf, "Ticker", _T)
    monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: f"{s}.NS")
    out = gw._get_nifty50_data()
    assert seen == ["BBB.NS"]
    assert [r["symbol"] for r in out] == ["AAA"]
