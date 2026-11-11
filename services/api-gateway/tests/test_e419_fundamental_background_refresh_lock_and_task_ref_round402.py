"""E419: the stale-while-revalidate fundamentals refresh (1) keeps a strong reference to its task until it finishes and (2) takes a lock that
outlives the 35 s upstream call (was 5 s)."""
from __future__ import annotations

import asyncio
import gc

from test_main_scoring import FKEY, feed, gw, kv, locks, run  # noqa: F401


def _soft_cached(kv, locks):
    kv.store[FKEY] = {"full": {"fundamental_score": 60}, "fallback": False}
    locks.soft = True


def test_the_background_lock_outlives_the_upstream_timeout(feed, kv, locks, monkeypatch):
    _soft_cached(kv, locks)

    async def bg(symbol, client, cache_key):
        return None

    monkeypatch.setattr(gw, "_background_refresh_fundamental", bg)

    async def go():
        await gw._fetch_fundamental_cached("TCS", "C")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(go())
    assert locks.acquired == [("TCS", gw._FUNDAMENTAL_BG_LOCK_TTL_SEC)]
    assert gw._FUNDAMENTAL_BG_LOCK_TTL_SEC > 35          # the upstream timeout in _refresh_fundamental_upstream


def test_a_running_background_refresh_is_strongly_referenced_until_done(feed, kv, locks, monkeypatch):
    _soft_cached(kv, locks)
    gate = {}

    async def bg(symbol, client, cache_key):
        gate["started"] = True
        await gate["release"].wait()
        gate["finished"] = True

    monkeypatch.setattr(gw, "_background_refresh_fundamental", bg)

    async def go():
        gate["release"] = asyncio.Event()
        await gw._fetch_fundamental_cached("TCS", "C")
        for _ in range(5):
            await asyncio.sleep(0)
        assert gate.get("started") and len(gw._BG_REFRESH_TASKS) == 1      # was: no reference held by the gateway
        gc.collect()
        gate["release"].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert gate.get("finished") and not gw._BG_REFRESH_TASKS            # released once done

    run(go())


def test_a_failing_background_refresh_does_not_leave_a_task_behind(feed, kv, locks, monkeypatch):
    _soft_cached(kv, locks)

    async def bg(symbol, client, cache_key):
        raise RuntimeError("upstream blew up")

    monkeypatch.setattr(gw, "_background_refresh_fundamental", bg)

    async def go():
        await gw._fetch_fundamental_cached("TCS", "C")
        for _ in range(5):
            await asyncio.sleep(0)

    run(go())
    assert not gw._BG_REFRESH_TASKS
