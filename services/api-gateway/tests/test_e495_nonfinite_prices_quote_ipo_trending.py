"""E495: NaN / inf prices reaching responses, Telegram text and the feed store (follow-up to E494's reading of the helpers its routes call).

1. `proxy_quote` (`GET /quote/{symbol}`, bound_routes_i.py) returned the market-data price as sent: NaN / inf made the JSON response itself fail (500),
   text was passed through; the yfinance fast_info fallback returned NaN (truthy). Unusable prices now fall through to the next key / the fallback.
2. `_trending_yf_row` (main.py, last resort of `/market/trending`) turned a NaN Close (incomplete bar) into a NaN price: 500 at response time.
3. `api_ipo_notify_top_picks` (bound_routes_d.py): a non-dict result raised on `.get`; NaN / inf price, score or change printed "₹nan" / "+nan%".
4. `data_feed.patch_feed_price`: NaN passed `px <= 0` and the cap test, and was stored as the live price (inf too when the cap is off).
5. `refresh_prepare_to_buy` (bound_routes_f.py): "inf" passed `v > 0` and was patched into the feed row.

Run from services/api-gateway:  python3 -m pytest tests/test_e495_nonfinite_prices_quote_ipo_trending.py -v"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import types

import pytest

from test_e494_hot_jobs_surprise_stream_and_trending import gw, posts, _run, FakeResp   # noqa: F401  (posts is a fixture)
import httpx
import data_feed as df

NAN, INF = float("nan"), float("inf")


# ── 1. quote proxy ───────────────────────────────────────────────────────────

def _quote(monkeypatch, body, yf_ok=False, fast_info=None):
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: FakeResp(200, body))
    monkeypatch.setattr(gw, "_direct_yf_ok", lambda: yf_ok)
    if yf_ok:
        monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: s + ".NS")
        monkeypatch.setattr(gw.yf, "Ticker", lambda t: types.SimpleNamespace(fast_info=fast_info or {}))
    return gw.proxy_quote("TCS")


def test_good_quote_is_unchanged(monkeypatch):
    out = _quote(monkeypatch, {"price": 101.5, "close": 100, "source": "dhan", "volume": 5})
    assert out["price"] == 101.5 and out["close"] == 100.0 and out["source"] == "dhan" and out["raw"] == {"volume": 5}


@pytest.mark.parametrize("bad", [NAN, INF, "abc", 0, -3, None])
def test_an_unusable_price_falls_through_to_the_next_key(monkeypatch, bad):
    out = _quote(monkeypatch, {"price": bad, "regularMarketPrice": 77.0})
    assert out["price"] == 77.0
    json.dumps(out, allow_nan=False)


def test_an_inf_close_never_becomes_the_close_field(monkeypatch):
    out = _quote(monkeypatch, {"price": 50.0, "close": INF})
    assert out["close"] == 50.0
    json.dumps(out, allow_nan=False)


def test_a_non_dict_body_goes_to_the_fallback(monkeypatch):
    with pytest.raises(Exception) as e:
        _quote(monkeypatch, ["junk"])
    assert getattr(e.value, "status_code", None) == 502


def test_only_unusable_prices_go_to_the_fallback_which_is_off_here(monkeypatch):
    with pytest.raises(Exception) as e:
        _quote(monkeypatch, {"price": NAN, "close": INF})
    assert getattr(e.value, "status_code", None) == 502


def test_a_nan_yfinance_price_is_not_returned_as_a_price(monkeypatch):
    out = _quote(monkeypatch, {}, yf_ok=True, fast_info={"last_price": NAN})
    assert out["price"] is None and out["source"] == "yfinance_fast"
    json.dumps(out, allow_nan=False)


def test_a_good_yfinance_price_is_kept(monkeypatch):
    out = _quote(monkeypatch, {}, yf_ok=True, fast_info={"last_price": 12.5})
    assert out["price"] == 12.5


# ── 2. trending last resort ──────────────────────────────────────────────────

class _Hist:
    empty = False

    def __init__(self, close, open_):
        self._d = {"Close": types.SimpleNamespace(iloc=[close]), "Open": types.SimpleNamespace(iloc=[open_])}

    def __getitem__(self, k):
        return self._d[k]


def _row(monkeypatch, close, open_):
    monkeypatch.setattr(gw, "_direct_yf_ok", lambda: True)
    monkeypatch.setattr(gw, "resolve_ns_ticker", lambda s: s + ".NS")
    monkeypatch.setattr(gw.yf, "Ticker", lambda t: types.SimpleNamespace(history=lambda period=None: _Hist(close, open_)))
    return gw._trending_yf_row("TCS")


def test_good_bar_is_unchanged(monkeypatch):
    assert _row(monkeypatch, 110.0, 100.0) == (110.0, 10.0, 10.0)


def test_nan_close_is_skipped(monkeypatch):
    assert _row(monkeypatch, NAN, 100.0) is None


def test_inf_close_is_skipped(monkeypatch):
    assert _row(monkeypatch, INF, 100.0) is None


def test_nan_open_keeps_the_price_and_drops_the_change(monkeypatch):
    assert _row(monkeypatch, 110.0, NAN) == (110.0, None, None)


# ── 3. IPO notify ────────────────────────────────────────────────────────────

def _ipo(sym, **kw):
    d = {"symbol": sym, "decision": "BUY NOW", "ipo_score": 78, "stage": "listing_day",
         "issue_price": 100, "current_price": 112.5, "current_vs_issue_pct": 12.5}
    d.update(kw)
    return d


def _ipo_notify(monkeypatch, results, top_n=5):
    mod = types.ModuleType("ipo_scanner")
    mod.get_ipo_list = lambda display_days=None: {"results": results}
    monkeypatch.setitem(sys.modules, "ipo_scanner", mod)
    return _run(gw.api_ipo_notify_top_picks(top_n=top_n))


def test_ipo_notify_skips_non_dict_entries_and_never_prints_nan(monkeypatch, posts):
    out = _ipo_notify(monkeypatch, [
        "junk", None,
        _ipo("AAA", issue_price=NAN, current_price=INF, ipo_score=NAN, pre_listing_advisory_score=None, current_vs_issue_pct=INF),
        _ipo("BBB"),
    ])
    assert out["ok"] is True and out["sent"] is True and out["count"] == 2
    msg = posts[0]["message"]
    assert "nan" not in msg.lower() and "inf" not in msg.lower()
    assert "AAA — BUY NOW (score —/100, listing day)\n   Issue ₹— → Current ₹— (—)" in msg
    assert "BBB — BUY NOW (score 78/100, listing day)\n   Issue ₹100 → Current ₹112.5 (+12.50%)" in msg


def test_ipo_notify_with_only_junk_is_no_results(monkeypatch, posts):
    out = _ipo_notify(monkeypatch, ["junk", 5, None])
    assert out["ok"] is False and out["sent"] is False and out["count"] == 0 and posts == []


def test_ipo_notify_good_rows_keep_their_format(monkeypatch, posts):
    _ipo_notify(monkeypatch, [_ipo("AAA", ipo_score=0)])
    assert posts[0]["message"] == ("🆕 *IPO Tracker — Top 1*\n"
                                   "1. AAA — BUY NOW (score 0/100, listing day)\n   Issue ₹100 → Current ₹112.5 (+12.50%)")


# ── 4. patch_feed_price ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [NAN, INF, -INF, "nan", "inf"])
def test_non_finite_live_price_is_never_patched_into_the_feed(monkeypatch, bad):
    monkeypatch.setattr(df, "MAX_STOCK_PRICE", 0.0)       # cap off: inf used to pass
    called = []
    monkeypatch.setattr(df, "get_data_feed_store", lambda: called.append(1))
    assert df.patch_feed_price("TCS", bad) is False
    assert called == []                                   # rejected before the store is touched


# ── 5. refresh_prepare_to_buy ────────────────────────────────────────────────

def test_refresh_prepare_to_buy_ignores_an_inf_quote_key(monkeypatch):
    patched, quotes = [], {"A": {"cmp": "inf", "price": 55.5}, "B": {"cmp": "inf"}}

    class _Cl:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, timeout=None):
            r = FakeResp(200, quotes[url.rsplit("/", 1)[1]])
            r.content = b"{}"          # the route reads `q_res.content` before `.json()`
            return r

    import data_feed
    monkeypatch.setattr(data_feed, "find_prepare_to_buy_candidates", lambda min_score=None, max_score=None: ["A", "B"])
    monkeypatch.setattr(data_feed, "patch_feed_price", lambda s, p: patched.append((s, p)) or True)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.t")
    monkeypatch.setattr(httpx, "AsyncClient", _Cl)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    import buy_sniper
    monkeypatch.setattr(buy_sniper, "_prep_mode", lambda: "off")
    out = _run(gw.refresh_prepare_to_buy())
    assert (("A", 55.5) in patched) and not any(s == "B" for s, _ in patched), (patched, out)
    assert all(math.isfinite(p) for _, p in patched)
