"""Round 220: the audit rows written right AFTER a REAL fill / close / holdings sync commit go through the write-behind queue (audit.logger.log_action_behind),
so a slow Oracle INSERT (2.3-4.6 s on 2 Nov, 2.7 s on 4 Nov) no longer waits inside the exit / reconcile tick. The position and cash are committed BEFORE the row is
written, nothing reads these rows back for a decision (GET /audit only lists them), and CYCLE_AUDIT_WRITE_BEHIND=false / a missing queue falls back to the old
synchronous write. Order, ARMED, login and every other deciding row stay on log_action."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import portfolio.portfolio as pf
from execution import shared_symbol_lock
import models
from tests.test_portfolio import db, _open_position  # noqa: F401


def test_force_close_queues_its_audit_row_after_the_cash_commit(db, monkeypatch):
    import write_behind
    queued = []
    monkeypatch.setattr(write_behind, "submit", lambda kind, payload: queued.append((kind, payload)) or True)
    monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: None)
    pos = _open_position(db, symbol="BEHINDCO", qty=10, entry=50.0)
    acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
    acct.cash_available = 100.0
    db.commit()
    pf.force_close_real_position(db, pos, "broker reports 0 held")
    db.refresh(acct)
    assert acct.cash_available == 600.0                                   # state committed
    assert len(queued) == 1 and queued[0][0] == "audit"
    assert queued[0][1]["action"] == "POSITION_CLOSED" and queued[0][1]["mode"] == "REAL" and "BEHINDCO" in queued[0][1]["detail"]
    assert db.query(models.TradeAuditLog).filter_by(action="POSITION_CLOSED").count() == 0   # not written synchronously


def test_switch_off_writes_the_row_synchronously_as_before(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "CYCLE_AUDIT_WRITE_BEHIND", False)
    monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: None)
    pos = _open_position(db, symbol="SYNCCO", qty=4, entry=25.0)
    pf.force_close_real_position(db, pos, "broker reports 0 held")
    assert db.query(models.TradeAuditLog).filter_by(action="POSITION_CLOSED").count() == 1


def test_only_the_trailing_real_rows_moved():
    src = (Path(__file__).resolve().parent.parent / "portfolio" / "portfolio.py").read_text(encoding="utf-8")
    behind = re.findall(r'log_action_behind\(\s*db, actor="system",\s*action=("[A-Z_]+"|"POSITION_CLOSED" if)', src)
    assert len(behind) == 5, behind                                      # HOLDINGS_SYNC_CLOSED / _QTY_CAPPED, ORDER_FILLED, POSITION_CLOSED / PARTIAL_EXIT, ghost POSITION_CLOSED
    assert 'log_action(db, actor="system", action="ORDER_FILLED", mode="DEMO"' in src   # DEMO rows untouched
