"""
tests/test_shared_exposure.py

Covers execution/shared_exposure.py — previously 76%, missing lines 74-76
(publish_own_exposure's fail-open except branch) and 86-88
(get_other_service_exposure's fail-open except branch).

This module feeds risk_engine's "capital_share_cap": the value returned by
get_other_service_exposure() is added into the shared Dhan account's total
before this service's 50% cap is enforced (entry_engine/entry.py,
manual_engine.py, main.py's dry-run endpoint). Two properties matter and are
pinned here:

  1. FAIL-OPEN, BOTH DIRECTIONS — any DB/argument error is logged and
     swallowed; a failed read returns 0.0 (the old, over-restrictive-but-never-
     overspending behaviour), a failed publish never raises into equity_sync.
  2. CROSS-SERVICE AGREEMENT — this service and position-stocks-service each
     carry their own copy of this module + model and never import each other.
     If the table name or the two service-name strings ever drift apart the
     read silently returns 0.0 forever (undercounted total, no error). A drift
     guard at the bottom compares both copies textually.

Uses the repo-wide real in-memory SQLite convention (no mocking of the ORM for
the happy paths); MagicMock sessions only where a DB failure must be forced.

    cd services/real-trade-service
    python -m pytest tests/test_shared_exposure.py -v --cov=execution.shared_exposure --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import re
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from execution import shared_exposure as se
from models import SharedServiceExposure

LOGGER = "real-trade-shared-exposure"
ME = "real-trade-service"
OTHER = "position-stocks-service"


@pytest.fixture()
def db():
    """Fresh in-memory DB per test (StaticPool so every session sees it)."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def db_without_table():
    """A live session whose DB has NO exposure table -> every statement raises
    a real OperationalError (a genuine DB failure, not a mock)."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _row(db, name):
    return db.query(SharedServiceExposure).filter_by(service_name=name).first()


def _seed(db, name, value):
    db.add(SharedServiceExposure(service_name=name, open_positions_market_value=value))
    db.commit()


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]


# ───────────────────────────────── constants ─────────────────────────────────

def test_service_name_constants_are_the_documented_pair():
    assert se.SERVICE_NAME == ME
    assert se.OTHER_SERVICE_NAME == OTHER


# ───────────────────────────── publish_own_exposure ─────────────────────────────

class TestPublishOwnExposure:
    def test_creates_the_row_when_absent(self, db):
        se.publish_own_exposure(db, 12_345.67)
        row = _row(db, ME)
        assert row is not None
        assert row.open_positions_market_value == pytest.approx(12_345.67)
        assert row.updated_at is not None
        assert db.query(SharedServiceExposure).count() == 1

    def test_updates_in_place_never_duplicates(self, db):
        se.publish_own_exposure(db, 100.0)
        se.publish_own_exposure(db, 250.5)
        se.publish_own_exposure(db, 75.0)
        assert db.query(SharedServiceExposure).count() == 1
        assert _row(db, ME).open_positions_market_value == pytest.approx(75.0)

    def test_value_is_committed_and_visible_to_a_second_session(self, db):
        se.publish_own_exposure(db, 500.0)
        other_session = sessionmaker(bind=db.get_bind())()
        try:
            assert _row(other_session, ME).open_positions_market_value == pytest.approx(500.0)
        finally:
            other_session.close()

    def test_never_touches_the_other_services_row(self, db):
        _seed(db, OTHER, 999.0)
        se.publish_own_exposure(db, 42.0)
        assert _row(db, OTHER).open_positions_market_value == pytest.approx(999.0)
        assert _row(db, ME).open_positions_market_value == pytest.approx(42.0)

    def test_zero_is_a_valid_published_value(self, db):
        se.publish_own_exposure(db, 800.0)
        se.publish_own_exposure(db, 0.0)  # book fully closed
        assert _row(db, ME).open_positions_market_value == 0.0

    @pytest.mark.parametrize("raw,expected", [
        (-500.0, 0.0),            # negative market value is clamped
        (None, 0.0),              # `or 0.0` handles None
        (0, 0.0),
        (7, 7.0),                 # int coerced to float
        ("123.5", 123.5),         # numeric string coerced
        (float("nan"), 0.0),      # max(0.0, nan) -> 0.0, never persists NaN
    ])
    def test_input_normalisation(self, db, raw, expected):
        se.publish_own_exposure(db, raw)
        stored = _row(db, ME).open_positions_market_value
        assert stored == pytest.approx(expected)
        assert isinstance(stored, float)

    def test_bad_input_is_swallowed_logged_and_leaves_previous_value_intact(self, db, caplog):
        se.publish_own_exposure(db, 300.0)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            se.publish_own_exposure(db, "not-a-number")  # float() raises inside the try
        assert _row(db, ME).open_positions_market_value == pytest.approx(300.0)
        msgs = _warnings(caplog)
        assert len(msgs) == 1 and "failed to publish own exposure" in msgs[0]

    def test_real_db_failure_is_swallowed_and_logged(self, db_without_table, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert se.publish_own_exposure(db_without_table, 100.0) is None  # no raise
        msgs = _warnings(caplog)
        assert len(msgs) == 1 and "failed to publish own exposure" in msgs[0]

    def test_session_stays_usable_after_a_failed_publish(self, db_without_table):
        se.publish_own_exposure(db_without_table, 100.0)  # fails, rolls back
        # the session was rolled back, not left in a "pending rollback" state
        assert db_without_table.execute(text("select 1")).scalar() == 1

    def test_query_error_rolls_back_and_does_not_raise(self, caplog):
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("connection reset")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            se.publish_own_exposure(mock_db, 10.0)
        mock_db.rollback.assert_called_once()
        mock_db.commit.assert_not_called()
        assert "connection reset" in _warnings(caplog)[0]

    def test_commit_error_rolls_back_and_does_not_raise(self, caplog):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.commit.side_effect = RuntimeError("deadlock detected")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            se.publish_own_exposure(mock_db, 10.0)
        mock_db.add.assert_called_once()
        mock_db.rollback.assert_called_once()
        assert "deadlock detected" in _warnings(caplog)[0]

    def test_success_path_never_rolls_back(self):
        mock_db = MagicMock()
        existing = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        se.publish_own_exposure(mock_db, 55.0)
        assert existing.open_positions_market_value == 55.0
        mock_db.add.assert_not_called()      # existing row -> update, not insert
        mock_db.commit.assert_called_once()
        mock_db.rollback.assert_not_called()

    def test_failing_rollback_inside_the_handler_does_not_escape(self, caplog):
        # Contract: "Fail-open — never raises". A dead connection can make the
        # cleanup rollback() fail too; that must not propagate into
        # equity_sync.py (and from there whatever triggered the sync cycle).
        # Session112 round 8: mirrors position-stocks-service's round 7 test
        # for the identical bug in this service's copy of the module.
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("db down")
        mock_db.rollback.side_effect = RuntimeError("rollback failed too")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            se.publish_own_exposure(mock_db, 1.0)  # must not raise
        assert any("failed to publish own exposure" in w for w in _warnings(caplog))


# ─────────────────────────── get_other_service_exposure ───────────────────────────

class TestGetOtherServiceExposure:
    def test_no_row_yet_returns_zero(self, db):
        assert se.get_other_service_exposure(db) == 0.0

    def test_returns_the_other_services_published_value(self, db):
        _seed(db, OTHER, 48_250.75)
        assert se.get_other_service_exposure(db) == pytest.approx(48_250.75)

    def test_reads_only_the_other_services_row_not_its_own(self, db):
        _seed(db, ME, 111.0)
        assert se.get_other_service_exposure(db) == 0.0       # own row must not leak in
        _seed(db, OTHER, 222.0)
        assert se.get_other_service_exposure(db) == pytest.approx(222.0)

    def test_zero_stored_returns_zero_float(self, db):
        _seed(db, OTHER, 0.0)
        result = se.get_other_service_exposure(db)
        assert result == 0.0 and isinstance(result, float)

    def test_always_returns_a_float(self, db):
        _seed(db, OTHER, 5)  # stored as int-ish
        result = se.get_other_service_exposure(db)
        assert result == 5.0 and isinstance(result, float)

    def test_null_value_on_a_row_is_treated_as_zero(self):
        mock_db = MagicMock()
        row = MagicMock()
        row.open_positions_market_value = None
        mock_db.query.return_value.filter_by.return_value.first.return_value = row
        assert se.get_other_service_exposure(mock_db) == 0.0

    def test_real_db_failure_returns_zero_and_logs(self, db_without_table, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert se.get_other_service_exposure(db_without_table) == 0.0
        msgs = _warnings(caplog)
        assert len(msgs) == 1 and "failed to read other service's exposure" in msgs[0]

    def test_query_error_returns_zero_and_never_raises(self, caplog):
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("server closed the connection")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert se.get_other_service_exposure(mock_db) == 0.0
        assert "server closed the connection" in _warnings(caplog)[0]

    def test_non_numeric_stored_value_returns_zero(self):
        mock_db = MagicMock()
        row = MagicMock()
        row.open_positions_market_value = "garbage"
        mock_db.query.return_value.filter_by.return_value.first.return_value = row
        assert se.get_other_service_exposure(mock_db) == 0.0  # float() raises -> fail-open

    def test_round_trip_publish_then_read_from_the_other_side(self, db):
        """Simulates position-stocks-service publishing under ITS name via the
        same table, then this service reading it back."""
        db.add(SharedServiceExposure(service_name=OTHER, open_positions_market_value=0.0))
        db.commit()
        db.query(SharedServiceExposure).filter_by(service_name=OTHER).update(
            {"open_positions_market_value": 61_000.0})
        db.commit()
        se.publish_own_exposure(db, 30_000.0)
        assert se.get_other_service_exposure(db) == pytest.approx(61_000.0)


# ───────────────────── cross-service drift guard (textual) ─────────────────────

_SERVICES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SIBLING_MODULE = os.path.join(_SERVICES_DIR, "position-stocks-service", "capital", "shared_exposure.py")
_SIBLING_MODELS = os.path.join(_SERVICES_DIR, "position-stocks-service", "models.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(not (os.path.exists(_SIBLING_MODULE) and os.path.exists(_SIBLING_MODELS)),
                    reason="position-stocks-service not present next to this service")
class TestCrossServiceDriftGuard:
    """The two services never import each other, so nothing but this test
    notices if one copy is renamed. A mismatch = every cross-service read
    silently returns 0.0 (undercounted account total, no error anywhere)."""

    def test_service_names_are_mirror_images(self):
        src = _read(_SIBLING_MODULE)
        their_self = re.search(r'^SERVICE_NAME\s*=\s*"([^"]+)"', src, re.M).group(1)
        their_other = re.search(r'^OTHER_SERVICE_NAME\s*=\s*"([^"]+)"', src, re.M).group(1)
        assert their_self == se.OTHER_SERVICE_NAME
        assert their_other == se.SERVICE_NAME

    def test_both_models_map_the_same_table_and_key_column(self):
        theirs = _read(_SIBLING_MODELS)
        block = theirs[theirs.index("class SharedServiceExposure"):]
        block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
        assert re.search(r'__tablename__\s*=\s*"([^"]+)"', block).group(1) == \
            SharedServiceExposure.__tablename__
        assert "service_name = Column(String(32), primary_key=True)" in block
        assert "open_positions_market_value = Column(Float, nullable=False, default=0.0)" in block

    def test_service_names_fit_the_shared_key_column(self):
        width = SharedServiceExposure.__table__.c.service_name.type.length
        assert len(ME) <= width and len(OTHER) <= width
