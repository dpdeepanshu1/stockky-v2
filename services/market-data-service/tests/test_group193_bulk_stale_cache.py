"""
group193: /quotes/bulk no longer hands back stale cached quote rows as if they were fresh.

On the 2026-10-06 boot real-trade-service logged "bulk-first priced 542/725 ... 183 left (older than limit 183)" and
then priced those 183 symbols one by one through GET /quote (a yfinance-backed worker thread each), which starved
market-data-service and produced the ReadTimeouts. Cause: /quotes/bulk returned any cached row with its ORIGINAL
fetched_at, so rows tens of seconds old reached the caller, which rejects anything older than 20 s.

Run from services/market-data-service:
    python3 -m pytest tests/test_group193_bulk_stale_cache.py -v
"""
from __future__ import annotations
import os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("BULK_CACHE_MAX_AGE_SEC", None)

import pytest

import main


def _row(sym, age_s, price=100.0):
    return {"symbol": sym, "price": price, "source": "cache_row",
            "fetched_at": (datetime.utcnow() - timedelta(seconds=age_s)).isoformat()}


@pytest.fixture()
def harness(monkeypatch):
    """Cache holds the rows we put in it; live feeds and AngelOne REST answer nothing; yfinance is 'saturated'
    so the core returns whatever it has without a download."""
    cache = {}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper()}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    import types
    live = types.ModuleType("angelone_ws_feed")
    live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", live)
    ylive = types.ModuleType("yahoo_ws_feed")
    ylive.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", ylive)
    monkeypatch.setattr(main._rl, "would_block", lambda *a, **k: True)
    return cache, live


class TestHelpers:
    def test_default_and_overrides(self, monkeypatch):
        monkeypatch.delenv("BULK_CACHE_MAX_AGE_SEC", raising=False)
        assert main._bulk_cache_max_age_s() == 15.0
        monkeypatch.setenv("BULK_CACHE_MAX_AGE_SEC", "30")
        assert main._bulk_cache_max_age_s() == 30.0
        for off in ("0", "-4"):
            monkeypatch.setenv("BULK_CACHE_MAX_AGE_SEC", off)
            assert main._bulk_cache_max_age_s() == 0.0
        for bad in ("abc", " ", ""):
            monkeypatch.setenv("BULK_CACHE_MAX_AGE_SEC", bad)
            assert main._bulk_cache_max_age_s() == 15.0

    def test_row_age(self):
        assert 59 <= main._quote_row_age_s(_row("A", 60)) <= 62
        assert main._quote_row_age_s({"fetched_at": "garbage"}) is None
        assert main._quote_row_age_s({}) is None
        assert main._quote_row_age_s(None) is None
        z = (datetime.utcnow() - timedelta(seconds=40)).isoformat() + "Z"
        assert 39 <= main._quote_row_age_s({"fetched_at": z}) <= 42
        future = (datetime.utcnow() + timedelta(seconds=90)).isoformat()
        assert main._quote_row_age_s({"fetched_at": future}) == 0.0


class TestBulkCore:
    def test_fresh_rows_served_stale_rows_sent_for_refresh(self, harness):
        cache, _ = harness
        cache["quote:FRESH.NS"] = _row("FRESH", 3)
        cache["quote:OLD.NS"] = _row("OLD", 90)
        stale = {}
        out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["FRESH", "OLD"]), stale)
        assert [q["symbol"] for q in out["quotes"]] == ["FRESH"]   # OLD was not handed back as if fresh
        assert list(stale) == ["OLD"]                               # ...but is remembered as a last resort

    def test_stale_row_refreshed_from_live_feed_is_not_duplicated(self, harness):
        cache, live = harness
        cache["quote:OLD.NS"] = _row("OLD", 90, price=100.0)
        live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {"OLD": {"price": 123.0, "source": "angelone_ws"}}
        out = main.get_quotes_bulk(main.BulkQuoteRequest(symbols=["OLD"]))
        qs = [q for q in out["quotes"] if q["symbol"] == "OLD"]
        assert len(qs) == 1 and qs[0]["price"] == 123.0
        assert "stale_served" not in out

    def test_stale_row_returned_when_every_source_fails(self, harness):
        cache, _ = harness
        cache["quote:OLD.NS"] = _row("OLD", 90, price=77.0)
        out = main.get_quotes_bulk(main.BulkQuoteRequest(symbols=["OLD"]))
        qs = [q for q in out["quotes"] if q["symbol"] == "OLD"]
        assert len(qs) == 1 and qs[0]["price"] == 77.0
        assert out["ok"] is True and out["stale_served"] == 1
        assert main._quote_row_age_s(qs[0]) >= 89            # original fetched_at kept: caller still sees its age

    def test_disabled_keeps_old_behaviour(self, harness, monkeypatch):
        cache, _ = harness
        monkeypatch.setenv("BULK_CACHE_MAX_AGE_SEC", "0")
        cache["quote:OLD.NS"] = _row("OLD", 900)
        stale = {}
        out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["OLD"]), stale)
        assert [q["symbol"] for q in out["quotes"]] == ["OLD"] and stale == {}

    def test_row_without_timestamp_is_served_as_before(self, harness):
        cache, _ = harness
        cache["quote:NOTS.NS"] = {"symbol": "NOTS", "price": 5.0}
        stale = {}
        out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=["NOTS"]), stale)
        assert [q["symbol"] for q in out["quotes"]] == ["NOTS"] and stale == {}

    def test_wrapper_survives_a_non_dict_core_result(self, harness, monkeypatch):
        monkeypatch.setattr(main, "_get_quotes_bulk_core", lambda req, st: (st.update({"X": {"symbol": "X"}}), "oops")[1])
        assert main.get_quotes_bulk(main.BulkQuoteRequest(symbols=["X"])) == "oops"

    def test_wrapper_fail_open_when_merging_raises(self, harness, monkeypatch):
        def core(req, st):
            st["X"] = {"symbol": "X", "price": 1}
            return {"ok": True, "quotes": [{"symbol": "Y"}]}
        monkeypatch.setattr(main, "_get_quotes_bulk_core", core)
        monkeypatch.setattr(main, "_sanitize_for_json", lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
        out = main.get_quotes_bulk(main.BulkQuoteRequest(symbols=["X", "Y"]))
        assert out["quotes"] == [{"symbol": "Y"}]
