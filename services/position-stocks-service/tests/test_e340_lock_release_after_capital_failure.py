"""
E340 - the symbol-lock release no longer depends on the capital release succeeding.

Where a row is closed and committed first, `ledger.release_capital(...)` ran next and `shared_symbol_lock.release(...)` after it. If the capital
release raised (a DB error), the lock release was skipped; the closed row is never visited again, so the lock stayed until the sweep cleared it
(SYMBOL_LOCK_SWEEP_MIN_AGE_S, 600 s). Each such pair is now `try: release_capital finally: lock release`. The exception still propagates exactly as
before; only the lock now goes back first. Pinned on the syntax tree (the modules need sqlalchemy to import).

Run from services/position-stocks-service:  python -m pytest tests/test_e340_lock_release_after_capital_failure.py -q
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED = {"orders/reconcile.py": 4, "orders/eod_squareoff.py": 2, "orders/scaleout.py": 1}


def _calls(nodes, attr_owner, attr):
    out = []
    for n in nodes:
        for c in ast.walk(n):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr
                    and isinstance(c.func.value, ast.Name) and c.func.value.id == attr_owner):
                out.append(c)
    return out


def _tree(rel):
    return ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())


def test_each_closed_row_release_pair_is_try_finally():
    for rel, want in EXPECTED.items():
        tree = _tree(rel)
        n = sum(1 for t in ast.walk(tree) if isinstance(t, ast.Try)
                and _calls(t.body, "ledger", "release_capital") and _calls(t.finalbody, "shared_symbol_lock", "release"))
        assert n == want, f"{rel}: {n} try/finally release pairs, expected {want}"


def test_no_release_capital_is_left_directly_before_a_lock_release():
    for rel in EXPECTED:
        tree = _tree(rel)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for a, b in zip(body, body[1:]):
                bare_cap = isinstance(a, ast.Expr) and _calls([a], "ledger", "release_capital")
                lock_next = isinstance(b, ast.Expr) and _calls([b], "shared_symbol_lock", "release")
                assert not (bare_cap and lock_next), f"{rel} line {a.lineno}: release_capital then lock release without try/finally"


def test_the_pattern_itself_releases_the_lock_and_keeps_the_error():
    order = []

    def cap():
        order.append("capital")
        raise RuntimeError("db down")

    def lock():
        order.append("lock")

    try:
        try:
            cap()
        finally:
            lock()
    except RuntimeError as e:
        assert str(e) == "db down"
    assert order == ["capital", "lock"]
