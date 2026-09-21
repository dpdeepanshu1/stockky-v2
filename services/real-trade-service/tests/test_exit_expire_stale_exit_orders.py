"""Phase 1 (100%-coverage plan, item exit_engine/exit.py lines 266-327): exercise
expire_stale_exit_orders() end to end -- it's the function that cancels a stale
resting LIMIT exit SELL at Dhan and resends the still-open remainder as MARKET.
Zero direct coverage before this file even though it's on the critical path for
"shares get stranded with no way to be sold" if it silently breaks.
Run from services/real-trade-service:  python -m pytest tests/test_exit_expire_stale_exit_orders.py -q"""
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


def _mkorder(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTSTOCK", side="SELL", order_type="LIMIT",
        qty=10, filled_qty_so_far=0, status="PLACED", dhan_order_id="D1",
        exit_reason="target_hit_partial",
        valid_until=datetime.now(timezone.utc) - timedelta(minutes=1),  # already expired
    )
    defaults.update(kw)
    order = models.TradeOrder(**defaults)
    db.add(order); db.commit()
    return order


def _mkposition(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTSTOCK", status="OPEN", qty_open=10,
        avg_entry_price=100.0, opened_at=datetime.now(timezone.utc),
        broker_imported=True,
    )
    defaults.update(kw)
    pos = models.TradePosition(**defaults)
    db.add(pos); db.commit()
    return pos


@pytest.fixture()
def db(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    session = sessionmaker(bind=_engine)()
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    monkeypatch.setattr(ex, "log_action", lambda *a, **k: None)
    return session


def _run(db, mode="REAL"):
    return asyncio.run(ex.expire_stale_exit_orders(db, mode))


def test_no_stale_orders_returns_zero_and_touches_nothing(db):
    _mkorder(db, valid_until=datetime.now(timezone.utc) + timedelta(minutes=30))  # not yet expired
    assert _run(db) == 0


def test_demo_mode_never_expires_anything(db):
    """DEMO fills its LIMIT partial-target sell synchronously -- it never has a
    resting order to expire, so this must be a same-mode no-op."""
    _mkorder(db, mode="DEMO")
    assert _run(db, mode="DEMO") == 0


def test_full_remainder_cancelled_and_resent_as_market(db, monkeypatch):
    order = _mkorder(db, qty=10, filled_qty_so_far=0)
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {"status": "cancelled"})
    resent = {}

    def _fake_send(db_, position, qty, reason, full=True):
        resent.update(qty=qty, reason=reason, full=full, symbol=position.symbol)
        return True
    monkeypatch.setattr(ex, "_send_real_sell", _fake_send)

    assert _run(db) == 1
    db.refresh(order)
    assert order.status == "EXPIRED"
    assert resent == {"qty": 10, "reason": "target_hit_partial", "full": True, "symbol": "TESTSTOCK"}


def test_partial_fill_resends_only_the_remaining_qty(db, monkeypatch):
    order = _mkorder(db, qty=10, filled_qty_so_far=6, status="PARTIAL")
    pos = _mkposition(db, qty_open=4)  # only the unfilled remainder is still open
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {"status": "cancelled"})
    resent = {}
    monkeypatch.setattr(
        ex, "_send_real_sell",
        lambda db_, position, qty, reason, full=True: resent.update(qty=qty, full=full) or True,
    )

    assert _run(db) == 1
    db.refresh(order)
    assert order.status == "EXPIRED"
    assert resent["qty"] == 4
    assert resent["full"] is True  # remaining_qty (4) >= position.qty_open (4)


def test_dhan_cancel_failure_leaves_order_placed_not_expired(db, monkeypatch):
    order = _mkorder(db)
    _mkposition(db)

    def _boom(*a, **k):
        raise RuntimeError("Dhan cancel API timeout")
    monkeypatch.setattr(dhan_client, "cancel_order", _boom)
    called = {"send": False}
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: called.__setitem__("send", True) or True)

    assert _run(db) == 0
    db.refresh(order)
    assert order.status == "PLACED", "order was marked EXPIRED despite the Dhan cancel failing"
    assert called["send"] is False, "remainder was resent even though we don't know if it already filled"


def test_no_open_position_found_still_expires_order_but_does_not_resend(db, monkeypatch):
    """Position already closed out by another route -- nothing to resend, but the
    stale LIMIT order itself is still correctly marked EXPIRED (it's cancelled at
    Dhan either way)."""
    order = _mkorder(db, qty=10, filled_qty_so_far=0)
    # deliberately no TradePosition row
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {"status": "cancelled"})
    called = {"send": False}
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: called.__setitem__("send", True) or True)

    assert _run(db) == 1
    db.refresh(order)
    assert order.status == "EXPIRED"
    assert called["send"] is False


def test_zero_remaining_qty_does_not_resend(db, monkeypatch):
    """Fully filled by the time it's picked up here (filled_qty_so_far == qty) --
    nothing left to sell, must not call _send_real_sell at all."""
    order = _mkorder(db, qty=10, filled_qty_so_far=10)
    _mkposition(db, qty_open=0)
    monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {"status": "cancelled"})
    called = {"send": False}
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: called.__setitem__("send", True) or True)

    assert _run(db) == 1
    db.refresh(order)
    assert order.status == "EXPIRED"
    assert called["send"] is False
