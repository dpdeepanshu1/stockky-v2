"""round 665 (D7c): the dashboard polling routes keep awaiting prices on the loop but run their blocking DB/broker step in a worker thread.
Source-level checks (main.py needs the full service stack to import) plus a behavioural check of the _db_off_loop helper and its kill-switch."""
from __future__ import annotations

import ast
import asyncio
import os
import threading
import time

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_SRC = open(_MAIN, encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_ROUTES = ("list_positions", "list_orders", "list_candidates", "structure_status", "dhan_live_positions")


def _helper():
    fn = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_db_off_loop")
    ns: dict = {}
    from starlette.concurrency import run_in_threadpool
    ns["run_in_threadpool"] = run_in_threadpool
    exec(compile(ast.Module([fn], []), _MAIN, "exec"), ns)
    return ns["_db_off_loop"]


def _route(name):
    return next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_routes_use_the_helper_and_stay_async():
    for name in _ROUTES:
        n = _route(name)
        assert any(isinstance(x, ast.Call) and ast.unparse(x.func) == "_db_off_loop" for x in ast.walk(n)), name


def test_no_bare_db_query_left_on_the_loop_in_these_routes():
    """every `db.query(` inside these routes sits inside a nested sync def (run off-loop), never directly in the async body."""
    for name in _ROUTES:
        n = _route(name)
        nested = [x for x in ast.walk(n) if isinstance(x, ast.FunctionDef)]
        inside = {id(y) for f in nested for y in ast.walk(f)}
        for x in ast.walk(n):
            if isinstance(x, ast.Attribute) and x.attr == "query" and ast.unparse(x.value) == "db":
                assert id(x) in inside, (name, x.lineno)


def test_helper_runs_in_worker_thread_and_keeps_loop_free():
    h = _helper()
    os.environ.pop("ROUTES_DB_OFF_LOOP", None)
    seen = {}

    def slow():
        seen["t"] = threading.current_thread().name
        time.sleep(0.4)
        return 7

    async def go():
        ticks = 0
        task = asyncio.create_task(h(slow))
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        return await task, ticks

    val, ticks = asyncio.run(go())
    assert val == 7 and ticks >= 10
    assert seen["t"] != threading.main_thread().name


def test_kill_switch_runs_inline():
    h = _helper()
    os.environ["ROUTES_DB_OFF_LOOP"] = "0"
    try:
        seen = {}

        def f(a, b=2):
            seen["t"] = threading.current_thread().name
            return a + b

        assert asyncio.run(h(f, 1, b=3)) == 4
        assert seen["t"] == threading.main_thread().name
    finally:
        os.environ.pop("ROUTES_DB_OFF_LOOP", None)


def test_helper_propagates_exceptions():
    h = _helper()

    def boom():
        raise ValueError("x")

    try:
        asyncio.run(h(boom))
        assert False
    except ValueError:
        pass
