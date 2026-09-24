"""
tests/test_feed_atr_persistence_and_source1.py

Coverage-round tests for market_feed/feed.py (session100). test_feed_fanout_
controls.py already covers the bounded-concurrency / ATR-refresh-scheduling
behaviour end-to-end against a real threaded upstream, always forcing
Source 2 (it returns 404 on every /live-quote call). This file covers what
that leaves untested:

  * _compute_atr_from_candles: too-few / malformed candle edge cases
  * _store_atr, _schedule_atr_flush, _flush_atr_cache_periodic,
    load_atr_cache_from_db, flush_atr_cache_to_db — the ATR cache's DB
    persistence path, none of which the fan-out tests exercise (they
    monkeypatch _store_atr to a no-DB stub)
  * get_quote()'s Source 1 (live_quotes / AngelOne) path: fresh hit, stale
    row falling through, missing/zero ltp falling through, a Source-1
    request exception falling through
  * get_quote()'s Source-2 zero-price and exception branches
  * get_quotes([]) / get_preview_quotes([]) empty-input short-circuits
  * _get_preview's per-path exception handling and its /last-close fallback

Run from services/real-trade-service:
    python -m pytest tests/test_feed_atr_persistence_and_source1.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

import market_feed.feed as f


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_atr_state(monkeypatch):
    """Every test starts with a cold cache / clean scheduler state, and
    _ATR_FLUSH_EVERY reset to its real default in case a prior test changed it."""
    monkeypatch.setattr(f, "_ATR_CACHE", {})
    f._ATR_INFLIGHT.clear()
    f._ATR_LAST_TRY.clear()
    f._ATR_LAST_OK.clear()
    monkeypatch.setattr(f, "_ATR_DIRTY_COUNT", 0)
    yield


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── _compute_atr_from_candles ────────────────────────────────────────────────

def test_compute_atr_none_or_empty_candles_returns_none():
    assert f._compute_atr_from_candles(None) is None
    assert f._compute_atr_from_candles([]) is None


def test_compute_atr_fewer_than_window_plus_one_returns_none():
    candles = [{"high": 10, "low": 9, "close": 9.5} for _ in range(f.ATR_WINDOW)]  # exactly WINDOW, need WINDOW+1
    assert f._compute_atr_from_candles(candles) is None


def test_compute_atr_skips_zero_or_negative_ohlc_and_can_still_return_none():
    # 20 candles but every one has low<=0, so every true-range is skipped ->
    # trs stays empty -> len(trs) < ATR_WINDOW -> None (line 137-138 path).
    candles = [{"high": 10, "low": 0, "close": 9} for _ in range(20)]
    assert f._compute_atr_from_candles(candles) is None


def test_compute_atr_valid_candles_returns_expected_average():
    # Constant true range of 10 every bar (high=110, low=100, prev close=100)
    # across window+several extra bars -> average should be exactly 10.
    candles = [{"high": 110, "low": 100, "close": 100} for _ in range(f.ATR_WINDOW + 5)]
    atr = f._compute_atr_from_candles(candles)
    assert atr == pytest.approx(10.0)


def test_compute_atr_malformed_candle_raises_internally_and_returns_none():
    # candles[i].get(...) on a non-dict raises AttributeError inside the loop
    # -> caught by the function's own except Exception -> None.
    candles = ["not-a-dict"] * (f.ATR_WINDOW + 2)
    assert f._compute_atr_from_candles(candles) is None


# ── _store_atr / flush scheduling ────────────────────────────────────────────

def test_store_atr_ignores_empty_symbol_or_non_positive_atr():
    f._store_atr("", 5.0)
    f._store_atr("RELIANCE", 0)
    f._store_atr("RELIANCE", -1)
    assert f._cached_atr("RELIANCE") is None


def test_store_atr_writes_cache_and_triggers_flush_at_threshold(monkeypatch):
    monkeypatch.setattr(f, "_ATR_FLUSH_EVERY", 2)
    calls = []
    monkeypatch.setattr(f, "_schedule_atr_flush", lambda: calls.append(1))

    f._store_atr("AAA", 1.0)
    assert f._cached_atr("AAA") == 1.0
    assert calls == []                       # 1st write: not yet at threshold

    f._store_atr("BBB", 2.0)
    assert calls == [1]                      # 2nd write: threshold hit, flush scheduled


def test_schedule_atr_flush_without_running_loop_runs_inline(monkeypatch):
    calls = []
    monkeypatch.setattr(f, "_flush_atr_cache_periodic", lambda: calls.append(1))
    f._schedule_atr_flush()                  # no event loop running in this sync test
    assert calls == [1]


def test_schedule_atr_flush_with_running_loop_schedules_a_background_task(monkeypatch):
    calls = []

    def _fake_flush():
        calls.append(1)

    monkeypatch.setattr(f, "_flush_atr_cache_periodic", _fake_flush)

    async def go():
        f._schedule_atr_flush()
        await asyncio.sleep(0.05)             # let the to_thread task land

    _run(go())
    assert calls == [1]


def test_schedule_atr_flush_task_failure_is_logged_not_raised(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(f, "_flush_atr_cache_periodic", _boom)

    async def go():
        f._schedule_atr_flush()
        await asyncio.sleep(0.05)

    import logging
    with caplog.at_level(logging.WARNING):
        _run(go())                        # must not raise despite the background failure
    assert "scheduled ATR flush task failed" in caplog.text


# ── _flush_atr_cache_periodic ────────────────────────────────────────────────

class _FakeDBSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_flush_atr_cache_periodic_opens_and_always_closes_session(monkeypatch):
    session = _FakeDBSession()
    monkeypatch.setattr("db.get_session_factory", lambda: (lambda: session))
    flushed_with = []
    monkeypatch.setattr(f, "flush_atr_cache_to_db", lambda db: flushed_with.append(db))

    f._flush_atr_cache_periodic()

    assert flushed_with == [session]
    assert session.closed is True


def test_flush_atr_cache_periodic_closes_session_even_if_flush_raises(monkeypatch):
    session = _FakeDBSession()
    monkeypatch.setattr("db.get_session_factory", lambda: (lambda: session))

    def _boom(db):
        raise RuntimeError("write failed")

    monkeypatch.setattr(f, "flush_atr_cache_to_db", _boom)

    f._flush_atr_cache_periodic()             # must not raise
    assert session.closed is True


def test_flush_atr_cache_periodic_swallows_session_factory_error(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("no db configured")

    monkeypatch.setattr("db.get_session_factory", _boom)
    import logging
    with caplog.at_level(logging.WARNING):
        f._flush_atr_cache_periodic()          # must not raise
    assert "_flush_atr_cache_periodic failed" in caplog.text


# ── load_atr_cache_from_db / flush_atr_cache_to_db ───────────────────────────

def test_load_atr_cache_from_db_populates_only_valid_positive_floats(monkeypatch):
    monkeypatch.setattr(
        "resilience.local_cache.load_snapshot",
        lambda db, key: {"atrs": {"AAA": 2.5, "BBB": 0, "CCC": -1, "DDD": "not-a-number"}},
    )
    f.load_atr_cache_from_db(db=object())
    assert f._cached_atr("AAA") == 2.5
    assert f._cached_atr("BBB") is None
    assert f._cached_atr("CCC") is None
    assert f._cached_atr("DDD") is None


def test_load_atr_cache_from_db_no_snapshot_is_a_noop(monkeypatch):
    monkeypatch.setattr("resilience.local_cache.load_snapshot", lambda db, key: None)
    f.load_atr_cache_from_db(db=object())      # must not raise
    assert f._cached_atr("ANYTHING") is None


def test_load_atr_cache_from_db_swallows_errors(monkeypatch, caplog):
    def _boom(db, key):
        raise RuntimeError("snapshot table missing")

    monkeypatch.setattr("resilience.local_cache.load_snapshot", _boom)
    import logging
    with caplog.at_level(logging.WARNING):
        f.load_atr_cache_from_db(db=object())  # must not raise
    assert "load_atr_cache_from_db failed" in caplog.text


def test_flush_atr_cache_to_db_saves_snapshot_and_resets_dirty_count(monkeypatch):
    f._ATR_CACHE["ZZZ"] = 9.9
    monkeypatch.setattr(f, "_ATR_DIRTY_COUNT", 7)
    saved = {}
    monkeypatch.setattr(
        "resilience.local_cache.save_snapshot",
        lambda db, key, payload: saved.update(payload=payload, key=key),
    )
    f.flush_atr_cache_to_db(db=object())
    assert saved["payload"]["atrs"] == {"ZZZ": 9.9}
    assert saved["key"] == f._ATR_CACHE_KEY
    assert f._ATR_DIRTY_COUNT == 0


def test_flush_atr_cache_to_db_swallows_errors_and_leaves_dirty_count(monkeypatch):
    monkeypatch.setattr(f, "_ATR_DIRTY_COUNT", 3)

    def _boom(db, key, payload):
        raise RuntimeError("write failed")

    monkeypatch.setattr("resilience.local_cache.save_snapshot", _boom)
    f.flush_atr_cache_to_db(db=object())        # must not raise
    assert f._ATR_DIRTY_COUNT == 3               # not reset — the failed attempt never got that far


# ── get_quote(): Source 1 (live_quotes / AngelOne) ───────────────────────────

def _lq_handler(*, ltp=101.5, age_s=1.0, source="angelone", volume=1000, missing_ltp=False,
                 missing_updated_at=False, raise_exc=False, status=200):
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()

    async def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc and "/live-quote/" in str(request.url):
            raise httpx.ConnectError("upstream unreachable", request=request)
        path = request.url.path
        if "/live-quote/" in path:
            body = {"source": source}
            if not missing_ltp:
                body["ltp"] = ltp
            if not missing_updated_at:
                body["updated_at"] = updated_at
            if volume is not None:
                body["volume"] = volume
            return httpx.Response(status, json=body)
        if path.startswith("/quote/"):
            return httpx.Response(200, json={"price": 55.0, "source": "market-data-service"})
        return httpx.Response(404, json={})

    return handler


def test_get_quote_source1_fresh_hit_returns_tick_with_that_price():
    async def go():
        async with _client(_lq_handler(ltp=250.25, age_s=0.5)) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 250.25
    assert tick.source.startswith("live_quotes(")


def test_get_quote_source1_schedules_atr_refresh_when_cache_cold(monkeypatch):
    scheduled = []
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda client, sym: scheduled.append(sym) or True)

    async def go():
        async with _client(_lq_handler()) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None
    assert tick.atr is None                       # cold cache, first cycle
    assert scheduled == ["RELIANCE"]


def test_get_quote_source1_stale_row_falls_through_to_source2():
    async def go():
        async with _client(_lq_handler(age_s=999.0)) as client:  # far older than LIVE_QUOTE_MAX_AGE_S
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 55.0                      # Source 2's price, not Source 1's
    assert tick.source == "market-data-service"


def test_get_quote_source1_missing_ltp_falls_through_to_source2():
    async def go():
        async with _client(_lq_handler(missing_ltp=True)) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None and tick.price == 55.0


def test_get_quote_source1_missing_updated_at_falls_through_to_source2():
    async def go():
        async with _client(_lq_handler(missing_updated_at=True)) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None and tick.price == 55.0


def test_get_quote_source1_non_200_falls_through_to_source2():
    async def go():
        async with _client(_lq_handler(status=404)) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None and tick.price == 55.0


def test_get_quote_source1_exception_falls_through_to_source2_and_logs(caplog):
    import logging

    async def go():
        async with _client(_lq_handler(raise_exc=True)) as client:
            return await f.get_quote(client, "RELIANCE")

    with caplog.at_level(logging.WARNING):
        tick = _run(go())
    assert tick is not None and tick.price == 55.0
    assert "falling through to source 2" in caplog.text


# ── get_quote(): Source 2 edge branches ──────────────────────────────────────

def test_get_quote_source2_zero_price_returns_none():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"price": 0})

    async def go():
        async with _client(handler) as client:
            return await f.get_quote(client, "RELIANCE")

    assert _run(go()) is None


def test_get_quote_source2_non_200_status_returns_none_and_logs(caplog):
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404, json={})
        # market-data-service reachable but returning a real error status
        # (not an exception) — the branch at line 432 in feed.py, distinct
        # from the except-block path exercised below.
        return httpx.Response(500, text="upstream data provider error")

    async def go():
        async with _client(handler) as client:
            return await f.get_quote(client, "RELIANCE")

    with caplog.at_level(logging.WARNING):
        assert _run(go()) is None
    assert "market-data-service /quote returned 500" in caplog.text


def test_get_quote_source2_exception_returns_none_and_logs(caplog):
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(404, json={})
        raise httpx.ReadTimeout("slow upstream", request=request)

    async def go():
        async with _client(handler) as client:
            return await f.get_quote(client, "RELIANCE")

    with caplog.at_level(logging.WARNING):
        assert _run(go()) is None
    assert "source-2 (market-data-service /quote) failed" in caplog.text


# ── get_quotes([]) / get_preview_quotes([]) ──────────────────────────────────

def test_get_quotes_empty_list_returns_empty_dict_with_no_requests():
    assert _run(f.get_quotes([])) == {}


def test_get_preview_quotes_empty_list_returns_empty_dict_with_no_requests():
    assert _run(f.get_preview_quotes([])) == {}


# ── _get_preview() ────────────────────────────────────────────────────────────

def test_get_preview_falls_back_to_last_close_when_quote_path_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/quote/"):
            return httpx.Response(500, json={})
        if path.startswith("/last-close/"):
            return httpx.Response(200, json={"prev_close": 42.5})
        return httpx.Response(404, json={})

    async def go():
        async with _client(handler) as client:
            return await f._get_preview(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 42.5
    assert tick.source == "preview:last_close"


def test_get_preview_both_paths_raise_returns_none_and_logs_each(caplog):
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async def go():
        async with _client(handler) as client:
            return await f._get_preview(client, "RELIANCE")

    with caplog.at_level(logging.DEBUG):
        assert _run(go()) is None
    assert caplog.text.count("get_preview(RELIANCE)") == 2   # one per attempted path


def test_get_preview_both_paths_return_no_usable_price_returns_none():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unrelated": "field"})

    async def go():
        async with _client(handler) as client:
            return await f._get_preview(client, "RELIANCE")

    assert _run(go()) is None


# ── remaining small branches ─────────────────────────────────────────────────

def test_bg_refresh_atr_swallows_history_fetch_exception(monkeypatch, caplog):
    # _bg_refresh_atr opens its own httpx.AsyncClient internally (deliberately,
    # per the docstring) rather than accepting one — point MARKET_DATA_URL at
    # a loopback port nothing is listening on, so the connection fails fast
    # and locally instead of needing a mock transport or real network access.
    monkeypatch.setattr(f, "MARKET_DATA_URL", "http://127.0.0.1:1")

    import logging
    with caplog.at_level(logging.DEBUG):
        _run(f._bg_refresh_atr(None, "RELIANCE"))   # must not raise
    assert "_bg_refresh_atr(RELIANCE) failed (non-fatal)" in caplog.text
    assert "RELIANCE" not in f._ATR_INFLIGHT         # inflight slot always cleared


def test_schedule_atr_refresh_empty_symbol_returns_false():
    assert f._schedule_atr_refresh(None, "") is False
    assert f._schedule_atr_refresh(None, "   ") is False


def test_schedule_atr_refresh_reclaims_leaked_inflight_slots(monkeypatch):
    # A slot older than _ATR_INFLIGHT_MAX_AGE_S is presumed leaked and reclaimed
    # (line 312), which is what lets a fresh schedule for a *different* symbol
    # succeed even though MAX_INFLIGHT is exhausted by the leaked entry.
    monkeypatch.setattr(f, "_ATR_MAX_INFLIGHT", 1)
    f._ATR_INFLIGHT["STALE"] = _run_time_far_past()

    async def go():
        return f._schedule_atr_refresh(None, "FRESH")

    scheduled = _run(go())
    assert scheduled is True
    assert "STALE" not in f._ATR_INFLIGHT


def _run_time_far_past():
    import time
    return time.monotonic() - (f._ATR_INFLIGHT_MAX_AGE_S + 5.0)


def test_schedule_atr_refresh_without_running_loop_returns_false_and_clears_slot():
    # Called outside any running event loop (this is a plain sync test), so
    # asyncio.create_task() raises RuntimeError — the function must swallow
    # that, undo the inflight reservation it just made, and return False.
    result = f._schedule_atr_refresh(None, "NOLOOP")
    assert result is False
    assert "NOLOOP" not in f._ATR_INFLIGHT


def test_schedule_atr_refresh_without_running_loop_closes_the_unscheduled_coroutine(monkeypatch):
    # session109: the coroutine object is created BEFORE create_task() raises,
    # so it must be closed explicitly — otherwise every call on this path
    # emitted "RuntimeWarning: coroutine '_bg_refresh_atr' was never awaited".
    import inspect
    import warnings
    made = []

    async def _fake_bg_refresh(client, symbol):
        pass

    def _factory(client, symbol):
        c = _fake_bg_refresh(client, symbol)
        made.append(c)
        return c

    monkeypatch.setattr(f, "_bg_refresh_atr", _factory)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert f._schedule_atr_refresh(None, "NOLOOP2") is False
        import gc
        gc.collect()
    assert len(made) == 1
    assert inspect.getcoroutinestate(made[0]) == "CORO_CLOSED"
    assert not [w for w in caught if "never awaited" in str(w.message)]
    assert "NOLOOP2" not in f._ATR_INFLIGHT


def test_get_quote_source1_naive_updated_at_is_treated_as_utc():
    # updated_at with no tzinfo (no offset suffix) exercises the
    # `.replace(tzinfo=timezone.utc)` branch instead of erroring on the
    # aware-vs-naive datetime subtraction.
    naive_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()   # no offset -> naive

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in str(request.url):
            return httpx.Response(200, json={"ltp": 88.0, "updated_at": naive_ts, "source": "angelone"})
        return httpx.Response(200, json={"price": 55.0})

    async def go():
        async with _client(handler) as client:
            return await f.get_quote(client, "RELIANCE")

    tick = _run(go())
    assert tick is not None
    assert tick.price == 88.0
