"""
tests/test_audit_logger.py

Covers audit/logger.py — currently at 79%, missing lines 25-29 (the except
branch of log_action where a DB commit error is caught, logged to stderr, and
the session is rolled back so the *caller's* work can still proceed).

Two tests suffice for full coverage:

  1. Happy path  — row is committed to the DB and nothing is raised.
  2. Failure path — db.commit() raises; the function catches it, calls
     logger.error, calls db.rollback(), and does NOT re-raise (so the caller
     is unaffected).

The `db` fixture is the same real in-memory SQLite convention this repo uses
everywhere else (test_candidates_orchestration.py, test_adaptive_thresholds.py,
etc.) — avoids leaking between tests and keeps the test hermetically runnable
with just:

    cd services/real-trade-service
    python -m pytest tests/test_audit_logger.py -v

CAVEAT (same as all session91 files): not run in this sandbox — hand-traced
against the real source. Run on the VM before trusting the result.
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from audit.logger import log_action

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


class TestLogAction:
    def test_happy_path_inserts_row(self, db):
        """log_action commits one TradeAuditLog row with the supplied fields."""
        log_action(db, actor="system", action="TEST_ACTION", detail="detail text", mode="DEMO")

        row = db.query(models.TradeAuditLog).first()
        assert row is not None
        assert row.actor == "system"
        assert row.action == "TEST_ACTION"
        assert row.detail == "detail text"
        assert row.mode == "DEMO"

    def test_happy_path_mode_none_is_allowed(self, db):
        """mode=None is valid for account-level events (login, arm/disarm)."""
        log_action(db, actor="admin", action="ADMIN_LOGIN")

        row = db.query(models.TradeAuditLog).first()
        assert row is not None
        assert row.mode is None
        assert row.actor == "admin"
        assert row.action == "ADMIN_LOGIN"

    def test_db_commit_failure_is_swallowed_and_logged(self, db):
        """
        If db.commit() raises, log_action must:
          - NOT re-raise (caller's work must not be broken)
          - call logger.error exactly once
          - call db.rollback() to leave the session clean
        """
        boom = RuntimeError("disk full")

        # Wrap the real session: intercept commit to raise, spy on rollback.
        original_commit = db.commit
        original_rollback = db.rollback
        rollback_calls = []

        def bad_commit():
            raise boom

        def spy_rollback():
            rollback_calls.append(True)
            original_rollback()

        db.commit = bad_commit
        db.rollback = spy_rollback

        with patch("audit.logger.logger") as mock_logger:
            # Must NOT raise — this is the core contract of the except branch.
            log_action(db, actor="system", action="RISKY_ACTION", detail="oops")

            mock_logger.error.assert_called_once()
            # The call should mention the action name so it's traceable.
            args = mock_logger.error.call_args
            assert "RISKY_ACTION" in str(args)

        # rollback must have been called to leave the session usable.
        assert len(rollback_calls) == 1

        # Restore so the fixture's own s.close() doesn't fail.
        db.commit = original_commit
        db.rollback = original_rollback

    def test_db_commit_failure_does_not_insert_row(self, db):
        """After a failed commit + rollback, no row should be visible."""
        def bad_commit():
            raise RuntimeError("connection lost")

        original_commit = db.commit
        db.commit = bad_commit

        with patch("audit.logger.logger"):
            log_action(db, actor="system", action="FAILED_ACTION")

        db.commit = original_commit
        # rollback was called, so the pending add is gone.
        assert db.query(models.TradeAuditLog).count() == 0
