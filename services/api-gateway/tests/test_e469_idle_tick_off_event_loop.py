"""E469: /ops/idle-tick and /ops/neon-keepalive called blocking Neon reads (`_neon_keepalive_ping`, `_redis_get` = kv_cache.get)
directly on the event loop. While Neon is slow or waking, every other gateway request stalled. Now both run on a worker thread.
No fastapi needed (a tiny APIRouter stand-in is put in sys.modules just for the import). Plain asserts; runs with pytest or
`python3 tests/test_e469_idle_tick_off_event_loop.py`."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)


def _load(name):
    fake = types.ModuleType("fastapi")

    class APIRouter:
        def _d(self, *a, **k):
            return lambda f: f
        get = post = put = delete = _d
    fake.APIRouter = APIRouter
    saved = sys.modules.get("fastapi")
    sys.modules["fastapi"] = fake
    try:
        spec = importlib.util.spec_from_file_location("e469_" + name, os.path.join(_SERVICE, "gateway_support", name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            sys.modules.pop("fastapi", None)
        else:
            sys.modules["fastapi"] = saved
    return mod


def _ref(threads, delay=0.3):
    main_thread = threading.get_ident()

    def ping():
        threads.append(("ping", threading.get_ident() != main_thread))
        time.sleep(delay)
        return {"ok": True}

    def rget(key):
        threads.append(("rget", threading.get_ident() != main_thread))
        time.sleep(delay)
        return {"x": 1}

    async def hot(force=False):
        return None

    ref = MagicMock()  # the builders read decorator arguments (ref.Depends, ref._require_admin ...) at build time
    ref.asyncio, ref.datetime, ref.IST = asyncio, datetime, None
    ref.logger = SimpleNamespace(debug=lambda *a, **k: None)
    ref._market_session_phase_ist = lambda: "open"
    ref._neon_keepalive_ping, ref._redis_get = ping, rget
    ref.get_market_indices = lambda force_refresh=False: None
    ref.HOT_STOCKS_CACHE_KEY, ref.stockky_hot_stocks = "k", hot
    return ref


async def _worst_gap_while(coro):
    gaps, stop = [], False

    async def ticker():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
    t = asyncio.ensure_future(ticker())
    await asyncio.sleep(0.05)
    out = await coro
    stop = True
    await t
    return out, max(gaps)


def test_idle_tick_does_not_block_the_loop():
    threads = []
    ns = _load("bound_routes_f").build_bound_routes_f(_ref(threads))
    out, gap = asyncio.new_event_loop().run_until_complete(_worst_gap_while(ns.ops_idle_tick()))
    assert out["ran"] is True and out["actions"][0] == "neon_keepalive_ok" and "hot_stocks_cached" in out["actions"]
    assert ("ping", True) in threads and ("rget", True) in threads, threads
    assert gap < 0.15, gap


def test_neon_keepalive_route_does_not_block_the_loop():
    threads = []
    ns = _load("bound_routes_b").build_bound_routes_b(_ref(threads))
    out, gap = asyncio.new_event_loop().run_until_complete(_worst_gap_while(ns.ops_neon_keepalive()))
    assert out["ok"] is True and "at" in out
    assert ("ping", True) in threads, threads
    assert gap < 0.15, gap


def test_scan_stream_price_fallback_is_on_a_worker_thread():
    """The per-symbol post-processing in /api/scan/stream runs on the loop; its single-quote fallback (httpx.get, 4 s) must not."""
    import ast
    src = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_j.py"), encoding="utf-8").read()
    t = ast.parse(src)
    parents = {}
    for n in ast.walk(t):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    hits = 0
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "ref._fetch_price_from_quote":
            hits += 1
            # a direct call is only allowed inside a sync def that is itself handed to a thread (the line-658 helper)
            p = parents.get(n)
            while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                p = parents.get(p)
            assert isinstance(p, ast.FunctionDef), "direct call inside an async def: " + str(n.lineno)
    assert "ref.asyncio.to_thread(ref._fetch_price_from_quote" in src and hits >= 1


if __name__ == "__main__":
    test_scan_stream_price_fallback_is_on_a_worker_thread()
    test_idle_tick_does_not_block_the_loop()
    test_neon_keepalive_route_does_not_block_the_loop()
    print("3 passed")
