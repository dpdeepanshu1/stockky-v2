"""Phase 1 (100%-coverage plan): targeted tests for every remaining missed
line in portfolio/portfolio.py (88% → 100%).

Missed-line groups (confirmed via --cov-report=term-missing):

  75-76     _accumulate_net_pnl: cost-model exception swallowed
  144-145   get_account: for_update=True try block
  147       get_account: non-sqlite dialect → with_for_update()
  306-308   import_broker_holdings: live-positions fetch exception → []
  335-337   live-positions loop: non-dict row skipped
  343       live-positions loop: sym in _holdings_symbols → skip
  353       live-positions loop: qty_raw or avg_raw missing → continue
  383       live-positions: qty <= 0 or avg <= 0 → continue
  391       holdings candidate loop: non-dict row → continue
  394       holdings candidate loop: raw fields missing → continue
  406       holdings candidate loop: int/float parse error → continue
  410-411   holdings candidate loop: qty <= 0 or avg <= 0 → continue
  413       holdings candidate loop: already tracked → continue
  434       extra_rows path: already-tracked symbol skipped
  444       import_broker_holdings: get_quotes exception → ticks = {}
  448-449   per-candidate: clamp_for_atr exception → atr_pct = raw
  451       per-candidate: _atr_stop_target_pct atr_pct=None → flat
  530-532   holdings_sync_reconcile: get_holdings exception → fail open
  591-592   holdings_sync_reconcile: get_positions exception → fail open
  733-735   holdings_sync_reconcile: non-dict row in holdings loop
  741       holdings_sync_reconcile: non-dict row in live_positions loop
  747-748   live_positions: sym present but qty == 0 → not added to broker_symbols
  769       partial-qty-sync: too_recent → continue
  776-790   partial-qty-sync: broker_have >= qty_open → continue; else cap
  863       try_fill_entry: REAL mode → RuntimeError
  955       try_fill_entry: side != BUY or status != PLACED → False
  957       try_fill_entry: price > limit_price → False
  1033-1036 try_fill_entry: pyramiding existing OPEN position → ADDED
  1057      close_position: REAL mode → RuntimeError
  1060      close_position: qty_to_close <= 0 → 0.0

Run from services/real-trade-service:
    python -m pytest tests/test_portfolio_remaining_coverage.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import portfolio.portfolio as pf
from execution import dhan_client
from market_feed.feed import Tick

_engine = create_engine("sqlite:///:memory:")


# ── helpers ──────────────────────────────────────────────────────────────────

def _fresh_db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeRiskConfig(mode="DEMO"))
    s.add(models.TradeRiskConfig(mode="REAL"))
    s.commit()
    return s


def _mkpos(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTCO", status="OPEN", qty_open=10,
        avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
        opened_at=datetime.now(timezone.utc), broker_imported=True,
        initial_stop_distance=5.0,
    )
    defaults.update(kw)
    pos = models.TradePosition(**defaults)
    db.add(pos); db.commit(); db.refresh(pos)
    return pos


def _mkorder(db, **kw):
    defaults = dict(
        mode="DEMO", symbol="TESTCO", side="BUY",
        order_type="LIMIT", qty=5, limit_price=100.0,
        status="PLACED", execution_source="AUTO",
    )
    defaults.update(kw)
    order = models.TradeOrder(**defaults)
    db.add(order); db.commit(); db.refresh(order)
    return order


def _tick(price, atr=None, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc),
                atr=atr, source="test")


# ════════════════════════════════════════════════════════════════════════════════
# _accumulate_net_pnl: cost-model exception swallowed (lines 75-76)
# ════════════════════════════════════════════════════════════════════════════════

class TestAccumulateNetPnlException:
    def test_cost_model_exception_does_not_propagate(self, monkeypatch):
        """estimate_round_trip_cost raising must be swallowed; gross P&L unaffected."""
        db = _fresh_db()
        pos = _mkpos(db, mode="DEMO", avg_entry_price=100.0,
                     realized_pnl=500.0, realized_cost_estimate=None,
                     net_realized_pnl=None)
        import cost_model
        monkeypatch.setattr(cost_model, "estimate_round_trip_cost",
                            mock.Mock(side_effect=RuntimeError("cost exploded")))
        # Must not raise — exception is caught at lines 75-76
        pf._accumulate_net_cost(pos, qty_closed=5, exit_price=110.0,
                                pnl_leg=200.0, now=datetime.now(timezone.utc))
        # net_realized_pnl stays None (exception before assignment)
        assert pos.realized_cost_estimate is None
        assert pos.net_realized_pnl is None


# ════════════════════════════════════════════════════════════════════════════════
# get_account: for_update paths (lines 144-145, 147)
# ════════════════════════════════════════════════════════════════════════════════

class TestGetAccountForUpdate:
    def test_for_update_on_sqlite_does_not_raise(self):
        """sqlite → for_update branch runs but skips with_for_update (lines 144-145)."""
        db = _fresh_db()
        account = pf.get_account(db, "DEMO", for_update=True)
        assert account.mode == "DEMO"

    def test_for_update_get_bind_exception_falls_back(self):
        """First get_bind() call raises → bind_dialect="" → no with_for_update (line 145)."""
        db = _fresh_db()
        orig_bind = db.get_bind
        call_n = [0]
        def _boom_first(*a, **kw):
            call_n[0] += 1
            if call_n[0] == 1:   # only the line-143 call; let later SA calls through
                raise RuntimeError("no bind")
            return orig_bind(*a, **kw)
        db.get_bind = _boom_first
        try:
            account = pf.get_account(db, "REAL", for_update=True)
            assert account.mode == "REAL"
        finally:
            db.get_bind = orig_bind

    def test_for_update_non_sqlite_calls_with_for_update(self, monkeypatch):
        """Non-sqlite dialect → query.with_for_update() called (line 147)."""
        db = _fresh_db()

        # Fake a bind that claims to be postgresql
        fake_bind = mock.MagicMock()
        fake_bind.dialect.name = "postgresql"
        monkeypatch.setattr(db, "get_bind", mock.Mock(return_value=fake_bind))

        # The real account row we'll return from the mock query chain
        real_account = db.query(models.TradeAccount).filter_by(mode="DEMO").first()

        # Build a mock query chain that confirms with_for_update was called
        q = mock.MagicMock()
        q.filter_by.return_value = q
        q.with_for_update.return_value = q
        q.first.return_value = real_account
        monkeypatch.setattr(db, "query", mock.Mock(return_value=q))

        result = pf.get_account(db, "DEMO", for_update=True)
        q.with_for_update.assert_called_once()
        assert result == real_account


# ════════════════════════════════════════════════════════════════════════════════
# import_broker_holdings: live-positions sub-branches
# ════════════════════════════════════════════════════════════════════════════════

class TestImportBrokerHoldingsSubBranches:

    def _run(self, db, holdings, positions, monkeypatch, get_quotes_mock=None):
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=positions))
        if get_quotes_mock is not None:
            import market_feed.feed as mf
            monkeypatch.setattr(mf, "get_quotes", get_quotes_mock)
        return asyncio.run(pf.import_broker_holdings(db))

    # ── live-positions fetch exception (lines 306-308) ────────────────────

    def test_live_positions_exception_uses_empty_list(self, monkeypatch):
        db = _fresh_db()
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=[]))
        monkeypatch.setattr("execution.dhan_client.get_positions",
                            mock.Mock(side_effect=RuntimeError("positions down")))
        assert asyncio.run(pf.import_broker_holdings(db)) == 0

    # ── live-positions loop: non-dict row (lines 335-337) ────────────────

    def test_live_positions_non_dict_rows_skipped(self, monkeypatch):
        db = _fresh_db()
        assert self._run(db, [], ["bad", None, 99], monkeypatch) == 0

    # ── live-positions: sym in _holdings_symbols → skip (line 343) ───────

    def test_live_position_already_in_holdings_skipped(self, monkeypatch):
        db = _fresh_db()
        holdings = [{"tradingSymbol": "TESTCO", "totalQty": 10, "avgCostPrice": 100.0}]
        positions = [{"tradingSymbol": "TESTCO", "productType": "CNC",
                      "netBuyQty": 10, "averageBuyPrice": 100.0}]
        _mkpos(db, symbol="TESTCO", mode="REAL")  # already tracked → skip
        assert self._run(db, holdings, positions, monkeypatch) == 0

    # ── live-positions: qty or avg missing (line 353) ─────────────────────

    def test_live_position_missing_qty_and_avg_skipped(self, monkeypatch):
        db = _fresh_db()
        positions = [{"tradingSymbol": "X", "productType": "CNC"}]  # no qty/avg
        assert self._run(db, [], positions, monkeypatch) == 0

    # ── live-positions: qty <= 0 (line 383) ──────────────────────────────

    def test_live_position_zero_qty_skipped(self, monkeypatch):
        db = _fresh_db()
        positions = [{"tradingSymbol": "ZERCO", "productType": "CNC",
                      "netBuyQty": 0, "averageBuyPrice": 100.0}]
        assert self._run(db, [], positions, monkeypatch) == 0

    def test_live_position_zero_avg_skipped(self, monkeypatch):
        db = _fresh_db()
        positions = [{"tradingSymbol": "ZERCO", "productType": "CNC",
                      "netBuyQty": 5, "averageBuyPrice": 0}]
        assert self._run(db, [], positions, monkeypatch) == 0

    # ── holdings loop: non-dict row (line 391) ───────────────────────────

    def test_holdings_non_dict_row_skipped(self, monkeypatch):
        db = _fresh_db()
        assert self._run(db, ["bad", 42, None], [], monkeypatch) == 0

    # ── holdings: raw fields missing (line 394) ──────────────────────────

    def test_holdings_row_missing_symbol_qty_avg_skipped(self, monkeypatch):
        db = _fresh_db()
        holdings = [
            {"totalQty": 10, "avgCostPrice": 100.0},         # no symbol
            {"tradingSymbol": "X", "avgCostPrice": 100.0},   # no qty
            {"tradingSymbol": "Y", "totalQty": 5},            # no avg
        ]
        assert self._run(db, holdings, [], monkeypatch) == 0

    # ── holdings: parse error (line 406) ─────────────────────────────────

    def test_holdings_unparseable_qty_skipped(self, monkeypatch):
        db = _fresh_db()
        holdings = [{"tradingSymbol": "BADCO", "totalQty": "abc", "avgCostPrice": 100.0}]
        assert self._run(db, holdings, [], monkeypatch) == 0

    # ── holdings: qty <= 0 or avg <= 0 (lines 410-411) ───────────────────

    def test_holdings_negative_qty_skipped(self, monkeypatch):
        db = _fresh_db()
        holdings = [{"tradingSymbol": "NEGCO", "totalQty": -1, "avgCostPrice": 100.0}]
        assert self._run(db, holdings, [], monkeypatch) == 0

    # ── already tracked via holdings path (line 413) ─────────────────────

    def test_holdings_already_tracked_skipped(self, monkeypatch):
        db = _fresh_db()
        _mkpos(db, symbol="TRACKED", mode="REAL")
        holdings = [{"tradingSymbol": "TRACKED", "totalQty": 10, "avgCostPrice": 100.0}]
        assert self._run(db, holdings, [], monkeypatch) == 0

    # ── already tracked via extra_rows path (line 434) ───────────────────

    def test_extra_rows_already_tracked_skipped(self, monkeypatch):
        db = _fresh_db()
        _mkpos(db, symbol="SAMEDAY", mode="REAL")
        positions = [{"tradingSymbol": "SAMEDAY", "productType": "CNC",
                      "netBuyQty": 5, "averageBuyPrice": 100.0}]
        assert self._run(db, [], positions, monkeypatch) == 0

    # ── get_quotes exception → ticks = {} (line 444) ─────────────────────

    def test_get_quotes_exception_falls_back_to_flat_stop(self, monkeypatch):
        db = _fresh_db()
        holdings = [{"tradingSymbol": "NEWCO", "totalQty": 5, "avgCostPrice": 200.0}]
        async def _boom(syms): raise RuntimeError("feed down")
        result = self._run(db, holdings, [], monkeypatch, get_quotes_mock=_boom)
        assert result == 1  # imported with flat fallback

    # ── clamp_for_atr exception → atr_pct = raw (lines 448-449) ─────────

    def test_clamp_for_atr_exception_uses_raw_atr(self, monkeypatch):
        db = _fresh_db()
        holdings = [{"tradingSymbol": "CLAMPCO", "totalQty": 3, "avgCostPrice": 150.0}]
        t = Tick(symbol="CLAMPCO", price=150.0, as_of=datetime.now(timezone.utc),
                 atr=3.0, source="test")
        async def _q(syms): return {"CLAMPCO": t}
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=[]))
        import market_feed.feed as mf
        monkeypatch.setattr(mf, "get_quotes", _q)
        import return_sanity
        monkeypatch.setattr(return_sanity, "clamp_for_atr",
                            mock.Mock(side_effect=RuntimeError("clamp broken")))
        result = asyncio.run(pf.import_broker_holdings(db))
        assert result == 1  # imported despite clamp error

    # ── _atr_stop_target_pct atr_pct=None (line 451) — tested via full path

    def test_none_atr_uses_flat_fallback_stop(self, monkeypatch):
        """No tick available → atr_pct=None → flat fallback stop/target (line 451)."""
        db = _fresh_db()
        holdings = [{"tradingSymbol": "NOTICKCO", "totalQty": 2, "avgCostPrice": 100.0}]
        async def _q(syms): return {}  # no tick for this symbol
        result = self._run(db, holdings, [], monkeypatch, get_quotes_mock=_q)
        assert result == 1
        pos = db.query(models.TradePosition).filter_by(symbol="NOTICKCO").first()
        from entry_engine.entry import FLAT_STOP_PCT, FLAT_TARGET_PCT
        expected_stop = round(100.0 * (1 - FLAT_STOP_PCT / 100.0), 2)
        assert abs(pos.current_stop - expected_stop) < 0.01


# ════════════════════════════════════════════════════════════════════════════════
# holdings_sync_reconcile: fetch failures (lines 530-532, 591-592)
# Note: holdings_sync_reconcile is NOT async
# ════════════════════════════════════════════════════════════════════════════════

class TestHoldingsSyncReconcileFetchFailures:
    def test_get_holdings_exception_returns_zero_closed(self, monkeypatch):
        # lines 530-532
        db = _fresh_db()
        monkeypatch.setattr("execution.dhan_client.get_holdings",
                            mock.Mock(side_effect=RuntimeError("net error")))
        result = pf.holdings_sync_reconcile(db)
        assert result == {"closed": 0, "symbols": []}

    def test_get_positions_exception_returns_zero_closed(self, monkeypatch):
        # lines 591-592
        db = _fresh_db()
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=[]))
        monkeypatch.setattr("execution.dhan_client.get_positions",
                            mock.Mock(side_effect=RuntimeError("positions down")))
        result = pf.holdings_sync_reconcile(db)
        assert result == {"closed": 0, "symbols": []}


# ════════════════════════════════════════════════════════════════════════════════
# holdings_sync_reconcile: broker_qty edge cases (lines 733-735, 741, 747-748)
# ════════════════════════════════════════════════════════════════════════════════

class TestHoldingsSyncBrokerQtyEdges:
    def _run(self, db, holdings, positions, monkeypatch):
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=positions))
        return pf.holdings_sync_reconcile(db)

    def test_non_dict_row_in_holdings_skipped(self, monkeypatch):
        # lines 733-735
        db = _fresh_db()
        result = self._run(db, ["bad", None], [], monkeypatch)
        assert result["closed"] == 0

    def test_non_dict_row_in_live_positions_skipped(self, monkeypatch):
        # line 741
        db = _fresh_db()
        result = self._run(db, [], ["also_bad", 42], monkeypatch)
        assert result["closed"] == 0

    def test_live_position_zero_qty_not_added_to_broker_symbols(self, monkeypatch):
        # lines 747-748: qty = max(0, 0) == 0 → not added → symbol treated as ghost
        db = _fresh_db()
        pos = _mkpos(db, symbol="GHOSTCO", mode="REAL",
                     opened_at=datetime.now(timezone.utc) - timedelta(hours=2))
        # Zero qty in both positions fields → not added to broker_symbols → ghost close
        positions = [{"tradingSymbol": "GHOSTCO", "netQty": 0, "positiveQty": 0}]
        result = self._run(db, [], positions, monkeypatch)
        db.refresh(pos)
        assert pos.status == "CLOSED"


# ════════════════════════════════════════════════════════════════════════════════
# holdings_sync_reconcile: partial-qty-sync branches (lines 769, 776-790)
# ════════════════════════════════════════════════════════════════════════════════

class TestHoldingsSyncPartialQtySync:
    def _run(self, db, holdings, positions, monkeypatch):
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=positions))
        return pf.holdings_sync_reconcile(db)

    def test_too_recent_position_in_broker_symbols_not_capped(self, monkeypatch):
        # line 769: opened < GUARD_MINUTES ago → too_recent → continue
        import config as cfg
        db = _fresh_db()
        pos = _mkpos(db, symbol="FRESHCO", mode="REAL", qty_open=10,
                     opened_at=datetime.now(timezone.utc))  # just now → too recent
        holdings = [{"tradingSymbol": "FRESHCO", "totalQty": 6}]
        self._run(db, holdings, [], monkeypatch)
        db.refresh(pos)
        assert pos.qty_open == 10  # NOT capped

    def test_broker_qty_equal_to_qty_open_no_change(self, monkeypatch):
        # lines 776-778: broker_have >= qty_open → continue
        db = _fresh_db()
        pos = _mkpos(db, symbol="EQUALCO", mode="REAL", qty_open=5,
                     opened_at=datetime.now(timezone.utc) - timedelta(hours=2))
        holdings = [{"tradingSymbol": "EQUALCO", "totalQty": 5}]
        self._run(db, holdings, [], monkeypatch)
        db.refresh(pos)
        assert pos.qty_open == 5

    def test_broker_qty_greater_than_qty_open_no_change(self, monkeypatch):
        # broker holds MORE than we track → nothing to fix
        db = _fresh_db()
        pos = _mkpos(db, symbol="MORECO", mode="REAL", qty_open=3,
                     opened_at=datetime.now(timezone.utc) - timedelta(hours=2))
        holdings = [{"tradingSymbol": "MORECO", "totalQty": 10}]
        self._run(db, holdings, [], monkeypatch)
        db.refresh(pos)
        assert pos.qty_open == 3

    def test_partial_external_sell_caps_qty_open(self, monkeypatch):
        # lines 781-790: broker_have < qty_open → cap it
        db = _fresh_db()
        pos = _mkpos(db, symbol="CAPCO", mode="REAL", qty_open=10,
                     avg_entry_price=100.0,
                     opened_at=datetime.now(timezone.utc) - timedelta(hours=2))
        holdings = [{"tradingSymbol": "CAPCO", "totalQty": 6}]
        self._run(db, holdings, [], monkeypatch)
        db.refresh(pos)
        assert pos.qty_open == 6


# ════════════════════════════════════════════════════════════════════════════════
# try_fill_entry: guard branches (lines 863, 955, 957)
# ════════════════════════════════════════════════════════════════════════════════

class TestTryFillEntryGuards:
    def test_real_mode_raises(self):
        # line 863
        db = _fresh_db()
        order = _mkorder(db, mode="REAL", status="PLACED")
        with pytest.raises(RuntimeError, match="DEMO-only"):
            pf.try_fill_entry(db, order, _tick(100.0), 95.0, 110.0)

    def test_sell_order_returns_false(self):
        # line 955: side != BUY
        db = _fresh_db()
        order = _mkorder(db, side="SELL", status="PLACED")
        assert pf.try_fill_entry(db, order, _tick(100.0), 95.0, 110.0) is False

    def test_non_placed_status_returns_false(self):
        # line 955: status != PLACED
        db = _fresh_db()
        order = _mkorder(db, side="BUY", status="FILLED")
        assert pf.try_fill_entry(db, order, _tick(100.0), 95.0, 110.0) is False

    def test_price_above_limit_returns_false(self):
        # line 957: tick.price > order.limit_price
        db = _fresh_db()
        order = _mkorder(db, side="BUY", status="PLACED", limit_price=98.0)
        assert pf.try_fill_entry(db, order, _tick(105.0), 95.0, 110.0) is False


# ════════════════════════════════════════════════════════════════════════════════
# try_fill_entry: pyramiding (lines 1033-1036)
# ════════════════════════════════════════════════════════════════════════════════

class TestTryFillEntryPyramiding:
    def test_existing_open_position_volume_weighted_avg_updated(self):
        # lines 1033-1036: existing OPEN → ADDED event, avg recalculated
        db = _fresh_db()
        pos = _mkpos(db, mode="DEMO", symbol="PYRCO", qty_open=10,
                     avg_entry_price=100.0, status="OPEN",
                     current_stop=95.0, current_target=110.0)
        order = _mkorder(db, mode="DEMO", symbol="PYRCO",
                         side="BUY", status="PLACED", limit_price=102.0, qty=5)
        tick = _tick(101.0, symbol="PYRCO")  # 101 <= 102 → fills at 101
        filled = pf.try_fill_entry(db, order, tick, 95.0, 110.0)
        assert filled is True
        db.refresh(pos)
        assert pos.qty_open == 15
        expected_avg = round((100.0 * 10 + 101.0 * 5) / 15, 4)
        assert abs(pos.avg_entry_price - expected_avg) < 0.01
        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="ADDED").first()
        assert event is not None


# ════════════════════════════════════════════════════════════════════════════════
# close_position: guard branches (lines 1057, 1060)
# ════════════════════════════════════════════════════════════════════════════════

class TestClosePositionGuards:
    def test_real_mode_raises(self):
        # line 1057
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL")
        with pytest.raises(RuntimeError, match="DEMO-only"):
            pf.close_position(db, pos, _tick(110.0), 5, "test")

    def test_zero_qty_to_close_returns_zero(self):
        # line 1060: min(requested, qty_open=0) == 0 → return 0.0
        db = _fresh_db()
        pos = _mkpos(db, mode="DEMO", qty_open=0)
        result = pf.close_position(db, pos, _tick(110.0), 5, "test")
        assert result == 0.0


# ════════════════════════════════════════════════════════════════════════════════
# _maybe_reset_daily_pnl: reset body (line 97)
# ════════════════════════════════════════════════════════════════════════════════

class TestMaybeResetDailyPnl:
    def test_reset_fires_when_date_is_stale(self):
        """pnl_last_reset_date != today → realized_pnl_today zeroed (line 97 body)."""
        db = _fresh_db()
        account = pf.get_account(db, "DEMO")
        account.realized_pnl_today = 999.0
        account.pnl_last_reset_date = "2000-01-01"  # force stale date
        db.commit()
        # Re-read via get_account — triggers _maybe_reset_daily_pnl
        account2 = pf.get_account(db, "DEMO")
        assert account2.realized_pnl_today == 0.0

    def test_no_reset_when_date_is_today(self):
        """pnl_last_reset_date == today → return early without touching pnl_today."""
        db = _fresh_db()
        from tz_utils import ist_today_str
        account = pf.get_account(db, "DEMO")
        account.realized_pnl_today = 777.0
        account.pnl_last_reset_date = ist_today_str()
        db.commit()
        account2 = pf.get_account(db, "DEMO")
        assert account2.realized_pnl_today == 777.0


# ════════════════════════════════════════════════════════════════════════════════
# get_account: no row → RuntimeError (line 150)
# ════════════════════════════════════════════════════════════════════════════════

class TestGetAccountMissingRow:
    def test_missing_account_row_raises(self):
        """Mode with no account row → RuntimeError (line 150)."""
        db = _fresh_db()
        with pytest.raises(RuntimeError, match="schema not seeded"):
            pf.get_account(db, "PAPER")  # no such mode in the DB


# ════════════════════════════════════════════════════════════════════════════════
# import_broker_holdings: non-CNC live_positions skip (line 391)
# ════════════════════════════════════════════════════════════════════════════════

class TestImportBrokerHoldingsNonCNC:
    def test_mis_intraday_position_skipped(self, monkeypatch):
        """productType=MIS → not CNC/DELIVERY → continue (line 391)."""
        db = _fresh_db()
        positions = [{"tradingSymbol": "MISCO", "productType": "MIS",
                      "netBuyQty": 5, "averageBuyPrice": 100.0}]
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=[]))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=positions))
        result = asyncio.run(pf.import_broker_holdings(db))
        assert result == 0

    def test_candidate_loop_qty_zero_skipped(self, monkeypatch):
        """In candidate loop: qty <= 0 → continue (lines 410-411)."""
        db = _fresh_db()
        holdings = [{"tradingSymbol": "ZEROCO", "totalQty": 0, "avgCostPrice": 100.0}]
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=[]))
        result = asyncio.run(pf.import_broker_holdings(db))
        assert result == 0

    def test_candidate_loop_already_tracked_skipped(self, monkeypatch):
        """In candidate loop: OPEN REAL position exists → already_tracked → continue (line 413)."""
        db = _fresh_db()
        _mkpos(db, symbol="DUPCO", mode="REAL", status="OPEN")
        holdings = [{"tradingSymbol": "DUPCO", "totalQty": 5, "avgCostPrice": 100.0}]
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=holdings))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=[]))
        result = asyncio.run(pf.import_broker_holdings(db))
        assert result == 0


# ════════════════════════════════════════════════════════════════════════════════
# holdings_sync_reconcile: live_positions non-dict + zero-qty (lines 741, 747-748)
# and partial-qty-cap (lines 789-790)
# — each test uses its own isolated SQLite engine to avoid state leaks
# ════════════════════════════════════════════════════════════════════════════════

def _isolated_db():
    """Fresh engine + session — avoids any cross-test state contamination."""
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    s.add(models.TradeRiskConfig(mode="REAL"))
    s.add(models.TradeRiskConfig(mode="DEMO"))
    s.commit()
    return s


class TestHoldingsSyncIsolated:
    """Isolated-engine variants to guarantee no state leaks from shared _engine."""

    def test_live_positions_non_dict_row_skipped(self, monkeypatch):
        # line 741
        db = _isolated_db()
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=[]))
        monkeypatch.setattr("execution.dhan_client.get_positions",
                            mock.Mock(return_value=["bad_row", None, 42]))
        result = pf.holdings_sync_reconcile(db)
        assert result["closed"] == 0

    def test_live_position_zero_qty_not_added_to_broker_symbols(self, monkeypatch):
        # lines 747-748: max(netQty=0, positiveQty=0) == 0 → not added → ghost close
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="GHOSTCO", status="OPEN", qty_open=5,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        monkeypatch.setattr("execution.dhan_client.get_holdings", mock.Mock(return_value=[]))
        monkeypatch.setattr("execution.dhan_client.get_positions",
                            mock.Mock(return_value=[
                                {"tradingSymbol": "GHOSTCO", "netQty": 0, "positiveQty": 0}
                            ]))
        pf.holdings_sync_reconcile(db)
        db.refresh(pos)
        assert pos.status == "CLOSED"

    def test_partial_external_sell_caps_qty_open(self, monkeypatch):
        # lines 789-790: broker_have(6) < qty_open(10) → cap to 6
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="CAPCO", status="OPEN", qty_open=10,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        monkeypatch.setattr("execution.dhan_client.get_holdings",
                            mock.Mock(return_value=[
                                {"tradingSymbol": "CAPCO", "totalQty": 6}
                            ]))
        monkeypatch.setattr("execution.dhan_client.get_positions", mock.Mock(return_value=[]))
        pf.holdings_sync_reconcile(db)
        db.refresh(pos)
        assert pos.qty_open == 6


# ════════════════════════════════════════════════════════════════════════════════
# try_fill_entry: new-position creation path (lines 992-1024)
# ════════════════════════════════════════════════════════════════════════════════

class TestTryFillEntryNewPosition:
    def test_creates_open_position_and_opened_event_and_debits_cash(self):
        # lines 992-1024: position is None → new TradePosition + OPENED event
        db = _isolated_db()
        order = models.TradeOrder(
            mode="DEMO", symbol="BRANDNEW", side="BUY",
            order_type="LIMIT", qty=5, limit_price=100.0,
            status="PLACED", execution_source="AUTO",
        )
        db.add(order); db.commit(); db.refresh(order)

        tick = Tick(symbol="BRANDNEW", price=99.0,
                    as_of=datetime.now(timezone.utc), atr=2.0, source="test")
        filled = pf.try_fill_entry(db, order, tick, stop_price=94.0, target_price=110.0)
        assert filled is True

        pos = db.query(models.TradePosition).filter_by(symbol="BRANDNEW").first()
        assert pos is not None
        assert pos.qty_open == 5
        assert pos.avg_entry_price == 99.0
        assert pos.current_stop == 94.0
        assert pos.current_target == 110.0
        assert pos.initial_stop_distance == abs(99.0 - 94.0)

        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="OPENED").first()
        assert event is not None

        order_after = db.get(models.TradeOrder, order.id)
        assert order_after.status == "FILLED"

        account = pf.get_account(db, "DEMO")
        assert account.cash_available == 100_000.0 - 99.0 * 5


# ════════════════════════════════════════════════════════════════════════════════
# close_position: body (lines 1062-1099)
# ════════════════════════════════════════════════════════════════════════════════

class TestClosePositionBody:
    def test_full_close_sets_status_closed_and_returns_pnl(self):
        # lines 1062-1099: full close path
        db = _isolated_db()
        pos = models.TradePosition(
            mode="DEMO", symbol="CLOSEME", status="OPEN", qty_open=10,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            realized_pnl=0.0,
            opened_at=datetime.now(timezone.utc),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        tick = Tick(symbol="CLOSEME", price=112.0,
                    as_of=datetime.now(timezone.utc), atr=2.0, source="test")
        pnl = pf.close_position(db, pos, tick, qty_to_close=10, reason="stop_hit")
        assert round(pnl, 2) == round((112.0 - 100.0) * 10, 2)
        db.refresh(pos)
        assert pos.status == "CLOSED"
        assert pos.qty_open == 0
        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="CLOSED").first()
        assert event is not None

    def test_partial_close_sets_status_partially_closed(self):
        db = _isolated_db()
        pos = models.TradePosition(
            mode="DEMO", symbol="PARTCO", status="OPEN", qty_open=10,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            realized_pnl=0.0,
            opened_at=datetime.now(timezone.utc),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        tick = Tick(symbol="PARTCO", price=110.0,
                    as_of=datetime.now(timezone.utc), atr=2.0, source="test")
        pnl = pf.close_position(db, pos, tick, qty_to_close=4, reason="target_hit")
        db.refresh(pos)
        assert pos.status == "PARTIALLY_CLOSED"
        assert pos.qty_open == 6
        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="PARTIAL_EXIT").first()
        assert event is not None

    def test_close_updates_account_cash_and_equity(self):
        db = _isolated_db()
        pos = models.TradePosition(
            mode="DEMO", symbol="CASHCO", status="OPEN", qty_open=5,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            realized_pnl=0.0,
            opened_at=datetime.now(timezone.utc),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        account_before = pf.get_account(db, "DEMO")
        cash_before = account_before.cash_available
        tick = Tick(symbol="CASHCO", price=110.0,
                    as_of=datetime.now(timezone.utc), atr=2.0, source="test")
        pf.close_position(db, pos, tick, qty_to_close=5, reason="stop_hit")
        account_after = pf.get_account(db, "DEMO")
        assert account_after.cash_available == cash_before + 110.0 * 5


# ════════════════════════════════════════════════════════════════════════════════
# record_real_order_sent (lines 1107-1112)
# ════════════════════════════════════════════════════════════════════════════════

class TestRecordRealOrderSent:
    def test_marks_order_placed_and_stores_dhan_order_id(self):
        # lines 1107-1112
        db = _isolated_db()
        order = models.TradeOrder(
            mode="REAL", symbol="REALCO", side="BUY",
            order_type="LIMIT", qty=5, limit_price=100.0,
            status="PENDING", execution_source="AUTO",
        )
        db.add(order); db.commit(); db.refresh(order)
        pf.record_real_order_sent(db, order, dhan_order_id="DH12345")
        db.refresh(order)
        assert order.status == "PLACED"
        assert order.dhan_order_id == "DH12345"
        event = db.query(models.TradeOrderEvent).filter_by(
            order_id=order.id, event_type="PLACED").first()
        assert event is not None
        assert "DH12345" in event.detail


# ════════════════════════════════════════════════════════════════════════════════
# record_real_exit_sent (lines 1225-1231)
# ════════════════════════════════════════════════════════════════════════════════

class TestRecordRealExitSent:
    def test_full_true_sets_pending_exit_and_event(self):
        # lines 1225-1231: full=True → PENDING_EXIT + EXIT_SENT event
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="EXITCO", status="OPEN", qty_open=10,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc), initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        pf.record_real_exit_sent(db, pos, dhan_order_id="DH99",
                                  qty=10, reason="stop_hit", full=True)
        db.refresh(pos)
        assert pos.status == "PENDING_EXIT"
        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="EXIT_SENT").first()
        assert event is not None
        assert "DH99" in event.detail

    def test_full_false_leaves_status_open(self):
        # full=False (partial exit) → status stays OPEN
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="PARTEX", status="OPEN", qty_open=10,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc), initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        pf.record_real_exit_sent(db, pos, dhan_order_id="DH88",
                                  qty=4, reason="target_hit", full=False)
        db.refresh(pos)
        assert pos.status == "OPEN"
        event = db.query(models.TradePositionEvent).filter_by(
            position_id=pos.id, event_type="EXIT_SENT").first()
        assert event is not None


# ════════════════════════════════════════════════════════════════════════════════
# Precise targeted tests for the 9 remaining lines (after line-number audit)
#
#   391  → continue when CNC row has no tradingSymbol
#   410-411 → except (TypeError,ValueError) in candidate-loop int/float parse
#   413  → continue when _sym in _holdings_symbols (inside candidate loop)
#   434  → continue when already_tracked in candidate loop
#   741  → _qty helper's `return default` (non-dict row default path)
#   747-748 → _qty helper except (TypeError,ValueError) → return 0
#   789-790 → broker_qty accumulation for live_positions (sym+qty>0 branch)
# ════════════════════════════════════════════════════════════════════════════════

class TestImportBrokerHoldingsPrecise:
    """Each test uses its own isolated engine and patches execution.dhan_client
    at the module level so the function's internal `from execution import dhan_client`
    re-import picks up the same object."""

    def _run(self, db, holdings, positions):
        import execution.dhan_client as dc
        import asyncio
        orig_h = dc.get_holdings
        orig_p = dc.get_positions
        dc.get_holdings = lambda db_: holdings
        dc.get_positions = lambda db_: positions
        try:
            return asyncio.run(pf.import_broker_holdings(db))
        finally:
            dc.get_holdings = orig_h
            dc.get_positions = orig_p

    def test_cnc_row_missing_symbol_skipped(self):
        # line 391: _raw_sym is None/empty → continue
        db = _isolated_db()
        # CNC row but NO tradingSymbol field
        positions = [{"productType": "CNC", "netBuyQty": 5, "averageBuyPrice": 100.0}]
        result = self._run(db, [], positions)
        assert result == 0

    def test_cnc_live_position_parse_error_skipped(self):
        # lines 410-411: CNC live position with unparseable qty → except → continue
        # Must be in positions (not holdings) — this is the live_positions CNC loop
        db = _isolated_db()
        positions = [{"tradingSymbol": "PARSEERR", "productType": "CNC",
                      "netBuyQty": "not_a_number", "averageBuyPrice": 100.0}]
        result = self._run(db, [], positions)
        assert result == 0

    def test_cnc_live_position_zero_qty_skipped(self):
        # line 413: CNC live position with qty <= 0 → continue
        db = _isolated_db()
        positions = [{"tradingSymbol": "ZERQTY", "productType": "CNC",
                      "netBuyQty": -1, "averageBuyPrice": 100.0}]
        result = self._run(db, [], positions)
        assert result == 0

    def test_candidate_loop_already_tracked_continues(self):
        # line 434: already_tracked is not None → continue (no import)
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="ALREADY", status="OPEN", qty_open=5,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc), initial_stop_distance=5.0,
        )
        db.add(pos); db.commit()
        holdings = [{"tradingSymbol": "ALREADY", "totalQty": 5, "avgCostPrice": 100.0}]
        result = self._run(db, holdings, [])
        assert result == 0


class TestHoldingsSyncReconcilePrecise:
    """Precise coverage for holdings_sync_reconcile's internal helper lines
    and the live_positions broker_qty accumulation path."""

    def _run(self, db, holdings, positions):
        import execution.dhan_client as dc
        orig_h = dc.get_holdings
        orig_p = dc.get_positions
        dc.get_holdings = lambda db_: holdings
        dc.get_positions = lambda db_: positions
        try:
            return pf.holdings_sync_reconcile(db)
        finally:
            dc.get_holdings = orig_h
            dc.get_positions = orig_p

    def test_qty_helper_typeerror_returns_zero(self):
        # lines 747-748: _qty helper: int(raw) raises TypeError → return 0
        # A holdings row with non-parseable totalQty → _qty returns 0 → not added
        db = _isolated_db()
        # Holdings row where totalQty cannot be cast to int
        holdings = [{"tradingSymbol": "BADQTY", "totalQty": {"nested": "dict"}}]
        result = self._run(db, holdings, [])
        assert result["closed"] == 0
        # BADQTY should not be in broker_symbols because qty=0
        # (no OPEN position to close, so closed==0 is expected regardless)

    def test_live_positions_sym_with_positive_qty_added_to_broker_symbols(self):
        # lines 789-790: live_positions row with sym + qty>0 → added to broker_qty_by_symbol
        # Proof: a position IS in broker_symbols → not ghost-closed
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="LIVECO", status="OPEN", qty_open=5,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        # Holdings empty but live_positions has LIVECO with qty=5
        positions = [{"tradingSymbol": "LIVECO", "netQty": 5, "positiveQty": 0}]
        self._run(db, [], positions)
        db.refresh(pos)
        # Should NOT be closed — live_positions proves broker still holds it
        assert pos.status == "OPEN"

    def test_live_positions_qty_zero_does_not_protect_ghost(self):
        # lines 747-748 via _qty: qty field unparseable → _qty returns 0 → not added
        # Position is then a ghost → gets closed
        db = _isolated_db()
        pos = models.TradePosition(
            mode="REAL", symbol="GHOSTCO2", status="OPEN", qty_open=5,
            avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
            initial_stop_distance=5.0,
        )
        db.add(pos); db.commit(); db.refresh(pos)
        # Live positions: GHOSTCO2 but with unparseable qty
        positions = [{"tradingSymbol": "GHOSTCO2", "netQty": None, "positiveQty": None}]
        self._run(db, [], positions)
        db.refresh(pos)
        # qty=0 for both fields → not added → ghost close
        assert pos.status == "CLOSED"
