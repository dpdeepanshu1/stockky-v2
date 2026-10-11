"""group319: operator routes in main.py must not run db.commit() on the shared event loop."""
import ast
import asyncio
import pathlib
import threading

MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"


def test_no_direct_db_commit_inside_async_routes():
    src = MAIN.read_text()
    tree = ast.parse(src)
    offenders = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "commit" and ast.get_source_segment(src, n) == "db.commit()"):
                    offenders.append((fn.name, n.lineno))
    assert offenders == []


def test_commit_runs_on_a_worker_thread():
    import main

    seen = {}

    class FakeDb:
        def commit(self):
            seen["thread"] = threading.current_thread()

    asyncio.run(main._commit_off_loop(FakeDb()))
    assert seen["thread"] is not threading.main_thread()


def test_commit_error_propagates():
    import main

    class BadDb:
        def commit(self):
            raise RuntimeError("boom")

    try:
        asyncio.run(main._commit_off_loop(BadDb()))
    except RuntimeError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("commit error was swallowed")
