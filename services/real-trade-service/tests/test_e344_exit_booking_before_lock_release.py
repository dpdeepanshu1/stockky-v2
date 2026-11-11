"""E344 - a REAL exit fill is booked (position, cash, P&L) in ONE commit, and only then is the symbol lock released.

shared_symbol_lock.release() commits and, when it fails, ROLLS BACK. record_real_exit_fill() and force_close_real_position() called it BEFORE the account was updated:
 - the release's commit saved the closed position before the cash and P&L were booked (a failure right after left a closed position with no cash booked), and
 - a failing release rolled the not-yet-committed close back; the booking that followed still went through, so the account was credited while the position was
   back to its old size, and the next reconcile pass booked the same fill again.
Now the lock goes back after the commit. The real functions are pulled out of the source, so these run without sqlalchemy.
Run from services/real-trade-service:  python -m pytest tests/test_e344_exit_booking_before_lock_release.py -q
"""
from __future__ import annotations

import ast
import os
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = ("record_real_exit_fill", "force_close_real_position", "raised_to_breakeven")   # E361: record_real_exit_fill calls the breakeven helper


def _build(account_raises=False, release_rolls_back=False):
    src = open(os.path.join(ROOT, "portfolio", "portfolio.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    mod = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES], type_ignores=[])
    ev = []

    class DB:
        def add(self, o): ev.append("add")
        def flush(self): ev.append("flush")
        def commit(self): ev.append("commit")
        def rollback(self): ev.append("rollback")

    account = SimpleNamespace(cash_available=100.0, realized_pnl_today=0.0, realized_pnl_total=0.0, current_equity=0.0, updated_at=None)

    def get_account(db, mode, for_update=False):
        if account_raises:
            raise RuntimeError("db down")
        return account

    def release(db, sym):
        ev.append(("release", sym))
        if release_rolls_back:
            db.rollback()

    ns = {
        "math": __import__("math"),
        "models": SimpleNamespace(TradePositionEvent=lambda **k: SimpleNamespace(**k)),
        "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
        "_accumulate_net_cost": lambda *a, **k: None, "get_account": get_account, "_open_positions_market_value": lambda db, m: 0.0,
        "log_action_behind": lambda *a, **k: ev.append("log"), "shared_symbol_lock": SimpleNamespace(release=release),
    }
    exec(compile(mod, "portfolio.py", "exec"), ns)
    pos = SimpleNamespace(id=1, mode="REAL", symbol="ABC", qty_open=10, avg_entry_price=50.0, realized_pnl=0.0, status="OPEN", closed_at=None, current_stop=45.0)
    return ns, DB(), pos, account, ev


def test_full_exit_releases_the_lock_after_the_commit_that_books_everything():
    ns, db, pos, acct, ev = _build()
    pnl = ns["record_real_exit_fill"](db, pos, 60.0, 10, "stop_hit")
    assert pnl == 100.0 and pos.status == "CLOSED" and acct.cash_available == 700.0 and acct.realized_pnl_today == 100.0
    assert ev.index("commit") < ev.index(("release", "ABC")), ev
    assert ev.count("commit") == 1 and "rollback" not in ev


def test_partial_exit_keeps_the_lock():
    ns, db, pos, acct, ev = _build()
    ns["record_real_exit_fill"](db, pos, 60.0, 4, "target_hit_partial")
    assert pos.status == "PARTIALLY_CLOSED" and ("release", "ABC") not in ev


def test_a_failing_booking_releases_nothing_and_commits_nothing():
    ns, db, pos, acct, ev = _build(account_raises=True)
    try:
        ns["record_real_exit_fill"](db, pos, 60.0, 10, "stop_hit")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert "commit" not in ev and ("release", "ABC") not in ev


def test_a_release_that_rolls_back_cannot_undo_the_booked_exit():
    ns, db, pos, acct, ev = _build(release_rolls_back=True)
    ns["record_real_exit_fill"](db, pos, 60.0, 10, "stop_hit")
    assert ev.index("commit") < ev.index("rollback"), ev      # the rollback comes after everything was committed, so it has nothing of the exit to undo


def test_force_close_releases_after_the_commit():
    ns, db, pos, acct, ev = _build()
    assert ns["force_close_real_position"](db, pos, "broker reports 0 held") == 0.0
    assert pos.status == "CLOSED" and pos.qty_open == 0 and acct.cash_available == 600.0
    assert ev.index("commit") < ev.index(("release", "ABC")), ev
    ns, db, pos, acct, ev = _build(account_raises=True)
    try:
        ns["force_close_real_position"](db, pos, "x")
    except RuntimeError:
        pass
    assert ("release", "ABC") not in ev
