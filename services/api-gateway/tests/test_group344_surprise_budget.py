"""group344: surprise scan counts what it really did and stops the per-symbol fallback after SURPRISE_SINGLE_BUDGET_S."""
import asyncio
import time

import surprise_scanner as ss


class _Resp:
    status_code = 200

    def json(self):
        return {"price": 10.0}


class _Client:
    def __init__(self):
        self.gets = 0

    async def get(self, url, timeout=None):
        self.gets += 1
        return _Resp()


def _engine():
    e = ss.SurpriseStockEngine()
    e._tick_from_bulk_cache = lambda sym: None
    return e


def test_bulk_primed_symbol_counts_as_bulk_not_single():
    e = _engine()
    e._bulk_ticks = {"AAA": {"price": 5.0}}
    c = _Client()
    t = asyncio.run(e._fetch_quote(c, "http://md", "AAA"))
    assert t["price"] == 5.0 and c.gets == 0
    assert e._scan_counts == {"bulk": 1}


def test_single_call_is_counted():
    e = _engine()
    c = _Client()
    assert asyncio.run(e._fetch_quote(c, "http://md", "BBB"))["price"] == 10.0
    assert e._scan_counts == {"single": 1} and c.gets == 1


def test_over_budget_symbols_are_not_requested():
    e = _engine()
    e._single_deadline = time.monotonic() - 1.0
    c = _Client()
    assert asyncio.run(e._fetch_quote(c, "http://md", "CCC")) is None
    assert c.gets == 0 and e._scan_counts == {"over_budget": 1}


def test_budget_does_not_block_bulk_primed_symbols():
    e = _engine()
    e._single_deadline = time.monotonic() - 1.0
    e._bulk_ticks = {"DDD": {"price": 7.0}}
    assert asyncio.run(e._fetch_quote(_Client(), "http://md", "DDD"))["price"] == 7.0


def test_no_deadline_means_unlimited():
    e = _engine()
    assert e._single_deadline is None
    c = _Client()
    asyncio.run(e._fetch_quote(c, "http://md", "EEE"))
    assert c.gets == 1
