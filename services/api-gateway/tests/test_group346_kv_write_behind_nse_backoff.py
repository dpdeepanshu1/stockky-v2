"""
group346 (log review #13, #16):
  * kv_cache durable writes issued from the asyncio event-loop thread are queued to a writer thread instead of doing an
    Oracle round trip + commit on the loop (the gateway loop was blocked 3.6-3.9 s in an Oracle commit).
  * one NSE endpoint that answers 404 / 5xx / fails at transport level is left alone for a while.

Run from services/api-gateway:
    python3 -m pytest tests/test_group346_kv_write_behind_nse_backoff.py -q
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


@pytest.fixture()
def db_calls(monkeypatch):
    calls = []

    def fake_set(key, value, ttl=None):
        calls.append((threading.current_thread().name, key, value))
        time.sleep(0.03)

    def fake_del(key):
        calls.append((threading.current_thread().name, "DEL", key))

    monkeypatch.setattr(kc, "_neon_set", fake_set)
    monkeypatch.setattr(kc, "_neon_delete_sync", fake_del)
    monkeypatch.setattr(kc, "_is_durable", lambda k: True)
    monkeypatch.setattr(kc, "KV_WRITE_BEHIND", True)
    return calls


def test_worker_thread_writes_synchronously(db_calls):
    kc.kv_set("a", 1)
    assert db_calls == [(threading.current_thread().name, "a", 1)]


def test_event_loop_caller_is_not_blocked_and_memory_is_current(db_calls):
    async def go():
        t0 = time.perf_counter()
        for i in range(20):
            kc.kv_set("k", i)
        kc.kv_set_many({"m1": 1, "m2": 2})
        kc.kv_delete("gone")
        elapsed = time.perf_counter() - t0
        assert kc.kv_get("k") == 19
        return elapsed

    elapsed = asyncio.run(go())
    assert elapsed < 0.2          # 23 blocking 30 ms writes would take > 0.6 s
    assert kc.flush_pending(5.0)
    assert {c[0] for c in db_calls} == {"kv-write-behind"}
    k_writes = [c for c in db_calls if c[1] == "k"]
    assert k_writes[-1][2] == 19 and len(k_writes) < 20     # latest value wins, writes coalesced
    assert any(c[1] == "DEL" and c[2] == "gone" for c in db_calls)
    assert {"m1", "m2"} <= {c[1] for c in db_calls}


def test_switch_off_restores_inline_write(db_calls, monkeypatch):
    monkeypatch.setattr(kc, "KV_WRITE_BEHIND", False)

    async def go():
        kc.kv_set("z", 1)

    asyncio.run(go())
    assert db_calls and db_calls[0][0] == threading.current_thread().name


def test_flush_pending_is_true_when_nothing_queued():
    assert kc.flush_pending(0.5) is True


# ── NSE endpoint backoff ─────────────────────────────────────────────────────────────────────────────────────────
def test_nse_endpoint_pause_doubles_and_is_per_endpoint(monkeypatch):
    import main as gw

    monkeypatch.delenv("NSE_ENDPOINT_FAIL_PAUSE_S", raising=False)
    monkeypatch.delenv("NSE_ENDPOINT_FAIL_MAX_S", raising=False)
    gw._nse_endpoint_fail.clear()
    assert not gw._nse_endpoint_paused("a")
    gw._nse_endpoint_note_failure("a", "HTTP 404")
    assert gw._nse_endpoint_paused("a") and not gw._nse_endpoint_paused("b")
    first = gw._nse_endpoint_fail["a"][1] - time.time()
    gw._nse_endpoint_note_failure("a", "HTTP 404")
    second = gw._nse_endpoint_fail["a"][1] - time.time()
    assert 880 < first <= 900 and 1780 < second <= 1800
    for _ in range(6):
        gw._nse_endpoint_note_failure("a", "HTTP 503")
    assert gw._nse_endpoint_fail["a"][1] - time.time() <= 3600
    gw._nse_endpoint_fail.clear()


def test_nse_endpoint_pause_can_be_switched_off(monkeypatch):
    import main as gw

    monkeypatch.setenv("NSE_ENDPOINT_FAIL_PAUSE_S", "0")
    gw._nse_endpoint_fail.clear()
    gw._nse_endpoint_note_failure("a", "HTTP 404")
    assert not gw._nse_endpoint_paused("a")


def test_paused_endpoint_makes_no_request(monkeypatch):
    import main as gw

    gw._nse_endpoint_fail.clear()
    gw._nse_api_block_until = 0.0
    monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("a paused endpoint must not be requested")

    monkeypatch.setattr(gw, "_get_nse_client", boom)
    gw._nse_endpoint_note_failure("live-analysis-variations?index=gainers", "HTTP 404")
    assert gw._fetch_from_nse_api("live-analysis-variations?index=gainers", "nse:gainers") is None
    gw._nse_endpoint_fail.clear()
