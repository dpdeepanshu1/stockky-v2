"""
tests/test_shared_order_budget.py

100%-coverage-plan follow-up: execution/shared_order_budget.py -- 44% -> target 100%.

This is the cross-service Dhan account-wide order-rate guard (shared with
position-stocks-service via the same `stockky_shared_order_budget` table).
Prior coverage only exercised the "happy path" indirectly through entry.py /
manual_engine.py / exit.py call sites (which always stay under budget and
never hit a DB error). This file directly tests:

  - _get_or_create_row(): the helper itself, both branches (row missing /
    row exists). NOTE: this helper is dead code as of this audit -- nothing
    in the codebase calls it (only _ensure_row_exists is actually wired up).
    Tested directly anyway since it's still public-looking, exported code;
    flagged in AUDIT_REPORT.md as a candidate for removal.
  - _ensure_row_exists(): the IntegrityError race-swallow branch, not just
    the "row already there" / "no row yet" branches.
  - check_and_reserve(): the budget-exhausted (False) branch, the
    fail-open exception branch, and the atomic-increment-succeeds branch
    under a real sqlite session (not mocked).
  - record_order_unconditional(): the normal unconditional-increment path
    (including when already over budget -- exits are never gated) and its
    fail-open exception branch.

Run from services/real-trade-service:
    python3 -m pytest tests/test_shared_order_budget.py -q \
        --cov=execution.shared_order_budget --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import shared_order_budget as sob

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 2)


def _capture_alerts(monkeypatch, mod):
    """group 448: stand-in for notifier.notify_fire_and_forget so no test ever sends a real Telegram; also resets the rate limiter."""
    import types
    sent = []
    monkeypatch.setitem(sys.modules, "notifier", types.SimpleNamespace(notify_fire_and_forget=lambda text: sent.append(text)))
    monkeypatch.setitem(mod._last_alert, "t", None)
    return sent


def _fail_open_switch(monkeypatch):
    import config as _c
    monkeypatch.setattr(_c, "SHARED_GATES_FAIL_CLOSED", False, raising=False)


class TestGetOrCreateRow:
    def test_creates_row_when_missing(self, db):
        row = sob._get_or_create_row(db, "2026-09-23")
        assert row.trade_date == "2026-09-23"
        assert row.orders_placed_today == 0
        # actually persisted (flushed), not just an in-memory object
        assert db.query(models.SharedOrderBudget).filter_by(trade_date="2026-09-23").first() is not None

    def test_returns_existing_row_without_duplicating(self, db):
        db.add(models.SharedOrderBudget(trade_date="2026-09-23", orders_placed_today=7))
        db.commit()
        row = sob._get_or_create_row(db, "2026-09-23")
        assert row.orders_placed_today == 7
        assert db.query(models.SharedOrderBudget).filter_by(trade_date="2026-09-23").count() == 1


class TestEnsureRowExists:
    def test_noop_when_row_already_present(self, db):
        db.add(models.SharedOrderBudget(trade_date="2026-09-23", orders_placed_today=3))
        db.commit()
        sob._ensure_row_exists(db, "2026-09-23")
        assert db.query(models.SharedOrderBudget).filter_by(trade_date="2026-09-23").first().orders_placed_today == 3

    def test_creates_row_when_missing(self, db):
        sob._ensure_row_exists(db, "2026-09-23")
        row = db.query(models.SharedOrderBudget).filter_by(trade_date="2026-09-23").first()
        assert row is not None
        assert row.orders_placed_today == 0

    def test_swallows_integrity_error_on_concurrent_insert_race(self, db, monkeypatch):
        """Simulates a concurrent process (or position-stocks-service)
        inserting the same trade_date row between our SELECT and our own
        INSERT -- the unique constraint on trade_date rejects whichever
        commits second, and _ensure_row_exists must swallow that and treat
        it as success (the row exists either way)."""
        original_commit = db.commit
        calls = {"n": 0}

        def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
            return original_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        # Should not raise -- IntegrityError is caught and rolled back.
        sob._ensure_row_exists(db, "2026-09-23")
        assert calls["n"] == 1  # only the failing commit was attempted inside this call


class TestCheckAndReserve:
    def test_reserves_when_under_budget(self, db):
        assert sob.check_and_reserve(db) is True
        row = db.query(models.SharedOrderBudget).first()
        assert row.orders_placed_today == 1

    def test_multiple_reserves_increment_atomically(self, db):
        assert sob.check_and_reserve(db) is True  # 0 -> 1
        assert sob.check_and_reserve(db) is True  # 1 -> 2 (budget pinned to 2)
        row = db.query(models.SharedOrderBudget).first()
        assert row.orders_placed_today == 2

    def test_returns_false_and_logs_when_budget_exhausted(self, db, caplog):
        assert sob.check_and_reserve(db) is True   # 0 -> 1
        assert sob.check_and_reserve(db) is True   # 1 -> 2 (== budget, now exhausted)
        with caplog.at_level(logging.WARNING, logger="real-trade-shared-order-budget"):
            result = sob.check_and_reserve(db)      # 2 !< 2 -> blocked
        assert result is False
        row = db.query(models.SharedOrderBudget).first()
        assert row.orders_placed_today == 2  # not incremented on the blocked call
        assert any("SHARED Dhan order budget exhausted" in r.message for r in caplog.records)

    def test_fails_closed_on_exception_with_alert(self, db, monkeypatch, caplog):
        sent = _capture_alerts(monkeypatch, sob)

        def boom():
            raise RuntimeError("db is down")
        monkeypatch.setattr(sob, "ist_today_str", boom)
        with caplog.at_level(logging.ERROR, logger="real-trade-shared-order-budget"):
            result = sob.check_and_reserve(db)
        assert result is False
        assert any("check_and_reserve failed" in r.message for r in caplog.records)
        assert "failing CLOSED" in caplog.text
        assert len(sent) == 1 and "New BUY entries are being refused" in sent[0]
        sob.check_and_reserve(db)
        assert len(sent) == 1       # rate-limited: no Telegram storm while the DB is down

    def test_fail_open_switch_restores_old_behaviour(self, db, monkeypatch):
        sent = _capture_alerts(monkeypatch, sob)
        _fail_open_switch(monkeypatch)
        monkeypatch.setattr(sob, "ist_today_str", lambda: (_ for _ in ()).throw(RuntimeError("db is down")))
        assert sob.check_and_reserve(db) is True and sent == []

    def test_rollback_failure_inside_exception_handler_is_swallowed(self, db, monkeypatch):
        """Even if db.rollback() itself raises inside the except branch,
        check_and_reserve must still return (False, fail-closed), not propagate."""
        _capture_alerts(monkeypatch, sob)

        def boom():
            raise RuntimeError("db is down")
        monkeypatch.setattr(sob, "ist_today_str", boom)
        monkeypatch.setattr(db, "rollback", lambda: (_ for _ in ()).throw(RuntimeError("rollback also broken")))
        assert sob.check_and_reserve(db) is False


class TestRecordOrderUnconditional:
    def test_increments_counter_unconditionally(self, db):
        sob.record_order_unconditional(db)
        row = db.query(models.SharedOrderBudget).first()
        assert row.orders_placed_today == 1

    def test_increments_even_past_budget(self, db):
        # budget pinned to 2 -- exits are never gated by it
        for _ in range(5):
            sob.record_order_unconditional(db)
        row = db.query(models.SharedOrderBudget).first()
        assert row.orders_placed_today == 5

    def test_non_blocking_on_exception(self, db, monkeypatch, caplog):
        def boom():
            raise RuntimeError("db is down")
        monkeypatch.setattr(sob, "ist_today_str", boom)
        with caplog.at_level(logging.ERROR, logger="real-trade-shared-order-budget"):
            sob.record_order_unconditional(db)  # must not raise
        assert any("record_order_unconditional failed" in r.message for r in caplog.records)

    def test_rollback_failure_inside_exception_handler_is_swallowed(self, db, monkeypatch):
        def boom():
            raise RuntimeError("db is down")
        monkeypatch.setattr(sob, "ist_today_str", boom)
        monkeypatch.setattr(db, "rollback", lambda: (_ for _ in ()).throw(RuntimeError("rollback also broken")))
        sob.record_order_unconditional(db)  # must not raise
