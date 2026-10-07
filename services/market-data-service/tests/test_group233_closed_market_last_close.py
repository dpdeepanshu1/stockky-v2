"""
group233: with the market closed, /quote/{symbol} and /quotes/bulk answer from the last close (cached row of any age,
30-day last-good fallback row, bhavcopy close) and never walk AngelOne-first / AngelOne REST / Yahoo.

Run from services/market-data-service:
    python3 -m pytest tests/test_group233_closed_market_last_close.py -v
"""
from __future__ import annotations
import os, sys, types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main


def _row(sym, age_s, price=100.0, source="angelone_ws"):
    return {"symbol": sym, "price": price, "source": source,
            "fetched_at": (datetime.utcnow() - timedelta(seconds=age_s)).isoformat()}


@pytest.fixture()
def harness(monkeypatch):
    cache, fallback = {}, {}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_get", lambda k: fallback.get(k))
    monkeypatch.setattr(main, "_fallback_set", lambda k, v: fallback.__setitem__(k, v))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper().replace('.NS', '')}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: True)
    monkeypatch.delenv("QUOTE_CLOSED_LAST_CLOSE_MAX_AGE_H", raising=False)
    boom = {"n": 0, "yf_ret": None}

    def _ao(*a, **k):
        boom["n"] += 1
        return None

    def _yf(sym, *a, **k):
        boom["n"] += 1          # closed-market tests assert this stays 0
        if boom["yf_ret"] is None:
            raise RuntimeError("no Yahoo in tests")
        return dict(boom["yf_ret"], symbol=sym)

    monkeypatch.setattr(main, "_angelone_rest_quote_first", _ao)
    monkeypatch.setattr(main, "_yahoo_ohlcv_quote", _yf)
    bh = {}
    monkeypatch.setattr(main, "_waterfall_bhavcopy_price", lambda s: bh.get(str(s).upper().replace(".NS", "")))
    live = types.ModuleType("angelone_ws_feed")
    live.get_live_quote = lambda s: None
    live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", live)
    ylive = types.ModuleType("yahoo_ws_feed")
    ylive.get_live_quote = lambda s: None
    ylive.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", ylive)
    monkeypatch.setattr(main._rl, "would_block", lambda *a, **k: True)
    return cache, fallback, bh, boom


class TestClosedFlag:
    def test_off_switch(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setenv("QUOTE_CLOSED_SERVE_LAST_CLOSE", "0")
        assert main._quote_market_closed() is False

    def test_uses_feed_window(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.delenv("QUOTE_CLOSED_SERVE_LAST_CLOSE", raising=False)
        import market_hours
        monkeypatch.setattr(market_hours, "is_feed_window_ist", lambda *a, **k: False)
        assert main._quote_market_closed() is True
        monkeypatch.setattr(market_hours, "is_feed_window_ist", lambda *a, **k: True)
        assert main._quote_market_closed() is False


class TestSingleQuote:
    def test_stale_cached_row_is_served_as_last_close(self, harness):
        cache, _fb, _bh, boom = harness
        cache["quote:TCS.NS"] = _row("TCS", 3 * 3600, 4100.5)
        out = main._get_quote_inner("TCS")
        assert out["price"] == 4100.5
        assert boom["n"] == 0

    def test_fallback_row_served_without_angelone_or_yahoo(self, harness):
        _c, fb, _bh, boom = harness
        fb["quote:INFY.NS"] = _row("INFY", 2 * 3600, 1500.0)
        out = main._get_quote_inner("INFY")
        assert out["price"] == 1500.0 and out["source"] == "last_close_fallback"
        assert boom["n"] == 0

    def test_old_fallback_prefers_bhavcopy(self, harness):
        _c, fb, bh, boom = harness
        fb["quote:SBIN.NS"] = _row("SBIN", 200 * 3600, 700.0)
        bh["SBIN"] = 712.3
        out = main._get_quote_inner("SBIN")
        assert out["price"] == 712.3 and out["source"] == "bhavcopy_eod"
        assert boom["n"] == 0

    def test_old_fallback_used_when_bhavcopy_has_nothing(self, harness):
        _c, fb, _bh, boom = harness
        fb["quote:ITC.NS"] = _row("ITC", 200 * 3600, 450.0)
        out = main._get_quote_inner("ITC")
        assert out["price"] == 450.0 and out["source"] == "last_close_fallback"
        assert boom["n"] == 0

    def test_bhavcopy_only(self, harness):
        _c, _fb, bh, boom = harness
        bh["NEWCO"] = 55.5
        out = main._get_quote_inner("NEWCO")
        assert out["price"] == 55.5 and out["source"] == "bhavcopy_eod"
        assert boom["n"] == 0

    def test_nothing_known_falls_through_to_waterfall(self, harness):
        _c, _fb, _bh, boom = harness
        boom["yf_ret"] = {"price": 77.0, "source": "yahoo_clean"}
        out = main._get_quote_inner("UNKNOWNCO")      # no cache / fallback / bhavcopy: the normal waterfall runs
        assert out["price"] == 77.0 and boom["n"] >= 1

    def test_open_market_unchanged(self, harness, monkeypatch):
        _c, fb, _bh, boom = harness
        monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
        boom["yf_ret"] = {"price": 88.0, "source": "yahoo_clean"}
        fb["quote:INFY.NS"] = _row("INFY", 60, 1500.0)
        out = main._get_quote_inner("INFY")           # open: waterfall runs, last-close shortcut is not taken
        assert out["price"] == 88.0 and boom["n"] >= 1


class TestBulk:
    def test_closed_bulk_serves_any_age_and_skips_angelone_rest(self, harness):
        cache, fb, bh, boom = harness
        cache["quote:TCS.NS"] = _row("TCS", 5 * 3600, 4100.0)       # old cached row: served, not refreshed
        fb["quote:INFY.NS"] = _row("INFY", 3600, 1500.0)
        bh["SBIN"] = 712.0
        out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["TCS", "INFY", "SBIN"]), {})
        prices = {q["symbol"]: q["price"] for q in out["quotes"]}
        assert prices == {"TCS": 4100.0, "INFY": 1500.0, "SBIN": 712.0}
        assert boom["n"] == 0

    def test_closed_bulk_unknown_symbol_goes_on_to_live_path(self, harness):
        _c, _fb, _bh, _boom = harness
        out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["ZZZNONE"]), {})
        assert out["ok"] is False                 # nothing known anywhere; the (saturated-bucket) live path answered empty

    def test_open_bulk_still_refreshes_stale_rows(self, harness, monkeypatch):
        cache, _fb, _bh, _boom = harness
        monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
        cache["quote:TCS.NS"] = _row("TCS", 300, 4100.0)
        stale = {}
        main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["TCS"]), stale)
        assert "TCS" in stale                     # group193 behaviour kept while the market is open
