"""E343 - two gaps on the capital path of the real-trade-service.

1. execution/heat_state.publish(): own_heat() answers (0, 0) when the positions cannot be read. That is right for this service's own cap (the old single-service
   cap) but it was also what got PUBLISHED, so the peer read a fresh row of zeros as "nothing at risk" and undercounted the shared heat. Now nothing is published when
   the read fails (the peer keeps the last figure until HEAT_PEER_MAX_AGE_S, then ignores it). A real empty book still publishes zeros.
2. portfolio.get_account(for_update=True): the first read of a new IST day commits the daily P&L reset; the commit ends the transaction and releases the row lock, so the
   caller's `cash_available += ...` ran unlocked once a day. Now the row is locked again (db.refresh(with_for_update=True)) after a reset commit.

The real functions are pulled out of the source, so these run without sqlalchemy.
Run from services/real-trade-service:  python -m pytest tests/test_e343_heat_publish_and_account_relock.py -q
"""
from __future__ import annotations

import ast
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_KEYS = ("sqlalchemy", "sqlalchemy.orm", "models", "execution.shared_heat", "execution.heat_state", "portfolio.portfolio")


def _load_heat_state(positions_fn):
    orig = {k: sys.modules.get(k) for k in _KEYS}
    try:
        import sqlalchemy.orm  # noqa: F401
        import models  # noqa: F401
    except Exception:  # noqa: BLE001
        sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.Session = object; sa.orm = orm
        m = types.ModuleType("models"); m.SharedHeat = object; m.SharedHeatSectors = object
        sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm, "models": m})
    for k in ("execution.shared_heat", "execution.heat_state"):
        sys.modules.pop(k, None)
    pf = types.ModuleType("portfolio.portfolio"); pf.held_exposure_positions = positions_fn
    sys.modules["portfolio.portfolio"] = pf
    from execution import heat_state

    def restore():
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return heat_state, restore


def _run_publish(positions_fn):
    heat_state, restore = _load_heat_state(positions_fn)
    got = {"own": [], "sectors": []}
    try:
        heat_state.shared_heat.publish_own = lambda db, risk, gap, base: got["own"].append((risk, gap, base))
        heat_state.shared_heat.publish_sectors = lambda db, sec: got["sectors"].append(dict(sec))
        heat_state.publish(object(), 100000.0)
    finally:
        restore()
    return got


def _pos():
    return SimpleNamespace(symbol="X", avg_entry_price=100.0, current_stop=98.0, qty_open=10, opened_at=datetime.now(timezone.utc), entry_decision_label=None)


def test_publish_sends_the_open_risk():
    got = _run_publish(lambda db, mode: [_pos()])
    assert got["own"] == [(20.0, 0.0, 100000.0)]


def test_publish_with_a_real_empty_book_still_sends_zeros():
    got = _run_publish(lambda db, mode: [])
    assert got["own"] == [(0.0, 0.0, 100000.0)]


def test_publish_sends_nothing_when_the_positions_cannot_be_read():
    def boom(db, mode):
        raise RuntimeError("db down")
    got = _run_publish(boom)
    assert got["own"] == [] and got["sectors"] == []


def test_publish_sends_nothing_when_the_read_fails_half_way():
    def half(db, mode):
        def gen():
            yield _pos()
            raise RuntimeError("connection lost")
        return gen()
    got = _run_publish(half)
    assert got["own"] == [] and got["sectors"] == []


# ---------------------------------------------------------------- get_account


def _account_funcs(dialect, refresh_raises=False):
    src = open(os.path.join(ROOT, "portfolio", "portfolio.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_maybe_reset_daily_pnl", "get_account")]
    mod = ast.Module(body=keep, type_ignores=[])
    calls = []

    class Q:
        def filter_by(self, **k): return self
        def with_for_update(self): calls.append("lock"); return self
        def first(self): return row

    row = SimpleNamespace(mode="REAL", pnl_last_reset_date="2000-01-01", realized_pnl_today=5.0)

    class DB:
        def query(self, m): return Q()
        def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        def commit(self): calls.append("commit")
        def refresh(self, r, with_for_update=False):
            calls.append(("refresh", with_for_update))
            if refresh_raises:
                raise RuntimeError("lock timeout")

    import logging
    ns = {"models": SimpleNamespace(TradeAccount=object), "ist_today_str": lambda: "2026-11-10", "logger": logging.getLogger("t"), "Session": object, "Optional": None}
    exec(compile(mod, "portfolio.py", "exec"), ns)
    real_get_account = ns["get_account"]

    def get_account(*a, **k):
        # the reset stores the finished day's P&L through charges_ledger; a no-op stand-in for the call, put back afterwards so other tests keep the real module
        prev = sys.modules.get("charges_ledger")
        sys.modules["charges_ledger"] = types.SimpleNamespace(freeze_day_gross=lambda *x, **y: None)
        try:
            return real_get_account(*a, **k)
        finally:
            if prev is None:
                sys.modules.pop("charges_ledger", None)
            else:
                sys.modules["charges_ledger"] = prev
    return get_account, DB(), row, calls


def test_first_read_of_a_new_day_locks_the_row_again_after_the_reset_commit():
    get_account, db, row, calls = _account_funcs("oracle")
    out = get_account(db, "REAL", for_update=True)
    assert out is row and row.pnl_last_reset_date == "2026-11-10" and row.realized_pnl_today == 0.0
    assert calls.index("commit") < calls.index(("refresh", True)), calls


def test_no_second_lock_without_a_reset_without_for_update_or_on_sqlite():
    get_account, db, row, calls = _account_funcs("oracle")
    row.pnl_last_reset_date = "2026-11-10"
    get_account(db, "REAL", for_update=True)
    assert "commit" not in calls and not any(isinstance(c, tuple) for c in calls)
    get_account, db, row, calls = _account_funcs("oracle")
    get_account(db, "REAL", for_update=False)
    assert not any(isinstance(c, tuple) for c in calls)
    get_account, db, row, calls = _account_funcs("sqlite")
    get_account(db, "REAL", for_update=True)
    assert not any(isinstance(c, tuple) for c in calls) and "lock" not in calls


def test_a_failed_relock_still_returns_the_account():
    get_account, db, row, calls = _account_funcs("postgresql", refresh_raises=True)
    assert get_account(db, "REAL", for_update=True) is row
