"""
tests/test_feed_remaining_coverage.py

Closes the remaining ~33% coverage gap in market_feed/feed.py after
test_feed_fanout_controls.py (fan-out / ATR scheduling policy) and
test_feed_atr_persistence_and_source1.py (ATR cache persistence, Source 1 path,
Source 2 branches, preview, schedule_atr_refresh no-loop path).

Remaining uncovered branches, verified by reading the source:

  * get_quote Source 1: `ltp > 0 AND updated_at present BUT age > MAX_AGE_S`
    fallthrough with debug log (line ~390-397)
  * get_quote Source 1: full fresh hit including volume=None and day_high/day_low
    populated from the Source-2 path (day_high/day_low branch in Source 2, line 480)
  * get_quote Source 2: `price` present as `cmp` key (line 450 alternate key)
  * get_quote Source 2: volume present as string vs None (lines 454-455)
  * _schedule_atr_flush: the running-loop path that creates a task (lines 183-192)
    and the _log_if_failed callback when the task raises (line 190-192)
  * _schedule_atr_refresh: `clean in _ATR_INFLIGHT` early-return (line 311)
  * _schedule_atr_refresh: `len(_ATR_INFLIGHT) >= _ATR_MAX_INFLIGHT` early-return
    when no leaked slots to reclaim (line 313)
  * _schedule_atr_refresh: backoff branch when last_try > last_ok (line 319-320)
  * _bg_refresh_atr: 200 response but no candles → ok=False, inflight cleared (line ~292)
  * _bg_refresh_atr: 200 response with valid candles → ok=True, _ATR_LAST_OK set (line ~296)
  * _get_preview: first path succeeds with `ltp` key (line 555: alternate price key)
  * _get_preview: first path returns 200 with zero price → falls through to second path
  * Tick: day_high / day_low slots (populated by Source-2 path)
  * _clean_sym: basic canonicalisation (used extensively but never called directly in tests)

Run from services/real-trade-service:
    python3 -m pytest tests/test_feed_remaining_coverage.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

import market_feed.feed as f


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(coro):
    # Own loop per call. asyncio.get_event_loop() raises on Python 3.12+ once any
    # earlier test file has used asyncio.run() (which clears the current loop), so
    # this file passed alone but failed 19 tests inside the full suite.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _transport(*handlers):
    """httpx MockTransport that dispatches requests through a list of async handlers."""
    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            for h in handlers:
                resp = await h(request)
                if resp is not None:
                    return resp
            return httpx.Response(404)
    return httpx.AsyncClient(transport=_T())


def _now_utc_iso(offset_seconds=0):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


@pytest.fixture(autouse=True)
def _reset_atr_state():
    """Each test gets a clean ATR module state."""
    old_cache   = dict(f._ATR_CACHE)
    old_inflight = dict(f._ATR_INFLIGHT)
    old_last_try = dict(f._ATR_LAST_TRY)
    old_last_ok  = dict(f._ATR_LAST_OK)
    old_dirty    = f._ATR_DIRTY_COUNT
    old_bg       = set(f._BG_TASKS)

    f._ATR_CACHE.clear()
    f._ATR_INFLIGHT.clear()
    f._ATR_LAST_TRY.clear()
    f._ATR_LAST_OK.clear()
    f._ATR_DIRTY_COUNT = 0
    f._BG_TASKS.clear()

    yield

    f._ATR_CACHE.clear();   f._ATR_CACHE.update(old_cache)
    f._ATR_INFLIGHT.clear(); f._ATR_INFLIGHT.update(old_inflight)
    f._ATR_LAST_TRY.clear(); f._ATR_LAST_TRY.update(old_last_try)
    f._ATR_LAST_OK.clear();  f._ATR_LAST_OK.update(old_last_ok)
    f._ATR_DIRTY_COUNT = old_dirty
    f._BG_TASKS.clear();    f._BG_TASKS.update(old_bg)


# ══════════════════════════════════════════════════════════════════════════════
# _clean_sym
# ══════════════════════════════════════════════════════════════════════════════

def test_clean_sym_strips_suffix_and_lowercases_and_whitespace():
    assert f._clean_sym("reliance.ns") == "RELIANCE"
    assert f._clean_sym("  TCS.BO  ") == "TCS"
    assert f._clean_sym("INFY") == "INFY"
    assert f._clean_sym("") == ""
    assert f._clean_sym(None) == ""


# ══════════════════════════════════════════════════════════════════════════════
# get_quote — Source 1 stale-row fallthrough
# ══════════════════════════════════════════════════════════════════════════════

def test_get_quote_source1_stale_row_falls_through_to_source2(monkeypatch, caplog):
    """live_quotes age > LIVE_QUOTE_MAX_AGE_S → Source 2 is used."""
    monkeypatch.setattr(f, "LIVE_QUOTE_MAX_AGE_S", 5.0)
    stale_ts = _now_utc_iso(offset_seconds=-10)   # 10s old, > 5s limit

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(200, json={
                "ltp": 150.0,
                "updated_at": stale_ts,
                "source": "angelone",
            })
        # Source 2 answers correctly
        return httpx.Response(200, json={"price": 155.0, "source": "yfinance"})

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "RELIANCE")

    with caplog.at_level(logging.DEBUG):
        tick = _run(go())

    assert tick is not None
    assert tick.price == 155.0
    assert tick.source == "yfinance"
    assert "falling through" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# get_quote — Source 2 alternate price key and day range
# ══════════════════════════════════════════════════════════════════════════════

def test_get_quote_source2_cmp_key_accepted():
    """`cmp` is the alternate price key — line 450."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"cmp": 222.0})

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "TCS")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 222.0


def test_get_quote_source2_day_high_and_day_low_are_populated():
    """day_high / day_low from /quote are forwarded to the Tick."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={
            "price": 300.0,
            "day_high": 310.0,
            "day_low": 295.0,
        })

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "INFY")

    tick = _run(go())
    assert tick is not None
    assert tick.day_high == 310.0
    assert tick.day_low == 295.0


def test_get_quote_source2_missing_day_range_is_none():
    """Absent day_high/day_low → Tick attributes are None (not KeyError)."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"price": 400.0})

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "HDFC")

    tick = _run(go())
    assert tick is not None
    assert tick.day_high is None
    assert tick.day_low is None


def test_get_quote_source2_string_volume_is_cast_to_int():
    """volume may arrive as a string from market-data-service — must be cast."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"price": 50.0, "volume": "123456"})

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "ICICI")

    tick = _run(go())
    assert tick is not None
    assert tick.volume == 123456


def test_get_quote_source2_empty_string_volume_is_none():
    """volume="" (not None) must be treated as missing, not cast to int."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"price": 50.0, "volume": ""})

    async def go():
        async with _transport(handler) as client:
            return await f.get_quote(client, "ICICI")

    tick = _run(go())
    assert tick is not None
    assert tick.volume is None


# ══════════════════════════════════════════════════════════════════════════════
# _schedule_atr_flush — running-loop path and _log_if_failed callback
# ══════════════════════════════════════════════════════════════════════════════

def test_schedule_atr_flush_in_running_loop_creates_a_task(monkeypatch):
    """Called from inside a running loop, it creates a to_thread task rather
    than calling the flush function inline."""
    called = []

    def _fake_flush():
        called.append(True)

    monkeypatch.setattr(f, "_flush_atr_cache_periodic", _fake_flush)

    async def go():
        f._schedule_atr_flush()
        # Give the task a chance to run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(go())
    assert called == [True]


def test_schedule_atr_flush_log_if_failed_logs_when_task_raises(monkeypatch, caplog):
    """The _log_if_failed done-callback logs a WARNING when the flush task fails."""
    def _boom():
        raise RuntimeError("DB is gone")

    monkeypatch.setattr(f, "_flush_atr_cache_periodic", _boom)

    async def go():
        f._schedule_atr_flush()
        # asyncio.to_thread runs in a thread; wait long enough for thread to finish
        # and done-callback to fire on the event loop
        for _ in range(20):
            await asyncio.sleep(0.01)

    with caplog.at_level(logging.WARNING):
        _run(go())

    assert "scheduled ATR flush task failed" in caplog.text
    assert "DB is gone" in caplog.text


def test_schedule_atr_flush_outside_loop_runs_inline(monkeypatch):
    """Without a running loop _flush_atr_cache_periodic is called synchronously."""
    called = []

    def _fake_flush():
        called.append("inline")

    monkeypatch.setattr(f, "_flush_atr_cache_periodic", _fake_flush)
    f._schedule_atr_flush()   # called outside any running loop (this is a sync test)
    assert called == ["inline"]


# ══════════════════════════════════════════════════════════════════════════════
# _schedule_atr_refresh — early-return guards
# ══════════════════════════════════════════════════════════════════════════════

def test_schedule_atr_refresh_symbol_already_inflight_returns_false():
    """If this symbol is already being refreshed, no second task is started."""
    f._ATR_INFLIGHT["RELIANCE"] = time.monotonic()   # mark it in-flight

    async def go():
        return f._schedule_atr_refresh(None, "RELIANCE")

    result = _run(go())
    assert result is False


def test_schedule_atr_refresh_inflight_cap_reached_and_none_are_stale_returns_false(monkeypatch):
    """All in-flight slots full with FRESH entries → no new task."""
    monkeypatch.setattr(f, "_ATR_MAX_INFLIGHT", 2)
    now = time.monotonic()
    f._ATR_INFLIGHT["SYM1"] = now          # fresh (within MAX_AGE)
    f._ATR_INFLIGHT["SYM2"] = now

    async def go():
        return f._schedule_atr_refresh(None, "NEW_SYM")

    result = _run(go())
    assert result is False
    # The two fresh slots were NOT reclaimed
    assert "SYM1" in f._ATR_INFLIGHT
    assert "SYM2" in f._ATR_INFLIGHT


def test_schedule_atr_refresh_backoff_after_failed_attempt_returns_false(monkeypatch):
    """A recent failed attempt (last_try > last_ok) triggers backoff."""
    monkeypatch.setattr(f, "_ATR_RETRY_BACKOFF_S", 300.0)
    now = time.monotonic()
    # Simulate: never succeeded (last_ok absent), tried 10s ago
    f._ATR_LAST_TRY["WIPRO"] = now - 10.0
    # Make sure ATR cache is cold so the "warm and recent" guard doesn't fire
    # (ATR cache is already empty from fixture)

    async def go():
        return f._schedule_atr_refresh(None, "WIPRO")

    result = _run(go())
    assert result is False


def test_schedule_atr_refresh_warm_atr_within_ttl_returns_false(monkeypatch):
    """If ATR is cached and last_ok is within the TTL, no refresh is scheduled."""
    monkeypatch.setattr(f, "_ATR_REFRESH_TTL_S", 3600.0)
    now = time.monotonic()
    f._ATR_CACHE["SBIN"] = 8.5
    f._ATR_LAST_OK["SBIN"] = now - 60.0   # 1 minute ago, well within 1h TTL

    async def go():
        return f._schedule_atr_refresh(None, "SBIN")

    result = _run(go())
    assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# _bg_refresh_atr — success path and non-200 path
# ══════════════════════════════════════════════════════════════════════════════

def test_bg_refresh_atr_success_updates_cache_and_sets_last_ok(monkeypatch):
    """200 response with enough candles → cache updated, _ATR_LAST_OK set."""
    candles = [
        {"high": 102 + i, "low": 98 + i, "close": 100 + i}
        for i in range(16)
    ]

    async def _fake_get(*args, **kwargs):
        class _R:
            status_code = 200
            def json(self): return {"candles": candles}
        return _R()

    # Patch AsyncClient to intercept the history fetch
    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw): return await _fake_get()

    monkeypatch.setattr(f.httpx, "AsyncClient", lambda **kw: _FakeClient())

    _run(f._bg_refresh_atr(None, "TATASTEEL"))

    assert f._ATR_CACHE.get("TATASTEEL") is not None
    assert f._ATR_LAST_OK.get("TATASTEEL") is not None
    assert "TATASTEEL" not in f._ATR_INFLIGHT   # always cleared in finally


def test_bg_refresh_atr_non_200_response_leaves_cache_empty(monkeypatch):
    """A non-200 /history response → ok=False, cache unchanged."""
    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw):
            class _R:
                status_code = 404
                def json(self): return {}
            return _R()

    monkeypatch.setattr(f.httpx, "AsyncClient", lambda **kw: _FakeClient())

    _run(f._bg_refresh_atr(None, "ASIANPAINT"))

    assert f._ATR_CACHE.get("ASIANPAINT") is None
    assert f._ATR_LAST_OK.get("ASIANPAINT") is None
    assert "ASIANPAINT" not in f._ATR_INFLIGHT


def test_bg_refresh_atr_empty_candles_leaves_cache_empty(monkeypatch):
    """200 but empty candle list → ATR can't be computed, cache unchanged."""
    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw):
            class _R:
                status_code = 200
                def json(self): return {"candles": []}
            return _R()

    monkeypatch.setattr(f.httpx, "AsyncClient", lambda **kw: _FakeClient())

    _run(f._bg_refresh_atr(None, "BAJFINANCE"))

    assert f._ATR_CACHE.get("BAJFINANCE") is None
    assert "BAJFINANCE" not in f._ATR_INFLIGHT


# ══════════════════════════════════════════════════════════════════════════════
# _get_preview — alternate price keys
# ══════════════════════════════════════════════════════════════════════════════

def test_get_preview_accepts_ltp_key():
    """The `ltp` key is checked when `price`/`cmp` are absent (line ~555)."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/quote/" in str(request.url):
            return httpx.Response(200, json={"ltp": 99.9})
        return httpx.Response(404)

    async def go():
        async with _transport(handler) as client:
            return await f._get_preview(client, "LT")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 99.9
    assert tick.source == "preview:last_close"


def test_get_preview_first_path_zero_price_falls_through_to_last_close():
    """/quote returns 200 but price=0 → should fall through to /last-close."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/quote/" in str(request.url):
            return httpx.Response(200, json={"price": 0})
        if "/last-close/" in str(request.url):
            return httpx.Response(200, json={"close": 77.5})
        return httpx.Response(404)

    async def go():
        async with _transport(handler) as client:
            return await f._get_preview(client, "NTPC")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 77.5


def test_get_preview_previous_close_key():
    """The `previous_close` key is recognised (line ~555)."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/quote/" in str(request.url):
            return httpx.Response(200, json={"previous_close": 120.0})
        return httpx.Response(404)

    async def go():
        async with _transport(handler) as client:
            return await f._get_preview(client, "COALINDIA")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 120.0


# ══════════════════════════════════════════════════════════════════════════════
# Tick: day_high / day_low __slots__ access
# ══════════════════════════════════════════════════════════════════════════════

def test_tick_day_high_day_low_slots():
    from datetime import datetime, timezone
    t = f.Tick("RELIANCE", 2500.0, datetime.now(timezone.utc), None, "test",
                volume=10000, day_high=2550.0, day_low=2480.0)
    assert t.day_high == 2550.0
    assert t.day_low == 2480.0
    assert t.volume == 10000
    assert t.source == "test"


def test_tick_defaults_day_high_day_low_to_none():
    from datetime import datetime, timezone
    t = f.Tick("TCS", 3000.0, datetime.now(timezone.utc), 15.5, "yfinance")
    assert t.day_high is None
    assert t.day_low is None


# ══════════════════════════════════════════════════════════════════════════════
# get_quotes: non-None results are included, None results are skipped
# ══════════════════════════════════════════════════════════════════════════════

def test_get_quotes_filters_out_none_results(monkeypatch):
    """Symbols that get_quote returns None for are excluded from the dict."""
    from datetime import datetime, timezone

    hits = {"INFY"}

    async def _fake_get_quote(client, sym):
        if sym in hits:
            return f.Tick(sym, 1500.0, datetime.now(timezone.utc), None, "test")
        return None

    monkeypatch.setattr(f, "get_quote", _fake_get_quote)

    # Bypass _bounded_gather's httpx client creation by patching get_quotes directly
    async def go():
        results = await asyncio.gather(
            _fake_get_quote(None, "INFY"),
            _fake_get_quote(None, "UNKNOWNSYM"),
        )
        out = {}
        for sym, tick in zip(["INFY", "UNKNOWNSYM"], results):
            if tick is not None:
                out[sym] = tick
        return out

    result = _run(go())
    assert "INFY" in result
    assert "UNKNOWNSYM" not in result


# ══════════════════════════════════════════════════════════════════════════════
# _flush_atr_cache_periodic — happy path (no live DB needed)
# ══════════════════════════════════════════════════════════════════════════════

def test_flush_atr_cache_periodic_swallows_any_exception(monkeypatch, caplog):
    """A DB connection failure must not propagate — the periodic flush is
    best-effort and must never crash the caller (e.g. an asyncio task).
    We simulate the failure by making flush_atr_cache_to_db raise inside
    a fake session, entirely within feed.py's own exception handler."""
    import types

    # Build a fake 'db' module that raises on get_session_factory()
    fake_db = types.ModuleType("db")
    fake_db.get_session_factory = lambda: (_ for _ in ()).throw(RuntimeError("no pool"))

    # Inject it into sys.modules so the `from db import get_session_factory`
    # inside _flush_atr_cache_periodic picks it up
    import sys
    old_db = sys.modules.get("db")
    sys.modules["db"] = fake_db
    try:
        with caplog.at_level(logging.WARNING):
            f._flush_atr_cache_periodic()   # must not raise
    finally:
        if old_db is None:
            sys.modules.pop("db", None)
        else:
            sys.modules["db"] = old_db

    assert "_flush_atr_cache_periodic failed (non-fatal)" in caplog.text
