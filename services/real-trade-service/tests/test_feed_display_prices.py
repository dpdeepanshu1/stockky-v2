"""
Dashboard price path (2026-10-04, items 3 & 15 — Real Trade tab fan-out):
  * Positions / Orders / Candidates share ONE short-TTL display-price cache;
    concurrent requests for overlapping symbols fetch each symbol once
  * the display path never triggers ATR /history refreshes (AngelOne candle limit)
  * while the market is closed the stale-tick /live-quote lookup is skipped
  * unquotable symbols are remembered briefly (no re-request every poll)
  * trading callers (get_quote default) are untouched: still live-quote first
"""
import asyncio
import http.server
import json
import os
import sys
import threading
import time
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.hits = Counter()
        self.quote_hits_by_sym = Counter()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        srv = self.server
        parts = self.path.split("?")[0].split("/")
        kind, sym = parts[1], (parts[2] if len(parts) > 2 else "")
        with srv.lock:
            srv.hits[kind] += 1
            if kind == "quote":
                srv.quote_hits_by_sym[sym] += 1
        if kind == "live-quote":
            return self._send(404, {"detail": "stale"})
        if kind == "quote":
            if sym == "BAD":
                return self._send(404, {"detail": "unknown"})
            return self._send(200, {"price": 100.0, "source": "test"})
        if kind == "history":
            candles = [{"high": 105 + i % 3, "low": 95, "close": 100} for i in range(30)]
            return self._send(200, {"candles": candles})
        return self._send(404, {})


@pytest.fixture()
def feed(monkeypatch):
    srv = _Upstream()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MARKET_DATA_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    import importlib
    import market_feed.feed as f
    f = importlib.reload(f)
    monkeypatch.setattr(f, "_ATR_CACHE", {})
    f._ATR_INFLIGHT.clear(); f._ATR_LAST_TRY.clear(); f._ATR_LAST_OK.clear()
    f.clear_display_price_cache()
    monkeypatch.setattr(f, "_store_atr", lambda sym, atr: f._ATR_CACHE.__setitem__(f._clean_sym(sym), atr))
    yield f, srv
    srv.shutdown()


def _run(coro):
    return asyncio.run(coro)


def test_three_tabs_polling_together_fetch_each_symbol_once(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    positions = [f"P{i}" for i in range(10)]
    candidates = [f"C{i}" for i in range(25)] + positions[:5]     # overlaps positions
    orders = positions[:3] + ["C1", "C2"]                          # overlaps both

    async def poll():
        return await asyncio.gather(
            f.get_display_prices(positions),
            f.get_display_prices(candidates),
            f.get_display_prices(orders),
        )

    a, b, c = _run(poll())
    assert set(a) == set(positions) and set(c) == set(orders)
    assert len(b) == len(set(candidates))
    assert max(srv.quote_hits_by_sym.values()) == 1, srv.quote_hits_by_sym.most_common(3)
    assert srv.hits["quote"] == len(set(positions + candidates + orders))
    assert srv.hits["history"] == 0
    assert srv.hits["live-quote"] == 0          # market closed: stale-tick lookup skipped


def test_market_open_still_tries_live_quote_but_never_history(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: True)
    out = _run(f.get_display_prices([f"S{i}" for i in range(12)]))
    time.sleep(0.2)
    assert len(out) == 12
    assert srv.hits["live-quote"] == 12
    assert srv.hits["history"] == 0, "display path must not schedule ATR refreshes"


def test_repeat_poll_within_ttl_makes_no_upstream_calls(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    syms = ["A", "B", "C"]
    _run(f.get_display_prices(syms))
    before = srv.hits["quote"]
    out = _run(f.get_display_prices(syms))
    assert out == {"A": 100.0, "B": 100.0, "C": 100.0}
    assert srv.hits["quote"] == before


def test_expired_entries_are_refetched(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    monkeypatch.setattr(f, "DISPLAY_PRICE_TTL_CLOSED_S", 0.0)
    _run(f.get_display_prices(["A"]))
    time.sleep(0.01)
    _run(f.get_display_prices(["A"]))
    assert srv.quote_hits_by_sym["A"] == 2


def test_unquotable_symbol_is_remembered_briefly(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    out1 = _run(f.get_display_prices(["BAD", "OK"]))
    out2 = _run(f.get_display_prices(["BAD", "OK"]))
    assert out1 == out2 == {"OK": 100.0}
    assert srv.quote_hits_by_sym["BAD"] == 1


def test_miss_memory_expires(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    monkeypatch.setattr(f, "DISPLAY_PRICE_MISS_TTL_S", 0.0)
    _run(f.get_display_prices(["BAD"]))
    time.sleep(0.01)
    _run(f.get_display_prices(["BAD"]))
    assert srv.quote_hits_by_sym["BAD"] == 2


def test_empty_and_duplicate_inputs(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    assert _run(f.get_display_prices([])) == {}
    out = _run(f.get_display_prices(["A", "A", "A"]))
    assert out == {"A": 100.0} and srv.quote_hits_by_sym["A"] == 1


def test_trading_path_unchanged_still_live_quote_first_and_atr_refresh(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)   # must NOT matter for non-display callers
    out = _run(f.get_quotes(["T1"]))
    time.sleep(0.3)
    assert "T1" in out
    assert srv.hits["live-quote"] == 1
    assert srv.hits["history"] == 1


def test_market_open_helper_fails_open(monkeypatch):
    import market_feed.feed as f
    import tz_utils
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert f._market_open_now() is True


def test_display_cache_is_per_clean_symbol(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_market_open_now", lambda: False)
    _run(f.get_display_prices(["INFY"]))
    out = _run(f.get_display_prices(["INFY.NS"]))     # same stock, different spelling
    assert out == {"INFY.NS": 100.0}
    assert srv.hits["quote"] == 1
