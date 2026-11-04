"""Round 185: the surprise scan reads the shared bulk-quote map ONCE per scan, off the event loop.

3-4 Nov log: scan() called get_bulk_quote_cache() -> kv_get() once PER SYMBOL on the loop thread; a memory miss made
kv_cache._guarded_read wait up to 1.5 s there ("event loop blocked for 3.3 s / 3.7 s"), the scan took 34-38 s against the 20 s
deadline and real-trade-service got a 258-383 s old result (and one ReadTimeout)."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import surprise_scanner as ss


def _row(price):
    return {"price": price, "close": price, "open": price, "day_high": price, "day_low": price, "volume": 10}


@pytest.fixture
def fake_feed(monkeypatch):
    """A stand-in `data_feed` that counts reads and records which thread did them."""
    calls = {"bulk_reads": 0, "per_symbol_reads": 0, "bulk_threads": [], "map": {"quotes": {"AAA": _row(100.0), "BBB": _row(50.0)}}}
    mod = types.ModuleType("data_feed")

    def get_bulk_quote_cache():
        calls["bulk_reads"] += 1
        calls["bulk_threads"].append(threading.get_ident())
        return calls["map"]

    def get_cached_quote(symbol, cache=None):
        if cache is None:                      # the OLD path: one kv read per symbol
            calls["per_symbol_reads"] += 1
            cache = get_bulk_quote_cache()
        row = (cache.get("quotes") or {}).get(symbol.upper())
        return row if isinstance(row, dict) and row.get("price") else None

    mod.get_bulk_quote_cache = get_bulk_quote_cache
    mod.get_cached_quote = get_cached_quote
    monkeypatch.setitem(sys.modules, "data_feed", mod)
    return calls


def test_snapshot_is_one_read_then_zero_per_symbol_reads(fake_feed):
    eng = ss.SurpriseStockEngine()

    async def go():
        await eng._load_cache_snapshot()
        reads_after_load = fake_feed["bulk_reads"]
        ticks = [eng._tick_from_bulk_cache("AAA") for _ in range(500)] + [eng._tick_from_bulk_cache("BBB")]
        return reads_after_load, ticks

    reads_after_load, ticks = asyncio.run(go())
    assert reads_after_load == 1
    assert fake_feed["bulk_reads"] == 1            # 501 lookups later: still ONE read
    assert fake_feed["per_symbol_reads"] == 0
    assert ticks[0]["price"] == 100.0 and ticks[-1]["price"] == 50.0 and ticks[0]["_from_cache"] is True


def test_snapshot_read_runs_off_the_event_loop_thread(fake_feed):
    eng = ss.SurpriseStockEngine()
    loop_thread = {}

    async def go():
        loop_thread["id"] = threading.get_ident()
        await eng._load_cache_snapshot()

    asyncio.run(go())
    assert fake_feed["bulk_threads"] and fake_feed["bulk_threads"][0] != loop_thread["id"]


def test_without_a_snapshot_the_old_per_symbol_read_still_works(fake_feed):
    eng = ss.SurpriseStockEngine()
    assert eng._tick_from_bulk_cache("AAA")["price"] == 100.0
    assert fake_feed["per_symbol_reads"] == 1


def test_unknown_symbol_is_a_miss_not_an_error(fake_feed):
    eng = ss.SurpriseStockEngine()

    async def go():
        await eng._load_cache_snapshot()
        return eng._tick_from_bulk_cache("ZZZ")

    assert asyncio.run(go()) is None


def test_failing_snapshot_read_gives_an_empty_snapshot_not_an_exception(monkeypatch):
    mod = types.ModuleType("data_feed")

    def boom():
        raise RuntimeError("neon down")

    mod.get_bulk_quote_cache = boom
    mod.get_cached_quote = lambda symbol, cache=None: (cache or {}).get(symbol)
    monkeypatch.setitem(sys.modules, "data_feed", mod)
    eng = ss.SurpriseStockEngine()

    async def go():
        await eng._load_cache_snapshot()
        return eng._cv_cache_snap.get(), eng._tick_from_bulk_cache("AAA")

    snap, tick = asyncio.run(go())
    assert snap == {} and tick is None             # the bulk prefetch / single-quote path answers instead


def test_slow_snapshot_read_is_cut_at_the_timeout(monkeypatch):
    mod = types.ModuleType("data_feed")
    release = threading.Event()

    def slow():
        release.wait(3)
        return {"quotes": {"AAA": _row(1.0)}}

    mod.get_bulk_quote_cache = slow
    mod.get_cached_quote = lambda symbol, cache=None: None
    monkeypatch.setitem(sys.modules, "data_feed", mod)
    monkeypatch.setenv("SURPRISE_CACHE_SNAPSHOT_TIMEOUT_S", "0.5")
    eng = ss.SurpriseStockEngine()

    async def go():
        t0 = asyncio.get_running_loop().time()
        await eng._load_cache_snapshot()
        return asyncio.get_running_loop().time() - t0, eng._cv_cache_snap.get()

    try:
        waited, snap = asyncio.run(go())
    finally:
        release.set()
    assert waited < 2.0 and snap == {}


def test_loop_stays_responsive_while_the_snapshot_loads(monkeypatch):
    """The point of the fix: a slow read must not freeze the loop (the old guard parked it for 1.5 s per miss)."""
    mod = types.ModuleType("data_feed")
    mod.get_bulk_quote_cache = lambda: (__import__("time").sleep(0.6), {"quotes": {}})[1]
    mod.get_cached_quote = lambda symbol, cache=None: None
    monkeypatch.setitem(sys.modules, "data_feed", mod)
    eng = ss.SurpriseStockEngine()

    async def go():
        ticks = []

        async def heartbeat():
            for _ in range(20):
                ticks.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.03)

        hb = asyncio.create_task(heartbeat())
        await eng._load_cache_snapshot()
        await hb
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return max(gaps)

    assert asyncio.run(go()) < 0.25                # a blocked loop would show a ~0.6 s gap


def test_snapshots_of_overlapping_scans_are_separate(fake_feed):
    """Each scan task has its own context copy, so one scan's snapshot never replaces another's mid-scan."""
    eng = ss.SurpriseStockEngine()
    seen = {}

    async def scan(name, price):
        eng._cv_cache_snap.set({"quotes": {"AAA": _row(price)}})
        await asyncio.sleep(0.01)                  # the other task runs and sets ITS snapshot meanwhile
        seen[name] = eng._tick_from_bulk_cache("AAA")["price"]

    async def go():
        await asyncio.gather(scan("A", 1.0), scan("B", 2.0))

    asyncio.run(go())
    assert seen == {"A": 1.0, "B": 2.0}


def test_data_feed_without_snapshot_support_keeps_the_old_path(monkeypatch):
    """A data_feed with no get_bulk_quote_cache (older build / test double): no snapshot, per-symbol lookups as before."""
    mod = types.ModuleType("data_feed")
    mod.get_cached_quote = lambda symbol: _row(7.0) if symbol == "AAA" else None
    monkeypatch.setitem(sys.modules, "data_feed", mod)
    eng = ss.SurpriseStockEngine()

    async def go():
        await eng._load_cache_snapshot()
        return eng._cv_cache_snap.get(), eng._tick_from_bulk_cache("AAA")

    snap, tick = asyncio.run(go())
    assert snap is None and tick["price"] == 7.0


def test_real_get_cached_quote_uses_the_map_it_is_given(monkeypatch):
    """data_feed.get_cached_quote(symbol, cache=...) answers from the passed map and does NOT read the shared cache."""
    monkeypatch.delitem(sys.modules, "data_feed", raising=False)   # restored at teardown
    import data_feed as df

    def must_not_be_called():
        raise AssertionError("get_bulk_quote_cache must not run when a map is passed")

    monkeypatch.setattr(df, "get_bulk_quote_cache", must_not_be_called)
    snap = {"quotes": {"AAA": {"price": 12.5}, "BBB": {"price": 0}}}
    assert df.get_cached_quote("AAA", cache=snap)["price"] == 12.5
    assert df.get_cached_quote("aaa.NS", cache=snap)["price"] == 12.5     # same symbol normalisation as before
    assert df.get_cached_quote("BBB", cache=snap) is None                  # a zero price is not a quote
    assert df.get_cached_quote("CCC", cache={}) is None


def test_real_get_cached_quote_without_a_map_still_reads_the_cache(monkeypatch):
    monkeypatch.delitem(sys.modules, "data_feed", raising=False)   # restored at teardown
    import data_feed as df

    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {"quotes": {"AAA": {"price": 3.0}}})
    assert df.get_cached_quote("AAA")["price"] == 3.0
