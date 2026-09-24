"""
tests/test_shared_exposure.py

Closes the coverage gaps in capital/shared_exposure.py (36% → 100%,
16 missing lines: 64-73, 80-85) and pins the behaviour of both functions.

This is this service's half of the cross-service exposure table
(`stockky_shared_service_exposure`): `publish_own_exposure` is called at the
tail of `ledger.sync_from_broker`, and real-trade-service reads the value back
to complete the account total its 50% capital_share_cap is checked against.
Until now it was only ever exercised through a stub in the ledger tests.

Properties pinned:

  * UPSERT — one row per service, updated in place, committed (visible to a
    second session), and never touching the other service's row.
  * FAIL-OPEN, BOTH DIRECTIONS — a failed publish never raises into the
    ledger sync (including when `rollback()` itself fails inside the
    handler); a failed read returns 0.0.
  * CROSS-SERVICE AGREEMENT — the two services never import each other, so a
    drift guard compares service names, table name and key column against
    real-trade-service's copy textually (skipped if that service isn't next
    to this one).

Uses the repo-wide real in-memory SQLite convention; MagicMock sessions only
where a DB failure must be forced.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_shared_exposure.py -q \\
        --cov=capital.shared_exposure --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from capital import ledger, shared_exposure as se
from execution import dhan_client
from models import SharedServiceExposure

LOGGER = "position-stocks-shared-exposure"
ME = "position-stocks-service"
OTHER = "real-trade-service"


def _engine():
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


@pytest.fixture()
def db():
    """Fresh in-memory DB per test (StaticPool so every session sees it)."""
    engine = _engine()
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def db_pair():
    """Two independent sessions on the same DB — to prove commits are real."""
    engine = _engine()
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    a, b = Session(), Session()
    yield a, b
    a.close()
    b.close()


@pytest.fixture()
def db_without_table():
    """A live session whose DB has NO exposure table → every statement raises
    a real OperationalError (a genuine DB failure, not a mock)."""
    s = sessionmaker(bind=_engine())()
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


# ══════════════════════════════════════════════════════════════════════════════
# constants
# ══════════════════════════════════════════════════════════════════════════════

def test_service_name_constants_are_the_documented_pair():
    assert se.SERVICE_NAME == ME
    assert se.OTHER_SERVICE_NAME == OTHER


# ══════════════════════════════════════════════════════════════════════════════
# publish_own_exposure
# ══════════════════════════════════════════════════════════════════════════════

class TestPublishOwnExposure:
    def test_creates_the_row_when_absent(self, db):
        se.publish_own_exposure(db, 12_345.5)
        assert _row(db, ME).open_positions_market_value == pytest.approx(12_345.5)

    def test_updates_in_place_never_duplicates(self, db):
        se.publish_own_exposure(db, 100.0)
        se.publish_own_exposure(db, 250.0)
        rows = db.query(SharedServiceExposure).filter_by(service_name=ME).all()
        assert len(rows) == 1
        assert rows[0].open_positions_market_value == pytest.approx(250.0)

    def test_value_is_committed_and_visible_to_a_second_session(self, db_pair):
        a, b = db_pair
        se.publish_own_exposure(a, 777.0)
        assert _row(b, ME).open_positions_market_value == pytest.approx(777.0)

    def test_never_touches_the_other_services_row(self, db):
        _seed(db, OTHER, 999.0)
        se.publish_own_exposure(db, 5.0)
        assert _row(db, OTHER).open_positions_market_value == pytest.approx(999.0)
        assert _row(db, ME).open_positions_market_value == pytest.approx(5.0)

    def test_zero_is_a_valid_published_value(self, db):
        # A flat book must overwrite a previous non-zero value, not skip it.
        se.publish_own_exposure(db, 500.0)
        se.publish_own_exposure(db, 0.0)
        assert _row(db, ME).open_positions_market_value == pytest.approx(0.0)

    @pytest.mark.parametrize("raw, expected", [
        (None, 0.0),        # `or 0.0`
        (0, 0.0),
        (-50.0, 0.0),       # clamped: exposure can't be negative
        (-0.01, 0.0),
        ("12.5", 12.5),     # numeric string coerced
        (3, 3.0),
    ])
    def test_input_normalisation(self, db, raw, expected):
        se.publish_own_exposure(db, raw)
        stored = _row(db, ME).open_positions_market_value
        assert stored == pytest.approx(expected)
        assert isinstance(stored, float)

    def test_bad_input_is_swallowed_logged_and_leaves_previous_value_intact(self, db, caplog):
        se.publish_own_exposure(db, 321.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            se.publish_own_exposure(db, "not-a-number")  # must not raise
        assert _row(db, ME).open_positions_market_value == pytest.approx(321.0)
        assert any("failed to publish own exposure" in w for w in _warnings(caplog))

    def test_bad_input_on_first_publish_leaves_no_half_written_row(self, db):
        se.publish_own_exposure(db, "not-a-number")
        assert _row(db, ME) is None  # the pending INSERT was rolled back

    def test_real_db_failure_is_swallowed_and_logged(self, db_without_table, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            se.publish_own_exposure(db_without_table, 10.0)  # must not raise
        assert any("failed to publish own exposure" in w for w in _warnings(caplog))

    def test_session_stays_usable_after_a_failed_publish(self, db):
        se.publish_own_exposure(db, "not-a-number")
        se.publish_own_exposure(db, 42.0)  # would raise if left "pending rollback"
        assert _row(db, ME).open_positions_market_value == pytest.approx(42.0)

    def test_query_error_rolls_back_and_does_not_raise(self, caplog):
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            se.publish_own_exposure(mock_db, 1.0)
        mock_db.rollback.assert_called_once()
        assert any("db down" in w for w in _warnings(caplog))

    def test_commit_error_rolls_back_and_does_not_raise(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.commit.side_effect = RuntimeError("disk full")
        se.publish_own_exposure(mock_db, 1.0)
        mock_db.rollback.assert_called_once()

    def test_success_path_never_rolls_back(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = SharedServiceExposure(
            service_name=ME, open_positions_market_value=0.0,
        )
        se.publish_own_exposure(mock_db, 5.0)
        mock_db.commit.assert_called_once()
        mock_db.rollback.assert_not_called()

    def test_failing_rollback_inside_the_handler_does_not_escape(self, caplog):
        # Contract: "Fail-open — never raises". A dead connection can make the
        # cleanup rollback() fail too; that must not propagate into
        # ledger.sync_from_broker (and from there POST /ledger/sync).
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("db down")
        mock_db.rollback.side_effect = RuntimeError("rollback failed too")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            se.publish_own_exposure(mock_db, 1.0)  # must not raise
        assert any("failed to publish own exposure" in w for w in _warnings(caplog))


# ══════════════════════════════════════════════════════════════════════════════
# get_other_service_exposure
# ══════════════════════════════════════════════════════════════════════════════

class TestGetOtherServiceExposure:
    def test_no_row_yet_returns_zero_without_a_warning(self, db, caplog):
        # "peer hasn't published yet" is normal, not an error worth logging.
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure(db) == 0.0
        assert _warnings(caplog) == []

    def test_returns_the_other_services_published_value(self, db):
        _seed(db, OTHER, 4_200.75)
        assert se.get_other_service_exposure(db) == pytest.approx(4_200.75)

    def test_reads_only_the_other_services_row_not_its_own(self, db):
        _seed(db, ME, 111.0)
        assert se.get_other_service_exposure(db) == 0.0
        _seed(db, OTHER, 222.0)
        assert se.get_other_service_exposure(db) == pytest.approx(222.0)

    def test_zero_stored_returns_zero_float(self, db):
        _seed(db, OTHER, 0.0)
        v = se.get_other_service_exposure(db)
        assert v == 0.0 and isinstance(v, float)

    def test_always_returns_a_float(self, db):
        _seed(db, OTHER, 7)
        assert isinstance(se.get_other_service_exposure(db), float)

    def test_null_value_on_a_row_is_treated_as_zero_without_a_warning(self, caplog):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = MagicMock(
            open_positions_market_value=None,
        )
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure(mock_db) == 0.0
        assert _warnings(caplog) == []

    def test_real_db_failure_returns_zero_and_logs(self, db_without_table, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure(db_without_table) == 0.0
        assert any("failed to read other service's exposure" in w for w in _warnings(caplog))

    def test_query_error_returns_zero_and_never_raises(self, caplog):
        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure(mock_db) == 0.0
        assert any("db down" in w for w in _warnings(caplog))

    def test_non_numeric_stored_value_returns_zero(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = MagicMock(
            open_positions_market_value="garbage",
        )
        assert se.get_other_service_exposure(mock_db) == 0.0

    def test_round_trip_publish_then_read_from_the_other_side(self, db):
        # real-trade-service's copy publishes under ITS name; ours must read it.
        db.add(SharedServiceExposure(service_name=OTHER, open_positions_market_value=1_500.0))
        db.commit()
        se.publish_own_exposure(db, 800.0)
        assert se.get_other_service_exposure(db) == pytest.approx(1_500.0)
        assert _row(db, ME).open_positions_market_value == pytest.approx(800.0)


# ══════════════════════════════════════════════════════════════════════════════
# wiring: ledger.sync_from_broker → publish_own_exposure (real, unstubbed)
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerSyncPublishesRealExposure:
    def _run_sync(self, db, monkeypatch):
        monkeypatch.setattr(ledger, "sync_peer_pnl", lambda d: None)
        monkeypatch.setattr(dhan_client, "get_funds", lambda d: {"availabelBalance": 200_000.0})
        monkeypatch.setattr(ledger, "_last_balance_key", None)
        return ledger.sync_from_broker(db)

    def _position(self, db, symbol, status, capital, sec_id):
        db.add(models.ScalpPosition(
            symbol=symbol, window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
            status=status, quantity=10, entry_price=500.0, capital_risked=capital,
            overnight_converted_to_cnc=False, dhan_security_id=sec_id,
            target_price=510.0, stop_price=490.0, opened_at=datetime.now(timezone.utc),
        ))
        db.commit()

    def test_sync_publishes_committed_capital_of_open_positions(self, db, monkeypatch):
        self._position(db, "A", "OPEN", 5_000.0, "1")
        self._position(db, "B", "EXIT_LEGS_REJECTED", 3_000.0, "2")
        self._position(db, "C", "CLOSED", 9_999.0, "3")
        self._run_sync(db, monkeypatch)
        assert _row(db, ME).open_positions_market_value == pytest.approx(8_000.0)

    def test_sync_with_flat_book_publishes_zero(self, db, monkeypatch):
        se.publish_own_exposure(db, 1_234.0)  # stale value from a previous cycle
        self._run_sync(db, monkeypatch)
        assert _row(db, ME).open_positions_market_value == pytest.approx(0.0)

    def test_publish_failure_does_not_break_the_sync(self, db, monkeypatch):
        # Break the exposure write for real (table gone): the ledger sync must
        # still complete and return the scalp allocation.
        models.SharedServiceExposure.__table__.drop(db.get_bind())
        result = self._run_sync(db, monkeypatch)
        expected = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        assert result == pytest.approx(expected)
        assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(expected)


# ══════════════════════════════════════════════════════════════════════════════
# cross-service drift guard
# ══════════════════════════════════════════════════════════════════════════════

_SERVICES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SIBLING_MODULE = os.path.join(_SERVICES_DIR, "real-trade-service", "execution", "shared_exposure.py")
_SIBLING_MODELS = os.path.join(_SERVICES_DIR, "real-trade-service", "models.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(not (os.path.exists(_SIBLING_MODULE) and os.path.exists(_SIBLING_MODELS)),
                    reason="real-trade-service not present next to this service")
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
