"""E471: gateway main.py read durable cache keys (kv_cache: a Neon round trip on a miss; on the loop thread kv_cache only caps the wait at
KV_LOOP_READ_TIMEOUT_S = 1.5 s, which still holds the loop) straight from async code: the decide cache once per scanned symbol, the last full
scan and the hot-stocks cache once per run. They now go through asyncio.to_thread. Writes already queue write-behind on the loop thread and
non-durable keys (stockky:fundamental:/event:/news:/scan_task:/last_decision:) are memory-only, so those direct calls are left alone.
AST guard, no fastapi needed: no direct `_redis_get(<durable key>)` inside an `async def` of main.py."""
from __future__ import annotations

import ast
import os
import sys

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DURABLE_NAMES = {"DECIDE_CACHE_PREFIX", "LAST_FULL_SCAN_KEY", "HOT_STOCKS_CACHE_KEY", "HOT_RESULT_KEY", "SCAN_UNIVERSE_KEY",
                  "SCAN_UNIVERSE_STALE_KEY", "BATCH_RESULT_CACHE_PREFIX"}


def _offenders(src: str):
    tree = ast.parse(src)
    bad = []

    def walk(node, in_async):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.AsyncFunctionDef):
                walk(c, True)
            elif isinstance(c, ast.FunctionDef):
                walk(c, False)
            else:
                if in_async and isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_redis_get" and c.args:
                    names = {n.id for n in ast.walk(c.args[0]) if isinstance(n, ast.Name)}
                    if names & _DURABLE_NAMES:
                        bad.append((c.lineno, ast.unparse(c)[:80]))
                walk(c, in_async)
    walk(tree, False)
    return bad


def test_main_has_no_direct_durable_redis_get_in_async_code():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    bad = _offenders(src)
    assert not bad, bad


def test_scan_worker_decide_cache_read_is_threaded():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    assert "cached_decide = await asyncio.to_thread(_redis_get, cache_key)" in src
    assert "cache_key = f\"{DECIDE_CACHE_PREFIX}{base_sym}\"" in src


if __name__ == "__main__":
    test_main_has_no_direct_durable_redis_get_in_async_code()
    test_scan_worker_decide_cache_read_is_threaded()
    print("2 passed")
