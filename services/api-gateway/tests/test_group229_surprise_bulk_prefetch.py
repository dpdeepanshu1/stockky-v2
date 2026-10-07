"""group229: surprise scan prices the universe with chunked POST /quotes/bulk before any per-symbol /quote.

No network: a fake httpx-style client records every call. Run from services/api-gateway:
    python3 -m pytest tests/test_group229_surprise_bulk_prefetch.py -q
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


def _load(env=None, monkeypatch=None, name="surprise_scanner_g229"):
    for k in ("SURPRISE_BULK_PREFETCH", "SURPRISE_BULK_CHUNK", "SURPRISE_BULK_TIMEOUT",
              "SURPRISE_BULK_CONCURRENCY", "SURPRISE_BULK_MAX_AGE_SEC"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(name, _MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sc(monkeypatch):
    return _load(monkeypatch=monkeypatch)


def _fresh(age_s=1.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()


def row(sym, price=100.0, **kw):
    d = {"symbol": sym, "price": price, "day_high": price + 1, "day_low": price - 1,
         "volume": 5000, "fetched_at": _fresh()}
    d.update(kw)
    return d


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class BulkClient:
    """post() answers from `rows` (by symbol) or raises; get() records per-symbol /quote calls."""

    def __init__(self, rows=None, status=200, exc=None, body=None):
        self.rows = rows or {}
        self.status = status
        self.exc = exc
        self.body = body
        self.posts = []
        self.gets = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url, list(json["symbols"]), timeout))
        if self.exc is not None:
            raise self.exc
        if self.body is not None:
            return Resp(self.status, self.body)
        return Resp(self.status, {"quotes": [self.rows[s] for s in json["symbols"] if s in self.rows]})

    async def get(self, url, timeout=None):
        self.gets.append(url)
        return Resp(404, {})


class TestRowToTick:
    def test_maps_fields_and_marks_cache(self, sc):
        t = sc.SurpriseStockEngine._row_to_tick(row("A", 50, open=49, vwap=50.5, buy_pct=60))
        assert t["price"] == 50 and t["high"] == 51 and t["low"] == 49 and t["volume"] == 5000
        assert t["open"] == 49 and t["vwap"] == 50.5 and t["buy_pct"] == 60 and t["_from_cache"] is True

    @pytest.mark.parametrize("bad", [None, "x", [], {}, {"price": 0}, {"price": None, "close": 0}])
    def test_unpriceable_rows_are_none(self, sc, bad):
        assert sc.SurpriseStockEngine._row_to_tick(bad) is None


class TestBulkRowFresh:
    def test_fresh_naive_and_aware(self, sc):
        f = sc.SurpriseStockEngine._bulk_row_fresh
        assert f({"fetched_at": _fresh(2)}, 30)
        assert f({"fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}, 30)   # naive = UTC
        assert f({"fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, 30)

    def test_old_missing_or_garbage_is_not_fresh(self, sc):
        f = sc.SurpriseStockEngine._bulk_row_fresh
        assert not f({"fetched_at": _fresh(120)}, 30)
        assert not f({}, 30) and not f({"fetched_at": 5}, 30) and not f({"fetched_at": "nope"}, 30)
        future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        assert not f({"fetched_at": future}, 30)


class TestPrefetch:
    def test_prices_symbols_in_chunks_and_normalises(self, monkeypatch):
        sc = _load({"SURPRISE_BULK_CHUNK": "2"}, monkeypatch)
        e = sc.SurpriseStockEngine()
        c = BulkClient({s: row(s) for s in ("AAA", "BBB", "CCC")})
        n = run(e._prefetch_bulk(c, "http://md/", ["aaa.ns", "BBB", "CCC.BO", "AAA", "", None]))
        assert n == 3
        assert sorted(len(p[1]) for p in c.posts) == [1, 2]
        assert all(p[0] == "http://md/quotes/bulk" for p in c.posts)
        assert set(e._bulk_ticks) == {"AAA", "BBB", "CCC"}
        assert e._bulk_ticks["AAA"]["_from_cache"] is False

    def test_old_rows_foreign_symbols_and_unpriced_rows_are_skipped(self, sc):
        e = sc.SurpriseStockEngine()
        c = BulkClient()
        c.body = {"quotes": ["junk", row("OLD", fetched_at=_fresh(500)), row("ZERO", price=0),
                             row("OK"), row("NOTASKED")]}
        n = run(e._prefetch_bulk(c, "http://md", ["OLD", "ZERO", "OK"]))
        assert n == 1 and set(e._bulk_ticks) == {"OK"}

    def test_failures_never_raise(self, sc):
        for c in (BulkClient(status=503), BulkClient(exc=RuntimeError("boom")),
                  BulkClient(body="not a dict"), BulkClient(body={"quotes": None})):
            e = sc.SurpriseStockEngine()
            assert run(e._prefetch_bulk(c, "http://md", ["A", "B"])) == 0
            assert e._bulk_ticks == {}

    def test_chunk_exception_object_in_gather_is_ignored(self, sc, monkeypatch):
        e = sc.SurpriseStockEngine()
        real = asyncio.gather

        async def fake_gather(*aws, **kw):
            res = await real(*aws, **kw)
            return list(res) + [RuntimeError("late")]

        monkeypatch.setattr(asyncio, "gather", fake_gather)
        assert run(e._prefetch_bulk(BulkClient({"A": row("A")}), "http://md", ["A"])) == 1

    def test_switch_off_empty_url_or_empty_list_do_nothing(self, monkeypatch):
        sc_off = _load({"SURPRISE_BULK_PREFETCH": "0"}, monkeypatch)
        c = BulkClient({"A": row("A")})
        assert run(sc_off.SurpriseStockEngine()._prefetch_bulk(c, "http://md", ["A"])) == 0 and c.posts == []
        sc_on = _load(monkeypatch=monkeypatch, name="surprise_scanner_g229b")
        e = sc_on.SurpriseStockEngine()
        assert run(e._prefetch_bulk(c, "", ["A"])) == 0
        assert run(e._prefetch_bulk(c, "http://md", [])) == 0
        assert run(e._prefetch_bulk(c, "http://md", ["", None])) == 0
        assert c.posts == []

    def test_already_priced_symbols_are_not_asked_again(self, sc):
        e = sc.SurpriseStockEngine()
        c = BulkClient({"A": row("A"), "B": row("B")})
        run(e._prefetch_bulk(c, "http://md", ["A"]))
        run(e._prefetch_bulk(c, "http://md", ["A", "B"]))
        assert [p[1] for p in c.posts] == [["A"], ["B"]]
        assert run(e._prefetch_bulk(c, "http://md", ["A", "B"])) == 0      # nothing left to ask


class TestFetchQueryUsesPrefetch:
    def test_prefetched_tick_skips_the_per_symbol_call(self, sc, monkeypatch):
        e = sc.SurpriseStockEngine()
        monkeypatch.setattr(e, "_tick_from_bulk_cache", lambda s: None)
        c = BulkClient({"TCS": row("TCS", 3000)})
        run(e._prefetch_bulk(c, "http://md", ["TCS"]))
        out = run(e._fetch_quote(c, "http://md", "tcs.ns"))
        assert out["price"] == 3000 and c.gets == []
        out["price"] = 1                                             # caller may mutate; stored tick is a copy
        assert e._bulk_ticks["TCS"]["price"] == 3000

    def test_unpriced_symbol_still_uses_the_per_symbol_path(self, sc, monkeypatch):
        e = sc.SurpriseStockEngine()
        monkeypatch.setattr(e, "_tick_from_bulk_cache", lambda s: None)
        c = BulkClient()
        assert run(e._fetch_quote(c, "http://md", "NOPE")) is None
        assert c.gets == ["http://md/quote/NOPE"]


class TestScanCallsPrefetch:
    def test_full_universe_then_peers_are_prefetched(self, sc, monkeypatch):
        static = {"LEAD": {"is_liquid": True, "sector": "IT"}, "B": {"is_liquid": True, "sector": "BANK"},
                  "PEER": {"is_liquid": False, "sector": "IT"}}
        e = sc.SurpriseStockEngine()
        calls = []

        async def prefetch(client, url, symbols):
            calls.append(list(symbols))
            return 0

        async def fetch(client, url, sym):
            return {"price": 1}

        def score(sym, tick):
            return {"symbol": sym, "score": 90, "tier": "breakout", "sector": "IT"} if sym == "LEAD" else None

        monkeypatch.setattr(e, "load_static_cache",
                            lambda force=False: (setattr(e, "static_cache", static) or len(static)))
        monkeypatch.setattr(e, "_prefetch_bulk", prefetch)
        monkeypatch.setattr(e, "_fetch_quote", fetch)
        monkeypatch.setattr(e, "score_stock", score)
        e._bulk_ticks = {"STALE": {"price": 1}}
        try:
            run(e.scan(object(), "http://md"))
        except Exception:
            pass                                                     # downstream result shaping is covered elsewhere
        assert calls[0] == ["LEAD", "B"]
        assert calls[1] == ["PEER"]
        assert "STALE" not in e._bulk_ticks                          # reset at the start of every scan
