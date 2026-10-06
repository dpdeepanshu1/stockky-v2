"""Group 197 - force_refresh=true on the scan routes now gets a REAL universe rebuild.

The force_refresh paths dropped the live key and called _build_scan_universe(), which found the durable stale copy and
re-served it (group 196 left this open). They now call _build_scan_universe_forced().
Run from services/api-gateway:  python3 -m pytest tests/test_group197_force_refresh_real_rebuild.py -v
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


# ── force_refresh=true gets a real rebuild ────────────────────────

def test_forced_build_replaces_the_stale_copy(env):
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    out = gw._build_scan_universe_forced()
    assert out[0].startswith("NEW") and "securities" in env.calls
    assert env.durable[gw.SCAN_UNIVERSE_STALE_KEY] == out


def test_forced_build_falls_back_to_the_plain_call_when_a_rebuild_is_running(env, monkeypatch):
    monkeypatch.setattr(gw, "_schedule_scan_universe_refresh", lambda: False)
    env.durable[gw.SCAN_UNIVERSE_STALE_KEY] = S(60, "OLD")
    assert gw._UNIVERSE_REFRESH_LOCK.acquire(blocking=False)
    try:
        assert gw._build_scan_universe_forced() == S(60, "OLD")        # served the normal way, never raised
    finally:
        gw._UNIVERSE_REFRESH_LOCK.release()


def test_forced_build_falls_back_when_the_real_rebuild_returns_nothing(monkeypatch):
    monkeypatch.setattr(gw, "_build_scan_universe_fresh", lambda: [])
    monkeypatch.setattr(gw, "_build_scan_universe", lambda: ["PLAIN"])
    assert gw._build_scan_universe_forced() == ["PLAIN"]


def test_start_scan_with_force_refresh_uses_the_real_rebuild(monkeypatch):
    seen = []
    monkeypatch.setattr(gw, "_build_scan_universe_forced", lambda: seen.append("forced") or ["AAA"])
    monkeypatch.setattr(gw, "_build_scan_universe", lambda: seen.append("plain") or ["AAA"])
    monkeypatch.setattr(gw, "_drop_cache_keys", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    monkeypatch.setattr(gw, "run_scan_parallel", lambda *a, **k: None)

    class BG:
        def add_task(self, *a, **k):
            pass

    for force, want in ((True, ["forced"]), (False, ["plain"])):
        seen.clear()
        try:
            gw.start_scan(force_refresh=force, lite=True, background_tasks=BG())
        except Exception:
            pass                                    # only the universe call matters here
        assert seen[:1] == want
