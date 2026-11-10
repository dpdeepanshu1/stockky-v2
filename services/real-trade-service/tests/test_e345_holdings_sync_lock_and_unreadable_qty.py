"""E345 - three gaps in the broker-holdings sync of the real-trade-service (portfolio/portfolio.py).

1. holdings_sync_reconcile() force-closes a REAL position the broker does not hold, but never released this service's cross-service symbol lock. It was the only path
   that closes a REAL position without releasing it, and this service has no lock sweep (position-stocks has one), so the claim stayed for good and position-stocks
   could never buy the symbol. Now released after the commit that holds the close and the refund (as in E344).
2. A holdings quantity that is PRESENT but unreadable ("abc", "nan", "inf") read as 0 held, so the symbol looked absent from the broker's answer and a position this
   service really holds was force-closed as a ghost (cash refunded, exits stopped). "inf" even raised out of a function documented as never raising. Such a symbol is now
   skipped for the cycle (neither closed nor capped).
3. import_broker_holdings(): a NaN / inf average cost passed "<= 0" and became a position with a NaN stop; an "inf" quantity in a same-day CNC row raised out of the loop and stopped
   the import for every holding. Both rows are skipped now.

The real function is pulled out of the source, so these run without sqlalchemy.
Run from services/real-trade-service:  python -m pytest tests/test_e345_holdings_sync_lock_and_unreadable_qty.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STUBBED = ("execution", "execution.dhan_client", "portfolio", "portfolio.exit_booking_heal", "exit_engine", "exit_engine.exit")


class _Col:
    def __eq__(self, o): return self
    def __hash__(self): return 1
    def in_(self, v): return self
    def isnot(self, v): return self
    def __ge__(self, o): return self
    def __getattr__(self, n): return self


def _run(holdings, positions, our_symbol="ABC", qty_open=10):
    src = open(os.path.join(ROOT, "portfolio", "portfolio.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "holdings_sync_reconcile"]
    mod = ast.Module(body=fn, type_ignores=[])
    ev = []
    pos = SimpleNamespace(id=1, symbol=our_symbol, mode="REAL", status="OPEN", qty_open=qty_open, avg_entry_price=50.0, realized_pnl=0.0, closed_at=None,
                          opened_at=datetime.now(timezone.utc) - timedelta(days=2))

    class Q:
        def __init__(self, model): self.model = model
        def filter(self, *a, **k): return self
        def all(self): return [pos] if self.model is models.TradePosition else []
        def first(self): return None

    class DB:
        def query(self, m): return Q(m)
        def add(self, o): ev.append("add")
        def commit(self): ev.append("commit")

    account = SimpleNamespace(cash_available=100.0, current_equity=0.0, updated_at=None)
    models = SimpleNamespace(TradePosition=SimpleNamespace(mode=_Col(), status=_Col(), symbol=_Col()),
                             TradePositionEvent=type("E", (), {"__init__": lambda self, **k: None, "position_id": _Col(), "event_type": _Col()}))
    ns = {
        "models": models, "config": SimpleNamespace(HOLDINGS_SYNC_GUARD_MINUTES=5, HOLDINGS_SYNC_EMPTY_CONFIRM_SECONDS=0, HOLDINGS_SYNC_NET_QTY=True),
        "logger": logging.getLogger("t345"), "datetime": datetime, "timezone": timezone, "timedelta": timedelta, "math": math,
        "get_account": lambda db, mode, for_update=False: account, "_open_positions_market_value": lambda db, m: 0.0,
        "log_action_behind": lambda *a, **k: None,
        "shared_symbol_lock": SimpleNamespace(release=lambda db, s: ev.append(("release", s))),
    }
    spec = importlib.util.spec_from_file_location("_ebh345", os.path.join(ROOT, "portfolio", "exit_booking_heal.py"))
    ebh = importlib.util.module_from_spec(spec); spec.loader.exec_module(ebh)
    orig = {k: sys.modules.get(k) for k in _STUBBED}
    dc = types.ModuleType("execution.dhan_client")
    dc.get_holdings = lambda db: holdings
    dc.get_positions = lambda db: positions
    ex = types.ModuleType("execution"); ex.dhan_client = dc
    pk = types.ModuleType("portfolio"); pk.exit_booking_heal = ebh
    ee = types.ModuleType("exit_engine"); eex = types.ModuleType("exit_engine.exit"); eex._has_pending_real_sell = lambda db, sym: False; ee.exit = eex
    sys.modules.update({"execution": ex, "execution.dhan_client": dc, "portfolio": pk, "portfolio.exit_booking_heal": ebh, "exit_engine": ee, "exit_engine.exit": eex})
    try:
        exec(compile(mod, "portfolio.py", "exec"), ns)
        out = ns["holdings_sync_reconcile"](DB())
    finally:
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return out, pos, account, ev


H = lambda sym, qty: {"tradingSymbol": sym, "totalQty": qty, "avgCostPrice": 50.0}


def test_a_ghost_closed_position_releases_the_symbol_lock_after_the_commit():
    out, pos, acct, ev = _run([H("OTHER", 5)], [])
    assert out["closed"] == 1 and pos.status == "CLOSED" and pos.qty_open == 0 and acct.cash_available == 600.0
    assert ev.index("commit") < ev.index(("release", "ABC")), ev


def test_a_position_the_broker_still_holds_keeps_its_lock():
    out, pos, acct, ev = _run([H("ABC", 10)], [])
    assert out["closed"] == 0 and pos.status == "OPEN" and ("release", "ABC") not in ev


def test_a_capped_position_keeps_its_lock():
    out, pos, acct, ev = _run([H("ABC", 4)], [])
    assert out.get("synced") == ["ABC"] and pos.qty_open == 4 and ("release", "ABC") not in ev


def test_an_unreadable_quantity_is_not_a_ghost():
    for bad in ("abc", "nan", "inf", float("nan"), float("inf")):
        out, pos, acct, ev = _run([H("ABC", bad), H("OTHER", 5)], [])
        assert out["closed"] == 0 and pos.status == "OPEN" and pos.qty_open == 10 and acct.cash_available == 100.0 and ("release", "ABC") not in ev, bad


def test_a_missing_quantity_still_reads_as_not_held_as_before():
    out, pos, acct, ev = _run([{"tradingSymbol": "ABC", "avgCostPrice": 50.0}, H("OTHER", 5)], [])
    assert out["closed"] == 1 and pos.status == "CLOSED"


# ---------------------------------------------------------------- import_broker_holdings


def test_import_skips_non_finite_cost_and_quantity_rows():
    src = open(os.path.join(ROOT, "portfolio", "portfolio.py"), encoding="utf-8").read()
    fn = [n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "import_broker_holdings"][0]
    text = ast.get_source_segment(src, fn)
    assert "not math.isfinite(avg_price)" in text and "not math.isfinite(_avg_float)" in text
    assert text.count("except (TypeError, ValueError, OverflowError)") >= 2
