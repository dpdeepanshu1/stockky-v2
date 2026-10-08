"""group242: the /surprise/scan/stream route primes the engine with one chunked POST /quotes/bulk pass.

Engine-side tests (prime_bulk_ticks + _prefetch_bulk store=); the route side lives in
test_main_surprise_routes.py (test_group242_*). No network: a fake httpx-style client records every call.
Run from services/api-gateway:
    python3 -m pytest tests/test_group242_surprise_stream_bulk_prime.py -q
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD = os.path.join(os.path.dirname(_HERE), "surprise_scanner.py")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sc(monkeypatch):
    for k in ("SURPRISE_BULK_PREFETCH", "SURPRISE_BULK_CHUNK", "SURPRISE_BULK_TIMEOUT",
              "SURPRISE_BULK_CONCURRENCY", "SURPRISE_BULK_MAX_AGE_SEC"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("surprise_scanner_g242", _MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh(age_s=1.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()


def row(sym, price=100.0):
    return {"symbol": sym, "price": price, "day_high": price + 1, "day_low": price - 1,
            "volume": 5000, "fetched_at": _fresh()}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class Client:
    def __init__(self, rows=None, exc=None):
        self.rows = rows or {}
        self.exc = exc
        self.posts = []
        self.gets = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append(list(json["symbols"]))
        if self.exc is not None:
            raise self.exc
        return Resp(200, {"quotes": [self.rows[s] for s in json["symbols"] if s in self.rows]})

    async def get(self, url, timeout=None):
        self.gets.append(url)
        return Resp(404, {})


class TestPrefetchStore:
    def test_store_receives_ticks_and_the_engine_dict_is_untouched(self, sc):
        e = sc.SurpriseStockEngine()
        e._bulk_ticks = {"KEEP": {"price": 1}}
        store = {}
        n = run(e._prefetch_bulk(Client({"AAA": row("AAA")}), "http://md", ["AAA"], store=store))
        assert n == 1 and set(store) == {"AAA"} and e._bulk_ticks == {"KEEP": {"price": 1}}

    def test_symbols_already_in_the_store_are_not_asked_again(self, sc):
        e = sc.SurpriseStockEngine()
        c = Client({"AAA": row("AAA"), "BBB": row("BBB")})
        run(e._prefetch_bulk(c, "http://md", ["AAA", "BBB"], store={"AAA": {"price": 9}}))
        assert c.posts == [["BBB"]]

    def test_default_still_fills_the_engine_dict(self, sc):
        e = sc.SurpriseStockEngine()
        run(e._prefetch_bulk(Client({"AAA": row("AAA")}), "http://md", ["AAA"]))
        assert set(e._bulk_ticks) == {"AAA"}


class TestPrimeBulkTicks:
    def test_primes_and_fetch_quote_then_makes_no_per_symbol_call(self, sc):
        e = sc.SurpriseStockEngine()
        c = Client({s: row(s) for s in ("AAA", "BBB")})
        assert run(e.prime_bulk_ticks(c, "http://md", ["AAA", "BBB"])) == 2
        t = run(e._fetch_quote(c, "http://md", "AAA"))
        assert t and t["price"] == 100.0 and c.gets == []

    def test_chunks_a_big_universe(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "BULK_CHUNK", 100)
        e = sc.SurpriseStockEngine()
        syms = [f"S{i}" for i in range(1000)]
        c = Client({s: row(s) for s in syms})
        assert run(e.prime_bulk_ticks(c, "http://md", syms)) == 1000
        assert len(c.posts) == 10 and c.gets == []

    def test_old_tick_for_a_symbol_bulk_missed_is_dropped_not_served(self, sc):
        e = sc.SurpriseStockEngine()
        e._bulk_ticks = {"AAA": {"price": 1.0}}
        c = Client({})                       # bulk answers nothing for AAA
        run(e.prime_bulk_ticks(c, "http://md", ["AAA"]))
        assert "AAA" not in e._bulk_ticks
        run(e._fetch_quote(c, "http://md", "AAA"))
        assert c.gets == ["http://md/quote/AAA"]   # falls back to the per-symbol path

    def test_other_symbols_ticks_are_left_alone(self, sc):
        e = sc.SurpriseStockEngine()
        e._bulk_ticks = {"OTHER": {"price": 7.0}}
        run(e.prime_bulk_ticks(Client({"AAA": row("AAA")}), "http://md", ["AAA"]))
        assert e._bulk_ticks["OTHER"] == {"price": 7.0} and "AAA" in e._bulk_ticks

    def test_names_are_normalised(self, sc):
        e = sc.SurpriseStockEngine()
        run(e.prime_bulk_ticks(Client({"AAA": row("AAA")}), "http://md", ["aaa.ns"]))
        assert "AAA" in e._bulk_ticks

    def test_failure_never_raises_and_returns_zero(self, sc):
        e = sc.SurpriseStockEngine()
        assert run(e.prime_bulk_ticks(Client(exc=RuntimeError("down")), "http://md", ["AAA"])) == 0

    def test_prefetch_blowing_up_is_swallowed(self, sc, monkeypatch):
        e = sc.SurpriseStockEngine()

        async def boom(*a, **k):
            raise ValueError("x")
        monkeypatch.setattr(e, "_prefetch_bulk", boom)
        assert run(e.prime_bulk_ticks(Client(), "http://md", ["AAA"])) == 0

    def test_switched_off_does_nothing(self, sc, monkeypatch):
        monkeypatch.setattr(sc, "BULK_PREFETCH", False)
        e = sc.SurpriseStockEngine()
        c = Client({"AAA": row("AAA")})
        assert run(e.prime_bulk_ticks(c, "http://md", ["AAA"])) == 0 and c.posts == []
