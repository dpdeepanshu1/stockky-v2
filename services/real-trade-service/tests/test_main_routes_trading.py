"""
main.py coverage, part 2 of 2 — manual order ticket (preview/confirm),
manual candidate injection, /cycle/run, after-hours manual scan, pipeline
status, watchlist-entries read, the _live_prices/_self_heal_orders
helpers, positions/orders/candidates listing, manual close/cancel,
reconcile + holdings-sync, and the read-only live Dhan account routes.

Companion to tests/test_main_routes_core.py (part 1: startup/shutdown,
gates, auth, Dhan connect, risk config, arm/disarm, risk-engine dry run,
feature/autopilot toggles, adaptive status, audit log, resilience).
Same approach throughout: call the `async def` route functions directly
(bypassing FastAPI's HTTP layer / Depends resolution), passing
dependencies as ordinary keyword arguments, against a shared in-memory
sqlite engine reset per test via `_fresh_db()`. Functions imported LOCALLY
inside a route body (manual_engine.evaluate_manual_order,
cycle_runner.run_cycle_core, execution.reconcile.reconcile_real_orders,
portfolio.portfolio.holdings_sync_reconcile, exit_engine.exit._send_real_sell
/ _has_pending_real_sell, entry_engine.entry.check_pending_fills /
expire_stale_orders, market_feed.feed.get_quotes,
execution.auto_pilot.run_afterhours_scan_manual_sync) are mocked at their
SOURCE module — a local `from x import y` picks up the patched attribute
at call time. Per-mode threading.Locks (execution.auto_pilot._get_lock /
_get_exit_lock) are real, process-wide singletons keyed by mode name — a
"lock busy" scenario is simulated by acquiring the real lock in the test
itself before calling the route, not by mocking the lock.

Run from services/real-trade-service:
    python -m pytest tests/test_main_routes_trading.py -q --cov=main --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

import config
import models
import db as db_module
import main
from execution.auto_pilot import _get_lock, _get_exit_lock

# ── shared DB engine (StaticPool so :memory: survives across sessions) ─────
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_SessionFactory = sessionmaker(bind=_engine)


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-session-secret-not-for-prod")
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")


def _fresh_db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    return _SessionFactory()


def _seed(db, real_ready: bool = False):
    demo_gate = models.TradeGateState(
        mode="DEMO", admin_authenticated=True,
        admin_authenticated_at=datetime.now(timezone.utc),
        risk_config_confirmed=True,
        risk_config_confirmed_at=datetime.now(timezone.utc),
        armed=True,
    )
    real_gate = models.TradeGateState(mode="REAL")
    if real_ready:
        real_gate.admin_authenticated = True
        real_gate.admin_authenticated_at = datetime.now(timezone.utc)
        real_gate.admin_session_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        real_gate.dhan_connected = True
        real_gate.dhan_connected_at = datetime.now(timezone.utc)
        real_gate.risk_config_confirmed = True
        real_gate.risk_config_confirmed_at = datetime.now(timezone.utc)
        real_gate.armed = True
    db.add(demo_gate)
    db.add(real_gate)
    for m, cap in (("DEMO", 100_000.0), ("REAL", 0.0)):
        db.add(models.TradeRiskConfig(mode=m))
        db.add(models.TradeAccount(mode=m, starting_capital=cap, current_equity=cap, cash_available=cap))
    db.commit()


def _run(coro):
    return asyncio.run(coro)


def _admin_bearer() -> str:
    from auth.admin_auth import issue_session_token
    token, _ = issue_session_token(config.ADMIN_USERNAME)
    return f"Bearer {token}"


def _expect_http_error(exc_info, status_code: int):
    assert exc_info.value.status_code == status_code


def _open_position(db, mode="DEMO", symbol="RELIANCE", qty_open=10, avg_entry_price=100.0, **kw):
    p = models.TradePosition(
        mode=mode, symbol=symbol, status=kw.pop("status", "OPEN"),
        qty_open=qty_open, avg_entry_price=avg_entry_price, **kw,
    )
    db.add(p)
    db.commit()
    return p


# ══════════════════════════════════════════════════════════════════════════
# /manual-order/{mode}/preview, /manual-order/{mode}/confirm
# ══════════════════════════════════════════════════════════════════════════
class TestManualOrderPreview:
    def _body(self, **kw):
        d = dict(symbol="reliance", side="BUY", qty=1)
        d.update(kw)
        return main.ManualOrderRequest(**d)

    def test_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_order_preview("SWING", self._body(), admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_success_calls_evaluate_manual_order_confirm_false(self):
        db = _fresh_db()
        _seed(db)
        captured = {}

        async def _fake_eval(db_, mode, armed, body, *, confirm, admin=None):
            captured.update(mode=mode, armed=armed, confirm=confirm)
            return {"ok": True, "reason": "preview"}

        with mock.patch("manual_engine.evaluate_manual_order", side_effect=_fake_eval):
            result = _run(main.manual_order_preview("demo", self._body(), admin=None, db=db))
        assert result == {"ok": True, "reason": "preview"}
        assert captured == {"mode": "DEMO", "armed": True, "confirm": False}


class TestManualOrderConfirm:
    def _body(self, side="BUY", **kw):
        d = dict(symbol="reliance", side=side, qty=1)
        d.update(kw)
        return main.ManualOrderRequest(**d)

    def test_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_order_confirm("SWING", self._body(), admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_buy_not_armed_409(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.armed = False
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_order_confirm("demo", self._body(side="BUY"), admin=None, db=db))
        _expect_http_error(ei, 409)
        assert "not armed" in ei.value.detail

    def test_sell_not_armed_is_allowed(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.armed = False
        db.commit()

        async def _fake_eval(db_, mode, armed, body, *, confirm, admin=None):
            return {"ok": True}

        with mock.patch("manual_engine.evaluate_manual_order", side_effect=_fake_eval):
            result = _run(main.manual_order_confirm("demo", self._body(side="SELL"), admin=None, db=db))
        assert result == {"ok": True}

    def test_buy_entry_lock_busy_409(self):
        db = _fresh_db()
        _seed(db)
        lock = _get_lock("DEMO")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_order_confirm("demo", self._body(side="BUY"), admin=None, db=db))
            _expect_http_error(ei, 409)
            assert "cycle for DEMO is already in progress" in ei.value.detail
        finally:
            lock.release()

    def test_sell_exit_lock_busy_409(self):
        db = _fresh_db()
        _seed(db)
        lock = _get_exit_lock("DEMO")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_order_confirm("demo", self._body(side="SELL"), admin=None, db=db))
            _expect_http_error(ei, 409)
            assert "exit for DEMO is already being processed" in ei.value.detail
        finally:
            lock.release()

    def test_success_runs_on_worker_thread_and_releases_lock(self):
        db = _fresh_db()
        _seed(db)
        captured = {}

        async def _fake_eval(db_, mode, armed, body, *, confirm, admin=None):
            captured.update(mode=mode, confirm=confirm)
            return {"ok": True, "order_id": 5}

        with mock.patch("manual_engine.evaluate_manual_order", side_effect=_fake_eval):
            result = _run(main.manual_order_confirm("demo", self._body(side="BUY"), admin=None, db=db))
        assert result == {"ok": True, "order_id": 5}
        assert captured == {"mode": "DEMO", "confirm": True}
        # lock must be released afterward — a fresh non-blocking acquire succeeds
        assert _get_lock("DEMO").acquire(blocking=False) is True
        _get_lock("DEMO").release()


# ══════════════════════════════════════════════════════════════════════════
# /candidates/manual/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestManualCandidateRoute:
    def _body(self, **kw):
        d = dict(symbol="reliance.ns")
        d.update(kw)
        return main.ManualCandidateRequest(**d)

    def test_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.add_manual_candidate("SWING", self._body(), admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_not_armed_409(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.armed = False
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _run(main.add_manual_candidate("demo", self._body(), admin=None, db=db))
        _expect_http_error(ei, 409)

    def test_empty_symbol_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.add_manual_candidate("demo", self._body(symbol="  "), admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_success_normalises_symbol_and_queues(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.add_manual_candidate(
            "demo", self._body(symbol="reliance.ns", decision_label="BUY NOW", conviction_score=8.5),
            admin=None, db=db,
        ))
        assert result == {"ok": True, "mode": "DEMO", "symbol": "RELIANCE", "queued": True}
        row = db.query(models.TradeCandidate).filter_by(mode="DEMO", symbol="RELIANCE").first()
        assert row is not None
        assert row.source_tab == "market_scan"
        assert row.decision_label == "BUY NOW"
        assert db.query(models.TradeAuditLog).filter_by(action="MANUAL_CANDIDATE").count() == 1


# ══════════════════════════════════════════════════════════════════════════
# /cycle/run/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestRunCycle:
    def test_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.run_cycle("SWING", admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_not_armed_409(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.armed = False
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _run(main.run_cycle("demo", admin=None, db=db))
        _expect_http_error(ei, 409)

    def test_lock_busy_409(self):
        db = _fresh_db()
        _seed(db)
        lock = _get_lock("DEMO")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.run_cycle("demo", admin=None, db=db))
            _expect_http_error(ei, 409)
        finally:
            lock.release()

    def test_success_runs_cycle_and_releases_lock(self):
        db = _fresh_db()
        _seed(db)
        captured = {}

        async def _fake_run(db_, mode, armed, trigger="manual"):
            captured.update(mode=mode, armed=armed, trigger=trigger)
            return {"ok": True, "entries": 0}

        with mock.patch("cycle_runner.run_cycle_core", side_effect=_fake_run):
            result = _run(main.run_cycle("demo", admin=None, db=db))
        assert result == {"ok": True, "entries": 0}
        assert captured == {"mode": "DEMO", "armed": True, "trigger": "manual"}
        assert _get_lock("DEMO").acquire(blocking=False) is True
        _get_lock("DEMO").release()


# ══════════════════════════════════════════════════════════════════════════
# /afterhours/run-manual/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestAfterhoursRunManual:
    def test_invalid_mode_400(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.run_afterhours_scan_manual("SWING", admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_missing_gate_row_404(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.run_afterhours_scan_manual("demo", admin=None, db=db))
        _expect_http_error(ei, 404)

    def test_already_in_progress_409(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch("execution.auto_pilot.run_afterhours_scan_manual_sync",
                        return_value={"reason": "already_in_progress"}):
            with pytest.raises(HTTPException) as ei:
                _run(main.run_afterhours_scan_manual("demo", admin=None, db=db))
            _expect_http_error(ei, 409)

    def test_success_logs_and_returns_result(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch("execution.auto_pilot.run_afterhours_scan_manual_sync",
                        return_value={"ran": True, "written": 3, "market_date": "2026-09-23"}):
            result = _run(main.run_afterhours_scan_manual("demo", admin=None, db=db))
        assert result["ok"] is True
        assert result["written"] == 3
        assert db.query(models.TradeAuditLog).filter_by(action="AFTERHOURS_SCAN_MANUAL").count() == 1


# ══════════════════════════════════════════════════════════════════════════
# /pipeline/status/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestPipelineStatusRoute:
    def test_invalid_mode_400(self):
        with pytest.raises(HTTPException) as ei:
            _run(main.pipeline_status_route("SWING", admin=None))
        _expect_http_error(ei, 400)

    def test_success_passthrough(self):
        with mock.patch("pipeline_status.get_status", return_value={"stage": "idle"}) as gs:
            result = _run(main.pipeline_status_route("demo", admin=None))
        gs.assert_called_once_with("DEMO")
        assert result == {"stage": "idle"}


# ══════════════════════════════════════════════════════════════════════════
# /watchlist-entries/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestWatchlistEntriesRoute:
    def _row(self, db, mode="DEMO", symbol="AAA", status="active", **kw):
        row = models.WatchlistEntry(
            mode=mode, symbol=symbol,
            catalyst_type=kw.pop("catalyst_type", "bulk_block"),
            catalyst_price=kw.pop("catalyst_price", 100.0),
            horizon_class=kw.pop("horizon_class", "short"),
            decay_half_life_days=kw.pop("decay_half_life_days", 3.0),
            entry_band_pct=kw.pop("entry_band_pct", 2.0),
            source_tier=kw.pop("source_tier", 1),
            status=status,
            expires_at=kw.pop("expires_at", datetime.now(timezone.utc) + timedelta(days=1)),
            **kw,
        )
        db.add(row)
        db.commit()
        return row

    def test_invalid_mode_400(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.watchlist_entries_route("SWING", db=db))
        _expect_http_error(ei, 400)

    def test_limit_is_clamped(self):
        db = _fresh_db()
        for i in range(3):
            self._row(db, symbol=f"SYM{i}")
        rows_low = _run(main.watchlist_entries_route("demo", limit=0, db=db))
        rows_high = _run(main.watchlist_entries_route("demo", limit=99999, db=db))
        assert rows_low["count"] == 1
        assert rows_high["count"] == 3

    def test_status_filter(self):
        db = _fresh_db()
        self._row(db, symbol="AAA", status="active")
        self._row(db, symbol="BBB", status="missed", missed_reason="ran too far")
        result = _run(main.watchlist_entries_route("demo", status="missed", db=db))
        assert result["count"] == 1
        assert result["entries"][0]["symbol"] == "BBB"
        assert result["entries"][0]["missed_reason"] == "ran too far"

    def test_success_shape(self):
        db = _fresh_db()
        self._row(db, symbol="AAA", conviction_score=7.5)
        result = _run(main.watchlist_entries_route("demo", db=db))
        assert result["mode"] == "DEMO"
        entry = result["entries"][0]
        assert entry["symbol"] == "AAA"
        assert entry["catalyst_ts"] is not None
        assert entry["expires_at"] is not None
        assert entry["conviction_score"] == 7.5


# ══════════════════════════════════════════════════════════════════════════
# _live_prices helper
# ══════════════════════════════════════════════════════════════════════════
class TestLivePrices:
    def test_empty_symbols_returns_empty(self):
        result = _run(main._live_prices([]))
        assert result == {}

    def test_success_returns_price_dict(self):
        tick_a = mock.Mock(price=101.5)
        tick_b = mock.Mock(price=None)  # a None tick means "no quote" — must be filtered out

        async def _fake_get_quotes(symbols):
            return {"AAA": tick_a, "BBB": None}

        with mock.patch("market_feed.feed.get_quotes", side_effect=_fake_get_quotes):
            result = _run(main._live_prices(["AAA", "AAA", "BBB"]))  # de-dupe check
        assert result == {"AAA": 101.5}

    def test_exception_is_swallowed_returns_empty(self):
        with mock.patch("market_feed.feed.get_quotes", side_effect=RuntimeError("feed down")):
            result = _run(main._live_prices(["AAA"]))
        assert result == {}


# ══════════════════════════════════════════════════════════════════════════
# _self_heal_orders helper
# ══════════════════════════════════════════════════════════════════════════
class TestSelfHealOrders:
    def test_demo_lock_acquired_runs_checks(self):
        db = _fresh_db()
        calls = []

        async def _fake_check_fills(db_, mode):
            calls.append(("check_pending_fills", mode))

        async def _fake_expire(db_, mode):
            calls.append(("expire_stale_orders", mode))

        with mock.patch("entry_engine.entry.check_pending_fills", side_effect=_fake_check_fills), \
             mock.patch("entry_engine.entry.expire_stale_orders", side_effect=_fake_expire):
            _run(main._self_heal_orders(db, "DEMO"))
        assert calls == [("check_pending_fills", "DEMO"), ("expire_stale_orders", "DEMO")]
        # lock released afterward
        assert _get_lock("DEMO").acquire(blocking=False) is True
        _get_lock("DEMO").release()

    def test_demo_lock_busy_skips_without_raising(self):
        db = _fresh_db()
        lock = _get_lock("DEMO")
        lock.acquire()
        try:
            with mock.patch("entry_engine.entry.check_pending_fills") as cf, \
                 mock.patch("entry_engine.entry.expire_stale_orders") as es:
                _run(main._self_heal_orders(db, "DEMO"))  # must not raise
            cf.assert_not_called()
            es.assert_not_called()
        finally:
            lock.release()

    def test_real_reconcile_due_runs_and_marks(self):
        db = _fresh_db()

        async def _fake_reconcile(db_):
            return {"reconciled": 1}

        with mock.patch("execution.auto_pilot._reconcile_due", return_value=True), \
             mock.patch("execution.auto_pilot._mark_reconciled") as mark, \
             mock.patch("execution.reconcile.reconcile_real_orders", side_effect=_fake_reconcile) as rec:
            _run(main._self_heal_orders(db, "REAL"))
        rec.assert_called_once()
        mark.assert_called_once_with("REAL")
        assert _get_exit_lock("REAL").acquire(blocking=False) is True
        _get_exit_lock("REAL").release()

    def test_real_reconcile_not_due_skips_entirely(self):
        db = _fresh_db()
        with mock.patch("execution.auto_pilot._reconcile_due", return_value=False), \
             mock.patch("execution.reconcile.reconcile_real_orders") as rec:
            _run(main._self_heal_orders(db, "REAL"))
        rec.assert_not_called()

    def test_real_reconcile_due_but_lock_busy_skips(self):
        db = _fresh_db()
        lock = _get_exit_lock("REAL")
        lock.acquire()
        try:
            with mock.patch("execution.auto_pilot._reconcile_due", return_value=True), \
                 mock.patch("execution.reconcile.reconcile_real_orders") as rec:
                _run(main._self_heal_orders(db, "REAL"))  # must not raise
            rec.assert_not_called()
        finally:
            lock.release()

    def test_exception_is_swallowed(self):
        db = _fresh_db()
        with mock.patch("execution.auto_pilot._reconcile_due", side_effect=RuntimeError("boom")):
            _run(main._self_heal_orders(db, "REAL"))  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# GET /positions/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestListPositions:
    def test_pnl_and_distance_fields_computed_from_live_price(self):
        db = _fresh_db()
        _open_position(db, symbol="AAA", qty_open=10, avg_entry_price=100.0,
                        current_stop=95.0, current_target=110.0)
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={"AAA": 105.0})):
            result = _run(main.list_positions("demo", admin=None, db=db))
        assert len(result) == 1
        row = result[0]
        assert row["current_price"] == 105.0
        assert row["pnl_pct"] == pytest.approx(5.0)
        assert row["stop_distance_pct"] == round((105.0 - 95.0) / 105.0 * 100.0, 2)
        assert row["target_distance_pct"] == round((110.0 - 105.0) / 105.0 * 100.0, 2)
        assert row["unrealized_pnl"] == pytest.approx((105.0 - 100.0) * 10)

    def test_no_live_price_falls_back_to_db_unrealized_and_none_fields(self):
        db = _fresh_db()
        _open_position(db, symbol="BBB", qty_open=5, avg_entry_price=50.0, unrealized_pnl=12.5)
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_positions("demo", admin=None, db=db))
        row = result[0]
        assert row["current_price"] is None
        assert row["pnl_pct"] is None
        assert row["unrealized_pnl"] == 12.5  # fell back to stale DB value

    def test_includes_pending_exit_status(self):
        db = _fresh_db()
        _open_position(db, symbol="CCC", status="PENDING_EXIT")
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_positions("demo", admin=None, db=db))
        assert len(result) == 1
        assert result[0]["status"] == "PENDING_EXIT"

    def test_closed_position_excluded(self):
        db = _fresh_db()
        _open_position(db, symbol="DDD", status="CLOSED", qty_open=0)
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_positions("demo", admin=None, db=db))
        assert result == []


# ══════════════════════════════════════════════════════════════════════════
# GET /positions/{mode}/history
# ══════════════════════════════════════════════════════════════════════════
class TestListClosedPositions:
    def test_win_rate_and_net_pnl_total(self):
        db = _fresh_db()
        _open_position(db, symbol="AAA", status="CLOSED", realized_pnl=100.0,
                        net_realized_pnl=95.0, closed_at=datetime.now(timezone.utc))
        _open_position(db, symbol="BBB", status="CLOSED", realized_pnl=-40.0,
                        net_realized_pnl=None, closed_at=datetime.now(timezone.utc))
        result = _run(main.list_closed_positions("demo", limit=100, admin=None, db=db))
        assert result["total"] == 2
        assert result["wins"] == 1
        assert result["win_rate"] == 0.5
        assert result["net_realized_pnl_total"] == 95.0  # only the non-None row counted

    def test_no_closed_positions_win_rate_none(self):
        db = _fresh_db()
        result = _run(main.list_closed_positions("demo", limit=100, admin=None, db=db))
        assert result["total"] == 0
        assert result["win_rate"] is None
        assert result["net_realized_pnl_total"] is None

    def test_limit_is_clamped(self):
        db = _fresh_db()
        for i in range(3):
            _open_position(db, symbol=f"S{i}", status="CLOSED", closed_at=datetime.now(timezone.utc))
        result = _run(main.list_closed_positions("demo", limit=0, admin=None, db=db))
        assert len(result["positions"]) == 1


# ══════════════════════════════════════════════════════════════════════════
# GET /stats/regime-override
# ══════════════════════════════════════════════════════════════════════════
class TestRegimeOverrideStats:
    def test_defaults_to_real_mode(self):
        db = _fresh_db()
        result = _run(main.regime_override_stats(db=db))
        assert result["mode"] == "REAL"

    def test_counts_and_win_rate(self):
        db = _fresh_db()
        _open_position(db, mode="REAL", symbol="AAA", status="CLOSED", realized_pnl=50.0,
                        net_realized_pnl=45.0, is_regime_override=True,
                        closed_at=datetime.now(timezone.utc))
        _open_position(db, mode="REAL", symbol="BBB", status="OPEN", is_regime_override=True)
        _open_position(db, mode="REAL", symbol="CCC", status="CLOSED", realized_pnl=10.0,
                        is_regime_override=False, closed_at=datetime.now(timezone.utc))
        result = _run(main.regime_override_stats(mode="real", db=db))
        assert result["closed_count"] == 1
        assert result["open_count"] == 1
        assert result["wins"] == 1
        assert result["win_rate"] == 1.0
        assert result["realized_pnl_total"] == 50.0
        assert result["net_realized_pnl_total"] == 45.0
        assert len(result["recent"]) == 1

    def test_no_closed_override_positions_win_rate_none(self):
        db = _fresh_db()
        result = _run(main.regime_override_stats(mode="REAL", db=db))
        assert result["closed_count"] == 0
        assert result["win_rate"] is None
        assert result["realized_pnl_total"] is None


# ══════════════════════════════════════════════════════════════════════════
# GET /orders/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestListOrders:
    def _order(self, db, mode="DEMO", symbol="AAA", status="PLACED", **kw):
        o = models.TradeOrder(mode=mode, symbol=symbol, side=kw.pop("side", "BUY"),
                               qty=kw.pop("qty", 10), status=status, **kw)
        db.add(o)
        db.commit()
        return o

    def test_only_in_flight_orders_get_live_prices(self):
        db = _fresh_db()
        self._order(db, symbol="AAA", status="PLACED", limit_price=100.0)
        self._order(db, symbol="BBB", status="FILLED", limit_price=50.0)
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={"AAA": 102.0})) as lp:
            result = _run(main.list_orders("demo", days=2, admin=None, db=db))
        lp.assert_awaited_once_with(["AAA"])
        by_symbol = {r["symbol"]: r for r in result}
        assert by_symbol["AAA"]["current_price"] == 102.0
        assert by_symbol["AAA"]["limit_distance_pct"] == pytest.approx(2.0)
        assert by_symbol["BBB"]["current_price"] is None
        assert by_symbol["BBB"]["limit_distance_pct"] is None

    def test_old_orders_excluded_by_days_window(self):
        db = _fresh_db()
        old = self._order(db, symbol="OLD", status="FILLED")
        old.created_at = datetime.now(timezone.utc) - timedelta(days=10)
        db.commit()
        self._order(db, symbol="NEW", status="FILLED")
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_orders("demo", days=2, admin=None, db=db))
        symbols = {r["symbol"] for r in result}
        assert symbols == {"NEW"}

    def test_days_clamped_to_valid_range(self):
        db = _fresh_db()
        self._order(db, symbol="AAA", status="FILLED")
        with mock.patch.object(main, "_self_heal_orders", mock.AsyncMock(return_value=None)), \
             mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result_low = _run(main.list_orders("demo", days=0, admin=None, db=db))
            result_high = _run(main.list_orders("demo", days=9999, admin=None, db=db))
        assert len(result_low) == 1
        assert len(result_high) == 1


# ══════════════════════════════════════════════════════════════════════════
# GET /candidates/{mode}
# ══════════════════════════════════════════════════════════════════════════
class TestListCandidates:
    def test_invalid_mode_400(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.list_candidates("SWING", admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_dedupe_keeps_newest_and_counts_fetches(self):
        db = _fresh_db()
        older = models.TradeCandidate(mode="DEMO", symbol="AAA", source_tab="hot_picks")
        db.add(older)
        db.commit()
        newer = models.TradeCandidate(mode="DEMO", symbol="AAA", source_tab="hot_picks")
        db.add(newer)
        db.commit()
        with mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_candidates("demo", admin=None, db=db))
        assert len(result) == 1
        assert result[0]["id"] == newer.id
        assert result[0]["fetch_count"] == 2

    def test_decision_keyed_by_candidate_id_not_symbol(self):
        """Regression for the 2026-09-03 bug: a second, unrelated candidate
        row for the same symbol must never borrow the first candidate's
        decision."""
        db = _fresh_db()
        cand1 = models.TradeCandidate(mode="DEMO", symbol="AAA")
        db.add(cand1)
        db.commit()
        db.add(models.TradeDecision(
            mode="DEMO", candidate_id=cand1.id, symbol="AAA",
            decision_type="ENTRY", action="WAIT", reasoning="stale reasoning",
        ))
        db.commit()
        # cand1 goes stale/consumed; a fresh candidate row for AAA appears with
        # no decision of its own yet.
        cand2 = models.TradeCandidate(mode="DEMO", symbol="AAA")
        db.add(cand2)
        db.commit()
        with mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={})):
            result = _run(main.list_candidates("demo", admin=None, db=db))
        newest = next(r for r in result if r["id"] == cand2.id)
        assert newest["latest_decision"] is None  # must NOT show cand1's stale reasoning

    def test_limit_distance_pct_only_for_wait_action_with_proposed_price(self):
        db = _fresh_db()
        cand = models.TradeCandidate(mode="DEMO", symbol="AAA")
        db.add(cand)
        db.commit()
        db.add(models.TradeDecision(
            mode="DEMO", candidate_id=cand.id, symbol="AAA",
            decision_type="ENTRY", action="WAIT", proposed_price=100.0,
        ))
        db.commit()
        with mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={"AAA": 102.0})):
            result = _run(main.list_candidates("demo", admin=None, db=db))
        assert result[0]["latest_decision"]["limit_distance_pct"] == pytest.approx(2.0)

    def test_enter_action_never_gets_limit_distance(self):
        db = _fresh_db()
        cand = models.TradeCandidate(mode="DEMO", symbol="AAA")
        db.add(cand)
        db.commit()
        db.add(models.TradeDecision(
            mode="DEMO", candidate_id=cand.id, symbol="AAA",
            decision_type="ENTRY", action="ENTER", proposed_price=100.0,
        ))
        db.commit()
        with mock.patch.object(main, "_live_prices", mock.AsyncMock(return_value={"AAA": 102.0})):
            result = _run(main.list_candidates("demo", admin=None, db=db))
        assert result[0]["latest_decision"]["limit_distance_pct"] is None


# ══════════════════════════════════════════════════════════════════════════
# POST /positions/{mode}/{position_id}/close
# ══════════════════════════════════════════════════════════════════════════
class TestManualClosePosition:
    def test_exit_lock_busy_409(self):
        db = _fresh_db()
        p = _open_position(db)
        lock = _get_exit_lock("DEMO")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_close_position("demo", p.id, admin=None, db=db))
            _expect_http_error(ei, 409)
        finally:
            lock.release()

    def test_position_not_found_404(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_close_position("demo", 999, admin=None, db=db))
        _expect_http_error(ei, 404)

    def test_wrong_status_409(self):
        db = _fresh_db()
        p = _open_position(db, status="CLOSED")
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_close_position("demo", p.id, admin=None, db=db))
        _expect_http_error(ei, 409)

    def test_zero_qty_open_400(self):
        db = _fresh_db()
        p = _open_position(db, qty_open=0)
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_close_position("demo", p.id, admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_demo_no_price_available_503(self):
        db = _fresh_db()
        p = _open_position(db, symbol="AAA")

        async def _fake_get_quotes(symbols):
            return {"AAA": None}

        with mock.patch("market_feed.feed.get_quotes", side_effect=_fake_get_quotes):
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_close_position("demo", p.id, admin=None, db=db))
            _expect_http_error(ei, 503)

    def test_demo_success(self):
        db = _fresh_db()
        p = _open_position(db, symbol="AAA", qty_open=10)
        tick = mock.Mock(price=105.0)

        async def _fake_get_quotes(symbols):
            return {"AAA": tick}

        with mock.patch("market_feed.feed.get_quotes", side_effect=_fake_get_quotes), \
             mock.patch.object(main, "_pf_close_position", return_value=50.0) as close_fn:
            result = _run(main.manual_close_position("demo", p.id, admin=None, db=db))
        close_fn.assert_called_once_with(db, p, tick, 10, "manual_close")
        assert result == {"ok": True, "mode": "DEMO", "symbol": "AAA", "qty_closed": 10, "pnl": 50.0}
        assert db.query(models.TradeAuditLog).filter_by(action="MANUAL_CLOSE").count() == 1

    def test_demo_partial_qty_close(self):
        db = _fresh_db()
        p = _open_position(db, symbol="AAA", qty_open=10)
        tick = mock.Mock(price=105.0)

        async def _fake_get_quotes(symbols):
            return {"AAA": tick}

        with mock.patch("market_feed.feed.get_quotes", side_effect=_fake_get_quotes), \
             mock.patch.object(main, "_pf_close_position", return_value=20.0) as close_fn:
            result = _run(main.manual_close_position(
                "demo", p.id, body=main.ManualCloseRequest(qty=4), admin=None, db=db,
            ))
        close_fn.assert_called_once_with(db, p, tick, 4, "manual_close")
        assert result["qty_closed"] == 4

    def test_real_pending_sell_409(self):
        db = _fresh_db()
        p = _open_position(db, mode="REAL", symbol="AAA")
        with mock.patch("exit_engine.exit._has_pending_real_sell", return_value=True):
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_close_position("real", p.id, admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_real_dhan_reject_502(self):
        db = _fresh_db()
        p = _open_position(db, mode="REAL", symbol="AAA", qty_open=10)
        with mock.patch("exit_engine.exit._has_pending_real_sell", return_value=False), \
             mock.patch("exit_engine.exit._send_real_sell", return_value=False):
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_close_position("real", p.id, admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_real_success_sent(self):
        db = _fresh_db()
        p = _open_position(db, mode="REAL", symbol="AAA", qty_open=10)
        with mock.patch("exit_engine.exit._has_pending_real_sell", return_value=False), \
             mock.patch("exit_engine.exit._send_real_sell", return_value=True) as send_fn:
            result = _run(main.manual_close_position("real", p.id, admin="admin", db=db))
        assert result == {
            "ok": True, "mode": "REAL", "symbol": "AAA", "qty_sent": 10,
            "status": "pending_broker_confirmation",
        }
        send_fn.assert_called_once_with(db, p, 10, "manual_close", full=True)
        assert db.query(models.TradeAuditLog).filter_by(action="MANUAL_CLOSE_SENT").count() == 1


# ══════════════════════════════════════════════════════════════════════════
# POST /orders/{mode}/{order_id}/cancel
# ══════════════════════════════════════════════════════════════════════════
class TestManualCancelOrder:
    def _order(self, db, mode="DEMO", status="PLACED", **kw):
        o = models.TradeOrder(mode=mode, symbol=kw.pop("symbol", "AAA"), side=kw.pop("side", "BUY"),
                               qty=kw.pop("qty", 10), status=status, **kw)
        db.add(o)
        db.commit()
        return o

    def test_entry_lock_busy_409(self):
        db = _fresh_db()
        o = self._order(db)
        lock = _get_lock("DEMO")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_cancel_order("demo", o.id, admin=None, db=db))
            _expect_http_error(ei, 409)
        finally:
            lock.release()

    def test_exit_lock_busy_releases_entry_lock_409(self):
        db = _fresh_db()
        o = self._order(db)
        exit_lock = _get_exit_lock("DEMO")
        exit_lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_cancel_order("demo", o.id, admin=None, db=db))
            _expect_http_error(ei, 409)
        finally:
            exit_lock.release()
        # entry lock must have been released on the way out
        assert _get_lock("DEMO").acquire(blocking=False) is True
        _get_lock("DEMO").release()

    def test_order_not_found_404(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_cancel_order("demo", 999, admin=None, db=db))
        _expect_http_error(ei, 404)

    def test_wrong_status_409(self):
        db = _fresh_db()
        o = self._order(db, status="FILLED")
        with pytest.raises(HTTPException) as ei:
            _run(main.manual_cancel_order("demo", o.id, admin=None, db=db))
        _expect_http_error(ei, 409)

    def test_demo_success_no_dhan_call(self):
        db = _fresh_db()
        o = self._order(db, status="PLACED")
        with mock.patch("execution.dhan_client.cancel_order") as cancel_fn:
            result = _run(main.manual_cancel_order("demo", o.id, admin=None, db=db))
        cancel_fn.assert_not_called()
        assert result == {"ok": True, "mode": "DEMO", "order_id": o.id, "status": "CANCELLED"}
        db.expire_all()
        assert db.query(models.TradeOrder).get(o.id).status == "CANCELLED"
        assert db.query(models.TradeAuditLog).filter_by(action="MANUAL_CANCEL").count() == 1
        assert db.query(models.TradeOrderEvent).filter_by(order_id=o.id, event_type="CANCELLED").count() == 1

    def test_real_dhan_reject_502(self):
        db = _fresh_db()
        o = self._order(db, mode="REAL", status="PLACED", dhan_order_id="DH123")
        with mock.patch("execution.dhan_client.cancel_order", side_effect=RuntimeError("dhan down")):
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_cancel_order("real", o.id, admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_real_success_calls_dhan(self):
        db = _fresh_db()
        o = self._order(db, mode="REAL", status="PARTIAL", dhan_order_id="DH123")
        with mock.patch("execution.dhan_client.cancel_order", return_value={"ok": True}) as cancel_fn:
            result = _run(main.manual_cancel_order("real", o.id, admin="admin", db=db))
        cancel_fn.assert_called_once_with(db, is_armed=True, dhan_order_id="DH123")
        assert result["status"] == "CANCELLED"


# ══════════════════════════════════════════════════════════════════════════
# POST /reconcile/{mode}, POST /reconcile/{mode}/holdings-sync
# ══════════════════════════════════════════════════════════════════════════
class TestManualReconcile:
    def test_demo_is_a_noop_note(self):
        db = _fresh_db()
        result = _run(main.manual_reconcile("demo", admin=None, db=db))
        assert result["ok"] is True
        assert "nothing to reconcile" in result["note"]

    def test_real_lock_busy_409(self):
        db = _fresh_db()
        lock = _get_exit_lock("REAL")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_reconcile("real", admin="admin", db=db))
            _expect_http_error(ei, 409)
        finally:
            lock.release()

    def test_real_success_marks_reconciled(self):
        db = _fresh_db()

        async def _fake_reconcile(db_):
            return {"orders_checked": 4}

        with mock.patch("execution.reconcile.reconcile_real_orders", side_effect=_fake_reconcile), \
             mock.patch("execution.auto_pilot._mark_reconciled") as mark:
            result = _run(main.manual_reconcile("real", admin="admin", db=db))
        assert result == {"ok": True, "mode": "REAL", "orders_checked": 4}
        mark.assert_called_once_with("REAL")
        assert _get_exit_lock("REAL").acquire(blocking=False) is True
        _get_exit_lock("REAL").release()


class TestManualHoldingsSync:
    def test_demo_is_a_noop_note(self):
        db = _fresh_db()
        result = _run(main.manual_holdings_sync("demo", admin=None, db=db))
        assert result["ok"] is True
        assert "nothing to sync" in result["note"]

    def test_real_lock_busy_409(self):
        db = _fresh_db()
        lock = _get_exit_lock("REAL")
        lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                _run(main.manual_holdings_sync("real", admin="admin", db=db))
            _expect_http_error(ei, 409)
        finally:
            lock.release()

    def test_real_success(self):
        db = _fresh_db()
        with mock.patch("portfolio.portfolio.holdings_sync_reconcile",
                        return_value={"closed": 2}) as sync_fn:
            result = _run(main.manual_holdings_sync("real", admin="admin", db=db))
        sync_fn.assert_called_once_with(db)
        assert result == {"ok": True, "mode": "REAL", "closed": 2}
        assert _get_exit_lock("REAL").acquire(blocking=False) is True
        _get_exit_lock("REAL").release()


# ══════════════════════════════════════════════════════════════════════════
# GET /dhan/positions, /dhan/holdings, /dhan/orders
# ══════════════════════════════════════════════════════════════════════════
class TestDhanLiveDataRoutes:
    def test_positions_not_connected_409(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_positions",
                        side_effect=main.dhan_client.DhanNotConnectedError("nope")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_positions(admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_positions_generic_error_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_positions", side_effect=RuntimeError("boom")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_positions(admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_positions_success(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_positions", return_value=[{"sym": "AAA"}]):
            result = _run(main.dhan_live_positions(admin="admin", db=db))
        assert result == {"ok": True, "positions": [{"sym": "AAA"}]}

    def test_holdings_not_connected_409(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_holdings",
                        side_effect=main.dhan_client.DhanNotConnectedError("nope")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_holdings(admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_holdings_generic_error_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_holdings", side_effect=RuntimeError("boom")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_holdings(admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_holdings_success(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_holdings", return_value=[{"sym": "BBB"}]):
            result = _run(main.dhan_live_holdings(admin="admin", db=db))
        assert result == {"ok": True, "holdings": [{"sym": "BBB"}]}

    def test_orders_not_connected_409(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_order_list",
                        side_effect=main.dhan_client.DhanNotConnectedError("nope")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_orders(admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_orders_generic_error_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_order_list", side_effect=RuntimeError("boom")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_live_orders(admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_orders_success(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.get_order_list", return_value=[{"id": "O1"}]):
            result = _run(main.dhan_live_orders(admin="admin", db=db))
        assert result == {"ok": True, "orders": [{"id": "O1"}]}


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v"]))
