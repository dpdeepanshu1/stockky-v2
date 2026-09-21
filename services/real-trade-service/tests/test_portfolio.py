"""
tests/test_portfolio.py

100%-coverage-plan Phase 1 #2: portfolio/portfolio.py -- 42% -> target 85%+.
This is P&L, cash, and position accounting; a wrong number here is either a
phantom loss or a phantom profit shown to a human making real decisions.

Two functions get dedicated attention, per the plan:

  - import_broker_holdings() -- session65's per-candidate exception
    isolation fix (one bad candidate in a batch must not silently drop
    every OTHER candidate in the same import cycle -- the RIR/ANUHPHR
    incident) plus the shared_symbol_lock gating and the recently-closed
    "same lot" guard (session66/RIR audit fix).
  - holdings_sync_reconcile() -- the ghost-position force-close +
    partial-qty-sync logic from the "19 OPEN vs 4 real holdings"
    incident, plus the session21c pending-real-sell race guard that
    prevents a partial exit from being double-booked.

record_real_fill/record_real_exit_fill get one hand-verified golden-number
P&L test each (known entry + known exit + known qty -> exact expected
number), not just "it ran".

Run from services/real-trade-service:
    python -m pytest tests/test_portfolio.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import portfolio.portfolio as pf
from execution import dhan_client, shared_symbol_lock
from market_feed.feed import Tick

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.add(models.TradeAccount(mode="REAL", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.commit()
    yield s
    s.close()


def _open_position(db, *, mode="REAL", symbol="TESTCO", qty=10, entry=100.0,
                    status="OPEN", opened_at=None, closed_at=None):
    pos = models.TradePosition(
        mode=mode, symbol=symbol, status=status, qty_open=qty,
        avg_entry_price=entry, opened_at=opened_at or datetime.now(timezone.utc),
        closed_at=closed_at, realized_pnl=0.0,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


class _Order:
    """Minimal stand-in for a TradeOrder passed to record_real_fill --
    real callers pass an actual TradeOrder row, but only these attributes
    are ever read."""
    def __init__(self, symbol="TESTCO", watchlist_entry_id=None, source_tab=None,
                 entry_decision_label=None, entry_conviction_score=None,
                 product_type=None, is_regime_override=False):
        self.symbol = symbol
        self.watchlist_entry_id = watchlist_entry_id
        self.source_tab = source_tab
        self.entry_decision_label = entry_decision_label
        self.entry_conviction_score = entry_conviction_score
        self.product_type = product_type
        self.is_regime_override = is_regime_override


# ── import_broker_holdings ───────────────────────────────────────────────────

class TestImportBrokerHoldings:
    def test_no_holdings_and_no_positions_imports_nothing(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        n = run(pf.import_broker_holdings(db))
        assert n == 0
        assert db.query(models.TradePosition).count() == 0

    def test_holdings_fetch_failure_fails_open_returns_zero(self, db, monkeypatch):
        def _boom(db_):
            raise RuntimeError("Dhan down")
        monkeypatch.setattr(dhan_client, "get_holdings", _boom)
        n = run(pf.import_broker_holdings(db))
        assert n == 0

    def test_already_tracked_symbol_is_not_reimported(self, db, monkeypatch):
        _open_position(db, symbol="TESTCO", qty=5)
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "TESTCO", "totalQty": 5, "avgCostPrice": 100.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        n = run(pf.import_broker_holdings(db))
        assert n == 0
        assert db.query(models.TradePosition).filter_by(symbol="TESTCO").count() == 1

    def test_new_holding_imported_with_flat_fallback_when_no_tick(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "NEWCO", "totalQty": 20, "avgCostPrice": 50.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        async def _no_quotes(symbols):
            return {}
        monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)

        n = run(pf.import_broker_holdings(db))
        assert n == 1
        pos = db.query(models.TradePosition).filter_by(symbol="NEWCO").first()
        assert pos is not None
        assert pos.qty_open == 20
        assert pos.avg_entry_price == 50.0
        assert pos.broker_imported is True
        assert pos.status == "OPEN"

    def test_symbol_locked_by_other_service_is_skipped_not_forced(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "LOCKEDCO", "totalQty": 5, "avgCostPrice": 100.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": False)

        n = run(pf.import_broker_holdings(db))
        assert n == 0
        assert db.query(models.TradePosition).filter_by(symbol="LOCKEDCO").count() == 0

    def test_recently_closed_same_lot_is_not_reimported(self, db, monkeypatch):
        """The 24h settlement-lag guard: a position WE closed minutes ago
        at ~the same avg cost must not be re-imported as if it were a
        pre-existing holding."""
        _open_position(
            db, symbol="SAMELOT", qty=0, entry=100.0, status="CLOSED",
            closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "SAMELOT", "totalQty": 10, "avgCostPrice": 100.2},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        n = run(pf.import_broker_holdings(db))
        assert n == 0

    def test_recently_closed_different_lot_is_reimported(self, db, monkeypatch):
        """session66/RIR fix: a meaningfully different avg cost means it's
        a genuinely new, separately-bought lot -- must import regardless
        of the 24h window."""
        _open_position(
            db, symbol="NEWLOT", qty=0, entry=100.0, status="CLOSED",
            closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "NEWLOT", "totalQty": 10, "avgCostPrice": 250.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        async def _no_quotes(symbols):
            return {}
        monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)

        n = run(pf.import_broker_holdings(db))
        assert n == 1

    def test_same_day_cnc_live_position_not_yet_in_holdings_is_imported(self, db, monkeypatch):
        """2026-09-17 fix: a same-day CNC buy shows up in get_positions()
        before it settles into get_holdings() -- must still be imported,
        not silently invisible until T+1."""
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [
            {"tradingSymbol": "SAMEDAY", "productType": "CNC",
             "netBuyQty": 15, "averageBuyPrice": 75.0},
        ])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        async def _no_quotes(symbols):
            return {}
        monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)

        n = run(pf.import_broker_holdings(db))
        assert n == 1
        pos = db.query(models.TradePosition).filter_by(symbol="SAMEDAY").first()
        assert pos.qty_open == 15 and pos.avg_entry_price == 75.0

    def test_intraday_live_position_is_never_imported(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [
            {"tradingSymbol": "SCALPCO", "productType": "INTRADAY",
             "netBuyQty": 15, "averageBuyPrice": 75.0},
        ])
        n = run(pf.import_broker_holdings(db))
        assert n == 0

    def test_one_bad_candidate_does_not_block_the_rest_of_the_batch(self, db, monkeypatch):
        """session65 regression guard: the RIR/ANUHPHR incident -- one
        candidate's import failing must not abandon every OTHER candidate
        queued in the same batch."""
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "BADCO", "totalQty": 5, "avgCostPrice": 100.0},
            {"tradingSymbol": "GOODCO", "totalQty": 8, "avgCostPrice": 60.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

        async def _no_quotes(symbols):
            return {}
        monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)

        real_flush = db.flush
        def _flush_boom_once(*a, **k):
            if not getattr(_flush_boom_once, "fired", False):
                pos = db.new.__iter__() if hasattr(db, "new") else None
                # Trigger the failure only for BADCO's own flush, not GOODCO's.
                for obj in list(db.new):
                    if isinstance(obj, models.TradePosition) and obj.symbol == "BADCO":
                        _flush_boom_once.fired = True
                        raise RuntimeError("simulated DB failure for BADCO")
            return real_flush(*a, **k)
        monkeypatch.setattr(db, "flush", _flush_boom_once)

        n = run(pf.import_broker_holdings(db))
        assert n == 1
        assert db.query(models.TradePosition).filter_by(symbol="GOODCO").count() == 1
        assert db.query(models.TradePosition).filter_by(symbol="BADCO").count() == 0


# ── holdings_sync_reconcile ───────────────────────────────────────────────────

class TestHoldingsSyncReconcile:
    def test_no_positions_returns_zero_closed(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0 and result["symbols"] == []

    def test_holdings_fetch_failure_fails_open(self, db, monkeypatch):
        def _boom(db_):
            raise RuntimeError("Dhan down")
        monkeypatch.setattr(dhan_client, "get_holdings", _boom)
        result = pf.holdings_sync_reconcile(db)
        assert result == {"closed": 0, "symbols": []}

    def test_ghost_position_force_closed_and_cash_refunded(self, db, monkeypatch):
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _open_position(db, symbol="GHOSTCO", qty=10, entry=100.0, opened_at=old_time)
        acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        acct.cash_available = 5000.0
        db.commit()

        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])

        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 1
        assert "GHOSTCO" in result["symbols"]
        db.refresh(pos)
        assert pos.status == "CLOSED"
        assert pos.qty_open == 0
        db.refresh(acct)
        assert acct.cash_available == 5000.0 + 1000.0  # 10 * 100.0 refunded

    def test_recently_opened_position_not_closed_settlement_lag_guard(self, db, monkeypatch):
        pos = _open_position(db, symbol="FRESHCO", qty=10, entry=100.0,
                              opened_at=datetime.now(timezone.utc))
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0
        db.refresh(pos)
        assert pos.status == "OPEN"

    def test_partial_external_sell_caps_qty_open_not_full_close(self, db, monkeypatch):
        """The ANDHRAPAP bug: broker holds SOME but less than qty_open --
        must cap down, not leave untouched and not fully ghost-close."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _open_position(db, symbol="ANDHRAPAP", qty=4, entry=100.0, opened_at=old_time)
        acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        acct.cash_available = 5000.0
        db.commit()

        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "ANDHRAPAP", "totalQty": 2, "avgCostPrice": 100.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])

        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0
        assert "ANDHRAPAP" in result["synced"]
        db.refresh(pos)
        assert pos.qty_open == 2
        assert pos.status == "PARTIALLY_CLOSED"
        db.refresh(acct)
        assert acct.cash_available == 5000.0 + 200.0  # 2 shares refunded @ 100.0

    def test_broker_qty_greater_or_equal_leaves_position_untouched(self, db, monkeypatch):
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _open_position(db, symbol="FINECO", qty=4, entry=100.0, opened_at=old_time)
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
            {"tradingSymbol": "FINECO", "totalQty": 4, "avgCostPrice": 100.0},
        ])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])
        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0
        assert result["synced"] == []
        db.refresh(pos)
        assert pos.qty_open == 4 and pos.status == "OPEN"

    def test_position_with_pending_real_sell_is_skipped_no_double_book(self, db, monkeypatch):
        """session21c race guard: a partial SELL already sent to Dhan this
        cycle must not ALSO be treated as an external ghost/partial-sell
        here -- that would double-decrement qty_open and double-refund
        cash once the pending-orders loop books the real fill too."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _open_position(db, symbol="RACECO", qty=10, entry=100.0, opened_at=old_time)
        db.add(models.TradeOrder(mode="REAL", symbol="RACECO", side="SELL", qty=5,
                                  order_type="MARKET", status="PLACED"))
        db.commit()
        monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda db_: [])

        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0
        assert result["synced"] == []
        db.refresh(pos)
        assert pos.qty_open == 10  # untouched -- pending-orders loop owns this fill


# ── record_real_fill / record_real_exit_fill — golden-number P&L ────────────

class TestFillAndExitPnl:
    def test_record_real_fill_opens_new_position_with_correct_cash_deduction(self, db):
        order = models.TradeOrder(mode="REAL", symbol="GOLDCO", side="BUY", qty=10,
                                   order_type="LIMIT", status="PLACED")
        db.add(order)
        db.commit()
        pf.record_real_fill(db, order, fill_price=200.0, filled_qty=10,
                             stop_price=190.0, target_price=220.0)
        pos = db.query(models.TradePosition).filter_by(symbol="GOLDCO").first()
        assert pos.qty_open == 10
        assert pos.avg_entry_price == 200.0
        acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        assert acct.cash_available == 100000.0 - 2000.0  # 10 * 200.0

    def test_record_real_fill_pyramids_into_existing_position_volume_weighted_avg(self, db):
        _open_position(db, symbol="PYRCO", qty=10, entry=100.0)
        order = models.TradeOrder(mode="REAL", symbol="PYRCO", side="BUY", qty=10,
                                   order_type="LIMIT", status="PLACED")
        db.add(order)
        db.commit()
        pf.record_real_fill(db, order, fill_price=120.0, filled_qty=10,
                             stop_price=95.0, target_price=140.0)
        pos = db.query(models.TradePosition).filter_by(symbol="PYRCO").first()
        assert pos.qty_open == 20
        # (10*100 + 10*120) / 20 = 110.0
        assert pos.avg_entry_price == 110.0

    def test_record_real_exit_fill_golden_number_pnl_full_close(self, db):
        pos = _open_position(db, symbol="EXITCO", qty=10, entry=90.0)
        acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        acct.cash_available = 1000.0
        db.commit()

        pnl = pf.record_real_exit_fill(db, pos, exit_price=105.0, qty_closed=10, reason="target_hit")
        assert pnl == 150.0  # (105-90) * 10
        db.refresh(pos)
        assert pos.status == "CLOSED"
        assert pos.qty_open == 0
        assert pos.realized_pnl == 150.0
        db.refresh(acct)
        assert acct.cash_available == 1000.0 + 105.0 * 10
        assert acct.realized_pnl_today == 150.0
        assert acct.realized_pnl_total == 150.0

    def test_record_real_exit_fill_partial_target_hit_moves_stop_to_breakeven(self, db):
        pos = _open_position(db, symbol="PARTCO", qty=10, entry=100.0)
        pos.current_stop = 90.0
        db.commit()
        pf.record_real_exit_fill(db, pos, exit_price=115.0, qty_closed=4, reason="target_hit_partial")
        db.refresh(pos)
        assert pos.status == "PARTIALLY_CLOSED"
        assert pos.qty_open == 6
        assert pos.current_stop == 100.0  # moved up to breakeven (avg_entry_price)

    def test_record_real_exit_fill_full_close_releases_symbol_lock(self, db, monkeypatch):
        pos = _open_position(db, symbol="RELEASECO", qty=5, entry=50.0)
        released = {"symbol": None}
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: released.update(symbol=sym))
        pf.record_real_exit_fill(db, pos, exit_price=60.0, qty_closed=5, reason="stop_hit")
        assert released["symbol"] == "RELEASECO"


# ── force_close_real_position ────────────────────────────────────────────────

class TestForceCloseRealPosition:
    def test_demo_position_rejected(self, db):
        pos = _open_position(db, mode="DEMO", symbol="X", qty=1, entry=1.0)
        with pytest.raises(RuntimeError, match="REAL-only"):
            pf.force_close_real_position(db, pos, "test")

    def test_refunds_cash_and_zero_pnl(self, db, monkeypatch):
        pos = _open_position(db, symbol="FORCECO", qty=10, entry=50.0)
        acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        acct.cash_available = 100.0
        db.commit()
        monkeypatch.setattr(shared_symbol_lock, "release", lambda db_, sym: None)

        pnl = pf.force_close_real_position(db, pos, "broker reports 0 held")
        assert pnl == 0.0
        db.refresh(pos)
        assert pos.status == "CLOSED" and pos.qty_open == 0
        db.refresh(acct)
        assert acct.cash_available == 100.0 + 500.0


# ── refresh_unrealized ───────────────────────────────────────────────────────

class TestRefreshUnrealized:
    def test_updates_unrealized_pnl_and_equity_from_live_tick(self, db):
        _open_position(db, mode="DEMO", symbol="LIVECO", qty=10, entry=100.0)
        acct = db.query(models.TradeAccount).filter_by(mode="DEMO").first()
        acct.cash_available = 5000.0
        db.commit()

        tick = Tick(symbol="LIVECO", price=112.0, as_of=datetime.now(timezone.utc), atr=1.0, source="test")
        pf.refresh_unrealized(db, "DEMO", {"LIVECO": tick})

        pos = db.query(models.TradePosition).filter_by(symbol="LIVECO").first()
        assert pos.unrealized_pnl == 120.0  # (112-100)*10
        acct = db.query(models.TradeAccount).filter_by(mode="DEMO").first()
        assert acct.current_equity == 5000.0 + 112.0 * 10

    def test_missing_tick_falls_back_to_avg_entry_price(self, db):
        _open_position(db, mode="DEMO", symbol="STALECO", qty=5, entry=80.0)
        pf.refresh_unrealized(db, "DEMO", {})
        pos = db.query(models.TradePosition).filter_by(symbol="STALECO").first()
        assert pos.unrealized_pnl == 0.0
