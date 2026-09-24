"""
tests/test_intraday_eligibility.py

SESSION93 — first direct coverage for intraday_eligibility.py. Every other
test file that touches this module (test_candidates_orchestration.py,
test_exit_circuit_limit_resend.py, test_exit_error_branches.py,
test_rt_reconcile.py) monkeypatches its public functions away entirely, so
none of the module's own logic — including the cross-service
`scalp_intraday_restricted` sister-table mirroring (see the module
docstring) — had ever actually run under test.

Two tables are involved:
  - trade_intraday_restricted (this service's own table, models.py's
    IntradayRestrictedSecurity — created via models.Base.metadata)
  - scalp_intraday_restricted (position-stocks-service's sister table,
    read/written via raw SQL text() since the two services are separate
    codebases — created here by hand with a matching shape, since no
    ORM model for it exists in this codebase to build it from)

Both live in the same in-memory SQLite engine/session, matching this
service's own module note that in production they share one Oracle schema.

Run from services/real-trade-service:
    python -m pytest tests/test_intraday_eligibility.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
import intraday_eligibility as ie

_engine = create_engine("sqlite:///:memory:")

_SISTER_DDL = """
CREATE TABLE scalp_intraday_restricted (
    symbol TEXT PRIMARY KEY,
    first_detected_at TEXT,
    last_detected_at TEXT,
    hit_count INTEGER,
    last_detail TEXT
)
"""


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    # The sister table lives outside models.Base (it belongs to
    # position-stocks-service's own schema — see module docstring), so
    # Base.metadata.drop_all() above never touches it. Since all tests in
    # this file share one in-memory-SQLite engine/connection (same
    # convention as test_afterhours_scan_orchestration.py's `db` fixture),
    # a table any earlier test created via raw CREATE TABLE would otherwise
    # persist — and its rows with it — into every later test, including
    # ones that specifically need the sister table ABSENT. Drop it here on
    # every test's setup so each test starts from a truly clean slate
    # regardless of what ran before it.
    s.execute(text("DROP TABLE IF EXISTS scalp_intraday_restricted"))
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def db_with_sister(db):
    """Same session, plus the sister table actually created — for tests
    of the success paths. Tests that want the sister table MISSING (to
    exercise the fail-open except branches) just use the bare `db`
    fixture instead and never call this one."""
    db.execute(text(_SISTER_DDL))
    db.commit()
    return db


def _insert_sister_row(db, symbol, hit_count=1, detail=None):
    db.execute(
        text(
            "INSERT INTO scalp_intraday_restricted "
            "(symbol, first_detected_at, last_detected_at, hit_count, last_detail) "
            "VALUES (:sym, '2026-09-24T00:00:00+00:00', '2026-09-24T00:00:00+00:00', :hc, :detail)"
        ),
        {"sym": symbol, "hc": hit_count, "detail": detail},
    )
    db.commit()


def _sister_row(db, symbol):
    return db.execute(
        text("SELECT symbol, hit_count, last_detail FROM scalp_intraday_restricted WHERE symbol = :sym"),
        {"sym": symbol},
    ).first()


# ── _sister_restricted_symbols ───────────────────────────────────────────────

class TestSisterRestrictedSymbols:
    def test_returns_symbols_from_sister_table(self, db_with_sister):
        _insert_sister_row(db_with_sister, "TCS")
        _insert_sister_row(db_with_sister, "INFY")
        assert ie._sister_restricted_symbols(db_with_sister) == {"TCS", "INFY"}

    def test_empty_sister_table_returns_empty_set(self, db_with_sister):
        assert ie._sister_restricted_symbols(db_with_sister) == set()

    def test_missing_sister_table_degrades_to_empty_set(self, db):
        # No CREATE TABLE this time — the sister DB is "unreachable" from
        # this service's point of view. Must never raise.
        assert ie._sister_restricted_symbols(db) == set()


# ── _sister_has_restriction ──────────────────────────────────────────────────

class TestSisterHasRestriction:
    def test_true_when_row_exists(self, db_with_sister):
        _insert_sister_row(db_with_sister, "RELIANCE")
        assert ie._sister_has_restriction(db_with_sister, "RELIANCE") is True

    def test_false_when_row_absent(self, db_with_sister):
        assert ie._sister_has_restriction(db_with_sister, "RELIANCE") is False

    def test_missing_sister_table_degrades_to_false(self, db):
        assert ie._sister_has_restriction(db, "RELIANCE") is False


# ── _record_sister_restriction ───────────────────────────────────────────────

class TestRecordSisterRestriction:
    def test_inserts_new_row_with_detail(self, db_with_sister):
        ie._record_sister_restriction(db_with_sister, "TCS", "security ASM stage")
        row = _sister_row(db_with_sister, "TCS")
        assert row is not None
        assert row[1] == 1  # hit_count
        assert row[2] == "security ASM stage"

    def test_inserts_new_row_without_detail(self, db_with_sister):
        ie._record_sister_restriction(db_with_sister, "TCS", None)
        row = _sister_row(db_with_sister, "TCS")
        assert row is not None
        assert row[2] is None

    def test_updates_existing_row_increments_hit_count_and_detail(self, db_with_sister):
        _insert_sister_row(db_with_sister, "TCS", hit_count=3, detail="old detail")
        ie._record_sister_restriction(db_with_sister, "TCS", "new detail")
        row = _sister_row(db_with_sister, "TCS")
        assert row[1] == 4
        assert row[2] == "new detail"

    def test_updates_existing_row_without_detail_leaves_last_detail_unchanged(self, db_with_sister):
        # The UPDATE statement only appends ", last_detail = :detail" when
        # detail is truthy — a None-detail rejection must not blow away a
        # detail string a previous rejection already recorded.
        _insert_sister_row(db_with_sister, "TCS", hit_count=1, detail="kept")
        ie._record_sister_restriction(db_with_sister, "TCS", None)
        row = _sister_row(db_with_sister, "TCS")
        assert row[1] == 2
        assert row[2] == "kept"

    def test_missing_sister_table_rolls_back_and_does_not_raise(self, db):
        # No exception should escape — this is a best-effort mirror only.
        ie._record_sister_restriction(db, "TCS", "some detail")
        # The session must still be usable afterward (rollback happened).
        db.query(models.IntradayRestrictedSecurity).count()


# ── record_restriction ───────────────────────────────────────────────────────

class TestRecordRestriction:
    def test_empty_symbol_is_a_noop(self, db_with_sister):
        ie.record_restriction(db_with_sister, "", "some detail")
        assert db_with_sister.query(models.IntradayRestrictedSecurity).count() == 0

    def test_new_symbol_creates_row_and_mirrors_to_sister(self, db_with_sister):
        ie.record_restriction(db_with_sister, "tcs", "security in ASM stage 2")
        row = db_with_sister.query(models.IntradayRestrictedSecurity).filter_by(symbol="TCS").first()
        assert row is not None
        assert row.hit_count == 1
        assert row.last_detail == "security in ASM stage 2"
        assert _sister_row(db_with_sister, "TCS") is not None

    def test_existing_symbol_increments_hit_count_and_updates_detail(self, db_with_sister):
        ie.record_restriction(db_with_sister, "TCS", "first rejection")
        ie.record_restriction(db_with_sister, "TCS", "second rejection")
        row = db_with_sister.query(models.IntradayRestrictedSecurity).filter_by(symbol="TCS").first()
        assert row.hit_count == 2
        assert row.last_detail == "second rejection"

    def test_existing_symbol_with_no_detail_keeps_prior_detail(self, db_with_sister):
        ie.record_restriction(db_with_sister, "TCS", "first rejection")
        ie.record_restriction(db_with_sister, "TCS", None)
        row = db_with_sister.query(models.IntradayRestrictedSecurity).filter_by(symbol="TCS").first()
        assert row.hit_count == 2
        assert row.last_detail == "first rejection"

    def test_detail_longer_than_255_chars_is_truncated(self, db_with_sister):
        long_detail = "x" * 400
        ie.record_restriction(db_with_sister, "TCS", long_detail)
        row = db_with_sister.query(models.IntradayRestrictedSecurity).filter_by(symbol="TCS").first()
        assert len(row.last_detail) == 255


# ── get_restricted_symbols ───────────────────────────────────────────────────

class TestGetRestrictedSymbols:
    def test_unions_own_and_sister_tables(self, db_with_sister):
        ie.record_restriction(db_with_sister, "TCS", "own-table hit")
        _insert_sister_row(db_with_sister, "INFY")
        assert ie.get_restricted_symbols(db_with_sister) == {"TCS", "INFY"}

    def test_own_fetch_exception_falls_back_to_sister_only(self, db_with_sister, monkeypatch):
        _insert_sister_row(db_with_sister, "INFY")

        def _raise(*a, **k):
            raise RuntimeError("own table unavailable")

        monkeypatch.setattr(db_with_sister, "query", _raise)
        assert ie.get_restricted_symbols(db_with_sister) == {"INFY"}


# ── is_restricted ─────────────────────────────────────────────────────────────

class TestIsRestricted:
    def test_empty_symbol_returns_false(self, db_with_sister):
        assert ie.is_restricted(db_with_sister, "") is False

    def test_true_when_in_own_table(self, db_with_sister):
        ie.record_restriction(db_with_sister, "TCS", "hit")
        assert ie.is_restricted(db_with_sister, "tcs") is True

    def test_true_when_in_sister_table_only(self, db_with_sister):
        _insert_sister_row(db_with_sister, "INFY")
        assert ie.is_restricted(db_with_sister, "INFY") is True

    def test_false_when_in_neither_table(self, db_with_sister):
        assert ie.is_restricted(db_with_sister, "RELIANCE") is False

    def test_own_lookup_exception_falls_through_to_sister_check(self, db_with_sister, monkeypatch):
        _insert_sister_row(db_with_sister, "INFY")

        def _raise(*a, **k):
            raise RuntimeError("own table unavailable")

        monkeypatch.setattr(db_with_sister, "query", _raise)
        assert ie.is_restricted(db_with_sister, "INFY") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
