"""
market_feed.feed fan-out controls (2026-09-21 post-redeploy incident):
  * get_quotes never has more than FEED_QUOTE_CONCURRENCY lookups in flight
  * the per-quote ATR /history refresh only fires when warranted (cold / past TTL),
    never once per quote per cycle, never twice concurrently for a symbol,
    and backs off after a failure
  * a slow upstream yields partial results at the batch deadline instead of a stalled cycle
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
        self.inflight = 0
        self.max_inflight = 0
        self.quote_delay = 0.02
        self.history_status = 200
        self.history_hits_by_sym = Counter()


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
        path = self.path.split("?")[0]
        kind = path.split("/")[1]
        with srv.lock:
            srv.hits[kind] += 1
            if kind == "quote":
                srv.inflight += 1
                srv.max_inflight = max(srv.max_inflight, srv.inflight)
            if kind == "history":
                srv.history_hits_by_sym[path.split("/")[2]] += 1
        try:
            if kind == "live-quote":
                return self._send(404, {"detail": "stale"})          # force Source 2 for every symbol
            if kind == "quote":
                time.sleep(srv.quote_delay)
                return self._send(200, {"price": 100.0, "source": "test"})
            if kind == "history":
                if srv.history_status != 200:
                    return self._send(srv.history_status, {"detail": "nope"})
                candles = [{"high": 105 + i % 3, "low": 95, "close": 100} for i in range(30)]
                return self._send(200, {"candles": candles})
            return self._send(404, {})
        finally:
            if kind == "quote":
                with srv.lock:
                    srv.inflight -= 1


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
    monkeypatch.setattr(f, "_store_atr", lambda sym, atr: f._ATR_CACHE.__setitem__(f._clean_sym(sym), atr))
    yield f, srv
    srv.shutdown()


def _run(coro):
    return asyncio.run(coro)


def test_concurrency_is_capped_and_all_symbols_resolve(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "FEED_QUOTE_CONCURRENCY", 10)
    syms = [f"S{i}" for i in range(200)]
    out = _run(f.get_quotes(syms))
    assert len(out) == 200
    assert srv.max_inflight <= 10, srv.max_inflight


def test_atr_history_fetched_once_per_symbol_not_once_per_quote_per_cycle(feed):
    f, srv = feed
    syms = [f"S{i}" for i in range(60)]

    async def cycles():
        for _ in range(5):                       # five evaluation cycles
            await f.get_quotes(syms)
            await asyncio.sleep(0.3)             # let background refreshes land

    _run(cycles())
    per_sym = srv.history_hits_by_sym
    assert max(per_sym.values()) == 1, per_sym.most_common(3)     # previously: 5 (one per cycle) per symbol
    assert srv.hits["history"] <= 60
    # ...and the cache is warming, gradually (bounded in-flight): symbols that
    # couldn't get a slot in one burst are simply picked up on a later cycle.
    warmed = sum(1 for s in syms if f._cached_atr(s))
    assert warmed >= f._ATR_MAX_INFLIGHT, warmed


def test_warm_atr_is_not_refetched_within_ttl(feed):
    f, srv = feed
    f._ATR_CACHE["S1"] = 3.3                     # e.g. warmed from the DB snapshot
    f._ATR_LAST_OK["S1"] = time.monotonic()
    _run(f.get_quotes(["S1"] * 1))
    time.sleep(0.2)
    assert srv.hits["history"] == 0


def test_failed_atr_refresh_backs_off(feed):
    f, srv = feed
    srv.history_status = 500

    async def cycles():
        for _ in range(4):
            await f.get_quotes(["BAD"])
            await asyncio.sleep(0.25)

    _run(cycles())
    assert srv.hits["history"] == 1              # not retried every cycle


def test_inflight_cap_process_wide(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "_ATR_MAX_INFLIGHT", 2)
    scheduled = []

    async def go():
        for i in range(10):
            scheduled.append(f._schedule_atr_refresh(None, f"C{i}"))
        await asyncio.sleep(0.5)

    _run(go())
    assert scheduled.count(True) == 2


def test_batch_deadline_returns_partial_results_on_slow_upstream(feed, monkeypatch):
    f, srv = feed
    srv.quote_delay = 0.3
    monkeypatch.setattr(f, "FEED_QUOTE_CONCURRENCY", 5)
    monkeypatch.setattr(f, "FEED_BATCH_DEADLINE_S", 1.0)
    t0 = time.time()
    out = _run(f.get_quotes([f"S{i}" for i in range(200)]))
    took = time.time() - t0
    assert 0 < len(out) < 200
    assert took < 4.0, took                       # would be 200/5*0.3 = 12s without the deadline


def test_preview_quotes_use_same_bounded_path(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "FEED_QUOTE_CONCURRENCY", 6)
    out = _run(f.get_preview_quotes([f"P{i}" for i in range(50)]))
    assert len(out) == 50
    assert srv.max_inflight <= 6
