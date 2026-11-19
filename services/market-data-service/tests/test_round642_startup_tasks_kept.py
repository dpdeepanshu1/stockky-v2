"""Round 642 (E668): the start-up loops of market-data are held by `_keep_task` instead of being bare `asyncio.create_task(...)` statements.

The event loop keeps only a weak reference to a task, so a task nobody else holds can be garbage-collected while it runs; an exception in such a
task is only reported when it is collected, and the loop is silently gone. `_keep_task` keeps a strong reference until the task ends and logs
an error when it stopped with an exception (and a warning when a loop that should run forever returned).

The helper is taken out of main.py by AST and run on its own, so the file needs neither fastapi nor the rest of the service.
Plain functions; `python3 <this file>` runs them without pytest.
"""
from __future__ import annotations

import ast
import asyncio
import gc
import logging
import os
import sys

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
SRC = open(MAIN, encoding="utf-8").read()


def _helper():
    tree = ast.parse(SRC)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "_keep_task")
            or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_BG_LOOP_TASKS")]
    assert len(keep) == 2, "main.py must define _BG_LOOP_TASKS and _keep_task"
    log = logging.getLogger("round642-" + str(id(keep)))
    log.propagate = False
    seen: list = []

    class _H(logging.Handler):
        def emit(self, record):
            seen.append((record.levelname, record.getMessage()))
    log.addHandler(_H(level=logging.DEBUG))
    ns = {"logger": log}
    exec(compile(ast.Module(body=keep, type_ignores=[]), MAIN, "exec"), ns)
    return ns, seen


def test_a_kept_task_survives_garbage_collection_and_is_released_when_done():
    ns, _ = _helper()

    async def run():
        ev = asyncio.Event()

        async def loop():
            await ev.wait()
        ns["_keep_task"](asyncio.create_task(loop()), "x")
        gc.collect()
        assert len(ns["_BG_LOOP_TASKS"]) == 1
        ev.set()
        await asyncio.sleep(0.05)
        assert len(ns["_BG_LOOP_TASKS"]) == 0
    asyncio.run(run())


def test_an_error_in_the_task_is_logged_once_at_error_level():
    ns, seen = _helper()

    async def run():
        async def boom():
            raise RuntimeError("bad loop")
        ns["_keep_task"](asyncio.create_task(boom()), "feed loop")
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert [lv for lv, _ in seen] == ["ERROR"] and "feed loop" in seen[0][1] and "bad loop" in seen[0][1]


def test_a_forever_loop_that_returns_is_warned_about_but_a_one_shot_is_not():
    ns, seen = _helper()

    async def run():
        async def done():
            return None
        ns["_keep_task"](asyncio.create_task(done()), "forever loop")
        ns["_keep_task"](asyncio.create_task(done()), "one shot", forever=False)
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert [lv for lv, _ in seen] == ["WARNING"] and "forever loop" in seen[0][1]


def test_a_cancelled_task_is_released_quietly():
    ns, seen = _helper()

    async def run():
        async def loop():
            await asyncio.sleep(60)
        t = ns["_keep_task"](asyncio.create_task(loop()), "x")
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.sleep(0.05)
        assert len(ns["_BG_LOOP_TASKS"]) == 0
    asyncio.run(run())
    assert seen == []


def test_a_test_double_instead_of_a_task_does_not_break_start_up():
    ns, seen = _helper()
    assert ns["_keep_task"](None, "x") is None
    assert ns["_keep_task"](object(), "x") is not None
    assert seen == []


def test_no_start_up_loop_is_a_bare_create_task_statement_any_more():
    tree = ast.parse(SRC)
    bare = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and ast.unparse(n.value.func).endswith("create_task")]
    assert bare == [], f"bare create_task at lines {bare}"
    assert SRC.count("_keep_task(asyncio.create_task(") == 2


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
