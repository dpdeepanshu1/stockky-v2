"""
group233 (real-trade-service): get_preview_quotes no longer sends GET /quote/{sym} + GET /last-close/{sym} for every
symbol. Order now: chunked POST /quotes/bulk (any age) -> GET /last-close/{sym} for the leftovers -> GET /quote/{sym}
for at most FEED_PREVIEW_QUOTE_FALLBACK_MAX of what is still unpriced.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group233_preview_bulk_first.py -v
"""
import asyncio
import http.server
import json
import os
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.hits = Counter()
        self.bulk_prices = {}        # symbol -> price answered by POST /quotes/bulk
        self.bulk_age_s = 6 * 3600   # a closed-market last-close row: hours old
        self.bulk_status = 200
        self.last_close = {}         # symbol -> price answered by GET /last-close/{sym}
        self.quote = {}              # symbol -> price answered by GET /quote/{sym}
        self.bulk_posts = []


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

    def do_POST(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with srv.lock:
            srv.hits["bulk"] += 1
            srv.bulk_posts.append(list(body.get("symbols") or []))
        if srv.bulk_status != 200:
            return self._send(srv.bulk_status, {})
        ts = (datetime.utcnow() - timedelta(seconds=srv.bulk_age_s)).isoformat()
        rows = [{"symbol": s, "price": srv.bulk_prices[s], "fetched_at": ts, "source": "last_close_cache"}
                for s in body.get("symbols") or [] if s in srv.bulk_prices]
        return self._send(200, {"quotes": rows})

    def do_GET(self):
        srv = self.server
        parts = self.path.split("?")[0].split("/")
        kind, sym = parts[1], (parts[2] if len(parts) > 2 else "")
        with srv.lock:
            srv.hits[kind] += 1
        if kind == "last-close" and sym in srv.last_close:
            return self._send(200, {"symbol": sym, "price": srv.last_close[sym]})
        if kind == "quote" and sym in srv.quote:
            return self._send(200, {"price": srv.quote[sym]})
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
    yield f, srv
    srv.shutdown()


def _run(coro):
    return asyncio.run(coro)


def test_bulk_prices_old_rows_and_no_per_symbol_calls(feed):
    f, srv = feed
    syms = [f"S{i}" for i in range(40)]
    srv.bulk_prices = {s: 10.0 + i for i, s in enumerate(syms)}
    out = _run(f.get_preview_quotes(syms))
    assert len(out) == 40 and out["S3"].price == 13.0
    assert out["S3"].source == "preview:last_close"
    assert srv.hits["quote"] == 0 and srv.hits["last-close"] == 0      # 6 h old bulk rows were accepted
    assert srv.hits["bulk"] >= 1


def test_leftovers_use_last_close_not_quote(feed):
    f, srv = feed
    srv.bulk_prices = {"A": 1.0}
    srv.last_close = {"B": 2.0, "C": 3.0}
    out = _run(f.get_preview_quotes(["A", "B", "C"]))
    assert {k: v.price for k, v in out.items()} == {"A": 1.0, "B": 2.0, "C": 3.0}
    assert srv.hits["last-close"] == 2 and srv.hits["quote"] == 0


def test_quote_fallback_is_capped(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "FEED_PREVIEW_QUOTE_FALLBACK_MAX", 3)
    syms = [f"N{i}" for i in range(20)]
    srv.quote = {s: 5.0 for s in syms}               # only GET /quote knows them
    out = _run(f.get_preview_quotes(syms))
    assert len(out) == 3 and srv.hits["quote"] == 3


def test_quote_fallback_can_be_switched_off(feed, monkeypatch):
    f, srv = feed
    monkeypatch.setattr(f, "FEED_PREVIEW_QUOTE_FALLBACK_MAX", 0)
    srv.quote = {"X": 5.0}
    assert _run(f.get_preview_quotes(["X"])) == {} and srv.hits["quote"] == 0


def test_failed_bulk_falls_back_to_last_close(feed):
    f, srv = feed
    srv.bulk_status = 503
    srv.last_close = {"A": 9.0}
    out = _run(f.get_preview_quotes(["A"]))
    assert out["A"].price == 9.0 and srv.hits["quote"] == 0


def test_duplicate_and_blank_symbols_are_requested_once(feed):
    f, srv = feed
    srv.bulk_prices = {"A": 1.0}
    out = _run(f.get_preview_quotes(["A", "A", ""]))
    assert list(out) == ["A"] and srv.bulk_posts == [["A"]]


def test_empty_input(feed):
    f, srv = feed
    assert _run(f.get_preview_quotes([])) == {} and not srv.hits
