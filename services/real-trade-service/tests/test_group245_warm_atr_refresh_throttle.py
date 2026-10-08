"""group245: after a restart the ATR cache is warm from the DB but _ATR_LAST_OK is empty, which made every priced symbol
"due" for a /history refresh (an AngelOne candle call each; 13 in one second in the 2026-10-08 log -> getCandleData 403).
A DB-warm symbol not yet refreshed by this process is now refreshed at most FEED_ATR_WARM_REFRESH_PER_MIN per minute;
symbols with no ATR at all are never limited.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group245_warm_atr_refresh_throttle.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_feed.feed as f


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(f, "_ATR_CACHE", {})
    f._ATR_INFLIGHT.clear(); f._ATR_LAST_TRY.clear(); f._ATR_LAST_OK.clear(); f._ATR_WARM_STAMPS.clear()
    monkeypatch.setattr(f, "_ATR_WARM_REFRESH_PER_MIN", 6)
    monkeypatch.setattr(f, "_ATR_MAX_INFLIGHT", 1000)
    started = []

    async def fake_bg(client, symbol):
        started.append(symbol)
        f._ATR_INFLIGHT.pop(f._clean_sym(symbol), None)

    monkeypatch.setattr(f, "_bg_refresh_atr", fake_bg)
    yield started
    f._ATR_WARM_STAMPS.clear()


def _schedule(symbols):
    async def go():
        n = [f._schedule_atr_refresh(None, s) for s in symbols]
        await asyncio.sleep(0)
        return n
    return asyncio.run(go())


def _warm(symbols):
    for s in symbols:
        f._ATR_CACHE[f._clean_sym(s)] = 5.0


def test_db_warm_symbols_are_limited_per_minute(_state):
    syms = [f"W{i}" for i in range(40)]
    _warm(syms)
    got = _schedule(syms)
    assert sum(got) == 6 and len(_state) == 6


def test_symbols_without_an_atr_are_never_limited(_state):
    syms = [f"C{i}" for i in range(40)]
    assert sum(_schedule(syms)) == 40


def test_limit_applies_per_minute_window(_state, monkeypatch):
    syms = [f"W{i}" for i in range(12)]
    _warm(syms)
    assert sum(_schedule(syms)) == 6
    f._ATR_WARM_STAMPS[:] = [t - 61.0 for t in f._ATR_WARM_STAMPS]      # a minute later
    f._ATR_INFLIGHT.clear()
    assert sum(_schedule(syms)) == 6


def test_zero_means_no_limit_old_behaviour(_state, monkeypatch):
    monkeypatch.setattr(f, "_ATR_WARM_REFRESH_PER_MIN", 0)
    syms = [f"W{i}" for i in range(20)]
    _warm(syms)
    assert sum(_schedule(syms)) == 20


def test_symbol_refreshed_by_this_process_is_not_counted_against_the_limit(_state):
    import time
    syms = [f"W{i}" for i in range(10)]
    _warm(syms)
    for s in syms:
        f._ATR_LAST_OK[s] = time.monotonic() - 7 * 3600        # older than the 6 h TTL: due, but not "never refreshed"
    assert sum(_schedule(syms)) == 10


def test_a_rejected_symbol_is_not_marked_as_tried(_state):
    syms = [f"W{i}" for i in range(8)]
    _warm(syms)
    _schedule(syms)
    assert len(f._ATR_LAST_TRY) == 6                             # the 2 over the limit can be tried later
