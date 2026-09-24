"""
tests/test_shared_order_budget.py

Closes the coverage gaps in capital/shared_order_budget.py (55% → 100%,
29 missing lines: 46-51, 67-68, 109-115, 142-147, 153-160) and pins the
behaviour of every function in it.

This is the cross-service Dhan account-wide order-rate guard: one row per IST
day in `stockky_shared_order_budget`, shared with real-trade-service. Until
now it was only exercised as a mocked collaborator of orders/entry.py and
orders/eod_squareoff.py, never directly — including its budget-exhausted
branch, its fail-open branch and its `status()` snapshot.

Properties pinned from both directions:

  * GATE — entries (`check_and_reserve`) are refused once the shared cap is
    reached, and the cap check is done by the DATABASE in one conditional
    UPDATE (not read-then-increment), proven with a deliberately stale ORM
    identity map.
  * NEVER GATE EXITS — `record_order_unconditional` increments even past the
    cap and swallows every error.
  * FAIL OPEN — any DB error in `check_and_reserve` allows the order.
  * `status()` never raises and never reports a negative `remaining`.

Note: `_get_or_create_row` is dead code in this service (nothing calls it;
only `_ensure_row_exists` is wired up) — same as the duplicated copy in
real-trade-service. Tested directly anyway and flagged in the session note.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_shared_order_budget.py -q \\
        --cov=capital.shared_order_budget --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import shared_order_budget as sob

LOGGER = "position-stocks-shared-order-budget"
TODAY = "2026-09-25"
TOMORROW = "2026-09-26"


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    monkeypatch.setattr(sob, "ist_today_str", lambda: TODAY)
    monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 3)
    yield session
    session.close()


def _row(db, date=TODAY):
    return db.query(models.SharedOrderBudget).filter_by(trade_date=date).first()


def _used(db, date=TODAY):
    db.expire_all()
    r = _row(db, date)
    return None if r is None else r.orders_placed_today


def _seed(db, used, date=TODAY):
    db.add(models.SharedOrderBudget(trade_date=date, orders_placed_today=used))
    db.commit()


def _hide_first_lookup(db):
    """Make the FIRST db.query(...) report 'no row' even though one exists —
    reproduces the SELECT-then-INSERT race window. Later queries are real."""
    real_query = db.query
    state = {"n": 0}

    class _Empty:
        def filter_by(self, **kw):
            return self

        def first(self):
            return None

    def q(*a, **k):
        state["n"] += 1
        return _Empty() if state["n"] == 1 else real_query(*a, **k)

    db.query = q


# ══════════════════════════════════════════════════════════════════════════════
# _get_or_create_row (dead code — direct tests only)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetOrCreateRow:
    def test_creates_row_when_missing(self, db):
        row = sob._get_or_create_row(db, TODAY)
        assert row.trade_date == TODAY
        assert row.orders_placed_today == 0
        assert row.id is not None  # flushed

    def test_flushes_but_does_not_commit(self, db):
        sob._get_or_create_row(db, TODAY)
        db.rollback()
        assert _row(db) is None

    def test_returns_existing_row_without_duplicating(self, db):
        _seed(db, 2)
        row = sob._get_or_create_row(db, TODAY)
        assert row.orders_placed_today == 2
        assert db.query(models.SharedOrderBudget).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# _ensure_row_exists
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsureRowExists:
    def test_creates_zeroed_row_and_commits(self, db):
        sob._ensure_row_exists(db, TODAY)
        db.rollback()  # proves it was committed, not merely flushed
        assert _used(db) == 0

    def test_noop_when_row_present_and_does_not_commit(self, db):
        _seed(db, 2)
        db.commit = MagicMock(wraps=db.commit)
        sob._ensure_row_exists(db, TODAY)
        db.commit.assert_not_called()
        assert _used(db) == 2

    def test_swallows_real_integrity_error_on_concurrent_insert(self, db):
        # Row already exists but our first lookup misses it → INSERT hits the
        # REAL unique(trade_date) constraint → IntegrityError swallowed.
        _seed(db, 2)
        db.expunge_all()
        _hide_first_lookup(db)
        sob._ensure_row_exists(db, TODAY)  # must not raise
        assert db.query(models.SharedOrderBudget).count() == 1
        assert _used(db) == 2  # the existing row is untouched
        # session usable afterwards (failed INSERT was rolled back)
        sob._ensure_row_exists(db, TOMORROW)
        assert _used(db, TOMORROW) == 0

    def test_rolls_back_after_integrity_error(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.commit.side_effect = IntegrityError("INSERT", {}, Exception("dup"))
        sob._ensure_row_exists(db, TODAY)
        db.rollback.assert_called_once()

    def test_non_integrity_errors_propagate_to_the_caller(self):
        # Callers (check_and_reserve / record_order_unconditional) own the
        # fail-open handling; the helper must not hide other failures.
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.commit.side_effect = RuntimeError("disk full")
        with pytest.raises(RuntimeError):
            sob._ensure_row_exists(db, TODAY)


# ══════════════════════════════════════════════════════════════════════════════
# check_and_reserve
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckAndReserve:
    def test_first_call_creates_row_and_reserves(self, db):
        assert sob.check_and_reserve(db) is True
        assert _used(db) == 1

    def test_increment_is_committed(self, db):
        sob.check_and_reserve(db)
        db.rollback()
        assert _used(db) == 1

    def test_fills_exactly_to_the_cap_then_refuses(self, db):
        assert [sob.check_and_reserve(db) for _ in range(3)] == [True, True, True]
        assert _used(db) == 3
        assert sob.check_and_reserve(db) is False
        assert _used(db) == 3  # refused call did not increment

    def test_refusal_is_sticky(self, db):
        for _ in range(3):
            sob.check_and_reserve(db)
        assert [sob.check_and_reserve(db) for _ in range(4)] == [False] * 4
        assert _used(db) == 3

    def test_exhausted_logs_used_over_budget(self, db, caplog):
        _seed(db, 3)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sob.check_and_reserve(db) is False
        assert "budget exhausted (3/3 today across both services)" in caplog.text

    def test_already_over_cap_from_unconditional_exits_still_refuses(self, db):
        _seed(db, 7)  # exits pushed it past the cap
        assert sob.check_and_reserve(db) is False
        assert _used(db) == 7

    def test_budget_is_read_from_config_at_call_time(self, db, monkeypatch):
        _seed(db, 3)
        assert sob.check_and_reserve(db) is False
        monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 4)
        assert sob.check_and_reserve(db) is True
        assert _used(db) == 4

    def test_new_day_starts_with_a_fresh_counter(self, db, monkeypatch):
        for _ in range(3):
            sob.check_and_reserve(db)
        assert sob.check_and_reserve(db) is False
        monkeypatch.setattr(sob, "ist_today_str", lambda: TOMORROW)
        assert sob.check_and_reserve(db) is True
        assert _used(db, TOMORROW) == 1
        assert _used(db, TODAY) == 3  # yesterday's row untouched

    def test_only_todays_row_is_incremented(self, db):
        # The UPDATE's WHERE must pin trade_date — otherwise it would bump
        # every day's row that is still under the cap.
        _seed(db, 1, date="2026-09-24")
        assert sob.check_and_reserve(db) is True
        assert _used(db, "2026-09-24") == 1
        assert _used(db, TODAY) == 1

    def test_cap_is_enforced_by_the_database_not_a_stale_orm_read(self, db):
        # AUDIT FIX regression: the cap check must be the UPDATE's own WHERE
        # clause. Load the row (identity map says 0), then bump the DB behind
        # the session's back to the cap. A read-then-increment implementation
        # would trust the stale 0 and overshoot; the atomic UPDATE must refuse.
        _seed(db, 0)
        stale = _row(db)
        assert stale.orders_placed_today == 0
        db.execute(
            update(models.SharedOrderBudget)
            .where(models.SharedOrderBudget.trade_date == TODAY)
            .values(orders_placed_today=3)
            .execution_options(synchronize_session=False)
        )
        # (deliberately no commit/expire: the identity map stays stale)
        assert stale.orders_placed_today == 0
        assert sob.check_and_reserve(db) is False
        assert _used(db) == 3

    def test_zero_rowcount_is_treated_as_exhausted(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
            orders_placed_today=5,
        )
        db.execute.return_value = SimpleNamespace(rowcount=0)
        assert sob.check_and_reserve(db) is False

    def test_exhausted_with_missing_row_logs_budget_and_refuses(self, db, monkeypatch, caplog):
        # If the row somehow doesn't exist when the UPDATE matches nothing,
        # the warning falls back to the budget figure instead of crashing.
        monkeypatch.setattr(sob, "_ensure_row_exists", lambda d, t: None)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sob.check_and_reserve(db) is False
        assert "budget exhausted (3/3 today" in caplog.text

    def test_fails_open_on_update_error_logs_and_rolls_back(self, db, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("db down")

        rolled = []
        real_rollback = db.rollback
        monkeypatch.setattr(db, "execute", boom)
        monkeypatch.setattr(db, "rollback", lambda: (rolled.append(1), real_rollback())[1])
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert sob.check_and_reserve(db) is True
        assert "failing open" in caplog.text
        assert rolled == [1]

    def test_fails_open_on_ensure_row_error(self, db, monkeypatch):
        def boom(d, t):
            raise RuntimeError("cannot insert")

        monkeypatch.setattr(sob, "_ensure_row_exists", boom)
        assert sob.check_and_reserve(db) is True

    def test_rollback_failure_inside_handler_is_swallowed(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        db.rollback.side_effect = RuntimeError("rollback failed too")
        assert sob.check_and_reserve(db) is True


# ══════════════════════════════════════════════════════════════════════════════
# record_order_unconditional
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordOrderUnconditional:
    def test_creates_row_and_increments(self, db):
        sob.record_order_unconditional(db)
        assert _used(db) == 1

    def test_increments_are_cumulative_and_committed(self, db):
        for _ in range(3):
            sob.record_order_unconditional(db)
        db.rollback()
        assert _used(db) == 3

    def test_never_gated_even_far_past_the_cap(self, db):
        _seed(db, 3)
        for _ in range(4):
            sob.record_order_unconditional(db)
        assert _used(db) == 7

    def test_exits_count_against_the_entry_gate(self, db):
        sob.record_order_unconditional(db)
        sob.record_order_unconditional(db)
        assert sob.check_and_reserve(db) is True   # 3rd
        assert sob.check_and_reserve(db) is False  # cap reached by mix of both

    def test_only_todays_row_is_incremented(self, db):
        _seed(db, 5, date="2026-09-24")
        sob.record_order_unconditional(db)
        assert _used(db, "2026-09-24") == 5
        assert _used(db, TODAY) == 1

    def test_error_is_swallowed_logged_and_rolled_back(self, db, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("db down")

        rolled = []
        real_rollback = db.rollback
        monkeypatch.setattr(db, "execute", boom)
        monkeypatch.setattr(db, "rollback", lambda: (rolled.append(1), real_rollback())[1])
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            sob.record_order_unconditional(db)  # must not raise
        assert "record_order_unconditional failed (non-blocking)" in caplog.text
        assert rolled == [1]

    def test_ensure_row_error_is_swallowed(self, db, monkeypatch):
        def boom(d, t):
            raise RuntimeError("cannot insert")

        monkeypatch.setattr(sob, "_ensure_row_exists", boom)
        sob.record_order_unconditional(db)  # must not raise

    def test_rollback_failure_inside_handler_is_swallowed(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        db.rollback.side_effect = RuntimeError("rollback failed too")
        sob.record_order_unconditional(db)  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# status
# ══════════════════════════════════════════════════════════════════════════════

class TestStatus:
    def test_no_row_today_reports_zero_used(self, db):
        assert sob.status(db) == {"used_today": 0, "budget": 3, "remaining": 3}

    def test_reports_used_and_remaining(self, db):
        _seed(db, 2)
        assert sob.status(db) == {"used_today": 2, "budget": 3, "remaining": 1}

    def test_exactly_at_cap_has_zero_remaining(self, db):
        _seed(db, 3)
        assert sob.status(db) == {"used_today": 3, "budget": 3, "remaining": 0}

    def test_over_cap_remaining_never_negative(self, db):
        _seed(db, 9)  # unconditional exits can overshoot
        s = sob.status(db)
        assert s["used_today"] == 9
        assert s["remaining"] == 0

    def test_only_todays_row_counts(self, db):
        _seed(db, 3, date="2026-09-24")
        assert sob.status(db)["used_today"] == 0

    def test_budget_reflects_config(self, db, monkeypatch):
        monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 5000)
        assert sob.status(db) == {"used_today": 0, "budget": 5000, "remaining": 5000}

    def test_error_returns_zeros_and_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 3)
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            s = sob.status(db)
        assert s == {"used_today": 0, "budget": 3, "remaining": 3}
        assert "status failed" in caplog.text
