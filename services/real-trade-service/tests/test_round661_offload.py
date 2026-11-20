"""round 661 (D7): read-only DB routes run in a worker thread. Source-level checks (main.py needs the full service stack to import) plus a
behavioural check of the _offload wrapper itself."""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import threading
import time

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_SRC = open(_MAIN, encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _offloaded():
    return [n for n in _TREE.body if isinstance(n, ast.FunctionDef) and any(ast.unparse(d) == "_offload" for d in n.decorator_list)]


def _wrapper_factory():
    fn = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_offload")
    ns: dict = {}
    from starlette.concurrency import run_in_threadpool
    ns["run_in_threadpool"] = run_in_threadpool
    exec(compile(ast.Module([fn], []), _MAIN, "exec"), ns)
    return ns["_offload"]


def test_offloaded_routes_are_plain_get_defs():
    fns = _offloaded()
    assert len(fns) >= 25
    for n in fns:
        decos = [ast.unparse(d) for d in n.decorator_list]
        assert decos[0].startswith(("app.get(", "app.post(")), (n.name, decos)
        assert decos[1] == "_offload"
        assert not any(isinstance(x, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for x in ast.walk(n)), n.name


def test_health_stays_async_and_unwrapped():
    h = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "health")
    assert [ast.unparse(d) for d in h.decorator_list] == ["app.get('/health')"]


def test_wrapper_runs_in_thread_keeps_signature_and_stays_coroutine():
    off = _wrapper_factory()
    seen = {}

    def handler(mode: str, limit: int = 5):
        seen["thread"] = threading.current_thread().name
        return {"mode": mode, "limit": limit}

    w = off(handler)
    assert inspect.iscoroutinefunction(w)
    assert list(inspect.signature(w).parameters) == ["mode", "limit"]
    assert asyncio.run(w("demo", limit=7)) == {"mode": "demo", "limit": 7}
    assert seen["thread"] != threading.main_thread().name


def test_slow_handler_does_not_block_the_event_loop():
    off = _wrapper_factory()

    def slow():
        time.sleep(0.6)
        return "done"

    async def main():
        t0 = time.monotonic()
        task = asyncio.create_task(off(slow)())
        await asyncio.sleep(0.05)
        responsive_at = time.monotonic() - t0     # a /health-style coroutine got to run while the DB call was stuck
        return responsive_at, await task

    responsive_at, res = asyncio.run(main())
    assert res == "done" and responsive_at < 0.3


# round 664 (D7b): the state-changing routes that are plain blocking code (DB commits, the TOTP token refresh, the broker holdings sync)
_D7B = {"regenerate_dhan_token", "disarm", "emergency_pause", "approvals_reset", "approvals_approve", "approvals_reject", "swing_resume", "manual_holdings_sync"}
# deliberately left on the loop: they start a thread / flip an in-memory object / await, or are the liveness probe
_STAY = {"health", "resilience_reset_route", "swing_scan_now", "arm", "login", "run_cycle", "manual_order_confirm", "manual_close_position", "close_all_and_disarm"}


def test_d7b_routes_are_offloaded_posts():
    by = {n.name: n for n in _offloaded()}
    for name in _D7B:
        assert name in by, name
        assert ast.unparse(by[name].decorator_list[0]).startswith("app.post("), name


def test_routes_that_must_stay_on_the_loop_are_untouched():
    off = {n.name for n in _offloaded()}
    assert not (_STAY & off), _STAY & off
    asyncs = {n.name for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef)}
    assert {"health", "resilience_reset_route", "swing_scan_now", "arm", "login"} <= asyncs
