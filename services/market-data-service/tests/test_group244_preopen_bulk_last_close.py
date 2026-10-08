"""
group244: in the 09:05-09:15 pre-open, /quotes/bulk answers symbols that the live caches and AngelOne REST could not
price from the last close (cache / fallback / bhavcopy) instead of sending them to yf.download (18 s hard timeout in
the 2026-10-08 log). Names with no close known anywhere still go to yfinance.

Run from services/market-data-service:
    python3 -m pytest tests/test_group244_preopen_bulk_last_close.py -v
"""
from __future__ import annotations
import os, sys, types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import market_hours

IST = ZoneInfo("Asia/Kolkata")


def _row(sym, age_s, price=100.0, source="angelone_ws"):
    return {"symbol": sym, "price": price, "source": source,
            "fetched_at": (datetime.utcnow() - timedelta(seconds=age_s)).isoformat()}


@pytest.fixture()
def harness(monkeypatch):
    cache, fallback, bh = {}, {}, {}
    calls = {"yf": 0}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_get", lambda k: fallback.get(k))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper().replace('.NS', '')}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)      # pre-open counts as open for group233
    monkeypatch.setattr(main, "_quote_preopen", lambda: True)
    monkeypatch.setattr(main, "_waterfall_bhavcopy_price", lambda s: bh.get(str(s).upper().replace(".NS", "")))
    live = types.ModuleType("angelone_ws_feed")
    live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", live)
    ylive = types.ModuleType("yahoo_ws_feed")
    ylive.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", ylive)
    monkeypatch.setattr(main._rl, "would_block", lambda *a, **k: False)

    def _boom(*a, **k):
        calls["yf"] += 1
        raise RuntimeError("no Yahoo in tests")

    monkeypatch.setattr(main.yf, "download", _boom)
    return cache, fallback, bh, calls


def _req(*syms):
    return main.BulkQuoteRequest(symbols=list(syms))


class TestPreopenBulk:
    def test_unpriced_symbols_get_last_close_and_yfinance_is_not_called(self, harness):
        cache, fb, bh, calls = harness
        cache["quote:TCS.NS"] = _row("TCS", 5 * 3600, 4100.0)       # too old for 'fresh', still a valid last close
        fb["quote:INFY.NS"] = _row("INFY", 3600, 1500.0)
        bh["SBIN"] = 712.0
        out = main._get_quotes_bulk_core(_req("TCS", "INFY", "SBIN"), {})
        assert {q["symbol"]: q["price"] for q in out["quotes"]} == {"TCS": 4100.0, "INFY": 1500.0, "SBIN": 712.0}
        assert calls["yf"] == 0
        assert not out.get("degraded")

    def test_symbol_with_no_close_anywhere_still_goes_to_yfinance(self, harness):
        _c, _fb, bh, calls = harness
        bh["SBIN"] = 712.0
        out = main._get_quotes_bulk_core(_req("SBIN", "NEWLISTING"), {})
        assert [q["symbol"] for q in out["quotes"]] == ["SBIN"]
        assert calls["yf"] == 1                      # only NEWLISTING reached yf.download
        assert out["degraded"] is True

    def test_rows_keep_their_original_age_for_the_callers_freshness_check(self, harness):
        _c, fb, _bh, _calls = harness
        row = _row("INFY", 3 * 3600, 1500.0)
        fb["quote:INFY.NS"] = row
        out = main._get_quotes_bulk_core(_req("INFY"), {})
        assert out["quotes"][0]["fetched_at"] == row["fetched_at"]

    def test_not_preopen_keeps_the_yfinance_path(self, harness, monkeypatch):
        _c, fb, _bh, calls = harness
        monkeypatch.setattr(main, "_quote_preopen", lambda: False)
        fb["quote:INFY.NS"] = _row("INFY", 3600, 1500.0)
        out = main._get_quotes_bulk_core(_req("INFY"), {})
        assert calls["yf"] == 1 and out["quotes"] == []

    def test_index_symbols_are_left_for_yfinance(self, harness):
        _c, _fb, _bh, calls = harness
        main._get_quotes_bulk_core(_req("^NSEI"), {})
        assert calls["yf"] == 1


class TestPreopenFlag:
    def test_switch_off(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setenv("QUOTE_PREOPEN_SERVE_LAST_CLOSE", "0")
        assert main._quote_preopen() is False

    def test_needs_closed_serve_on(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setenv("QUOTE_CLOSED_SERVE_LAST_CLOSE", "0")
        monkeypatch.delenv("QUOTE_PREOPEN_SERVE_LAST_CLOSE", raising=False)
        assert main._quote_preopen() is False

    def test_follows_market_hours(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.delenv("QUOTE_CLOSED_SERVE_LAST_CLOSE", raising=False)
        monkeypatch.delenv("QUOTE_PREOPEN_SERVE_LAST_CLOSE", raising=False)
        monkeypatch.setattr(market_hours, "is_preopen_ist", lambda *a, **k: True)
        assert main._quote_preopen() is True
        monkeypatch.setattr(market_hours, "is_preopen_ist", lambda *a, **k: False)
        assert main._quote_preopen() is False


class TestIsPreopenIst:
    def _at(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(timezone.utc)

    def test_window_edges_on_a_trading_day(self):
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 9, 5)) is True
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 9, 14)) is True
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 9, 15)) is False
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 9, 4)) is False
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 14, 0)) is False

    def test_weekend_and_holiday_are_never_preopen(self):
        assert market_hours.is_preopen_ist(self._at(2026, 10, 10, 9, 10)) is False     # Saturday
        assert market_hours.is_preopen_ist(self._at(2026, 10, 20, 9, 10)) is False     # Dussehra

    def test_always_on_escape_hatch(self, monkeypatch):
        monkeypatch.setattr(market_hours, "_ALWAYS_ON", True)
        assert market_hours.is_preopen_ist(self._at(2026, 10, 8, 9, 10)) is False
