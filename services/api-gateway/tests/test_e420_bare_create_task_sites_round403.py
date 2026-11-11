"""E420: the three remaining bare fire-and-forget `create_task` sites (the two startup warms in main.py and the stale-serve universe rebuild in
bound_routes_c) go through `_spawn_background_task`: a strong reference until done, and for the universe rebuild one running job per key."""
from __future__ import annotations

import asyncio
import gc

from test_main_market_universe_routes import FakeStaleCache, _syms, _universe, gw, kv, uenv  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


def test_a_spawned_task_is_referenced_until_it_finishes_and_then_released():
    gate = {}

    async def job():
        gate["started"] = True
        await gate["release"].wait()
        gate["finished"] = True

    async def go():
        gate["release"] = asyncio.Event()
        t = gw._spawn_background_task(job())
        for _ in range(3):
            await asyncio.sleep(0)
        assert gate.get("started") and t in gw._BG_REFRESH_TASKS
        gc.collect()
        gate["release"].set()
        for _ in range(3):
            await asyncio.sleep(0)
        assert gate.get("finished") and t not in gw._BG_REFRESH_TASKS

    _run(go())


def test_a_keyed_job_already_running_is_reused_and_the_new_coroutine_never_starts():
    ran = []
    gate = {}

    async def first():
        ran.append("first")
        await gate["release"].wait()

    async def second():
        ran.append("second")

    async def go():
        gate["release"] = asyncio.Event()
        a = gw._spawn_background_task(first(), key="k-e420")
        await asyncio.sleep(0)
        b = gw._spawn_background_task(second(), key="k-e420")
        assert b is a
        gate["release"].set()
        for _ in range(3):
            await asyncio.sleep(0)
        assert "k-e420" not in gw._BG_KEYED_TASKS
        c = gw._spawn_background_task(second(), key="k-e420")      # the key is free again once the first job is done
        assert c is not a
        await c

    _run(go())
    assert ran == ["first", "second"]


def test_a_failing_keyed_job_frees_its_key():
    async def boom():
        raise RuntimeError("x")

    async def go():
        gw._spawn_background_task(boom(), key="k-fail-e420")
        for _ in range(3):
            await asyncio.sleep(0)

    _run(go())
    assert "k-fail-e420" not in gw._BG_KEYED_TASKS


def test_the_stale_serve_rebuild_goes_through_the_keyed_spawner(uenv, kv, monkeypatch):
    monkeypatch.setattr(gw, "_kv_cache", FakeStaleCache(_syms(65, "T")))
    seen = []

    def spawn(coro, key=None):
        seen.append(key)
        coro.close()

    monkeypatch.setattr(gw, "_spawn_background_task", spawn)
    out = _universe(cached=True)
    assert out["stale"] is True and seen == ["scan-universe-rebuild"]
