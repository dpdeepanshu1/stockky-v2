"""
E195 - a manual LIMIT SELL that was not filled inside EXIT_LIMIT_VALIDITY_MINUTES was cancelled and then resent as a MARKET sell by
expire_stale_exit_orders (built for the target-hit partial, whose target is already cleared). A manual ticket is a price instruction: cancel only.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e195_manual_limit_sell_not_market_resent.py -q
"""
import asyncio, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    monkeypatch.setattr(ex, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {"status": "cancelled"})
    return s


def _order(db, **kw):
    d = dict(mode="REAL", symbol="TESTSTOCK", side="SELL", order_type="LIMIT", qty=4, filled_qty_so_far=0, status="PLACED",
             dhan_order_id="D1", exit_reason="manual_sell", execution_source="MANUAL",
             valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    d.update(kw)
    o = models.TradeOrder(**d); db.add(o); db.commit(); return o


def _pos(db, qty=10):
    p = models.TradePosition(mode="REAL", symbol="TESTSTOCK", status="OPEN", qty_open=qty, avg_entry_price=100.0,
                             opened_at=datetime.now(timezone.utc), broker_imported=True)
    db.add(p); db.commit(); return p


def _run(db):
    return asyncio.run(ex.expire_stale_exit_orders(db, "REAL"))


def test_manual_partial_limit_sell_is_not_resent_as_market(db, monkeypatch):
    o = _order(db, qty=4); _pos(db, qty=10)
    sent, notes = [], []
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: sent.append((a, k)) or True)
    monkeypatch.setattr(ex, "notify_sync", lambda m, *a, **k: notes.append(m))
    assert _run(db) == 1
    db.refresh(o)
    assert o.status == "EXPIRED"
    assert sent == []
    assert len(notes) == 1 and "NOT sent as a market order" in notes[0]


def test_manual_flag_by_exit_reason_alone(db, monkeypatch):
    o = _order(db, execution_source="AUTO", exit_reason="manual_sell"); _pos(db)
    sent = []
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: sent.append(1) or True)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    _run(db)
    assert sent == []


def test_event_text_says_not_resent(db, monkeypatch):
    o = _order(db); _pos(db)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: True)
    _run(db)
    ev = db.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="EXPIRED").one()
    assert "NOT resent as MARKET" in ev.detail


def test_target_partial_is_still_resent_as_market(db, monkeypatch):
    o = _order(db, exit_reason="target_hit_partial", execution_source="AUTO", qty=10); _pos(db, qty=10)
    sent = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_send_real_sell", lambda db_, p, qty, reason, full=True: sent.append((qty, reason, full)) or True)
    assert _run(db) == 1
    assert sent == [(10, "target_hit_partial", True)]


def test_unfilled_manual_order_with_dhan_cancel_failure_stays_placed(db, monkeypatch):
    o = _order(db); _pos(db)
    def boom(*a, **k): raise RuntimeError("dhan down")
    monkeypatch.setattr(dhan_client, "cancel_order", boom)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _run(db) == 0
    db.refresh(o)
    assert o.status == "PLACED"


def test_helper():
    class O: pass
    a = O(); a.execution_source = "MANUAL"; a.exit_reason = None
    b = O(); b.execution_source = "AUTO"; b.exit_reason = "target_hit_partial"
    assert ex._is_manual_sell_order(a) is True
    assert ex._is_manual_sell_order(b) is False
    assert ex._is_manual_sell_order(object()) is False
