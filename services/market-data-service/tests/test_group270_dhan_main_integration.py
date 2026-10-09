"""group270: the Dhan stages inside main.py - /quote, /quotes/bulk, /history and /internal/dhan-status.

The Dhan layer itself is faked (its own tests are in test_group270_dhan_core.py); what is checked here is WHERE the
stage sits for each provider order, that every failure falls through to the unchanged AngelOne/yfinance code, and
that results keep the shapes callers already rely on.

Run from services/market-data-service:   python3 -m pytest tests/test_group270_dhan_main_integration.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as m
import dhan_data
from dhan_data import client, history, quotes, scrip_master
from dhan_data.errors import DhanApiError, DhanNoDataError


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.setattr(client, "_breaker", lambda: None)
    client._reset_for_tests()
    scrip_master._set_for_tests({"TCS": 11536, "INFY": 1594}, {"NIFTY": 13})
    m._mem._d.clear()
    m._history_flights.clear()
    monkeypatch.setattr(m, "cache", None)
    monkeypatch.setattr(m, "_neg_blocked", lambda s: False)
    monkeypatch.setattr(m, "_angelone_rest_quote_first", lambda s: None)
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 60.0)
    monkeypatch.setattr(m, "_history_candle_cooling", lambda: False)
    monkeypatch.setattr(m, "_history_last_good_get", lambda k: None)
    monkeypatch.setattr(m, "_in_cooldown", lambda name="yfinance": False)      # earlier tests may leave a cooldown behind
    monkeypatch.setattr(m, "_quote_cooldown_cfg", lambda: (False, False, 0.0))
    yield
    m._mem._d.clear()
    m._history_flights.clear()
    client._reset_for_tests()


def _row(price=3500.0, **kw):
    r = {"symbol": "TCS", "price": price, "previous_close": price - 10, "day_change_pct": 0.29, "day_high": price + 5,
         "day_low": price - 5, "volume": 4242, "source": "dhan"}
    r.update(kw)
    return r


@pytest.fixture()
def dq(monkeypatch):
    """Fake Dhan quote layer: dq.rows = {key: row}; dq.single / dq.bulk record the calls."""
    class F:
        rows: dict = {}
        single: list = []
        bulk: list = []
    f = F()
    f.rows, f.single, f.bulk = {}, [], []

    def get_quote(sym, **kw):
        f.single.append(sym)
        return f.rows.get(quotes.key_for(sym))

    def get_quotes(syms, **kw):
        f.bulk.append(list(syms))
        return {quotes.key_for(s): f.rows[quotes.key_for(s)] for s in syms if quotes.key_for(s) in f.rows}
    monkeypatch.setattr(quotes, "get_quote", get_quote)
    monkeypatch.setattr(quotes, "get_quotes", get_quotes)
    return f


class _Boom:
    """Stand-in for a source that must NOT be consulted."""
    def __call__(self, *a, **k):
        raise AssertionError("this source should not have been asked")


# ── /quote ──────────────────────────────────────────────────────────────────────────────────────────────────────
class TestQuoteFirst:
    def test_dhan_first_answers_before_angelone(self, monkeypatch, dq):
        dq.rows["TCS"] = _row()
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", _Boom())
        r = m._get_quote_inner("TCS")
        assert r["source"] == "dhan" and r["price"] == 3500.0 and r["previous_close"] == 3490.0
        assert r["day_high"] == 3505.0 and r["volume"] == 4242 and r["symbol"] == "TCS.NS"   # same .NS form every source returns

    def test_result_is_cached_for_the_next_caller(self, dq):
        dq.rows["TCS"] = _row()
        m._get_quote_inner("TCS")
        assert m._cache_get("quote:TCS.NS")["source"] == "dhan"

    def test_dhan_miss_falls_through_to_angelone_ws(self, monkeypatch, dq):
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", lambda s: {"price": 3499.0, "source": "angelone"})
        r = m._get_quote_inner("TCS")
        assert r["source"] == "angelone_ws" and r["price"] == 3499.0 and dq.single == ["TCS.NS"]

    def test_dhan_exception_falls_through(self, monkeypatch):
        monkeypatch.setattr(quotes, "get_quote", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", lambda s: {"price": 10.0})
        assert m._get_quote_inner("TCS")["source"] == "angelone_ws"

    def test_off_when_disabled(self, monkeypatch, dq):
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", lambda s: {"price": 10.0})
        m._get_quote_inner("TCS")
        assert dq.single == []

    def test_closed_market_equity_skips_dhan_but_index_does_not(self, monkeypatch, dq):
        monkeypatch.setattr(m, "_quote_market_closed", lambda: True)
        monkeypatch.setattr(m, "_closed_last_close_row", lambda s: {"symbol": "TCS", "price": 3400.0, "source": "last_close"})
        dq.rows["TCS"] = _row()
        dq.rows["^NSEI"] = _row(24000.0, symbol="^NSEI")
        assert m._get_quote_inner("TCS")["source"] == "last_close" and dq.single == []
        r = m._get_quote_inner("^NSEI")
        assert r["source"] == "dhan" and r["price"] == 24000.0 and dq.single == ["^NSEI"]


class TestQuoteOtherPositions:
    def test_after_angelone_angelone_wins_when_it_has_a_price(self, monkeypatch, dq):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,dhan,yfinance")
        dq.rows["TCS"] = _row()
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", lambda s: {"price": 3499.0})
        assert m._get_quote_inner("TCS")["source"] == "angelone_ws" and dq.single == []

    def test_after_angelone_dhan_answers_when_angelone_has_nothing(self, monkeypatch, dq):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,dhan,yfinance")
        dq.rows["TCS"] = _row()
        monkeypatch.setattr(m, "_yahoo_ohlcv_quote", _Boom())
        assert m._get_quote_inner("TCS")["source"] == "dhan"

    def test_after_yfinance_only_when_both_yahoo_stages_fail(self, monkeypatch, dq):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,yfinance,dhan")
        dq.rows["TCS"] = _row()
        monkeypatch.setattr(m, "_yahoo_ohlcv_quote", lambda s: None)

        class T:
            def __init__(self, *a, **k): self.info = {}
        monkeypatch.setattr(m.yf, "Ticker", T)
        assert m._get_quote_inner("TCS")["source"] == "dhan"

    def test_after_yfinance_yahoo_price_wins(self, monkeypatch, dq):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,yfinance,dhan")
        dq.rows["TCS"] = _row()
        monkeypatch.setattr(m, "_yahoo_ohlcv_quote", lambda s: {"symbol": "TCS", "price": 3300.0})
        r = m._get_quote_inner("TCS")
        assert r["price"] == 3300.0 and dq.single == []


# ── /quotes/bulk ────────────────────────────────────────────────────────────────────────────────────────────────
class TestBulk:
    def _req(self, *syms):
        return m.BulkQuoteRequest(symbols=list(syms))

    def test_dhan_prices_everything_in_one_call_and_nothing_else_is_asked(self, monkeypatch, dq):
        dq.rows["TCS"] = _row(3500.0)
        dq.rows["INFY"] = _row(1500.0, symbol="INFY")
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quotes_bulk", _Boom())
        monkeypatch.setattr(m.yf, "download", _Boom())
        out = m._get_quotes_bulk_core(self._req("TCS", "INFY"), {})
        assert out["ok"] is True
        by = {q["symbol"]: q for q in out["quotes"]}
        assert by["TCS"]["price"] == 3500.0 and by["INFY"]["price"] == 1500.0
        assert {q["source"] for q in out["quotes"]} == {"dhan"}
        assert len(dq.bulk) == 1 and sorted(dq.bulk[0]) == ["INFY", "TCS"]

    def test_partial_answer_sends_the_rest_down_the_old_path(self, monkeypatch, dq):
        dq.rows["TCS"] = _row(3500.0)
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quotes_bulk",
                            lambda syms, max_age_sec=15.0: {"INFY": {"price": 1499.0, "source": "angelone_ws"}}
                            if "INFY" in syms else {})
        monkeypatch.setattr(m.yf, "download", _Boom())
        out = m._get_quotes_bulk_core(self._req("TCS", "INFY"), {})
        by = {q["symbol"]: q for q in out["quotes"]}
        assert by["TCS"]["source"] == "dhan" and by["INFY"]["price"] == 1499.0 and by["INFY"]["source"] == "angelone_ws"

    def test_dhan_failure_changes_nothing(self, monkeypatch, dq):
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quotes_bulk",
                            lambda syms, max_age_sec=15.0: {s: {"price": 10.0, "source": "angelone_ws"} for s in syms})
        out = m._get_quotes_bulk_core(self._req("TCS", "INFY"), {})
        assert {q["source"] for q in out["quotes"]} == {"angelone_ws"}

    def test_dhan_exception_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(quotes, "get_quotes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "get_live_quotes_bulk",
                            lambda syms, max_age_sec=15.0: {s: {"price": 10.0, "source": "angelone_ws"} for s in syms})
        assert m._get_quotes_bulk_core(self._req("TCS"), {})["quotes"][0]["source"] == "angelone_ws"

    def test_after_angelone_position_runs_after_the_live_feed(self, monkeypatch, dq):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,dhan,yfinance")
        dq.rows["TCS"] = _row()
        import angelone_ws_feed, angelone_client
        monkeypatch.setattr(angelone_ws_feed, "get_live_quotes_bulk", lambda syms, max_age_sec=15.0: {})
        monkeypatch.setattr(angelone_client, "get_session", lambda: type("S", (), {"is_configured": lambda s: False})())
        monkeypatch.setattr(m.yf, "download", _Boom())
        out = m._get_quotes_bulk_core(self._req("TCS"), {})
        assert out["quotes"][0]["source"] == "dhan"

    def test_stage_helper_maps_index_symbols(self, dq):
        dq.rows["^NSEI"] = _row(24000.0, symbol="^NSEI")
        res = []
        left = m._dhan_bulk_stage(["^NSEI", "ZZZ"], {"^NSEI": "^NSEI", "ZZZ": "ZZZ"}, res)
        assert left == ["ZZZ"] and res[0]["price"] == 24000.0


# ── /history ────────────────────────────────────────────────────────────────────────────────────────────────────
def _candles(n=372):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return [{"date": f"{(today - timedelta(days=b)).isoformat()} 00:00", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 10} for b in range(n, -1, -1)]


@pytest.fixture()
def dh(monkeypatch):
    class F:
        calls: list = []
        exc = None
    f = F()
    f.calls = []

    def fake(symbol, interval, frm, to):
        f.calls.append((symbol, interval, frm, to))
        if f.exc:
            raise f.exc
        return _candles(400)
    monkeypatch.setattr(history, "fetch_candles", fake)
    return f


class TestHistory:
    def test_dhan_first_four_short_periods_cost_one_dhan_call(self, monkeypatch, dh):
        monkeypatch.setattr(m, "_angelone_history_candles", _Boom())
        out = {p: m._get_history_impl("TCS", p, "1d", False, None) for p in ("5d", "1mo", "3mo", "6mo")}
        assert len(dh.calls) == 1 and dh.calls[0][1] == "1d"
        assert all(out[p]["candles"] for p in out)
        assert len(out["5d"]["candles"]) < len(out["1mo"]["candles"]) < len(out["6mo"]["candles"])
        assert m._cache_get("history:TCS.NS:1y:1d:")["source"] == "dhan"

    def test_result_shape_matches_other_sources(self, dh):
        r = m._get_history_impl("TCS", "1y", "1d", False, None)
        assert r["source"] == "dhan" and r["period"] == "1y" and r["interval"] == "1d" and r["symbol"] == "TCS.NS"
        assert set(r["candles"][0]) == {"date", "open", "high", "low", "close", "volume"}
        assert len(r["candles"]) <= m.MAX_HISTORY_ROWS

    def test_dhan_failure_falls_through_to_angelone(self, monkeypatch, dh):
        dh.exc = DhanApiError("boom")
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _candles(200))
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        r = m._get_history_impl("TCS", "6mo", "1d", False, None)
        assert r["source"] == "angelone"

    def test_unknown_symbol_never_calls_dhan(self, monkeypatch, dh):
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _candles(200))
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        r = m._get_history_impl("NOPE", "6mo", "1d", False, None)
        assert dh.calls == [] and r["source"] == "angelone"

    def test_no_data_error_falls_through_without_pausing_dhan(self, monkeypatch, dh):
        dh.exc = DhanNoDataError("x")
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _candles(200))
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        assert m._get_history_impl("TCS", "6mo", "1d", False, None)["source"] == "angelone"
        assert client.available() is True

    def test_weekly_interval_goes_to_dhan_as_1wk(self, dh):
        m._get_history_impl("TCS", "1y", "1wk", False, None)
        assert dh.calls[0][1] == "1wk"

    def test_hourly_interval_and_window_cap(self, dh):
        m._get_history_impl("TCS", "6mo", "1h", False, None)
        sym, iv, frm, to = dh.calls[0]
        assert iv == "1h" and (to - frm).days <= 61

    def test_unsupported_interval_skips_dhan(self, monkeypatch, dh):
        class T:
            def __init__(self, *a, **k): pass
            def history(self, **k): return None
        monkeypatch.setattr(m.yf, "Ticker", T)
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: None)
        monkeypatch.setattr(m, "_nse_history_candles", lambda *a, **k: None)
        try:
            m._get_history_impl("TCS", "1y", "1mo", False, None)
        except Exception:
            pass
        assert dh.calls == []

    def test_days_window_uses_exact_start_and_end(self, dh):
        m._get_history_impl("TCS", "1mo", "1d", False, 20)
        _, iv, frm, to = dh.calls[0]
        assert (to - frm).days == 20

    def test_after_angelone_dhan_is_not_called_when_angelone_has_data(self, monkeypatch, dh):
        monkeypatch.setenv("HISTORY_PROVIDER_ORDER", "angelone,dhan,yfinance")
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _candles(200))
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        assert m._get_history_impl("TCS", "6mo", "1d", False, None)["source"] == "angelone" and dh.calls == []

    def test_after_angelone_dhan_answers_when_angelone_is_empty(self, monkeypatch, dh):
        monkeypatch.setenv("HISTORY_PROVIDER_ORDER", "angelone,dhan,yfinance")
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: None)
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        assert m._get_history_impl("TCS", "6mo", "1d", False, None)["source"] == "dhan"

    def test_second_identical_request_is_a_cache_hit(self, dh):
        m._get_history_impl("TCS", "1y", "1d", False, None)
        m._get_history_impl("TCS", "1y", "1d", False, None)
        assert len(dh.calls) == 1

    def test_index_history(self, dh):
        r = m._get_history_impl("^NSEI", "1y", "1d", False, None)
        assert dh.calls[0][0] == "^NSEI" and r["source"] == "dhan"

    def test_disabled_never_calls_dhan(self, monkeypatch, dh):
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        monkeypatch.setattr(m, "_angelone_history_candles", lambda *a, **k: _candles(200))
        monkeypatch.setattr(m, "_history_widen_on", lambda: False)
        m._get_history_impl("TCS", "6mo", "1d", False, None)
        assert dh.calls == []


# ── status route / startup ──────────────────────────────────────────────────────────────────────────────────────
class TestStatusRoute:
    def test_route_returns_health_without_secrets(self, monkeypatch):
        from fastapi.testclient import TestClient
        monkeypatch.setenv("DHAN_CREDENTIAL_ENC_KEY", "k" * 44)
        body = TestClient(m.app).get("/internal/dhan-status").json()
        for k in ("enabled", "quote_order", "history_order", "quote_position", "credentials", "scrip_master",
                  "client", "quotes", "live_poller", "live_store", "websocket"):
            assert k in body
        assert body["quote_position"] == "first" and body["scrip_master"]["equities"] == 2
        assert "k" * 44 not in str(body)

    def test_universe_getter_never_raises(self, monkeypatch):
        monkeypatch.setattr(m, "_current_feed_universe", ["TCS", "INFY"])
        assert m._dhan_live_universe() == ["TCS", "INFY"]
        monkeypatch.setattr(m, "_current_feed_universe", [])
        assert isinstance(m._dhan_live_universe(), list)

    def test_start_background_is_idempotent_and_non_blocking(self, monkeypatch):
        monkeypatch.setattr(dhan_data, "_started", False)
        monkeypatch.setenv("DHAN_LIVE_POLLER", "0")
        t0 = datetime.now()
        dhan_data.start_background(lambda: [])
        dhan_data.start_background(lambda: [])
        assert (datetime.now() - t0).total_seconds() < 1.0
