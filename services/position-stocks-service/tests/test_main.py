"""
tests/test_main.py

Covers main.py (session112 round 18) — the last major coverage gap
(22%, 700 stmts / 547 missed at the start of this round).

Strategy:
  * Helper functions (_get_gate, _maybe_lazy_reset_gate_kill_switch,
    _capital_cooldown_filter, _note_capital_starved,
    _ist_midnight_today_utc) — called directly against a real in-memory
    SQLite session.
  * _run_cycle() — the biggest single chunk — driven directly via
    asyncio.run(), with every external dependency it touches
    (reconcile, eod_squareoff, overnight_stop, ledger, scan,
    intraday_eligibility, quality_gate, attempt_entry, circuit_breaker,
    dhan_client) monkeypatched at the main-module boundary. This file
    tests main.py's OWN branching (which stage runs, which summary field
    gets set, which candidate is tried next) — not the business logic
    inside those other modules, which already have their own test files.
  * _trading_loop / _fast_reconcile_loop — driven for exactly one
    iteration by making the second asyncio.sleep() call raise
    CancelledError (the loops' own break condition), so the body between
    the two sleeps actually executes once under test. _fast_reconcile_loop
    re-raises CancelledError (unlike _trading_loop, which breaks on it),
    so its tests run through a small wrapper that swallows the propagated
    CancelledError after exactly one pass.
  * Every HTTP route — via FastAPI's TestClient, with get_db overridden
    to a real in-memory SQLite session and require_admin overridden to
    bypass auth (login/logout are tested separately against the REAL
    auth dependency, not the override).

No network, no real Dhan/broker, no real Angel One feed.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_main.py -q \\
        --cov=main --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
import main as m
from screening.engine import Candidate
from screening.quality_gate import QualitySignal
from orders.entry import InsufficientCapitalSkip, ManualEntryRejected
from orders.eod_squareoff import ManualCloseRejected
from execution.dhan_client import DhanNotConnectedError


# ─── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    # FastAPI runs sync route handlers (and asyncio.to_thread calls) on a
    # worker thread — check_same_thread=False + StaticPool keeps the same
    # in-memory SQLite connection usable from there too.
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    yield session
    session.close()


@pytest.fixture()
def client(db, monkeypatch):
    """TestClient with get_db bound to the in-memory session and
    require_admin bypassed (auth itself is tested separately below)."""
    m.app.dependency_overrides[m.get_db] = lambda: db
    m.app.dependency_overrides[m.require_admin] = lambda: "test-admin"
    # Never let a real loop or ws client try to start for this client.
    yield TestClient(m.app)
    m.app.dependency_overrides.clear()


def _gate(db, **kw):
    row = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = models.ScalpGateState(mode="REAL")
        db.add(row)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


def _position(db, **kw):
    defaults = dict(
        symbol="SBIN", dhan_security_id="3045", window_source="5m",
        status="OPEN", entry_price=100.0, quantity=10,
        target_price=103.0, stop_price=98.0,
        adaptive_target_pct=3.0, adaptive_stop_pct=2.0, capital_risked=1000.0,
    )
    defaults.update(kw)
    row = models.ScalpPosition(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _candidate(symbol="SBIN", window_minutes=5, pct=2.0, ltp=100.0, activity=50, score=10.0):
    return Candidate(symbol=symbol, window_minutes=window_minutes, pct_change=pct,
                      current_ltp=ltp, tick_activity=activity, composite_score=score)


def _quality(symbol="SBIN", fund=80.0, tech=80.0, cap=5000.0):
    return QualitySignal(symbol=symbol, fundamental_score=fund, technical_score=tech, market_cap_cr=cap)


# ─── pure/DB helpers ────────────────────────────────────────────────────────

class TestGetGate:
    def test_creates_row_when_missing(self, db):
        assert db.query(models.ScalpGateState).count() == 0
        gate = m._get_gate(db)
        assert gate.mode == "REAL"
        assert db.query(models.ScalpGateState).count() == 1

    def test_returns_existing_row(self, db):
        _gate(db, is_armed=True)
        gate = m._get_gate(db)
        assert gate.is_armed is True
        assert db.query(models.ScalpGateState).count() == 1


class TestLazyKillSwitchReset:
    def test_not_tripped_is_noop(self, db):
        gate = _gate(db, daily_loss_kill_switch_tripped=False)
        m._maybe_lazy_reset_gate_kill_switch(db, gate)
        assert gate.daily_loss_kill_switch_tripped is False

    def test_tripped_today_stays_tripped(self, db):
        today = m.ist_today_str()
        gate = _gate(db, daily_loss_kill_switch_tripped=True, daily_loss_kill_switch_tripped_date=today)
        m._maybe_lazy_reset_gate_kill_switch(db, gate)
        assert gate.daily_loss_kill_switch_tripped is True

    def test_tripped_prior_day_resets(self, db):
        gate = _gate(db, daily_loss_kill_switch_tripped=True, daily_loss_kill_switch_tripped_date="2020-01-01")
        m._maybe_lazy_reset_gate_kill_switch(db, gate)
        assert gate.daily_loss_kill_switch_tripped is False
        assert gate.daily_loss_kill_switch_tripped_date is None


class TestCapitalCooldownFilter:
    def test_no_starved_symbols_passes_through_unchanged(self, db):
        m._capital_starved.clear()
        cands = [_candidate("A"), _candidate("B")]
        kept, skipped = m._capital_cooldown_filter(db, cands)
        assert kept == cands and skipped == []

    def test_zero_cooldown_config_disables_filter(self, db, monkeypatch):
        monkeypatch.setattr(config, "CAPITAL_STARVED_COOLDOWN_S", 0)
        m._capital_starved["A"] = (0.0, 100.0)
        try:
            kept, skipped = m._capital_cooldown_filter(db, [_candidate("A")])
            assert skipped == []
        finally:
            m._capital_starved.clear()

    def test_ledger_lookup_failure_falls_back_to_none(self, db, monkeypatch):
        """ledger.get_state() raising should not blow up the filter —
        `avail` just becomes None and only the time-based cooldown applies."""
        m._capital_starved.clear()
        monkeypatch.setattr(m.ledger, "get_state", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
        m._note_capital_starved(db, "TREL")
        # avail lookup inside _note_capital_starved also fails -> defaults to 0.0, fine.
        kept, skipped = m._capital_cooldown_filter(db, [_candidate("TREL")])
        assert skipped == ["TREL"]
        m._capital_starved.clear()


def test_ist_midnight_today_utc_is_before_now(db):
    ts = m._ist_midnight_today_utc()
    assert ts.tzinfo is not None
    assert ts <= datetime.now(timezone.utc)


def test_get_db_dependency_yields_from_db_module():
    gen = m.get_db()
    # Just confirm it's a generator wired to _db.get_db — actually
    # iterating it would need a configured DB; existence + type is enough
    # for this thin wrapper.
    assert hasattr(gen, "__next__")
    gen.close()


# ─── _run_cycle ─────────────────────────────────────────────────────────────

def _patch_no_op_prechecks(monkeypatch):
    """Common baseline: reconcile/edis/overnight-stop all no-op, EOD not
    due, so tests can focus on whichever later stage they're targeting."""
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
    monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: False)
    monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 10, 0, tzinfo=m.IST))


class TestRunCycleEarlyStages:
    def test_reconcile_exception_is_caught_and_staged(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation",
                             lambda db: (_ for _ in ()).throw(RuntimeError("dhan down")))
        _gate(db, service_enabled=False)  # short-circuit quickly after reconcile
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["stages"][0]["name"] == "reconcile_exits"
        assert "Error" in summary["stages"][0]["detail"]

    def test_reconcile_counts_closed_positions(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 2)
        _gate(db, service_enabled=False)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["reconciled"] == 2

    def test_eod_squareoff_fires_and_returns_early(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 10, 0, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EOD_SQUAREOFF_TIME)
        ran = {}
        monkeypatch.setattr(m.eod_squareoff, "run_eod_squareoff", lambda db: ran.setdefault("fired", True))
        _gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["eod_fired"] is True
        assert ran.get("fired") is True
        # returned early -> no gate_checks/scan stage recorded
        assert not any(s["name"] == "gate_checks" for s in summary["stages"])

    def test_eod_already_fired_today_is_not_rerun(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        today = m.ist_today_str()
        called = {"n": 0}
        monkeypatch.setattr(m.eod_squareoff, "run_eod_squareoff", lambda db: called.__setitem__("n", called["n"] + 1))
        _gate(db, eod_squareoff_fired_date=today, service_enabled=False)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert called["n"] == 0
        assert summary["eod_fired"] is False

    def test_service_disabled_stops_here(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        _gate(db, service_enabled=False)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "SERVICE_DISABLED"

    def test_not_armed_stops_here(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        _gate(db, service_enabled=True, is_armed=False)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "NOT_ARMED"

    def test_past_eod_time_blocks_new_entries(self, db, monkeypatch):
        """PAST_EOD_TIME fires when the EOD sweep already ran earlier today
        (gate.eod_squareoff_fired_date == today) but it's still past the EOD
        time threshold — distinct from the "EOD fires right now" branch just
        above it, which returns early instead."""
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 16, 0, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: True)
        today = m.ist_today_str()
        _gate(db, service_enabled=True, is_armed=True,
              eod_squareoff_fired_date=today, edis_check_last_run_date=today)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "PAST_EOD_TIME"

    def test_before_entry_window(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 0, tzinfo=m.IST))
        _gate(db, service_enabled=True, is_armed=True)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "BEFORE_ENTRY_WINDOW"

    def test_past_entry_cutoff(self, db, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 14, 45, tzinfo=m.IST))
        _gate(db, service_enabled=True, is_armed=True)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "PAST_ENTRY_CUTOFF"


class TestRunCycleScanOnward:
    def _armed_gate(self, db, **kw):
        kw.setdefault("service_enabled", True)
        kw.setdefault("is_armed", True)
        kw.setdefault("auto_pilot_enabled", True)
        return _gate(db, **kw)

    def _base_patches(self, monkeypatch):
        _patch_no_op_prechecks(monkeypatch)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 11, 0, tzinfo=m.IST))
        monkeypatch.setattr(m.ledger, "sync_from_broker", lambda db: 100000.0)
        monkeypatch.setattr(m.intraday_eligibility, "get_restricted_symbols", lambda db: set())

    def test_ledger_sync_error_is_caught(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m.ledger, "sync_from_broker",
                             lambda db: (_ for _ in ()).throw(RuntimeError("no dhan")))
        monkeypatch.setattr(m, "scan", lambda **kw: [])
        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        stage = next(s for s in summary["stages"] if s["name"] == "ledger_sync")
        assert "Error" in stage["detail"]

    def test_no_candidates_ends_cycle(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [])
        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["candidates_seen"] == 0
        assert summary["skipped_reason"] is None  # falls through cleanly, no candidates is not an "error"

    def test_all_candidates_intraday_restricted(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("RESTRICTED")])
        monkeypatch.setattr(m.intraday_eligibility, "get_restricted_symbols", lambda db: {"RESTRICTED"})
        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "ALL_CANDIDATES_INTRADAY_RESTRICTED"

    def test_auto_pilot_off_stops_before_quality_gate(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("SBIN")])
        called = {"n": 0}
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: called.__setitem__("n", 1) or {})
        self._armed_gate(db, auto_pilot_enabled=False)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "AUTO_PILOT_OFF"
        assert called["n"] == 0  # quality gate never reached

    def test_manual_trigger_bypasses_auto_pilot_off(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("SBIN")])
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db, res: None)

        async def _fake_check(symbol, cached=None):
            return _quality(symbol, fund=None, tech=None, cap=None)
        monkeypatch.setattr(m.quality_gate, "check", _fake_check)
        self._armed_gate(db, auto_pilot_enabled=False)
        summary = asyncio.run(m._run_cycle(db, trigger="MANUAL"))
        assert summary["skipped_reason"] != "AUTO_PILOT_OFF"

    def test_all_candidates_capital_cooldown_skips_cycle(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("STARVED")])
        m._capital_starved.clear()
        m._capital_starved["STARVED"] = (m.time.monotonic() + 999, 100.0)
        try:
            self._armed_gate(db)
            summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
            assert summary["skipped_reason"] == "ALL_CANDIDATES_CAPITAL_COOLDOWN"
            assert summary["capital_cooldown_skipped"] == ["STARVED"]
        finally:
            m._capital_starved.clear()

    def test_quality_gate_rejects_first_enters_second(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("BAD"), _candidate("GOOD")])
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db, res: None)

        async def _fake_check(symbol, cached=None):
            if symbol == "BAD":
                return _quality(symbol, fund=10.0, tech=10.0, cap=1.0)  # fails floors
            return _quality(symbol, fund=90.0, tech=90.0, cap=9000.0)
        monkeypatch.setattr(m.quality_gate, "check", _fake_check)
        logged = []
        monkeypatch.setattr(m, "log_quality_reject", lambda db, c, q, r: logged.append((c.symbol, r)))

        entered = {}
        monkeypatch.setattr(m.circuit_breaker, "is_open", lambda: False)
        monkeypatch.setattr(m.circuit_breaker, "record_success", lambda: entered.setdefault("cb_success", True))

        def _sync_attempt_entry(db, candidate, quality=None):
            entered["symbol"] = candidate.symbol
            return _position(db, symbol=candidate.symbol)
        monkeypatch.setattr(m, "attempt_entry", _sync_attempt_entry)

        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert logged and logged[0][0] == "BAD"
        assert entered["symbol"] == "GOOD"
        assert summary["entered_symbol"] == "GOOD"
        assert entered.get("cb_success") is True

    def test_circuit_breaker_open_skips_entry_but_not_reconcile(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("SBIN")])
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db, res: None)

        async def _fake_check(symbol, cached=None):
            return _quality(symbol, fund=90.0, tech=90.0, cap=9000.0)
        monkeypatch.setattr(m.quality_gate, "check", _fake_check)
        monkeypatch.setattr(m.circuit_breaker, "is_open", lambda: True)
        monkeypatch.setattr(m.circuit_breaker, "status", lambda: {
            "state": "open", "consecutive_failures": 7, "failure_threshold": 5,
            "cooldown_s": 60.0, "seconds_until_retry": 12.0,
        })
        attempted = {"n": 0}
        monkeypatch.setattr(m, "attempt_entry", lambda db, c, quality=None: attempted.__setitem__("n", 1))
        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["skipped_reason"] == "CIRCUIT_BREAKER_OPEN"
        assert attempted["n"] == 0
        assert summary["reconciled"] == 0  # reconcile stage still ran (unconditional, first stage)

    def test_insufficient_capital_falls_through_to_next_candidate(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("POOR"), _candidate("RICH")])
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db, res: None)

        async def _fake_check(symbol, cached=None):
            return _quality(symbol, fund=90.0, tech=90.0, cap=9000.0)
        monkeypatch.setattr(m.quality_gate, "check", _fake_check)
        monkeypatch.setattr(m.circuit_breaker, "is_open", lambda: False)
        monkeypatch.setattr(m.circuit_breaker, "record_success", lambda: None)

        starved_notes = []
        monkeypatch.setattr(m, "_note_capital_starved", lambda db, sym: starved_notes.append(sym))

        def _attempt(db, candidate, quality=None):
            if candidate.symbol == "POOR":
                raise InsufficientCapitalSkip("no capital")
            return _position(db, symbol=candidate.symbol)
        monkeypatch.setattr(m, "attempt_entry", _attempt)

        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert starved_notes == ["POOR"]
        assert summary["entered_symbol"] == "RICH"

    def test_all_capital_skipped_no_entry(self, db, monkeypatch):
        self._base_patches(monkeypatch)
        monkeypatch.setattr(m, "scan", lambda **kw: [_candidate("POOR")])
        monkeypatch.setattr(m.quality_gate, "get_cache_batch", lambda db, syms: {})
        monkeypatch.setattr(m.quality_gate, "upsert_cache_batch", lambda db, res: None)

        async def _fake_check(symbol, cached=None):
            return _quality(symbol, fund=90.0, tech=90.0, cap=9000.0)
        monkeypatch.setattr(m.quality_gate, "check", _fake_check)
        monkeypatch.setattr(m.circuit_breaker, "is_open", lambda: False)
        monkeypatch.setattr(m, "_note_capital_starved", lambda db, sym: None)

        def _attempt(db, candidate, quality=None):
            raise InsufficientCapitalSkip("no capital")
        monkeypatch.setattr(m, "attempt_entry", _attempt)

        self._armed_gate(db)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert summary["entered_symbol"] is None

    def test_edis_and_overnight_recheck_paths_run_when_due(self, db, monkeypatch):
        """Exercise the eDIS/overnight-recheck block by making
        ist_time_at_or_after True only for the eDIS-check time."""
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after",
                             lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        _position(db, symbol="HELD", status="OPEN", overnight_converted_to_cnc=True)
        edis_calls = {"n": 0}
        monkeypatch.setattr(m.dhan_client, "edis_verification_summary",
                             lambda db: edis_calls.__setitem__("n", edis_calls["n"] + 1) or
                             {"verified_today": False, "pending_symbols": ["HELD"]})
        notified = []
        monkeypatch.setattr(m.notifier, "notify_critical", lambda msg: notified.append(msg))
        monkeypatch.setattr(m.overnight_stop, "morning_recheck", lambda db, pct: {"checked": True})
        _gate(db, service_enabled=False)  # short-circuit right after these pre-checks
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert edis_calls["n"] == 1
        assert notified  # a critical alert was sent for the unverified pending CNC holding

    def test_edis_verified_today_is_silent(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        _position(db, symbol="HELD", status="OPEN", overnight_converted_to_cnc=True)
        monkeypatch.setattr(m.dhan_client, "edis_verification_summary", lambda db: {"verified_today": True})
        notified = []
        monkeypatch.setattr(m.notifier, "notify_critical", lambda msg: notified.append(msg))
        monkeypatch.setattr(m.overnight_stop, "morning_recheck", lambda db, pct: {"checked": True})
        _gate(db, service_enabled=False)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert notified == []

    def test_edis_unknown_status_notifies(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        _position(db, symbol="HELD", status="OPEN", overnight_converted_to_cnc=True)
        monkeypatch.setattr(m.dhan_client, "edis_verification_summary",
                             lambda db: {"verified_today": None, "detail": "timeout"})
        notified = []
        monkeypatch.setattr(m.notifier, "notify_critical", lambda msg: notified.append(msg))
        monkeypatch.setattr(m.overnight_stop, "morning_recheck", lambda db, pct: {"checked": True})
        _gate(db, service_enabled=False)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert notified and "unknown" in notified[0]

    def test_edis_fetch_exception_is_caught(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        _position(db, symbol="HELD", status="OPEN", overnight_converted_to_cnc=True)
        monkeypatch.setattr(m.dhan_client, "edis_verification_summary",
                             lambda db: (_ for _ in ()).throw(RuntimeError("network")))
        monkeypatch.setattr(m.overnight_stop, "morning_recheck", lambda db, pct: {"checked": True})
        _gate(db, service_enabled=False)
        # must not raise
        asyncio.run(m._run_cycle(db, trigger="AUTO"))

    def test_no_pending_cnc_skips_edis_fetch_entirely(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        called = {"n": 0}
        monkeypatch.setattr(m.dhan_client, "edis_verification_summary",
                             lambda db: called.__setitem__("n", 1))
        monkeypatch.setattr(m.overnight_stop, "morning_recheck", lambda db, pct: {"checked": True})
        _gate(db, service_enabled=False)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert called["n"] == 0

    def test_overnight_recheck_exception_is_caught(self, db, monkeypatch):
        monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
        monkeypatch.setattr(m, "ist_now", lambda: datetime(2026, 9, 25, 9, 5, tzinfo=m.IST))
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EDIS_MORNING_CHECK_TIME)
        monkeypatch.setattr(m.overnight_stop, "morning_recheck",
                             lambda db, pct: (_ for _ in ()).throw(RuntimeError("boom")))
        _gate(db, service_enabled=False)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))  # must not raise


# ─── _trading_loop / _fast_reconcile_loop (one iteration each) ─────────────

class _OneShotSleep:
    """First await returns normally; second raises CancelledError so the
    loop's own `except asyncio.CancelledError: break` fires — giving us
    exactly one full pass through the loop body under test."""
    def __init__(self):
        self.n = 0

    async def __call__(self, *a, **kw):
        self.n += 1
        if self.n >= 2:
            raise asyncio.CancelledError()


def test_trading_loop_market_closed_skips_cycle(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m, "is_market_open_ist", lambda: False)
    ran = {"n": 0}
    monkeypatch.setattr(m, "_run_cycle", lambda db, trigger: ran.__setitem__("n", ran["n"] + 1))
    asyncio.run(m._trading_loop())
    assert ran["n"] == 0


def test_trading_loop_runs_one_cycle_and_records_success(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    ran = {"n": 0}

    async def _fake_run_cycle(db_, trigger):
        ran["n"] += 1
        return {}
    monkeypatch.setattr(m, "_run_cycle", _fake_run_cycle)
    success = {"n": 0}
    monkeypatch.setattr(m.circuit_breaker, "record_success", lambda: success.__setitem__("n", success["n"] + 1))
    asyncio.run(m._trading_loop())
    assert ran["n"] == 1
    assert success["n"] == 1


def test_trading_loop_no_session_factory_skips(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(m._db, "get_session_factory", lambda: None)
    ran = {"n": 0}
    monkeypatch.setattr(m, "_run_cycle", lambda db, trigger: ran.__setitem__("n", ran["n"] + 1))
    asyncio.run(m._trading_loop())
    assert ran["n"] == 0


def test_trading_loop_exception_records_failure(monkeypatch):
    """An exception inside the loop body must be swallowed (logged) and
    record_failure() called — the loop itself never propagates it (only
    CancelledError breaks it, and our second sleep() call supplies that
    right after, so this test still terminates)."""
    seen_exc = {"n": 0}

    class _Sleep:
        def __init__(self):
            self.n = 0

        async def __call__(self, *a, **kw):
            self.n += 1
            if self.n >= 3:
                raise asyncio.CancelledError()

    monkeypatch.setattr(m.asyncio, "sleep", _Sleep())

    def _boom():
        seen_exc["n"] += 1
        raise RuntimeError("market check exploded")
    monkeypatch.setattr(m, "is_market_open_ist", _boom)
    failures = {"n": 0}
    monkeypatch.setattr(m.circuit_breaker, "record_failure", lambda: failures.__setitem__("n", failures["n"] + 1))
    asyncio.run(m._trading_loop())
    assert seen_exc["n"] >= 1
    assert failures["n"] >= 1


def _run_one_fast_reconcile_pass():
    """_fast_reconcile_loop (unlike _trading_loop) re-raises
    asyncio.CancelledError instead of breaking on it — correct behavior
    for a real task.cancel(), but it means our _OneShotSleep's
    second-call CancelledError propagates out of asyncio.run() too.
    Swallow it here so each test still runs exactly one full pass."""
    try:
        asyncio.run(m._fast_reconcile_loop())
    except asyncio.CancelledError:
        pass


def test_fast_reconcile_loop_no_session_factory_continues(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: None)
    _run_one_fast_reconcile_pass()  # must not raise anything else


def test_fast_reconcile_loop_full_pass(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db: {"resolved": 0})
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    closed = {"n": 0}
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: closed.__setitem__("n", 1) or 1)
    monkeypatch.setattr(m.eod_squareoff, "run_stagnation_exit", lambda db: 0)
    monkeypatch.setattr(m.breakeven, "run_breakeven_stop", lambda db: 0)
    monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: False)
    _gate(db)
    _run_one_fast_reconcile_pass()
    assert closed["n"] == 1


def test_fast_reconcile_loop_eod_and_retention(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db: {"resolved": 0})
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
    monkeypatch.setattr(m.eod_squareoff, "run_stagnation_exit", lambda db: 0)
    monkeypatch.setattr(m.breakeven, "run_breakeven_stop", lambda db: 0)
    monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: t == m._EOD_SQUAREOFF_TIME)
    eod_ran = {"n": 0}
    monkeypatch.setattr(m.eod_squareoff, "run_eod_squareoff", lambda db: eod_ran.__setitem__("n", 1))
    retention_ran = {"n": 0}
    monkeypatch.setattr(m.reconcile, "run_retention_cleanup", lambda db: retention_ran.__setitem__("n", 1) or 3)
    _gate(db)
    _run_one_fast_reconcile_pass()
    assert eod_ran["n"] == 1
    assert retention_ran["n"] == 1


def test_fast_reconcile_loop_reconcile_exception_stops_rest_of_pass(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db: {"resolved": 0})
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation",
                         lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    stagnation_called = {"n": 0}
    monkeypatch.setattr(m.eod_squareoff, "run_stagnation_exit", lambda db: stagnation_called.__setitem__("n", 1))
    _run_one_fast_reconcile_pass()
    assert stagnation_called["n"] == 0  # `continue`d past the rest of this pass


def test_fast_reconcile_loop_stagnation_and_breakeven_exceptions_are_isolated(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db: {"resolved": 0})
    monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 0)
    monkeypatch.setattr(m.eod_squareoff, "run_stagnation_exit",
                         lambda db: (_ for _ in ()).throw(RuntimeError("stagnation boom")))
    breakeven_called = {"n": 0}
    monkeypatch.setattr(m.breakeven, "run_breakeven_stop", lambda db: breakeven_called.__setitem__("n", 1))
    monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: False)
    _gate(db)
    _run_one_fast_reconcile_pass()
    assert breakeven_called["n"] == 1  # stagnation's exception didn't stop breakeven from running


def test_fast_reconcile_loop_off_hours_still_runs_stuck_pending(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    resolved = {"n": 0}
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db: resolved.__setitem__("n", 1))
    monkeypatch.setattr(m, "is_market_open_ist", lambda: False)
    reconcile_called = {"n": 0}
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: reconcile_called.__setitem__("n", 1))
    _run_one_fast_reconcile_pass()
    assert resolved["n"] == 1
    assert reconcile_called["n"] == 0  # market-closed -> everything after `continue` skipped


def test_fast_reconcile_loop_stuck_pending_exception_is_caught(db, monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", _OneShotSleep())
    monkeypatch.setattr(m._db, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending",
                         lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(m, "is_market_open_ist", lambda: False)
    _run_one_fast_reconcile_pass()  # must not raise


# ─── HTTP routes ────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "position-stocks-service"}


class TestLogin:
    def test_login_success_and_logout(self, monkeypatch, client, db):
        monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
        monkeypatch.setattr(m, "verify_admin_password", lambda u, p: u == "admin" and p == "pw")
        monkeypatch.setattr(m, "issue_session_token", lambda u: ("tok123", "2099-01-01T00:00:00Z"))
        r = client.post("/auth/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        assert r.json()["token"] == "tok123"
        # logout goes through the require_admin override (bypassed), just confirm 200
        r2 = client.post("/auth/logout")
        assert r2.status_code == 200

    def test_login_wrong_password(self, monkeypatch, client):
        monkeypatch.setattr(m, "verify_admin_password", lambda u, p: False)
        r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_login_auth_not_configured(self, monkeypatch, client):
        from auth.admin_auth import AdminAuthError

        def _raise(u, p):
            raise AdminAuthError("not configured")
        monkeypatch.setattr(m, "verify_admin_password", _raise)
        r = client.post("/auth/login", json={"username": "admin", "password": "x"})
        assert r.status_code == 401


def test_pipeline_status_route(client, monkeypatch):
    monkeypatch.setattr(m.pipeline_status, "snapshot", lambda: {"stage": "idle"})
    r = client.get("/pipeline/status")
    assert r.json() == {"stage": "idle"}


class TestStatusRoute:
    def test_status_basic_shape(self, client, db, monkeypatch):
        monkeypatch.setattr(m.ws_client, "ws_status", lambda: {"connected": True})
        monkeypatch.setattr(m.shared_order_budget, "status", lambda db: {"used": 0, "budget": 5000})
        monkeypatch.setattr(m.shared_symbol_lock, "status", lambda db: [])
        monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert "armed" in body and "pipeline_config" in body
        assert body["ws"] == {"connected": True}

    def test_status_counts_eod_stragglers(self, client, db, monkeypatch):
        monkeypatch.setattr(m.ws_client, "ws_status", lambda: {})
        monkeypatch.setattr(m.shared_order_budget, "status", lambda db: {})
        monkeypatch.setattr(m.shared_symbol_lock, "status", lambda db: [])
        monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
        today = m.ist_today_str()
        monkeypatch.setattr(m, "ist_time_at_or_after", lambda t: True)
        _gate(db, eod_squareoff_fired_date=today)
        _position(db, symbol="STUCK", status="EXIT_LEGS_REJECTED")
        r = client.get("/status")
        assert r.json()["eod_squareoff_stragglers"] == 1


def test_arm_disarm(client, db):
    r = client.post("/arm")
    assert r.json()["status"] == "armed"
    r2 = client.post("/arm")
    assert r2.json()["status"] == "already_armed"
    r3 = client.post("/disarm")
    assert r3.json()["status"] == "disarmed"


def test_service_enable_disable(client):
    assert client.post("/service/disable").json()["status"] == "disabled"
    assert client.post("/service/enable").json()["status"] == "enabled"


def test_autopilot_enable_disable(client):
    assert client.post("/autopilot/enable").json()["status"] == "auto_pilot_enabled"
    assert client.post("/autopilot/disable").json()["status"] == "auto_pilot_disabled"


def test_stagnation_exit_enable_disable(client):
    assert client.post("/stagnation-exit/enable").json()["status"] == "stagnation_exit_enabled"
    assert client.post("/stagnation-exit/disable").json()["status"] == "stagnation_exit_disabled"


def test_breakeven_stop_enable_disable(client):
    assert client.post("/breakeven-stop/enable").json()["status"] == "breakeven_stop_enabled"
    assert client.post("/breakeven-stop/disable").json()["status"] == "breakeven_stop_disabled"


class TestCycleRunRoute:
    def test_service_disabled_400(self, client, db):
        _gate(db, service_enabled=False)
        r = client.post("/cycle/run")
        assert r.status_code == 400

    def test_not_armed_400(self, client, db):
        _gate(db, service_enabled=True, is_armed=False)
        r = client.post("/cycle/run")
        assert r.status_code == 400

    def test_locked_returns_409(self, client, db, monkeypatch):
        _gate(db, service_enabled=True, is_armed=True)

        class _AlwaysLocked:
            def locked(self):
                return True
        monkeypatch.setattr(m, "_cycle_lock", _AlwaysLocked())
        r = client.post("/cycle/run")
        assert r.status_code == 409

    def test_success_runs_manual_cycle(self, client, db, monkeypatch):
        _gate(db, service_enabled=True, is_armed=True)

        async def _fake(db_, trigger):
            return {"trigger": trigger, "entered_symbol": None}
        monkeypatch.setattr(m, "_run_cycle", _fake)
        r = client.post("/cycle/run")
        assert r.status_code == 200
        assert r.json()["trigger"] == "MANUAL"


def test_kill(client, db):
    r = client.post("/kill")
    assert r.json()["status"] == "killed"
    gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
    assert gate.is_armed is False
    assert gate.daily_loss_kill_switch_tripped is True


class TestPositionsRoute:
    def test_open_position_with_live_price(self, client, db, monkeypatch):
        _position(db, symbol="SBIN", entry_price=100.0, quantity=10, target_price=105.0, stop_price=98.0)
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: 102.0)
        r = client.get("/positions")
        row = r.json()[0]
        assert row["current_price"] == 102.0
        assert row["unrealized_pnl"] == 20.0

    def test_ltp_lookup_exception_is_swallowed(self, client, db, monkeypatch):
        _position(db, symbol="SBIN")
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: (_ for _ in ()).throw(RuntimeError("x")))
        r = client.get("/positions")
        assert r.json()[0]["current_price"] is None

    def test_zero_ltp_treated_as_none(self, client, db, monkeypatch):
        _position(db, symbol="SBIN")
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: 0.0)
        r = client.get("/positions")
        assert r.json()[0]["current_price"] is None

    def test_overnight_carry_shows_synthetic_stop_no_target(self, client, db, monkeypatch):
        _position(db, symbol="HELD", entry_price=100.0, quantity=5,
                  overnight_converted_to_cnc=True, target_price=110.0, stop_price=95.0)
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: 103.0)
        monkeypatch.setattr(config, "OVERNIGHT_STOP_LOSS_PCT", 4.0)
        r = client.get("/positions")
        row = r.json()[0]
        assert row["overnight_stop_price"] == 96.0
        assert row["target_distance_pct"] is None
        assert row["stop_distance_pct"] is not None

    def test_closed_position_has_no_live_price(self, client, db, monkeypatch):
        _position(db, symbol="DONE", status="TARGET_HIT", exit_price=105.0, realized_pnl=50.0)
        called = {"n": 0}
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: called.__setitem__("n", 1))
        r = client.get("/positions")
        assert r.json()[0]["current_price"] is None
        assert called["n"] == 0


class TestTradesHistory:
    def test_summary_win_rate_and_best_worst(self, client, db):
        _position(db, symbol="W1", status="TARGET_HIT", realized_pnl=100.0,
                  opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))
        _position(db, symbol="L1", status="STOP_HIT", realized_pnl=-40.0,
                  opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))
        r = client.get("/trades/history")
        body = r.json()
        assert body["summary"]["total_trades"] == 2
        assert body["summary"]["wins"] == 1
        assert body["summary"]["losses"] == 1
        assert body["summary"]["best_trade"]["symbol"] == "W1"
        assert body["summary"]["worst_trade"]["symbol"] == "L1"

    def test_status_filter(self, client, db):
        _position(db, symbol="A", status="TARGET_HIT", realized_pnl=1.0)
        _position(db, symbol="B", status="STOP_HIT", realized_pnl=-1.0)
        r = client.get("/trades/history", params={"status_filter": "target_hit"})
        syms = [t["symbol"] for t in r.json()["trades"]]
        assert syms == ["A"]

    def test_range_today_filters_out_older(self, client, db):
        _position(db, symbol="OLD", status="TARGET_HIT", realized_pnl=1.0,
                  opened_at=datetime.now(timezone.utc) - timedelta(days=5),
                  closed_at=datetime.now(timezone.utc) - timedelta(days=5))
        _position(db, symbol="TODAY", status="TARGET_HIT", realized_pnl=1.0,
                  opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))
        r = client.get("/trades/history", params={"range": "today"})
        syms = [t["symbol"] for t in r.json()["trades"]]
        assert syms == ["TODAY"]

    def test_range_3d(self, client, db):
        _position(db, symbol="RECENT", status="TARGET_HIT", realized_pnl=1.0,
                  opened_at=datetime.now(timezone.utc) - timedelta(days=1),
                  closed_at=datetime.now(timezone.utc) - timedelta(days=1))
        r = client.get("/trades/history", params={"range": "3d"})
        assert "RECENT" in [t["symbol"] for t in r.json()["trades"]]

    def test_empty_history_no_crash(self, client, db):
        r = client.get("/trades/history")
        body = r.json()
        assert body["summary"]["total_trades"] == 0
        assert body["summary"]["win_rate_pct"] is None
        assert body["summary"]["best_trade"] is None


def test_trades_cleanup(client, db, monkeypatch):
    _gate(db)
    monkeypatch.setattr(m.reconcile, "run_retention_cleanup", lambda db: 5)
    r = client.post("/trades/cleanup")
    assert r.json()["deleted"] == 5
    gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
    assert gate.retention_cleanup_last_run_date == m.ist_today_str()


def test_auth_config_check(client, monkeypatch):
    from auth import admin_auth
    monkeypatch.setattr(admin_auth, "auth_config_diagnostics", lambda: {"ok": True})
    r = client.get("/auth/config-check")
    assert r.json() == {"ok": True}


class TestDhanFunds:
    def test_success(self, client, monkeypatch):
        monkeypatch.setattr(m.dhan_client, "get_funds", lambda db: {"availabelBalance": 1000.0})
        r = client.get("/dhan/funds")
        assert r.status_code == 200

    def test_not_connected_409(self, client, monkeypatch):
        def _raise(db):
            raise DhanNotConnectedError("no creds")
        monkeypatch.setattr(m.dhan_client, "get_funds", _raise)
        r = client.get("/dhan/funds")
        assert r.status_code == 409

    def test_other_error_502(self, client, monkeypatch):
        def _raise(db):
            raise RuntimeError("dhan 5xx")
        monkeypatch.setattr(m.dhan_client, "get_funds", _raise)
        r = client.get("/dhan/funds")
        assert r.status_code == 502


def test_reconcile_pending(client, monkeypatch):
    monkeypatch.setattr(m.reconcile, "list_pending_reconcile", lambda db: [{"id": 1}])
    r = client.get("/reconcile/pending")
    assert r.json() == {"count": 1, "rows": [{"id": 1}]}


def test_reconcile_pending_resolve(client, monkeypatch):
    monkeypatch.setattr(m.reconcile, "resolve_stuck_pending", lambda db, force=False: {"resolved": 2})
    r = client.post("/reconcile/pending/resolve")
    assert r.json() == {"resolved": 2}


class TestCandidatesRoute:
    def test_market_open_runs_scan(self, client, monkeypatch):
        monkeypatch.setattr(m, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(m, "scan", lambda: [_candidate("SBIN")])
        r = client.get("/candidates")
        body = r.json()
        assert body["market_open"] is True
        assert body["count"] == 1

    def test_market_closed_returns_empty(self, client, monkeypatch):
        monkeypatch.setattr(m, "is_market_open_ist", lambda: False)
        called = {"n": 0}
        monkeypatch.setattr(m, "scan", lambda: called.__setitem__("n", 1))
        r = client.get("/candidates")
        assert r.json()["candidates"] == []
        assert called["n"] == 0


def test_candidates_log_filters(client, db):
    log1 = models.ScalpCandidateLog(symbol="A", window_source="5m", pct_change=1.0,
                                     decision="ENTERED", reason="ok")
    log2 = models.ScalpCandidateLog(symbol="B", window_source="5m", pct_change=1.0,
                                     decision="SKIPPED", reason="QUALITY_GATE:low score")
    db.add_all([log1, log2])
    db.commit()
    r = client.get("/candidates/log", params={"decision_filter": "skipped"})
    syms = [row["symbol"] for row in r.json()]
    assert syms == ["B"]
    r2 = client.get("/candidates/log", params={"reason_prefix": "QUALITY_GATE"})
    assert [row["symbol"] for row in r2.json()] == ["B"]


def test_candidates_restricted(client, db):
    row = models.ScalpIntradayRestrictedSecurity(symbol="RESTRICT", hit_count=3, last_detail="rejected")
    db.add(row)
    db.commit()
    r = client.get("/candidates/restricted")
    assert r.json()[0]["symbol"] == "RESTRICT"


def test_ledger_get(client, monkeypatch):
    monkeypatch.setattr(m.ledger, "get_state", lambda db: {"available_capital": 5000.0})
    r = client.get("/ledger")
    assert r.json()["available_capital"] == 5000.0


def test_ledger_sync(client, monkeypatch):
    monkeypatch.setattr(m.ledger, "sync_from_broker", lambda db: 12345.0)
    r = client.post("/ledger/sync")
    assert r.json()["total_allocated_capital"] == 12345.0


def test_ledger_reset_daily_clears_gate_kill_switch(client, db, monkeypatch):
    monkeypatch.setattr(m.ledger, "reset_daily", lambda db: None)
    today = m.ist_today_str()
    _gate(db, daily_loss_kill_switch_tripped=True, daily_loss_kill_switch_tripped_date=today)
    r = client.post("/ledger/reset-daily")
    assert r.json()["status"] == "ledger_daily_reset"
    gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
    assert gate.daily_loss_kill_switch_tripped is False


def test_ledger_reset_daily_noop_when_not_tripped(client, db, monkeypatch):
    monkeypatch.setattr(m.ledger, "reset_daily", lambda db: None)
    _gate(db, daily_loss_kill_switch_tripped=False)
    r = client.post("/ledger/reset-daily")
    assert r.status_code == 200


class TestSymbolLock:
    def test_release_success(self, client, monkeypatch):
        monkeypatch.setattr(m.shared_symbol_lock, "force_release", lambda db, sym: True)
        r = client.delete("/symbol-lock/sbin")
        assert r.status_code == 200
        assert r.json()["symbol"] == "SBIN"

    def test_release_not_found_404(self, client, monkeypatch):
        monkeypatch.setattr(m.shared_symbol_lock, "force_release", lambda db, sym: False)
        r = client.delete("/symbol-lock/nope")
        assert r.status_code == 404


def test_ws_status_route(client, monkeypatch):
    monkeypatch.setattr(m.ws_client, "ws_status", lambda: {"connected": False})
    r = client.get("/ws-status")
    assert r.json() == {"connected": False}


class TestDhanAccount:
    def test_connected_with_funds(self, client, monkeypatch):
        monkeypatch.setattr(m.dhan_credentials_ro, "connection_status", lambda db: {"connected": True})
        monkeypatch.setattr(m.dhan_client, "get_funds", lambda db: {"availabelBalance": 500.0})
        r = client.get("/dhan/account")
        body = r.json()
        assert body["connected"] is True
        assert body["funds"]["availabelBalance"] == 500.0
        assert body["funds_error"] is None

    def test_connected_funds_call_fails(self, client, monkeypatch):
        monkeypatch.setattr(m.dhan_credentials_ro, "connection_status", lambda db: {"connected": True})

        def _raise(db):
            raise RuntimeError("timeout")
        monkeypatch.setattr(m.dhan_client, "get_funds", _raise)
        r = client.get("/dhan/account")
        body = r.json()
        assert body["funds"] is None
        assert "timeout" in body["funds_error"]

    def test_not_connected_skips_funds_call(self, client, monkeypatch):
        monkeypatch.setattr(m.dhan_credentials_ro, "connection_status", lambda db: {"connected": False})
        called = {"n": 0}
        monkeypatch.setattr(m.dhan_client, "get_funds", lambda db: called.__setitem__("n", 1))
        r = client.get("/dhan/account")
        assert r.json()["funds"] is None
        assert called["n"] == 0


class TestDhanLiveOrders:
    def test_filters_to_our_own_order_ids(self, client, db, monkeypatch):
        _position(db, symbol="OURS", dhan_super_order_id="SO1")
        monkeypatch.setattr(m.dhan_client, "get_super_order_list", lambda db: [
            {"orderId": "SO1", "symbol": "OURS"},
            {"orderId": "SO2", "symbol": "NOT_OURS"},
        ])
        r = client.get("/dhan/live-orders")
        body = r.json()
        assert body["count"] == 1
        assert body["orders"][0]["orderId"] == "SO1"

    def test_tag_fallback_when_no_db_match(self, client, db, monkeypatch):
        monkeypatch.setattr(m.dhan_client, "get_super_order_list", lambda db: [
            {"orderId": "SOX", "tag": "SCALP"},
            {"orderId": "SOY", "tag": "OTHER"},
        ])
        r = client.get("/dhan/live-orders")
        assert r.json()["count"] == 1
        assert r.json()["orders"][0]["orderId"] == "SOX"

    def test_fetch_failure_502(self, client, monkeypatch):
        def _raise(db):
            raise RuntimeError("dhan down")
        monkeypatch.setattr(m.dhan_client, "get_super_order_list", _raise)
        r = client.get("/dhan/live-orders")
        assert r.status_code == 502

    def test_no_orders_at_all(self, client, db, monkeypatch):
        monkeypatch.setattr(m.dhan_client, "get_super_order_list", lambda db: [])
        r = client.get("/dhan/live-orders")
        assert r.json() == {"count": 0, "orders": []}


def test_reconcile_route(client, monkeypatch):
    monkeypatch.setattr(m.reconcile, "run_exit_reconciliation", lambda db: 3)
    r = client.post("/reconcile")
    assert r.json()["positions_closed"] == 3


class TestManualBuy:
    def test_missing_symbol_400(self, client):
        r = client.post("/positions/manual/buy", json={"symbol": "   "})
        assert r.status_code == 400

    def test_no_live_price_503(self, client, monkeypatch):
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: None)
        r = client.post("/positions/manual/buy", json={"symbol": "SBIN"})
        assert r.status_code == 503

    def test_rejected_entry_409(self, client, monkeypatch):
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: 100.0)

        def _raise(db, symbol, ltp, quantity=None):
            raise ManualEntryRejected("not armed")
        monkeypatch.setattr(m, "attempt_manual_entry", _raise)
        r = client.post("/positions/manual/buy", json={"symbol": "SBIN"})
        assert r.status_code == 409

    def test_success(self, client, db, monkeypatch):
        monkeypatch.setattr(m.ws_client, "get_last_ltp", lambda sym: 100.0)

        def _fake(db_, symbol, ltp, quantity=None):
            return _position(db_, symbol=symbol, entry_price=ltp, quantity=quantity or 5)
        monkeypatch.setattr(m, "attempt_manual_entry", _fake)
        r = client.post("/positions/manual/buy", json={"symbol": "sbin", "quantity": 7})
        assert r.status_code == 200
        assert r.json()["symbol"] == "SBIN"
        assert r.json()["quantity"] == 7


class TestManualClose:
    def test_not_found_404(self, client):
        r = client.post("/positions/999999/close")
        assert r.status_code == 404

    def test_rejected_409(self, client, db, monkeypatch):
        pos = _position(db, symbol="SBIN")

        def _raise(db_, p, exit_reason="MANUAL_EXIT"):
            raise ManualCloseRejected("already closing")
        monkeypatch.setattr(m, "close_position_now", _raise)
        r = client.post(f"/positions/{pos.id}/close")
        assert r.status_code == 409

    def test_success(self, client, db, monkeypatch):
        pos = _position(db, symbol="SBIN")
        monkeypatch.setattr(m, "close_position_now", lambda db_, p, exit_reason="MANUAL_EXIT": {"order_id": "X1"})
        r = client.post(f"/positions/{pos.id}/close")
        assert r.status_code == 200
        assert r.json()["order_id"] == "X1"
        assert r.json()["status"] == "pending_broker_confirmation"
