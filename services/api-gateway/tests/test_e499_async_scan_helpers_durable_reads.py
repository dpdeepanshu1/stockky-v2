"""E499: a read of the async scan helpers in `main.py` (the route bodies are all in `gateway_support` now).

`_fetch_fundamental_cached` (2 reads), `_fetch_events_cached`, `_fetch_news_cached`, the crash handler of `run_scan_parallel` and the per-symbol
`last_decision` read in `stockky_hot_stocks` called `_redis_get` straight from the event loop. `_redis_get` goes to the durable store (Neon / Oracle)
on a memory miss; kv_cache bounds that read at KV_LOOP_READ_TIMEOUT_S (1.5 s) but the loop is frozen for the wait, and these run once per symbol in a
scan (E471 already moved the decide-cache, last-scan and Hot Picks cache reads). They now go through `asyncio.to_thread`.

No fastapi / httpx needed (main.py is read as text / AST). Runs with pytest or `python3 tests/test_e499_async_scan_helpers_durable_reads.py`.
"""
from __future__ import annotations

import ast
import asyncio
import os
import threading
import types

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_SRC = open(_MAIN, encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_FUNCS = ("_fetch_fundamental_cached", "_fetch_events_cached", "_fetch_news_cached", "run_scan_parallel", "stockky_hot_stocks")


def _fn(name):
    return next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def _direct_redis_get_calls(fn):
    """Calls of `_redis_get(...)` that are not handed to asyncio.to_thread / wait_for as an argument."""
    hits = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_redis_get":
            hits.append(node.lineno)
    return hits


def test_no_direct_redis_get_left_in_the_async_scan_helpers():
    for name in _FUNCS:
        assert _direct_redis_get_calls(_fn(name)) == [], name


def test_the_moved_reads_are_to_thread_calls():
    seen = 0
    for name in _FUNCS:
        for node in ast.walk(_fn(name)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "_redis_get"):
                seen += 1
    assert seen >= 6          # fundamental x2, events, news, crash handler, last_decision (stockky_hot_stocks has more from E471)


def _lift(name, ns):
    fn = _fn(name)
    fn.decorator_list = []
    fn.returns = None
    for a in fn.args.args:
        a.annotation = None
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


def test_news_read_runs_off_the_event_loop_and_returns_the_cached_dict():
    seen = []

    def fake_get(key):
        seen.append((key, threading.get_ident()))
        return {"headlines": ["a"], "sentiment_score": 61.0}

    ns = {"asyncio": asyncio, "_redis_get": fake_get, "NEWS_CACHE_PREFIX": "news:", "Optional": object, "httpx": types.SimpleNamespace(AsyncClient=object)}
    h = _lift("_fetch_news_cached", ns)

    async def run():
        loop_tid = threading.get_ident()
        out = await h("ABC", None)
        return loop_tid, out

    loop_tid, out = asyncio.run(run())
    assert out == {"headlines": ["a"], "sentiment_score": 61.0}
    assert seen and seen[0][0] == "news:ABC" and seen[0][1] != loop_tid


if __name__ == "__main__":
    import sys
    fails = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                fails += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:160])
    sys.exit(1 if fails else 0)
