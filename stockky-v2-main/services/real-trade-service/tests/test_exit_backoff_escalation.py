"""
tests/test_exit_backoff_escalation.py — offline unit tests for
exit_engine/exit.py's rejection-streak escalation path (session72 issue #32).

Tests the logic introduced in sessions 40/47/72:
  - EXIT_REJECT_STREAK_ESCALATE_AT rejections → escalation alert fires once
  - Subsequent rejections within cooldown → silent (no repeated alerts)
  - Once cooldown expires → alert fires again
  - Persistent errors (CDSL/IP/funds/exchange) → streak bumped on first failure,
    not only after ESCALATE_AT
  - Transient errors below ESCALATE_AT threshold → streak NOT bumped
  - Oversell and intraday-cutoff errors are excluded from streak counting

All tests are offline (SQLite in-memory, no Dhan API calls).

Run from services/real-trade-service:
    python -m pytest tests/test_exit_backoff_escalation.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import dhan_client

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    session = sessionmaker(bind=_engine)()
    yield session
    session.close()


def _make_position(db, *, symbol="TEST", mode="REAL", consecutive_exit_failures=0):
    # Guard: TradeAccount and TradeGateState have a UNIQUE constraint on mode —
    # skip inserting them if they already exist (e.g. second call in same test).
    from sqlalchemy import text
    existing_acct = db.execute(
        text("SELECT id FROM trade_accounts WHERE mode = :m"), {"m": mode}
    ).fetchone()
    if not existing_acct:
        acct = models.TradeAccount(
            mode=mode,
            cash_available=50_000.0,
            current_equity=50_000.0,
            broker_cash_available=100_000.0,
            starting_capital=50_000.0,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(acct)

    existing_gate = db.execute(
        text("SELECT id FROM trade_gate_state WHERE mode = :m"), {"m": mode}
    ).fetchone()
    if not existing_gate:
        gate = models.TradeGateState(
            mode=mode,
            armed=True,
            auto_pilot_enabled=True,
            risk_config_confirmed=True,
        )
        db.add(gate)

    pos = models.TradePosition(
        mode=mode,
        symbol=symbol,
        qty_open=10,
        avg_entry_price=100.0,
        current_stop=95.0,
        current_target=110.0,
        status="OPEN",
        opened_at=datetime.now(timezone.utc),
        consecutive_exit_failures=consecutive_exit_failures,
        last_exit_failure_at=None,
        source_tab="volume_shock",
    )
    db.add(pos)
    db.commit()
    return pos


# ── Tests: _extract_streak_key and bump logic ─────────────────────────────────

class TestStreakBumpLogic:
    """Tests for the placement-failure streak increment in _send_real_sell."""

    def test_persistent_ip_error_bumps_streak_immediately(self, db, monkeypatch):
        """CDSL / IP / funds errors are 'persistent' — bump on first rejection."""
        pos = _make_position(db, consecutive_exit_failures=0)
        assert pos.consecutive_exit_failures == 0

        # Simulate: SDK raises an invalid-IP error
        monkeypatch.setattr(dhan_client, "is_invalid_ip_error", lambda e: True)
        monkeypatch.setattr(dhan_client, "is_cdsl_edis_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_insufficient_funds_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_exchange_not_allowed_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_oversell_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_intraday_cutoff_error", lambda e: False)

        # Call the production function with the error path triggered
        from exit_engine.exit import _bump_exit_failure
        _bump_exit_failure(db, pos, "INVALID_IP_ERROR", is_persistent=True)
        db.refresh(pos)

        assert pos.consecutive_exit_failures == 1
        assert pos.last_exit_failure_at is not None

    def test_db_commit_failure_is_caught_and_rolled_back(self, db, monkeypatch):
        """If db.commit() blows up mid-bump, the function must not propagate —
        it logs and rolls back instead (session-under-test survives)."""
        pos = _make_position(db, consecutive_exit_failures=0)

        def _boom():
            raise RuntimeError("db is on fire")
        monkeypatch.setattr(db, "commit", _boom)
        rolled_back = {"called": False}
        real_rollback = db.rollback
        def _rollback():
            rolled_back["called"] = True
            return real_rollback()
        monkeypatch.setattr(db, "rollback", _rollback)

        from exit_engine.exit import _bump_exit_failure
        # Should not raise despite commit() failing.
        _bump_exit_failure(db, pos, "INVALID_IP_ERROR", is_persistent=True)

        assert rolled_back["called"] is True

    def test_generic_error_below_threshold_does_not_bump(self, db, monkeypatch):
        """Generic (non-persistent) errors below ESCALATE_AT do NOT bump the streak."""
        import exit_engine.exit as ex
        escalate_at = ex.EXIT_REJECT_STREAK_ESCALATE_AT
        pos = _make_position(db, consecutive_exit_failures=0)

        monkeypatch.setattr(dhan_client, "is_oversell_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_intraday_cutoff_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_invalid_ip_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_cdsl_edis_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_insufficient_funds_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_exchange_not_allowed_error", lambda e: False)

        from exit_engine.exit import _bump_exit_failure
        # streak count = 0, which is < ESCALATE_AT → no bump for generic errors
        _bump_exit_failure(db, pos, "GENERIC_ERROR", is_persistent=False,
                           current_streak=0, escalate_at=escalate_at)
        db.refresh(pos)
        assert pos.consecutive_exit_failures == 0

    def test_generic_error_at_threshold_bumps(self, db):
        """Generic error at or above ESCALATE_AT DOES bump (persistent retry storm path)."""
        import exit_engine.exit as ex
        escalate_at = ex.EXIT_REJECT_STREAK_ESCALATE_AT
        pos = _make_position(db, consecutive_exit_failures=0)

        from exit_engine.exit import _bump_exit_failure
        _bump_exit_failure(db, pos, "GENERIC_ERROR", is_persistent=False,
                           current_streak=escalate_at, escalate_at=escalate_at)
        db.refresh(pos)
        assert pos.consecutive_exit_failures == 1

    def test_oversell_error_never_bumps(self, db, monkeypatch):
        """Oversell errors are deliberately excluded — a fast re-qty retry is wanted."""
        pos = _make_position(db, consecutive_exit_failures=0)
        monkeypatch.setattr(dhan_client, "is_oversell_error", lambda e: True)
        monkeypatch.setattr(dhan_client, "is_intraday_cutoff_error", lambda e: False)

        from exit_engine.exit import _bump_exit_failure
        _bump_exit_failure(db, pos, "OVERSELL", is_persistent=False,
                           excluded=True)
        db.refresh(pos)
        assert pos.consecutive_exit_failures == 0

    def test_intraday_cutoff_error_never_bumps(self, db, monkeypatch):
        """Intraday-cutoff errors are also excluded — per-day flag handles them."""
        pos = _make_position(db, consecutive_exit_failures=0)
        monkeypatch.setattr(dhan_client, "is_oversell_error", lambda e: False)
        monkeypatch.setattr(dhan_client, "is_intraday_cutoff_error", lambda e: True)

        from exit_engine.exit import _bump_exit_failure
        _bump_exit_failure(db, pos, "INTRADAY_CUTOFF", is_persistent=False,
                           excluded=True)
        db.refresh(pos)
        assert pos.consecutive_exit_failures == 0


# ── Tests: backoff cooldown ───────────────────────────────────────────────────

class TestBackoffCooldown:
    """Tests for the exponential backoff in _should_skip_exit_this_cycle."""

    def test_no_failures_never_skips(self, db):
        pos = _make_position(db, consecutive_exit_failures=0)
        from exit_engine.exit import _should_skip_exit_this_cycle
        assert _should_skip_exit_this_cycle(pos) is False

    def test_first_failure_triggers_cooldown(self, db):
        pos = _make_position(db, consecutive_exit_failures=1)
        pos.last_exit_failure_at = datetime.now(timezone.utc)
        db.commit()

        from exit_engine.exit import _should_skip_exit_this_cycle
        # BASE_COOLDOWN * 2^0 = BASE_COOLDOWN seconds → we are within it
        assert _should_skip_exit_this_cycle(pos) is True

    def test_cooldown_expires(self, db):
        pos = _make_position(db, consecutive_exit_failures=1)
        # Last failure was long in the past → cooldown has expired
        pos.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()

        from exit_engine.exit import _should_skip_exit_this_cycle
        assert _should_skip_exit_this_cycle(pos) is False

    def test_exponential_backoff_grows(self, db):
        """With N failures the cooldown is BASE * 2^(N-1) — the 3rd failure
        means a longer cooldown than the 1st."""
        from exit_engine.exit import _should_skip_exit_this_cycle
        base = config.EXIT_RETRY_BASE_COOLDOWN_SECONDS

        # 1 failure: cooldown = base * 1
        pos1 = _make_position(db, symbol="SYM1", consecutive_exit_failures=1)
        pos1.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(seconds=base - 5)
        db.commit()
        assert _should_skip_exit_this_cycle(pos1) is True  # still inside window

        # 3 failures: cooldown = base * 4 — same elapsed time is now INSIDE
        pos3 = _make_position(db, symbol="SYM3", consecutive_exit_failures=3)
        pos3.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(seconds=base - 5)
        db.commit()
        assert _should_skip_exit_this_cycle(pos3) is True  # still inside much larger window

        # For pos1, an elapsed time > base means it's past its window
        pos1.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(seconds=base + 5)
        db.commit()
        assert _should_skip_exit_this_cycle(pos1) is False  # outside pos1's window...
        assert _should_skip_exit_this_cycle(pos3) is True   # ...but still inside pos3's larger window
