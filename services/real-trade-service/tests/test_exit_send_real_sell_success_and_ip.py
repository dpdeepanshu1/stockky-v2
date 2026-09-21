"""Phase 1 (100%-coverage plan): the happy-path SELL placement in _send_real_sell
(order/event creation, streak-reset, notification) plus the invalid-IP rejection
branch -- both previously 0% direct coverage despite being the two outcomes every
other error-classification branch exists to be the *exception* to.
Run from services/real-trade-service:  python -m pytest tests/test_exit_send_real_sell_success_and_ip.py -q"""
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
from resilience.local_cache import save_snapshot
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


def _mkposition(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTSTOCK", status="OPEN", qty_open=10,
        avg_entry_price=100.0, opened_at=datetime.now(timezone.utc),
        broker_imported=True, current_stop=95.0, current_target=115.0,
        unrealized_pnl=250.0,
    )
    defaults.update(kw)
    pos = models.TradePosition(**defaults)
    db.add(pos); db.commit()
    return pos


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "1234")
    monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
    return db


def _sell(db, pos, **kw):
    return asyncio.run(ex._send_real_sell(db, pos, 10, "stop_hit", **kw)) if asyncio.iscoroutinefunction(ex._send_real_sell) \
        else ex._send_real_sell(db, pos, 10, "stop_hit", **kw)


def test_successful_market_sell_creates_order_and_resets_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    save_snapshot(db, f"exit_reject_streak_{pos.id}", {"count": 3})  # prior rejection streak
    monkeypatch.setattr(dhan_client, "place_order", lambda *a, **k: {"orderId": "DHAN-999"})
    recorded = {}
    monkeypatch.setattr(
        ex, "record_real_exit_sent",
        lambda db_, position, order_id, qty, reason, full=True: recorded.update(
            order_id=order_id, qty=qty, reason=reason, full=full,
        ),
    )

    assert _sell(db, pos) is True
    order = db.query(models.TradeOrder).filter_by(dhan_order_id="DHAN-999").first()
    assert order is not None
    assert order.side == "SELL" and order.order_type == "MARKET" and order.qty == 10
    assert order.status == "PLACED" and order.valid_until is None  # MARKET never expires here
    event = db.query(models.TradeOrderEvent).filter_by(order_id=order.id).first()
    assert event is not None and event.event_type == "PLACED"
    assert recorded == {"order_id": "DHAN-999", "qty": 10, "reason": "stop_hit", "full": True}
    from resilience.local_cache import load_snapshot
    assert load_snapshot(db, f"exit_reject_streak_{pos.id}") == {"count": 0}, \
        "a successful placement must clear a prior rejection streak"


def test_dhan_accepts_but_returns_no_order_id_is_treated_as_a_failure(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", lambda *a, **k: {"status": "ok"})  # no orderId anywhere
    monkeypatch.setattr(ex, "record_real_exit_sent", lambda *a, **k: None)

    assert _sell(db, pos) is False
    assert db.query(models.TradeOrder).count() == 0, "no order row should be created on a failed placement"


def test_invalid_ip_error_disarms_and_alerts(env, monkeypatch):
    db = env
    pos = _mkposition(db)

    def _boom(*a, **k):
        raise RuntimeError("DH-905: Invalid IP address")
    monkeypatch.setattr(dhan_client, "place_order", _boom)
    disarm_calls = {}
    import auth.dhan_credentials as creds
    monkeypatch.setattr(creds, "disarm_on_invalid_ip", lambda db_, mode, err: disarm_calls.update(mode=mode, err=err) or True)
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda msg: alerts.append(msg))

    assert _sell(db, pos) is False
    assert disarm_calls.get("mode") == "REAL"
    assert alerts and "auto-paused" in alerts[0].lower()
    assert db.query(models.TradeOrder).count() == 0


def test_invalid_ip_error_when_already_disarmed_sends_the_still_blocked_message(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Invalid IP address")))
    import auth.dhan_credentials as creds
    monkeypatch.setattr(creds, "disarm_on_invalid_ip", lambda db_, mode, err: False)  # already disarmed earlier
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda msg: alerts.append(msg))

    assert _sell(db, pos) is False
    assert alerts and "still blocked" in alerts[0].lower()


def test_pre_migration_position_falls_back_to_same_day_heuristic(env, monkeypatch):
    """entry_product_type is NULL for every position opened before the session38
    migration -- must fall back to the original same-day-opened heuristic rather
    than crashing or defaulting to the wrong product_type."""
    db = env
    captured = {}
    monkeypatch.setattr(
        dhan_client, "place_order",
        lambda *a, **k: captured.update(product_type=k.get("product_type")) or {"orderId": "D1"},
    )
    monkeypatch.setattr(ex, "record_real_exit_sent", lambda *a, **k: None)

    pos_today = _mkposition(
        db, symbol="TODAYPOS", broker_imported=False, entry_product_type=None,
        opened_at=datetime.now(timezone.utc),
    )
    assert _sell(db, pos_today) is True
    assert captured["product_type"] == "INTRADAY"

    pos_old = _mkposition(
        db, symbol="OLDPOS", broker_imported=False, entry_product_type=None,
        opened_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert _sell(db, pos_old) is True
    assert captured["product_type"] == "CNC"


def test_entry_product_type_intraday_or_mis_mirrors_as_intraday(env, monkeypatch):
    """session38 fix: a position actually bought as INTRADAY/MIS must be sold
    as INTRADAY, mirroring what was bought rather than re-deriving from
    opened_at -- regardless of when it was opened."""
    db = env
    captured = {}
    monkeypatch.setattr(
        dhan_client, "place_order",
        lambda *a, **k: captured.update(product_type=k.get("product_type")) or {"orderId": "D1"},
    )
    monkeypatch.setattr(ex, "record_real_exit_sent", lambda *a, **k: None)

    for entry_pt in ("INTRADAY", "MIS"):
        captured.clear()
        pos = _mkposition(
            db, symbol=f"MIS-{entry_pt}", broker_imported=False,
            entry_product_type=entry_pt,
            opened_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # old, but must not matter
        )
        assert _sell(db, pos) is True
        assert captured["product_type"] == "INTRADAY"


def test_entry_product_type_cnc_mirrors_as_cnc_even_same_day(env, monkeypatch):
    """The core session38 bug: a same-day CNC entry must still be sold as CNC
    (not INTRADAY, which Dhan margin-rejects as a naked short)."""
    db = env
    captured = {}
    monkeypatch.setattr(
        dhan_client, "place_order",
        lambda *a, **k: captured.update(product_type=k.get("product_type")) or {"orderId": "D1"},
    )
    monkeypatch.setattr(ex, "record_real_exit_sent", lambda *a, **k: None)

    pos = _mkposition(
        db, symbol="CNCTODAY", broker_imported=False, entry_product_type="CNC",
        opened_at=datetime.now(timezone.utc),
    )
    assert _sell(db, pos) is True
    assert captured["product_type"] == "CNC"
