"""
tests/test_manual_engine_remaining_coverage.py

Closes the branches test_manual_engine.py's original suite left untouched.
That suite covers the two known regressions (session52 cash_available,
session39 SELL order_type/limit_price) plus the main preview/confirm/BUY/
SELL flows, but always drove requests down one side of a few two-sided
branches:

  - approved_qty's fallback ternary (line ~231) was only ever exercised with
    RiskResult.approved_qty explicitly set (by both the approve() and
    reject() helpers) -- the `result.approved_qty is None` side of
    `result.approved_qty if ... is not None else (...)` was never taken,
    for either verdict.
  - stop_price/target_price's `req.X or <fallback>` (line ~218-219) was only
    ever driven by the fallback side for target_price, and only indirectly
    (via the invalid-stop rejection test) for stop_price -- a ticket that
    supplies both explicit, valid values and proceeds to a real preview was
    never tested.
  - reference_price's `req.limit_price if (order_type == "LIMIT" and
    req.limit_price) else tick.price` (line ~206) was only ever taken down
    the tick.price side for BUY (every BUY test leaves Req.limit_price at
    its None default) -- paired with this, try_fill_entry's "price hasn't
    come down into the entry zone yet" False-return branch (portfolio.py)
    was never reached either, since every DEMO BUY confirm used a limit
    price equal to the tick.
  - the `admin` parameter's three `or`/ternary sites (confirmed_by/
    confirmed_at, the "sent by {admin or 'demo-user'}" event detail, and
    every `actor=admin or "admin"` log_action call) were exercised with
    admin="tester" in every single confirm-path test -- the admin=None
    (demo-user / system) side was never taken.
  - close_position's DEMO status ternary ("CLOSED" if fully closed else
    "PARTIALLY_CLOSED", preview line ~419) was only ever tested with a
    full close (qty == position.qty_open) -- PARTIALLY_CLOSED was never
    produced.
  - the REAL SELL path's `full = qty >= position.qty_open` (line ~452) was
    only ever tested with qty == position.qty_open (full=True) -- the
    partial-real-sell (full=False) call into _send_real_sell was never
    produced.

risk_engine.evaluate() is mocked here exactly as in the original suite --
these tests are about manual_engine.py's own branching on the ticket/verdict,
not risk-sizing math.

Run from services/real-trade-service:
    python -m pytest tests/test_manual_engine_remaining_coverage.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import manual_engine as me
from execution import dhan_client
from risk_engine.engine import RiskResult, RiskVerdict
from market_feed.feed import Tick

_engine = create_engine("sqlite:///:memory:")


@dataclass
class Req:
    symbol: str = "TESTCO"
    side: str = "BUY"
    qty: int = 10
    order_type: Optional[str] = "LIMIT"
    product_type: Optional[str] = "CNC"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    position_id: Optional[int] = None


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.add(models.TradeAccount(mode="REAL", starting_capital=100000.0, current_equity=100000.0,
                               cash_available=40000.0, broker_cash_available=80000.0))
    s.add(models.TradeRiskConfig(mode="DEMO"))
    s.add(models.TradeRiskConfig(mode="REAL"))
    s.commit()
    monkeypatch.setattr(me, "is_market_open_ist", lambda: True)
    monkeypatch.setattr("execution.equity_sync.sync_real_equity", lambda db_: None)
    yield s
    s.close()


def _quotes(monkeypatch, price=100.0, symbol="TESTCO", atr=2.0):
    async def _q(symbols):
        return {symbol: Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=atr, source="test")}
    monkeypatch.setattr(me, "get_quotes", _q)


def _approve(monkeypatch, qty=10):
    monkeypatch.setattr(
        me, "risk_evaluate",
        lambda intent, account: RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=qty),
    )


def _open_position(db, *, mode="DEMO", symbol="TESTCO", qty=10, entry=100.0):
    pos = models.TradePosition(
        mode=mode, symbol=symbol, status="OPEN", qty_open=qty,
        avg_entry_price=entry, opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


# ── approved_qty fallback ternary: RiskResult omitting approved_qty ────────

class TestApprovedQtyFallback:
    def test_approved_qty_none_and_approved_verdict_falls_back_to_requested_qty(self, db, monkeypatch):
        """result.approved_qty is None but verdict is APPROVED -> must use
        req.qty, not silently treat it as 0/rejected."""
        _quotes(monkeypatch, price=100.0)
        monkeypatch.setattr(
            me, "risk_evaluate",
            lambda intent, account: RiskResult(
                verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=None,
            ),
        )
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(qty=7), confirm=False))
        assert r["ok"] is True
        assert r["approved_qty"] == 7

    def test_approved_qty_none_and_rejected_verdict_defaults_to_zero(self, db, monkeypatch):
        """result.approved_qty is None and verdict is not APPROVED -> must
        default to 0, never req.qty."""
        _quotes(monkeypatch, price=100.0)
        monkeypatch.setattr(
            me, "risk_evaluate",
            lambda intent, account: RiskResult(
                verdict=RiskVerdict.REJECTED, check_name="max_positions", reason="too many", approved_qty=None,
            ),
        )
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(qty=7), confirm=False))
        assert r["ok"] is False
        assert r["approved_qty"] == 0


# ── explicit stop/target price (bypassing the fallback calc) ───────────────

class TestExplicitStopAndTarget:
    def test_explicit_stop_and_target_price_used_verbatim_not_fallback(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=10)
        r = run(me.evaluate_manual_order(
            db, "DEMO", True, Req(stop_price=92.0, target_price=115.0), confirm=False,
        ))
        assert r["ok"] is True
        assert r["stop_price"] == 92.0
        assert r["target_price"] == 115.0
        # fallback would have been 100*(1-3.2/100)=96.8 / 100*(1+6.5/100)=106.5
        assert r["stop_price"] != 96.8
        assert r["target_price"] != 106.5


# ── explicit BUY limit_price (reference_price's other ternary branch) ──────

class TestExplicitBuyLimitPrice:
    def test_buy_limit_below_market_prices_off_the_limit_not_the_tick(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=10)
        r = run(me.evaluate_manual_order(
            db, "DEMO", True, Req(order_type="LIMIT", limit_price=90.0), confirm=False,
        ))
        assert r["ok"] is True
        assert r["entry_price"] == 90.0

    def test_buy_limit_below_market_confirm_demo_stays_placed_not_filled(self, db, monkeypatch):
        """try_fill_entry's 'price hasn't come down into the entry zone
        yet' guard -- a LIMIT BUY priced below the current tick must not
        simulate-fill; it must stay PLACED, unlike every other confirm test
        in the original suite (which all use limit==tick and always fill)."""
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=10)
        r = run(me.evaluate_manual_order(
            db, "DEMO", True, Req(order_type="LIMIT", limit_price=90.0), confirm=True, admin="tester",
        ))
        assert r["ok"] is True
        assert r["status"] == "PLACED"
        assert r["filled"] is False
        order = db.query(models.TradeOrder).first()
        assert order.status == "PLACED"


# ── admin=None: the demo-user / system-actor side of every admin branch ────

class TestAdminNoneBranch:
    def test_confirm_buy_demo_without_admin_leaves_confirmed_fields_unset(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=10)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=True, admin=None))
        assert r["ok"] is True
        order = db.query(models.TradeOrder).first()
        assert order.confirmed_by is None
        assert order.confirmed_at is None
        event = (
            db.query(models.TradeOrderEvent)
            .filter_by(order_id=order.id, event_type="PLACED")
            .first()
        )
        assert "demo-user" in event.detail

    def test_confirm_sell_demo_without_admin_still_closes(self, db, monkeypatch):
        _open_position(db, qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL", qty=10), confirm=True, admin=None))
        assert r["ok"] is True
        assert r["status"] == "CLOSED"


# ── DEMO SELL: partial close (status stays PARTIALLY_CLOSED, not CLOSED) ───

class TestSellDemoPartial:
    def test_confirm_partial_sell_demo_leaves_position_partially_closed(self, db, monkeypatch):
        _open_position(db, qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL", qty=4), confirm=True, admin="tester"))
        assert r["ok"] is True
        assert r["status"] == "PARTIALLY_CLOSED"
        pos = db.query(models.TradePosition).first()
        assert pos.status == "PARTIALLY_CLOSED"
        assert pos.qty_open == 6


# ── REAL SELL: partial exit (full=False threaded into _send_real_sell) ─────

class TestSellRealPartial:
    def test_confirm_partial_sell_real_sends_full_false(self, db, monkeypatch):
        _open_position(db, mode="REAL", qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        captured = {}

        def _fake_send(db_, position, qty, reason, full=True, execution_source="AUTO",
                        confirmed_by=None, order_type="MARKET", limit_price=None):
            captured.update(full=full, qty=qty)
            return True
        monkeypatch.setattr(me, "_send_real_sell", _fake_send)

        r = run(me.evaluate_manual_order(
            db, "REAL", True, Req(side="SELL", qty=4), confirm=True, admin="tester",
        ))
        assert r["ok"] is True and r["status"] == "SENT_TO_BROKER"
        assert captured["full"] is False
        assert captured["qty"] == 4
