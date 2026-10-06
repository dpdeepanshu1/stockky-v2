"""Group 196 - a stale-served scan universe now gets a REAL background rebuild.

Before: _build_scan_universe() served the durable stale copy whenever the live key was cold and promised
"background rebuild will follow", but every rebuild path (the startup warm, the cached=true route's
fire-and-forget task, later callers) called the same function, which found the stale copy again and re-served it.
Only a real build rewrites the stale key, so the universe was never rebuilt once a stale copy existed.

No network, no real KV: sources, caches and the kv fake are replaced.
Run from services/api-gateway:  python3 -m pytest tests/test_group196_stale_universe_refresh.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def S(n, prefix="S"):
    return [f"{prefix}{i:03d}" for i in range(n)]


@pytest.fixture
def env(monkeypatch):
    e = types.SimpleNamespace(live={}, durable={}, sets=[], calls=[], securities=S(120, "NEW"), kv_reads=[])

    def rget(k):
        return e.live.get(k)

    def rset(k, v, ttl=None):
        e.sets.append((k, list(v), ttl))
        e.live[k] = list(v)
        if k == gw.SCAN_UNIVERSE_STALE_KEY:
            e.durable[k] = list(v)

    class Kv:
        def get_stale(self, key):
            e.kv_reads.append(key)
            return e.durable.get(key)

    def securities():
        e.calls.append("securities")
        return list(e.securities)

    monkeypatch.setattr(gw, "_redis_get", rget)
    monkeypatch.setattr(gw, "_redis_set", rset)
    monkeypatch.setattr(gw, "_kv_cache", Kv())
    monkeypatch.setattr(gw, "_get_all_nse_securities", securities)
    for name in ("_get_nifty_indices", "_get_momentum_movers", "_get_bulk_deal_symbols", "_get_52w_extreme_symbols",
                 "_get_news_mentioned_symbols", "_get_recent_ipos", "_get_event_symbols", "_load_watchlist",
                 "_load_searched"):
        monkeypatch.setattr(gw, name, lambda: [])
    monkeypatch.setattr(gw, "_is_symbol_pruned", lambda s: False)
    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", lambda s: list(s))
    monkeypatch.setattr(gw, "_filter_equities", lambda s: list(s))
    monkeypatch.setattr(gw, "SYMBOL_ALIASES", {})
    monkeypatch.setattr(gw.random, "shuffle", lambda x: None)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 500)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_HARD_CAP", 5000)
    monkeypatch.delenv("SCAN_UNIVERSE_STALE_REFRESH", raising=False)
    return e


def _wait(ev, timeout=5.0):
    assert ev.wait(timeout), "background rebuild did not run"


# ── the bug, end to end ──────────────────────────────────────────────────────

def test_stale_copy_is_served_and_a_real_rebuild_replaces_it(env, monkeypatch):
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    started = []
    monkeypatch.setattr(gw, "_schedule_scan_universe_refresh", lambda: started.append(1) or True)

    served = gw._build_scan_universe()
    assert served == S(60, "OLD") and started == [1] and env.calls == []     # fast answer, rebuild scheduled

    rebuilt = gw._build_scan_universe_fresh()                                 # what the scheduled thread runs
    assert "securities" in env.calls                                          # sources were really queried
    assert rebuilt and rebuilt[0].startswith("NEW")
    keys = [k for k, _v, _t in env.sets]
    assert gw.SCAN_UNIVERSE_KEY in keys and gw.SCAN_UNIVERSE_STALE_KEY in keys
    assert env.durable[gw.SCAN_UNIVERSE_STALE_KEY] == rebuilt                # the stale copy is replaced

    env.live.pop(gw.SCAN_UNIVERSE_KEY)                                        # next cold start serves the NEW copy
    assert gw._build_scan_universe()[0].startswith("NEW")


def test_old_behaviour_pinned_the_plain_call_never_rebuilds_while_a_stale_copy_exists(env, monkeypatch):
    monkeypatch.setattr(gw, "_schedule_scan_universe_refresh", lambda: False)
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    for _ in range(3):
        env.live.pop(gw.SCAN_UNIVERSE_KEY, None)
        assert gw._build_scan_universe() == S(60, "OLD")
    assert env.calls == []


def test_fresh_rebuild_ignores_a_warm_live_cache_and_the_stale_copy(env):
    env.live[gw.SCAN_UNIVERSE_KEY] = S(80, "LIVE")
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    out = gw._build_scan_universe_fresh()
    assert out[0].startswith("NEW") and "securities" in env.calls
    assert env.kv_reads == []                                                  # the stale read was skipped


def test_plain_call_still_returns_a_warm_live_cache(env):
    env.live[gw.SCAN_UNIVERSE_KEY] = S(80, "LIVE")
    assert gw._build_scan_universe() == S(80, "LIVE") and env.calls == []


# ── single flight / state hygiene ────────────────────────────────────────────

def test_fresh_rebuild_is_single_flight(env):
    assert gw._UNIVERSE_REFRESH_LOCK.acquire(blocking=False)
    try:
        assert gw._build_scan_universe_fresh() is None
        assert env.calls == []
    finally:
        gw._UNIVERSE_REFRESH_LOCK.release()


def test_fresh_rebuild_releases_the_lock_and_flag_after_a_crash(env, monkeypatch):
    monkeypatch.setattr(gw, "_get_all_nse_securities", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: (_ for _ in ()).throw(RuntimeError("y")))
    monkeypatch.setattr(gw, "_clean_equity_symbol", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        gw._build_scan_universe_fresh()
    assert not gw._UNIVERSE_REFRESH_LOCK.locked()
    assert getattr(gw._universe_refresh_ctx, "on", False) is False


def test_refresh_flag_is_off_for_plain_calls_after_a_fresh_run(env, monkeypatch):
    monkeypatch.setattr(gw, "_schedule_scan_universe_refresh", lambda: False)
    gw._build_scan_universe_fresh()
    env.live.clear()
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    env.calls.clear()
    assert gw._build_scan_universe() == S(60, "OLD")                            # plain call serves the stale copy again
    assert env.calls == []


def test_a_thin_rebuild_never_overwrites_the_stored_universe(env, monkeypatch, caplog):
    env.securities = ["ONLYONE"]
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: [])
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    with caplog.at_level("WARNING"):
        out = gw._build_scan_universe_fresh()
    assert len(out) < 50
    assert env.sets == []                                                       # nothing written
    assert env.durable[gw.SCAN_UNIVERSE_STALE_KEY] == S(60, "OLD")
    assert "kept the stored universe" in caplog.text


# ── scheduler ────────────────────────────────────────────────────────────────

def test_scheduler_runs_the_fresh_rebuild_on_a_daemon_thread(env, monkeypatch):
    done, seen = threading.Event(), {}

    def fake_fresh():
        seen["thread"] = threading.current_thread()
        done.set()

    monkeypatch.setattr(gw, "_build_scan_universe_fresh", fake_fresh)
    assert gw._schedule_scan_universe_refresh() is True
    _wait(done)
    assert seen["thread"].daemon and seen["thread"] is not threading.main_thread()


def test_scheduler_does_nothing_while_a_rebuild_is_running(env, monkeypatch):
    called = []
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: called.append(1))
    assert gw._UNIVERSE_REFRESH_LOCK.acquire(blocking=False)
    try:
        assert gw._schedule_scan_universe_refresh() is False
    finally:
        gw._UNIVERSE_REFRESH_LOCK.release()
    assert called == []


@pytest.mark.parametrize("val", ["0", "false", "No", "OFF", " 0 "])
def test_scheduler_switch_off(env, monkeypatch, val):
    monkeypatch.setenv("SCAN_UNIVERSE_STALE_REFRESH", val)
    called = []
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: called.append(1))
    assert gw._schedule_scan_universe_refresh() is False and called == []


@pytest.mark.parametrize("val", ["", "   ", "1", "true"])
def test_scheduler_blank_or_on_values_keep_it_enabled(env, monkeypatch, val):
    monkeypatch.setenv("SCAN_UNIVERSE_STALE_REFRESH", val)
    done = threading.Event()
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: done.set())
    assert gw._schedule_scan_universe_refresh() is True
    _wait(done)


def test_scheduler_swallows_a_failing_rebuild_thread(env, monkeypatch, caplog):
    done = threading.Event()

    def boom():
        done.set()
        raise RuntimeError("nse down")

    monkeypatch.setattr(gw, "_build_scan_universe_fresh", boom)
    with caplog.at_level("WARNING"):
        assert gw._schedule_scan_universe_refresh() is True
        _wait(done)
        for _ in range(50):
            if "background rebuild failed" in caplog.text:
                break
            threading.Event().wait(0.02)
    assert "background rebuild failed (non-fatal): nse down" in caplog.text


def test_scheduler_never_raises_when_the_thread_cannot_start(env, monkeypatch):
    class BadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(gw.threading, "Thread", BadThread)
    assert gw._schedule_scan_universe_refresh() is False


# ── callers ──────────────────────────────────────────────────────────────────

def test_startup_warm_rebuilds_for_real_when_the_live_key_is_cold(env, monkeypatch):
    plain, fresh = [], []
    monkeypatch.setattr(gw, "_get_momentum_movers", lambda: [])
    monkeypatch.setattr(gw, "_build_scan_universe", lambda: plain.append(1) or [])
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: fresh.append(1) or [])

    async def go():
        await gw._warm_momentum_movers_cache()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert fresh == [1] and plain == []


def test_startup_warm_keeps_the_cheap_path_when_the_live_key_is_warm(env, monkeypatch):
    env.live[gw.SCAN_UNIVERSE_KEY] = S(80, "LIVE")
    plain, fresh = [], []
    monkeypatch.setattr(gw, "_get_momentum_movers", lambda: [])
    monkeypatch.setattr(gw, "_build_scan_universe", lambda: plain.append(1) or [])
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: fresh.append(1) or [])

    async def go():
        await gw._warm_momentum_movers_cache()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert plain == [1] and fresh == []


def test_cached_route_schedules_the_real_rebuild_not_the_plain_one(env, monkeypatch):
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    plain, fresh = [], []
    monkeypatch.setattr(gw, "_build_scan_universe", lambda: plain.append(1) or [])
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: fresh.append(1) or [])

    async def searched():
        return []

    async def movers():
        return [], False

    monkeypatch.setattr(gw, "_load_searched_safe", searched)
    monkeypatch.setattr(gw, "_movers_with_deadline", movers)

    async def go():
        out = await gw.get_scan_universe(cached=True)
        await asyncio.sleep(0.2)
        return out

    out = asyncio.run(go())
    assert out["stale"] is True and out["symbols"] == S(60, "OLD")
    assert fresh == [1] and plain == []
