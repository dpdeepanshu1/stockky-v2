"""group160: symbols with no price are paused instead of retried every cycle.
Run: python3 -m pytest tests/test_group160_dead_symbol_pause.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from market_feed import feed as f
from market_feed.feed import Tick


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("FEED_DEAD_SKIP", "FEED_DEAD_AFTER_MISSES", "FEED_DEAD_BACKOFF_S", "FEED_DEAD_BACKOFF_MAX_S"):
        monkeypatch.delenv(k, raising=False)
    f.clear_dead_symbols()
    yield
    f.clear_dead_symbols()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _quote_handler(status=404, body=None, hits=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(request.url.path)
        if request.url.path.startswith("/live-quote/"):
            return httpx.Response(404, json={})
        return httpx.Response(status, json=body if body is not None else {})
    return handler


def _get_quote(symbol, handler):
    async def go():
        async with _client(handler) as c:
            return await f.get_quote(c, symbol)
    return _run(go())


# ── config ───────────────────────────────────────────────────────────────────

def test_defaults():
    assert f._dead_cfg() == (True, 3, 1800.0, 21600.0)


def test_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("FEED_DEAD_AFTER_MISSES", "abc")
    monkeypatch.setenv("FEED_DEAD_BACKOFF_S", "-5")
    monkeypatch.setenv("FEED_DEAD_BACKOFF_MAX_S", "nan")
    assert f._dead_cfg() == (True, 3, 1800.0, 21600.0)


def test_off_switch(monkeypatch):
    monkeypatch.setenv("FEED_DEAD_SKIP", "0")
    for _ in range(5):
        f._note_no_data("DEADCO")
    assert f._paused_symbols(["DEADCO"]) == []


# ── counting ─────────────────────────────────────────────────────────────────

def test_not_paused_before_the_third_miss():
    f._note_no_data("DEADCO")
    f._note_no_data("DEADCO.NS")           # same stock, other spelling
    assert f._paused_symbols(["DEADCO"]) == []
    f._note_no_data("deadco")
    assert f._paused_symbols(["DEADCO"]) == ["DEADCO"]


def test_a_real_price_clears_the_count():
    f._note_no_data("DEADCO")
    f._note_no_data("DEADCO")
    f._note_priced("DEADCO")
    f._note_no_data("DEADCO")
    assert f._paused_symbols(["DEADCO"]) == []


def test_pause_ends_after_the_backoff(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    for _ in range(3):
        f._note_no_data("DEADCO")
    assert f._paused_symbols(["DEADCO"]) == ["DEADCO"]
    t[0] += 1801
    assert f._paused_symbols(["DEADCO"]) == []


def test_backoff_doubles_up_to_the_cap(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    for _ in range(3):
        f._note_no_data("DEADCO")
    assert f._DEAD["DEADCO"][1] - t[0] == pytest.approx(1800)
    f._note_no_data("DEADCO")
    assert f._DEAD["DEADCO"][1] - t[0] == pytest.approx(3600)
    for _ in range(10):
        f._note_no_data("DEADCO")
    assert f._DEAD["DEADCO"][1] - t[0] == pytest.approx(21600)


def test_helpers_never_raise():
    f._note_no_data(None)
    f._note_no_data("")
    f._note_priced(None)
    assert f._paused_symbols(None) == []


# ── what counts as a miss ────────────────────────────────────────────────────

def test_404_on_quote_counts():
    _get_quote("DEADCO", _quote_handler(404))
    assert f._DEAD["DEADCO"][0] == 1


def test_200_without_price_counts():
    _get_quote("DEADCO", _quote_handler(200, {"price": 0}))
    assert f._DEAD["DEADCO"][0] == 1


def test_5xx_does_not_count():
    _get_quote("DEADCO", _quote_handler(500, {}))
    assert "DEADCO" not in f._DEAD


def test_timeout_does_not_count():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/live-quote/"):
            return httpx.Response(404, json={})
        raise httpx.ReadTimeout("slow", request=request)
    _get_quote("DEADCO", handler)
    assert "DEADCO" not in f._DEAD


def test_a_priced_quote_clears():
    f._note_no_data("GOODCO")
    f._note_no_data("GOODCO")
    tick = _get_quote("GOODCO", _quote_handler(200, {"price": 55.0}))
    assert tick is not None and tick.price == 55.0
    assert "GOODCO" not in f._DEAD


# ── get_quotes ───────────────────────────────────────────────────────────────

def test_get_quotes_leaves_paused_symbols_out_of_the_lookup(monkeypatch):
    seen = []

    async def fake_bounded(symbols, fn, label):
        seen.append(list(symbols))
        return [Tick(symbol=s, price=10.0, as_of=datetime.now(timezone.utc), atr=None, source="t")
                for s in symbols]
    monkeypatch.setattr(f, "_bounded_gather", fake_bounded)
    for _ in range(3):
        f._note_no_data("DEADCO")
    out = _run(f.get_quotes(["DEADCO", "GOODCO", "DEADCO.NS"]))
    assert seen == [["GOODCO"]]
    assert list(out) == ["GOODCO"]


def test_get_quotes_with_only_paused_symbols_makes_no_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no lookup expected")
    monkeypatch.setattr(f, "_bounded_gather", boom)
    monkeypatch.setattr(f, "_bulk_ticks", boom)
    for _ in range(3):
        f._note_no_data("DEADCO")
    assert _run(f.get_quotes(["DEADCO"])) == {}


def test_priority_lane_never_skips(monkeypatch):
    got = []

    async def fake_priority(symbols):
        got.append(list(symbols))
        return {}
    monkeypatch.setattr(f, "_priority_quotes", fake_priority)
    for _ in range(3):
        f._note_no_data("DEADCO")
    _run(f.get_quotes(["DEADCO"], priority=True))
    assert got == [["DEADCO"]]


def test_symbol_is_retried_after_the_pause(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    seen = []

    async def fake_bounded(symbols, fn, label):
        seen.append(list(symbols))
        return [None for _ in symbols]
    monkeypatch.setattr(f, "_bounded_gather", fake_bounded)
    for _ in range(3):
        f._note_no_data("DEADCO")
    _run(f.get_quotes(["DEADCO"]))
    assert seen == []
    t[0] += 1801
    _run(f.get_quotes(["DEADCO"]))
    assert seen == [["DEADCO"]]


def test_three_cycles_stop_hitting_the_server():
    hits = []
    h = _quote_handler(404, hits=hits)

    async def go():
        async with _client(h) as c:
            for _ in range(3):
                await f.get_quote(c, "DEADCO")
    _run(go())
    assert f._paused_symbols(["DEADCO"]) == ["DEADCO"]
    n = len(hits)
    # a fourth cycle through get_quotes now makes no request at all
    out = _run(f.get_quotes(["DEADCO"]))
    assert out == {} and len(hits) == n
