"""
group152 (log-audit 2026-10-05, items 1 + 2): market_feed.feed priority lane + bulk-first batches.

At the open the five REAL positions' /live-quote and /quote calls all timed out on every 8s exit cycle while a
~923-symbol one-request-per-symbol batch hit its 45s deadline. Pinned here:
  * get_quotes(..., priority=True) uses scaled timeouts and falls back to POST /quotes/bulk for any symbol the
    per-symbol cascade could not price (and returns a Tick for it)
  * a large default batch is priced with chunked /quotes/bulk first; only symbols bulk could not price fresh
    go through the per-symbol path
  * bulk items that are stale (older than FEED_BULK_MAX_AGE_S), unparseable or have no price are ignored
  * a failing /quotes/bulk degrades to the old per-symbol path instead of losing the batch
"""
import asyncio
import http.server
import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.hits = Counter()
        self.bulk_chunks = []
        self.single_delay = 0.0          # delay for /live-quote and /quote
        self.bulk_status = 200
        self.bulk_age_s = 0.0            # how old fetched_at is on bulk items
        self.bulk_skip = set()           # symbols bulk does not return
        self.quote_status = 200


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
        if kind in ("live-quote", "quote"):
            time.sleep(srv.single_delay)
        if kind == "live-quote":
            return self._send(404, {"detail": "stale"})
        if kind == "quote":
            if srv.quote_status != 200:
                return self._send(srv.quote_status, {"detail": "boom"})
            return self._send(200, {"price": 50.0, "source": "single"})
        if kind == "history":
            candles = [{"high": 105, "low": 95, "close": 100} for _ in range(30)]
            return self._send(200, {"candles": candles})
        return self._send(404, {})

    def do_POST(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with srv.lock:
            srv.hits["bulk"] += 1
            srv.bulk_chunks.append(list(body.get("symbols") or []))
        if srv.bulk_status != 200:
            return self._send(srv.bulk_status, {"detail": "bulk down"})
        ts = (datetime.now(timezone.utc) - timedelta(seconds=srv.bulk_age_s)).replace(tzinfo=None).isoformat()
        quotes = [
            {"symbol": s, "price": 77.0, "source": "angelone", "fetched_at": ts, "volume": 1000,
             "day_high": 80.0, "day_low": 70.0}
            for s in body.get("symbols") or [] if s not in srv.bulk_skip
        ]
        return self._send(200, {"ok": True, "quotes": quotes})


@pytest.fixture()
def feed(monkeypatch):
    srv = _Upstream()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MARKET_DATA_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    # group171: these tests pin the per-symbol cascade and the /quotes/bulk fallback, so bulk-first for the
    # priority lane (and the share cache) is off here; tests/test_group171_held_quote_calls.py covers those.
    monkeypatch.setenv("FEED_PRIORITY_BULK_FIRST", "0")
    monkeypatch.setenv("FEED_PRIORITY_SHARE_S", "0")
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


# ── bulk-first for large batches ────────────────────────────────────────────

def test_large_batch_is_priced_by_bulk_not_one_request_per_symbol(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "FEED_BULK_CHUNK_SIZE", 100)
    syms = [f"S{i}" for i in range(250)]
    out = _run(f.get_quotes(syms))
    assert len(out) == 250
    assert srv.hits["bulk"] == 3                      # 100 + 100 + 50
    assert srv.hits["quote"] == 0 and srv.hits["live-quote"] == 0
    t = out["S7"]
    assert t.price == 77.0 and t.source == "bulk(angelone)" and t.day_high == 80.0 and t.volume == 1000


def test_small_batch_keeps_the_per_symbol_path(feed):
    f, srv = feed
    out = _run(f.get_quotes([f"S{i}" for i in range(5)]))
    assert len(out) == 5
    assert srv.hits["bulk"] == 0
    assert srv.hits["quote"] == 5


def test_symbols_bulk_cannot_price_fall_back_per_symbol(feed, monkeypatch):
    f, srv = feed
    srv.bulk_skip = {"S3", "S4"}
    out = _run(f.get_quotes([f"S{i}" for i in range(40)]))
    assert len(out) == 40
    assert out["S3"].price == 50.0 and out["S3"].source == "single"
    assert out["S10"].price == 77.0
    assert srv.hits["quote"] == 2


def test_stale_bulk_items_are_ignored(feed, monkeypatch):
    f, srv = feed
    srv.bulk_age_s = 120.0                            # older than FEED_BULK_MAX_AGE_S (20s)
    out = _run(f.get_quotes([f"S{i}" for i in range(30)]))
    assert len(out) == 30
    assert all(t.price == 50.0 for t in out.values())  # all priced by the per-symbol path instead
    assert srv.hits["quote"] == 30


def test_failing_bulk_degrades_to_per_symbol_path(feed):
    f, srv = feed
    srv.bulk_status = 500
    out = _run(f.get_quotes([f"S{i}" for i in range(30)]))
    assert len(out) == 30
    assert srv.hits["quote"] == 30


def test_tick_from_bulk_item_rejects_bad_rows(feed):
    f, _ = feed
    fresh = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert f._tick_from_bulk_item({"symbol": "A", "price": 10, "fetched_at": fresh}).price == 10.0
    assert f._tick_from_bulk_item({"symbol": "A", "price": None, "fetched_at": fresh}) is None
    assert f._tick_from_bulk_item({"symbol": "A", "price": 0, "fetched_at": fresh}) is None
    assert f._tick_from_bulk_item({"symbol": "A", "price": 10}) is None            # no timestamp
    assert f._tick_from_bulk_item({"symbol": "A", "price": 10, "fetched_at": "garbage"}) is None
    assert f._tick_from_bulk_item({"symbol": "", "price": 10, "fetched_at": fresh}) is None
    assert f._tick_from_bulk_item("nope") is None
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).replace(tzinfo=None).isoformat()
    assert f._tick_from_bulk_item({"symbol": "A", "price": 10, "fetched_at": old}) is None


# ── priority lane (open positions) ──────────────────────────────────────────

def test_priority_lane_uses_per_symbol_path_when_it_works(feed):
    f, srv = feed
    out = _run(f.get_quotes(["P1", "P2"], priority=True))
    assert set(out) == {"P1", "P2"}
    assert srv.hits["bulk"] == 0


def test_priority_lane_recovers_timed_out_symbols_through_bulk(feed, monkeypatch):
    f, srv = feed
    srv.single_delay = 0.6                            # per-symbol answers arrive after the (scaled) timeouts
    # make both single-symbol timeouts shorter than the upstream delay
    orig_get_quote = f.get_quote

    async def _tight(client, symbol, **kw):
        kw["timeout_scale"] = 0.05                    # 0.15s / 0.4s
        return await orig_get_quote(client, symbol, **kw)

    monkeypatch.setattr(f, "get_quote", _tight)
    out = _run(f.get_quotes(["GUJALKALI", "MARINE"], priority=True))
    assert set(out) == {"GUJALKALI", "MARINE"}
    assert out["MARINE"].price == 77.0 and out["MARINE"].source.startswith("bulk(")
    assert srv.hits["bulk"] == 1


def test_priority_lane_returns_what_it_can_when_bulk_is_down_too(feed, monkeypatch):
    f, srv = feed
    srv.quote_status = 500
    srv.bulk_status = 500
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert out == {}


def test_priority_lane_maps_bulk_result_back_to_requested_spelling(feed):
    f, srv = feed
    srv.quote_status = 500                            # force the bulk fallback
    out = _run(f.get_quotes(["marine.NS"], priority=True))
    assert list(out) == ["marine.NS"]
    assert out["marine.NS"].price == 77.0


def test_get_quote_timeout_scale_defaults_to_one(feed):
    f, _ = feed
    import inspect
    assert inspect.signature(f.get_quote).parameters["timeout_scale"].default == 1.0


def test_exit_evaluation_requests_the_priority_lane():
    src = open(os.path.join(os.path.dirname(__file__), "..", "exit_engine", "exit.py")).read()
    assert "get_quotes(symbols, priority=True)" in src
