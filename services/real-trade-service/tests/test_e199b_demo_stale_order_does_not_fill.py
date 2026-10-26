"""E199b: check_pending_fills must not simulate a fill for a DEMO entry order whose valid_until window has closed;
expire_stale_orders retires it instead (no-chase)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import models
from tests.account_seed import seed_account
from entry_engine import entry
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401
from tests.test_rt_entry_helpers import demo_order, Tick


def _rig(db, monkeypatch, price=99.0):
    seed_account(db, "DEMO", starting_capital=100_000.0, current_equity=100_000.0, cash_available=100_000.0, broker_cash_available=0.0, realized_pnl_today=0.0, realized_pnl_total=0.0)
    db.commit()

    async def get_quotes(symbols):
        return {s: Tick(s, price, datetime.now(timezone.utc), None, "t") for s in symbols}
    monkeypatch.setattr(entry, "get_quotes", get_quotes)


def test_expired_demo_order_is_not_filled_even_when_price_is_in_the_zone(db, monkeypatch):
    _rig(db, monkeypatch)
    o = demo_order(db)
    o.valid_until = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    assert run(entry.check_pending_fills(db, "DEMO")) == 0
    assert o.status == "PLACED"
    assert db.query(models.TradePosition).count() == 0


def test_cycle_order_fills_then_expire_leaves_stale_order_expired_unfilled(db, monkeypatch):
    _rig(db, monkeypatch)
    o = demo_order(db)
    o.valid_until = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    run(entry.check_pending_fills(db, "DEMO"))
    assert run(entry.expire_stale_orders(db, "DEMO")) == 1
    assert o.status == "EXPIRED" and db.query(models.TradePosition).count() == 0


def test_order_inside_its_window_still_fills(db, monkeypatch):
    _rig(db, monkeypatch)
    o = demo_order(db)
    o.valid_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()
    assert run(entry.check_pending_fills(db, "DEMO")) == 1 and o.status == "FILLED"


def test_naive_valid_until_is_read_as_utc(db, monkeypatch):
    _rig(db, monkeypatch)
    o = demo_order(db)
    o.valid_until = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
    db.commit()
    assert run(entry.check_pending_fills(db, "DEMO")) == 0


def test_order_without_valid_until_keeps_old_behaviour(db, monkeypatch):
    _rig(db, monkeypatch)
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 1 and o.status == "FILLED"
