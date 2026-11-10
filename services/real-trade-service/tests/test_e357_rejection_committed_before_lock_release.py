"""E357: a failed BUY placement commits its REJECTED status + event BEFORE shared_symbol_lock.release() (which rolls back on failure)."""
import ast
import logging
import pathlib
import types

SRC = pathlib.Path(__file__).resolve().parents[1] / "entry_engine" / "entry.py"
_TEXT = SRC.read_text(encoding="utf-8")
_TREE = ast.parse(_TEXT)


def _fn(name):
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


class _Event:
    def __init__(self, order_id, event_type, detail):
        self.order_id, self.event_type, self.detail = order_id, event_type, detail


class _DB:
    def __init__(self, fail_commits=0, fail_rollback=False):
        self.fail_commits, self.fail_rollback = fail_commits, fail_rollback
        self.pending, self.committed, self.rollbacks = [], [], 0

    def add(self, o):
        self.pending.append(o)

    def commit(self):
        if self.fail_commits > 0:
            self.fail_commits -= 1
            raise RuntimeError("connection dropped")
        self.committed.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.rollbacks += 1
        self.pending = []
        if self.fail_rollback:
            raise RuntimeError("rollback failed")


def _load():
    ns = {"models": types.SimpleNamespace(TradeOrderEvent=_Event), "Session": object,
          "logger": logging.getLogger("e357-test")}
    exec(compile(ast.Module(body=[_fn("_commit_rejection")], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["_commit_rejection"]


def test_rejection_is_committed():
    order = types.SimpleNamespace(id=7, status="PLACED")
    db = _DB()
    assert _load()(db, order, "boom") is True
    assert order.status == "REJECTED"
    assert [(e.order_id, e.event_type, e.detail) for e in db.committed] == [(7, "REJECTED", "boom")]


def test_one_failed_commit_is_retried_without_a_duplicate_event():
    order = types.SimpleNamespace(id=7, status="PLACED")
    db = _DB(fail_commits=1)
    assert _load()(db, order, "boom") is True
    assert db.rollbacks == 1 and len(db.committed) == 1 and order.status == "REJECTED"


def test_two_failures_return_false_and_never_raise(caplog):
    order = types.SimpleNamespace(id=7, status="PLACED")
    db = _DB(fail_commits=5, fail_rollback=True)
    with caplog.at_level(logging.ERROR, logger="e357-test"):
        assert _load()(db, order, "boom") is False
    assert "could not commit the REJECTED status" in caplog.text
    assert "also failed" in caplog.text


def test_call_site_commits_before_the_lock_release():
    seg = ast.get_source_segment(_TEXT, _fn("evaluate_mode")) if any(
        getattr(n, "name", "") == "evaluate_mode" for n in ast.walk(_TREE)) else _TEXT
    i = seg.index("_commit_rejection(db, order, f\"Dhan placement failed: {ex}\")")
    j = seg.index("shared_symbol_lock.release(db, cand.symbol)", i)
    assert i < j
    assert 'order.status = "REJECTED"\n                    # AUDIT FIX (session60)' not in seg
