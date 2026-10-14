"""
group359 (14 Oct log item 3: "event loop blocked for 3.2s ... oracledb rollback, closing a connection over SSL"): durable READS
issued from the asyncio event-loop thread run on a worker with a bounded wait instead of blocking the loop.

Run from services/api-gateway:
    python3 -m pytest tests/test_group359_kv_loop_read_guard.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import kv_cache as kc
import loop_watchdog as lw


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(kc, "KV_LOOP_READ_GUARD", True)
    monkeypatch.setattr(kc, "_is_durable", lambda k: True)
    monkeypatch.setattr(kc, "_get_redis", lambda: None)
    monkeypatch.delenv("KV_LOOP_READ_TIMEOUT_S", raising=False)
    monkeypatch.delenv("KV_LOOP_READ_MAX_INFLIGHT", raising=False)
    kc._mem._store.clear()
    with kc._lr_lock:
        kc._lr_inflight.clear()
    for k in kc._lr_stats:
        kc._lr_stats[k] = 0
    yield
    kc._mem._store.clear()


def _slow_fetch(delay, value="V", calls=None):
    def f(key):
        if calls is not None:
            calls.append((threading.current_thread().name, key))
        time.sleep(delay)
        return value, None
    return f


def test_worker_thread_reads_inline_no_pool(monkeypatch):
    calls = []
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.0, "X", calls))
    assert kc.kv_get("a") == "X"
    assert calls[0][0] == threading.current_thread().name
    assert kc.loop_read_guard_status()["guarded"] == 0


def test_loop_read_runs_on_a_worker_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.0, "X", calls))

    async def go():
        return kc.kv_get("a")

    assert asyncio.run(go()) == "X"
    assert calls[0][0].startswith("kv-loop-read")
    assert kc.kv_get("a") == "X"                         # warmed: memory hit now
    assert len(calls) == 1


def test_slow_read_returns_a_miss_within_the_timeout_and_warms_memory(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.2")
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.8, "LATE"))

    async def go():
        t0 = time.perf_counter()
        v = kc.kv_get("slow")
        return v, time.perf_counter() - t0

    v, took = asyncio.run(go())
    assert v is None and took < 0.6                      # not the 0.8 s the read needs
    assert kc.loop_read_guard_status()["timeouts"] == 1
    time.sleep(1.0)                                      # the worker finished in the background
    assert kc.kv_get("slow") == "LATE"


def test_second_loop_caller_is_a_memory_hit_after_the_first_read(monkeypatch):
    calls = []
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.2, "J", calls))

    async def two():
        return kc.kv_get("k2"), kc.kv_get("k2")

    assert asyncio.run(two()) == ("J", "J")
    assert sum(1 for c in calls if c[1] == "k2") == 1


def test_in_flight_read_is_joined_not_duplicated(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.1")
    calls = []
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.5, "S", calls))

    async def go():
        first = kc.kv_get("same")                        # times out, read keeps running
        second = kc.kv_get("same")                       # joins the running read
        return first, second

    assert asyncio.run(go()) == (None, None)
    st = kc.loop_read_guard_status()
    assert st["guarded"] == 1 and st["joined"] == 1 and len(calls) == 1


def test_reads_are_shed_past_the_inflight_cap(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_MAX_INFLIGHT", "1")
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.1")
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.5, "S"))

    async def go():
        return kc.kv_get("one"), kc.kv_get("two")

    assert asyncio.run(go()) == (None, None)
    assert kc.loop_read_guard_status()["shed"] == 1


def test_switch_off_reads_inline_on_the_loop(monkeypatch):
    monkeypatch.setattr(kc, "KV_LOOP_READ_GUARD", False)
    calls = []
    monkeypatch.setattr(kc, "_neon_fetch", _slow_fetch(0.0, "X", calls))

    async def go():
        return kc.kv_get("a")

    assert asyncio.run(go()) == "X"
    assert not calls[0][0].startswith("kv-loop-read")


def test_blank_timeout_uses_default(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", " ")
    assert kc._lr_env_float("KV_LOOP_READ_TIMEOUT_S", 1.5, 0.1) == 1.5
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "abc")
    assert kc._lr_env_float("KV_LOOP_READ_TIMEOUT_S", 1.5, 0.1) == 1.5
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.01")  # below the floor
    assert kc._lr_env_float("KV_LOOP_READ_TIMEOUT_S", 1.5, 0.1) == 1.5


def test_get_stale_is_guarded_and_returns_none_on_timeout(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.1")
    seen = []

    def slow(key):
        seen.append(threading.current_thread().name)
        time.sleep(0.5)
        return "OLD"

    monkeypatch.setattr(kc, "_stale_db_read", slow)

    async def go():
        return kc.kv_get_stale("s")

    assert asyncio.run(go()) is None
    time.sleep(0.6)
    assert seen[0].startswith("kv-loop-read")


def test_get_stale_inline_off_the_loop(monkeypatch):
    monkeypatch.setattr(kc, "_stale_db_read", lambda key: "OLD")
    assert kc.kv_get_stale("s") == "OLD"


def test_get_many_is_guarded_and_merges_memory_and_db(monkeypatch):
    kc._mem.set("have", 1, ttl=60)
    monkeypatch.setattr(kc, "_get_neon", lambda: object())
    seen = []

    def fake_many(eng, keys):
        seen.append((threading.current_thread().name, list(keys)))
        return {"db": 2}

    monkeypatch.setattr(kc, "_many_db_read", fake_many)

    async def go():
        return kc.kv_get_many(["have", "db"])

    assert asyncio.run(go()) == {"have": 1, "db": 2}
    assert seen[0][0].startswith("kv-loop-read") and seen[0][1] == ["db"]


def test_get_many_slow_db_returns_the_memory_part(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.1")
    kc._mem.set("have", 1, ttl=60)
    monkeypatch.setattr(kc, "_get_neon", lambda: object())
    monkeypatch.setattr(kc, "_many_db_read", lambda e, k: (time.sleep(0.5), {"db": 2})[1])

    async def go():
        return kc.kv_get_many(["have", "db"])

    assert asyncio.run(go()) == {"have": 1}


def test_status_probe_is_bounded_on_the_loop(monkeypatch):
    monkeypatch.setenv("KV_LOOP_READ_TIMEOUT_S", "0.1")

    class _Eng:
        def connect(self):
            time.sleep(0.5)
            raise RuntimeError("late")

    monkeypatch.setattr(kc, "_get_neon", lambda: _Eng())
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:                                   # minimal environments: the probe only needs `text`
        import types
        fake = types.ModuleType("sqlalchemy")
        fake.text = lambda s: s
        monkeypatch.setitem(sys.modules, "sqlalchemy", fake)

    async def go():
        t0 = time.perf_counter()
        st = kc.status()
        return st, time.perf_counter() - t0

    st, took = asyncio.run(go())
    assert took < 0.4 and st["neon_connected"] is False and "guard" in st["neon_error"]
    assert "loop_read_guard" in st


def test_watchdog_stack_names_the_calling_code_not_the_cache_shim():
    # a frame in this test file stands in for the handler; kv_cache frames must be skipped
    lw._state["loop_thread"] = threading.get_ident()
    stack = lw._loop_thread_stack()
    assert "called from our code:" in stack and "test_group359" in stack
