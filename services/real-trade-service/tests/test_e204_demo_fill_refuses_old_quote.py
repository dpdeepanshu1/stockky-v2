"""E204: the DEMO fill simulation applies the same quote-age gate as real entries (_entry_quote_age_reason): an old or unknown-age
tick must not fill a pending order; ENTRY_MAX_QUOTE_AGE_S=0 turns the gate off."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import models
from entry_engine import entry
from tests.test_entry_evaluate_mode_remaining_coverage_2 import db, run  # noqa: F401
from tests.test_rt_entry_helpers import Tick, demo_order


def _rig(db, monkeypatch, tick):
    db.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0, current_equity=100_000.0,
                               cash_available=100_000.0, broker_cash_available=0.0,
                               realized_pnl_today=0.0, realized_pnl_total=0.0))
    db.commit()
    monkeypatch.delenv("ENTRY_MAX_QUOTE_AGE_S", raising=False)
    monkeypatch.delenv("ENTRY_QUOTE_AGE_UNKNOWN", raising=False)

    async def get_quotes(symbols):
        return {s: tick for s in symbols}
    monkeypatch.setattr(entry, "get_quotes", get_quotes)


def _tick(age_s=0, **kw):
    return Tick("ABC", 99.0, datetime.now(timezone.utc) - timedelta(seconds=age_s), None, "t", **kw)


def test_old_tick_does_not_fill(db, monkeypatch):
    _rig(db, monkeypatch, _tick(age_s=120))
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 0
    assert o.status == "PLACED" and db.query(models.TradePosition).count() == 0


def test_fresh_tick_fills(db, monkeypatch):
    _rig(db, monkeypatch, _tick(age_s=1))
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 1 and o.status == "FILLED"


def test_unknown_age_tick_does_not_fill_by_default(db, monkeypatch):
    _rig(db, monkeypatch, _tick(age_known=False))
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 0 and o.status == "PLACED"


def test_unknown_age_tick_fills_when_switched_to_allow(db, monkeypatch):
    _rig(db, monkeypatch, _tick(age_known=False))
    monkeypatch.setenv("ENTRY_QUOTE_AGE_UNKNOWN", "allow")
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 1 and o.status == "FILLED"


def test_gate_off_with_zero_limit(db, monkeypatch):
    _rig(db, monkeypatch, _tick(age_s=600))
    monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", "0")
    o = demo_order(db)
    assert run(entry.check_pending_fills(db, "DEMO")) == 1 and o.status == "FILLED"


def test_one_old_quote_does_not_block_a_fresh_one(db, monkeypatch):
    _rig(db, monkeypatch, None)
    old = Tick("AAA", 99.0, datetime.now(timezone.utc) - timedelta(seconds=300), None, "t")
    new = Tick("BBB", 99.0, datetime.now(timezone.utc), None, "t")

    async def get_quotes(symbols):
        return {"AAA": old, "BBB": new}
    monkeypatch.setattr(entry, "get_quotes", get_quotes)
    a = demo_order(db, "AAA")
    b = demo_order(db, "BBB")
    assert run(entry.check_pending_fills(db, "DEMO")) == 1
    assert a.status == "PLACED" and b.status == "FILLED"
