"""
tests/test_manual_engine.py

100%-coverage-plan Phase 1 #4: manual_engine.py -- every manual BUY/SELL from
a human goes through evaluate_manual_order(). Previously 0% direct coverage
despite already having had two real bugs found by inspection, not by a
failing test:
  - session52: _account_state() never set cash_available at all, so every
    manual REAL BUY confirmation was unconditionally rejected by risk_engine's
    cash_available_cap check.
  - session39: order_type/limit_price were validated and priced for a SELL
    ticket but never actually threaded through to _send_real_sell -- a manual
    LIMIT sell silently went out as MARKET every time.

risk_engine.evaluate() itself is mocked throughout (it already has its own
dedicated, currently-100%-covered suite in test_risk_engine.py) -- these
tests are about what manual_engine.py itself does with the ticket and the
verdict, not about risk-sizing math.

Run from services/real-trade-service:
    python -m pytest tests/test_manual_engine.py -q
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


def _reject(monkeypatch, check_name="max_positions", reason="too many open positions"):
    monkeypatch.setattr(
        me, "risk_evaluate",
        lambda intent, account: RiskResult(verdict=RiskVerdict.REJECTED, check_name=check_name, reason=reason, approved_qty=0),
    )


# ── Request validation ──────────────────────────────────────────────────────

class TestValidation:
    def test_missing_symbol_rejected(self, db):
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(symbol=""), confirm=False))
        assert r == {"ok": False, "reason": "invalid_request", "detail": "symbol is required."}

    def test_bad_side_rejected(self, db):
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="HOLD"), confirm=False))
        assert r["ok"] is False and r["reason"] == "invalid_request"

    def test_nonpositive_qty_rejected(self, db):
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(qty=0), confirm=False))
        assert r["ok"] is False and r["reason"] == "invalid_request"

    def test_bad_order_type_rejected(self, db):
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(order_type="STOP"), confirm=False))
        assert r["ok"] is False and r["reason"] == "invalid_request"

    def test_no_price_available_rejected(self, db, monkeypatch):
        async def _empty(symbols):
            return {}
        monkeypatch.setattr(me, "get_quotes", _empty)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=False))
        assert r["ok"] is False and r["reason"] == "no_price"


# ── BUY: intraday-restricted pre-check (session11-class fix) ────────────────

class TestIntradayRestrictedGuard:
    def test_intraday_restricted_symbol_blocked_before_pricing(self, db, monkeypatch):
        monkeypatch.setattr(me, "is_intraday_restricted", lambda db_, sym: True)
        r = run(me.evaluate_manual_order(
            db, "REAL", True, Req(product_type="INTRADAY"), confirm=False,
        ))
        assert r["ok"] is False and r["reason"] == "intraday_restricted"

    def test_cnc_ticket_not_blocked_even_if_symbol_is_restricted(self, db, monkeypatch):
        monkeypatch.setattr(me, "is_intraday_restricted", lambda db_, sym: True)
        _quotes(monkeypatch)
        _approve(monkeypatch)
        r = run(me.evaluate_manual_order(db, "REAL", True, Req(product_type="CNC"), confirm=False))
        assert r["reason"] != "intraday_restricted"


# ── _account_state (session52 regression guard) ─────────────────────────────

class TestAccountState:
    def test_real_account_state_populates_cash_fields(self, db):
        acc = me._account_state(db, "REAL", gate_armed=True)
        assert acc.cash_available == 40000.0
        assert acc.broker_cash_available == 80000.0

    def test_demo_account_state_populates_cash_fields(self, db):
        acc = me._account_state(db, "DEMO", gate_armed=True)
        assert acc.cash_available == 100000.0

    def test_gate_armed_maps_to_trading_globally_paused(self, db):
        assert me._account_state(db, "DEMO", gate_armed=True).trading_globally_paused is False
        assert me._account_state(db, "DEMO", gate_armed=False).trading_globally_paused is True


# ── BUY: preview vs confirm ──────────────────────────────────────────────────

class TestBuyPreview:
    def test_preview_does_not_write_any_rows(self, db, monkeypatch):
        _quotes(monkeypatch)
        _approve(monkeypatch)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=False))
        assert r["ok"] is True
        assert db.query(models.TradeOrder).count() == 0
        assert db.query(models.TradeDecision).count() == 0

    def test_preview_stop_above_entry_rejected(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(
            db, "DEMO", True, Req(stop_price=105.0), confirm=False,
        ))
        assert r["ok"] is False and r["reason"] == "invalid_stop"

    def test_preview_reflects_risk_engine_rejection(self, db, monkeypatch):
        _quotes(monkeypatch)
        _reject(monkeypatch, check_name="max_positions", reason="too many open")
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=False))
        assert r["ok"] is False
        assert r["check_name"] == "max_positions"


class TestBuyConfirmDemo:
    def test_confirm_writes_decision_and_order_and_attempts_fill(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=10)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is True
        assert db.query(models.TradeOrder).count() == 1
        assert db.query(models.TradeDecision).count() == 1
        order = db.query(models.TradeOrder).first()
        assert order.execution_source == "MANUAL"
        assert order.confirmed_by == "tester"
        assert r["status"] in ("FILLED", "PLACED")

    def test_confirm_rejected_by_risk_engine_writes_nothing(self, db, monkeypatch):
        _quotes(monkeypatch)
        _reject(monkeypatch)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False
        assert db.query(models.TradeOrder).count() == 0


class TestBuyConfirmReal:
    def test_confirm_sends_market_order_with_zero_price(self, db, monkeypatch):
        """session21c regression: a MARKET ticket must send price=0 to Dhan,
        never the reference tick price."""
        _quotes(monkeypatch, price=250.0)
        _approve(monkeypatch, qty=4)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
        calls = {}

        def _place(db_, **kw):
            calls.update(kw)
            return {"orderId": "ORD1"}
        monkeypatch.setattr(dhan_client, "place_order", _place)
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        r = run(me.evaluate_manual_order(
            db, "REAL", True, Req(order_type="MARKET"), confirm=True, admin="tester",
        ))
        assert r["ok"] is True and r["status"] == "SENT_TO_BROKER"
        assert calls["order_type"] == "MARKET"
        assert calls["price"] == 0

    def test_confirm_sends_limit_order_with_reference_price(self, db, monkeypatch):
        _quotes(monkeypatch, price=250.0)
        _approve(monkeypatch, qty=4)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
        calls = {}
        monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: (calls.update(kw), {"orderId": "ORD1"})[1])
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        r = run(me.evaluate_manual_order(
            db, "REAL", True, Req(order_type="LIMIT", limit_price=248.0), confirm=True, admin="tester",
        ))
        assert r["ok"] is True
        assert calls["order_type"] == "LIMIT"
        assert calls["price"] == 248.0

    def test_order_budget_exhausted_rejects_without_calling_dhan(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=5)
        placed = {"called": False}
        monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: placed.update(called=True))
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        from execution import shared_order_budget
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: False)

        r = run(me.evaluate_manual_order(db, "REAL", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False and r["status"] == "REJECTED"
        assert placed["called"] is False
        order = db.query(models.TradeOrder).first()
        assert order.status == "REJECTED"

    def test_symbol_lock_held_by_other_service_rejects(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=5)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": False)
        released = {"called": False}
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: released.update(called=True))

        r = run(me.evaluate_manual_order(db, "REAL", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False and r["status"] == "REJECTED"
        assert "position-stocks-service" in r["detail"]
        assert released["called"] is True, "session60 fix: the symbol claim must be released on any placement failure"

    def test_dhan_placement_failure_rejects_and_releases_lock(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=5)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: (_ for _ in ()).throw(RuntimeError("RMS: generic reject")))
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)
        released = {"called": False}
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: released.update(called=True))

        r = run(me.evaluate_manual_order(db, "REAL", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False and r["status"] == "REJECTED" and r["reason"] == "dhan_error"
        assert released["called"] is True
        order = db.query(models.TradeOrder).first()
        assert order.status == "REJECTED"

    def test_dhan_accepted_but_no_order_id_returned_is_rejected(self, db, monkeypatch):
        """Line 351: place_order() can return a 200-ish payload with no
        orderId/order_id key (seen from Dhan on a couple of edge responses
        in prior sessions) -- must not be treated as a silent success with
        an empty/None broker order id on the row."""
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=5)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: {"status": "ok"})
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: None)

        r = run(me.evaluate_manual_order(db, "REAL", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False and r["status"] == "REJECTED"
        assert "no order id" in r["detail"]

    def test_dhan_invalid_ip_failure_disarms_and_gives_actionable_detail(self, db, monkeypatch):
        _quotes(monkeypatch, price=100.0)
        _approve(monkeypatch, qty=5)
        monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "999")
        monkeypatch.setattr(dhan_client, "is_invalid_ip_error", lambda msg: True)
        monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: (_ for _ in ()).throw(RuntimeError("DH-903: IP not whitelisted")))
        from execution import shared_order_budget, shared_symbol_lock
        monkeypatch.setattr(shared_order_budget, "check_and_reserve", lambda db_: True)
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: None)
        disarmed = {"called": False}
        monkeypatch.setattr("auth.dhan_credentials.disarm_on_invalid_ip", lambda db_, mode, msg: disarmed.update(called=True))

        r = run(me.evaluate_manual_order(db, "REAL", True, Req(), confirm=True, admin="tester"))
        assert r["ok"] is False
        assert r["reason"] == "invalid_ip"
        assert disarmed["called"] is True
        assert "IP" in r["detail"]


# ── SELL ─────────────────────────────────────────────────────────────────────

def _open_position(db, *, mode="DEMO", symbol="TESTCO", qty=10, entry=100.0):
    pos = models.TradePosition(
        mode=mode, symbol=symbol, status="OPEN", qty_open=qty,
        avg_entry_price=entry, opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


class TestSellValidation:
    def test_sell_with_no_matching_position_rejected(self, db, monkeypatch):
        _quotes(monkeypatch)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL"), confirm=False))
        assert r == {"ok": False, "reason": "no_position", "detail": "No open DEMO position in TESTCO to sell."}

    def test_sell_qty_capped_to_position_qty_open(self, db, monkeypatch):
        _open_position(db, qty=5)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL", qty=100), confirm=False))
        assert r["ok"] is True
        assert r["approved_qty"] == 5

    def test_sell_by_position_id_resolves_that_position_not_just_symbol(self, db, monkeypatch):
        """Covers _resolve_position's position_id branch (line 160) --
        a caller passing an explicit position_id (e.g. the positions table's
        per-row Sell button) must get that exact row, not just any open
        position matching the symbol."""
        older = _open_position(db, qty=3, entry=80.0)
        newer = _open_position(db, qty=7, entry=95.0)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(
            db, "DEMO", True, Req(side="SELL", qty=100, position_id=older.id), confirm=False,
        ))
        assert r["ok"] is True
        assert r["position_id"] == older.id
        assert r["approved_qty"] == 3  # older position's qty_open, not newer's

    def test_sell_zero_qty_open_position_rejected_as_invalid_qty(self, db, monkeypatch):
        """Edge case (line 401): a position row with qty_open<=0 (e.g. a
        stale OPEN row an exit fill hasn't fully reconciled down yet) must
        not be sellable -- min(req.qty, 0) collapses to 0, which must be
        rejected explicitly rather than proceeding with a zero-share order."""
        _open_position(db, qty=0)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL", qty=5), confirm=False))
        assert r["ok"] is False and r["reason"] == "invalid_qty"


class TestSellDemo:
    def test_confirm_sell_demo_closes_position(self, db, monkeypatch):
        _open_position(db, qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        r = run(me.evaluate_manual_order(db, "DEMO", True, Req(side="SELL", qty=10), confirm=True, admin="tester"))
        assert r["ok"] is True
        assert r["status"] == "CLOSED"
        pos = db.query(models.TradePosition).first()
        assert pos.status == "CLOSED"


class TestSellReal:
    def test_pending_sell_blocks_a_second_manual_sell(self, db, monkeypatch):
        """session21c: a partial exit already sent to Dhan (still awaiting
        fill) must block a second manual SELL from being sent for the same
        symbol -- otherwise both SELLs could land at the broker together."""
        pos = _open_position(db, mode="REAL", qty=10, entry=100.0)
        db.add(models.TradeOrder(mode="REAL", symbol=pos.symbol, side="SELL", qty=6,
                                  order_type="LIMIT", status="PLACED"))
        db.commit()
        _quotes(monkeypatch, price=105.0)
        r = run(me.evaluate_manual_order(db, "REAL", True, Req(side="SELL", qty=4), confirm=True, admin="tester"))
        assert r["ok"] is False
        assert r["reason"] == "pending_sell"

    def test_sell_threads_order_type_and_limit_price_into_send_real_sell(self, db, monkeypatch):
        """session39 regression: a manual LIMIT sell ticket must actually be
        sent to Dhan as LIMIT at the requested price, not silently downgraded
        to MARKET."""
        _open_position(db, mode="REAL", qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        captured = {}

        def _fake_send(db_, position, qty, reason, full=True, execution_source="AUTO",
                        confirmed_by=None, order_type="MARKET", limit_price=None):
            captured.update(order_type=order_type, limit_price=limit_price, qty=qty,
                             execution_source=execution_source, confirmed_by=confirmed_by)
            return True
        monkeypatch.setattr(me, "_send_real_sell", _fake_send)

        r = run(me.evaluate_manual_order(
            db, "REAL", True, Req(side="SELL", qty=10, order_type="LIMIT", limit_price=101.0),
            confirm=True, admin="tester",
        ))
        assert r["ok"] is True and r["status"] == "SENT_TO_BROKER"
        assert captured["order_type"] == "LIMIT"
        assert captured["limit_price"] == 101.0
        assert captured["execution_source"] == "MANUAL"
        assert captured["confirmed_by"] == "tester"

    def test_send_real_sell_failure_marks_rejected(self, db, monkeypatch):
        _open_position(db, mode="REAL", qty=10, entry=90.0)
        _quotes(monkeypatch, price=100.0)
        monkeypatch.setattr(me, "_send_real_sell", lambda *a, **k: False)
        r = run(me.evaluate_manual_order(db, "REAL", True, Req(side="SELL", qty=10), confirm=True, admin="tester"))
        assert r["ok"] is False and r["status"] == "REJECTED"
