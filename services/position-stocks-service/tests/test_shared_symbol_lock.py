"""
tests/test_shared_symbol_lock.py

Closes the coverage gaps in capital/shared_symbol_lock.py (30% → 100%,
65 missing lines: 54, 63-86, 104-109, 115-128, 137-159, 170-200) and pins the
behaviour of every public function.

test_entry.py / test_reconcile.py / test_eod_squareoff.py only ever exercise
this module through mocks of its callers; nothing tested the module itself.

This is the cross-service guard that stops position-stocks-service and
real-trade-service both holding a live position in the same symbol on the one
shared Dhan account (AEGISVOPAK, 17 Sept). Two properties matter and are
tested from both directions:

  * BLOCK — a symbol held by the other service must refuse our BUY (including
    when we only find out via the unique-constraint race on INSERT).
  * FAIL OPEN — any unexpected DB error must ALLOW the order and never raise
    out of release / status / force_release / cleanup_stale, because a broken
    lock must never itself block a real entry or, especially, a real exit.

Grouped by function:
  try_claim     — fresh claim (+ symbol normalisation), already-ours, held by
                  peer, REAL IntegrityError race (lost / won-by-self), race
                  where the winner vanished or the re-read fails, generic
                  error fail-open (+ rollback failure swallowed)
  release       — deletes only our own row, never the peer's, no-op when
                  absent, normalisation, error swallowing
  status        — empty, both services listed with ISO timestamps, error → []
  force_release — deletes either service's row, False when absent, warning
                  names the holder, error → False
  cleanup_stale — releases our locks with no OPEN / EXIT_LEGS_REJECTED
                  position, keeps live ones, never touches the peer's, single
                  commit only when something was released, error paths

Run from services/position-stocks-service:
    python3 -m pytest tests/test_shared_symbol_lock.py -q \\
        --cov=capital.shared_symbol_lock --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import models
from capital import shared_symbol_lock as sl

LOGGER = "position-stocks-shared-symbol-lock"
US = "position-stocks-service"
PEER = "real-trade-service"


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    yield session
    session.close()


def _lock(db, symbol, service=US, mode=None, claimed_at=None):
    row = models.SharedSymbolLock(symbol=symbol, held_by_service=service, held_by_mode=mode)
    if claimed_at is not None:
        row.claimed_at = claimed_at
    db.add(row)
    db.commit()
    return row


def _position(db, symbol, status="OPEN", sec_id="1"):
    p = models.ScalpPosition(
        symbol=symbol, window_source="5m",
        adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        status=status, quantity=10, entry_price=500.0, capital_risked=5000.0,
        overnight_converted_to_cnc=False, dhan_security_id=sec_id,
        target_price=510.0, stop_price=490.0,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.commit()
    return p


def _symbols(db):
    return sorted(r.symbol for r in db.query(models.SharedSymbolLock).all())


def _integrity_error():
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def _hide_first_lookup(db):
    """Make the FIRST db.query(...).filter_by(...).first() report 'no row'
    even though one exists — reproduces the SELECT-then-INSERT race window.
    Every later query goes to the real session."""
    real_query = db.query
    state = {"n": 0}

    class _Empty:
        def filter_by(self, **kw):
            return self

        def first(self):
            return None

    def q(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            return _Empty()
        return real_query(*a, **k)

    db.query = q


def _broken_db():
    """Session double where every query blows up and rollback also fails."""
    m = MagicMock()
    m.query.side_effect = RuntimeError("db down")
    m.rollback.side_effect = RuntimeError("rollback failed too")
    return m


# ══════════════════════════════════════════════════════════════════════════════
# try_claim
# ══════════════════════════════════════════════════════════════════════════════

class TestTryClaim:
    def test_fresh_symbol_is_claimed(self, db):
        assert sl.try_claim(db, "ABC") is True
        row = db.query(models.SharedSymbolLock).filter_by(symbol="ABC").one()
        assert row.held_by_service == US
        assert row.held_by_mode is None
        assert row.claimed_at is not None

    def test_symbol_is_stripped_and_uppercased(self, db):
        assert sl.try_claim(db, "  abc \n") is True
        assert _symbols(db) == ["ABC"]

    def test_lowercase_lookup_finds_existing_upper_row(self, db):
        _lock(db, "ABC", service=PEER, mode="REAL")
        assert sl.try_claim(db, "abc") is False

    def test_already_ours_returns_true_without_duplicate_row(self, db):
        _lock(db, "ABC", service=US)
        assert sl.try_claim(db, "ABC") is True
        assert _symbols(db) == ["ABC"]

    def test_held_by_peer_is_blocked_and_row_untouched(self, db, caplog):
        _lock(db, "ABC", service=PEER, mode="REAL")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sl.try_claim(db, "ABC") is False
        assert "BLOCKED buy of ABC" in caplog.text
        assert PEER in caplog.text and "mode=REAL" in caplog.text
        row = db.query(models.SharedSymbolLock).filter_by(symbol="ABC").one()
        assert row.held_by_service == PEER and row.held_by_mode == "REAL"

    def test_other_symbols_are_unaffected_by_a_peer_lock(self, db):
        _lock(db, "ABC", service=PEER)
        assert sl.try_claim(db, "XYZ") is True
        assert _symbols(db) == ["ABC", "XYZ"]

    def test_real_unique_constraint_race_lost_to_peer(self, db, caplog):
        # SELECT sees nothing, peer's row is there by INSERT time → the real
        # UNIQUE(symbol) constraint raises IntegrityError.
        _lock(db, "ABC", service=PEER, mode="REAL")
        db.expunge_all()
        _hide_first_lookup(db)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sl.try_claim(db, "ABC") is False
        assert "lost race to real-trade-service" in caplog.text
        rows = db.query(models.SharedSymbolLock).all()
        assert len(rows) == 1 and rows[0].held_by_service == PEER

    def test_real_unique_constraint_race_won_by_ourselves_is_allowed(self, db):
        # A concurrent request in THIS service claimed it first — not a conflict.
        _lock(db, "ABC", service=US)
        db.expunge_all()
        _hide_first_lookup(db)
        assert sl.try_claim(db, "ABC") is True
        assert _symbols(db) == ["ABC"]

    def test_race_rolls_back_the_failed_insert(self, db):
        # After the lost race the session must still be usable (rolled back).
        _lock(db, "ABC", service=PEER)
        db.expunge_all()
        _hide_first_lookup(db)
        assert sl.try_claim(db, "ABC") is False
        assert sl.try_claim(db, "NEW") is True  # would raise if session left poisoned
        assert _symbols(db) == ["ABC", "NEW"]

    def test_race_but_winner_row_vanished_fails_open(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [None, None]
        db.commit.side_effect = _integrity_error()
        assert sl.try_claim(db, "ABC") is True
        db.rollback.assert_called_once()

    def test_race_and_reread_failure_fails_open(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [
            None, RuntimeError("reread failed"),
        ]
        db.commit.side_effect = _integrity_error()
        assert sl.try_claim(db, "ABC") is True

    def test_unexpected_error_fails_open_logs_and_rolls_back(self, caplog):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert sl.try_claim(db, "abc") is True
        assert "failing open" in caplog.text and "ABC" in caplog.text
        db.rollback.assert_called_once()

    def test_unexpected_error_with_failing_rollback_still_fails_open(self):
        assert sl.try_claim(_broken_db(), "ABC") is True

    def test_commit_error_other_than_integrity_fails_open(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.commit.side_effect = RuntimeError("disk full")
        assert sl.try_claim(db, "ABC") is True
        db.rollback.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# release
# ══════════════════════════════════════════════════════════════════════════════

class TestRelease:
    def test_deletes_our_row(self, db):
        _lock(db, "ABC", service=US)
        sl.release(db, "ABC")
        assert _symbols(db) == []

    def test_deletion_is_committed_not_just_flushed(self, db):
        _lock(db, "ABC", service=US)
        sl.release(db, "ABC")
        db.rollback()
        assert _symbols(db) == []

    def test_never_touches_the_peers_row(self, db):
        _lock(db, "ABC", service=PEER, mode="REAL")
        sl.release(db, "ABC")
        rows = db.query(models.SharedSymbolLock).all()
        assert len(rows) == 1 and rows[0].held_by_service == PEER

    def test_only_the_named_symbol_is_released(self, db):
        _lock(db, "ABC", service=US)
        _lock(db, "XYZ", service=US)
        sl.release(db, "ABC")
        assert _symbols(db) == ["XYZ"]

    def test_absent_symbol_is_a_no_op(self, db):
        sl.release(db, "NOPE")  # must not raise
        assert _symbols(db) == []

    def test_symbol_is_normalised(self, db):
        _lock(db, "ABC", service=US)
        sl.release(db, "  abc ")
        assert _symbols(db) == []

    def test_release_then_claim_round_trip(self, db):
        assert sl.try_claim(db, "ABC") is True
        sl.release(db, "ABC")
        _lock(db, "ABC", service=PEER)  # peer can now take it
        assert sl.try_claim(db, "ABC") is False

    def test_error_is_swallowed_logged_and_rolled_back(self, caplog):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            sl.release(db, "ABC")  # must not raise
        assert "release(ABC) failed" in caplog.text
        db.rollback.assert_called_once()

    def test_error_with_failing_rollback_is_still_swallowed(self):
        sl.release(_broken_db(), "ABC")

    def test_delete_commit_failure_is_swallowed(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = object()
        db.commit.side_effect = RuntimeError("commit failed")
        sl.release(db, "ABC")
        db.delete.assert_called_once()
        db.rollback.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# status
# ══════════════════════════════════════════════════════════════════════════════

class TestStatus:
    def test_empty(self, db):
        assert sl.status(db) == []

    def test_lists_both_services_with_iso_timestamps(self, db):
        _lock(db, "ABC", service=US, claimed_at=datetime(2026, 9, 25, 4, 7, 0))
        _lock(db, "XYZ", service=PEER, mode="REAL", claimed_at=datetime(2026, 9, 25, 5, 0, 0))
        out = {d["symbol"]: d for d in sl.status(db)}
        assert set(out) == {"ABC", "XYZ"}
        assert out["ABC"] == {
            "symbol": "ABC", "held_by_service": US, "held_by_mode": None,
            "claimed_at": "2026-09-25T04:07:00",
        }
        assert out["XYZ"]["held_by_service"] == PEER
        assert out["XYZ"]["held_by_mode"] == "REAL"
        assert out["XYZ"]["claimed_at"] == "2026-09-25T05:00:00"

    def test_missing_claimed_at_is_none(self):
        db = MagicMock()
        db.query.return_value.all.return_value = [
            SimpleNamespace(symbol="ABC", held_by_service=US, held_by_mode=None, claimed_at=None),
        ]
        assert sl.status(db)[0]["claimed_at"] is None

    def test_error_returns_empty_list_and_logs(self, caplog):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert sl.status(db) == []
        assert "status failed" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# force_release
# ══════════════════════════════════════════════════════════════════════════════

class TestForceRelease:
    def test_deletes_our_own_row(self, db):
        _lock(db, "ABC", service=US)
        assert sl.force_release(db, "ABC") is True
        assert _symbols(db) == []

    def test_deletes_the_peers_row_too_and_warns_with_holder(self, db, caplog):
        _lock(db, "ABC", service=PEER, mode="REAL")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert sl.force_release(db, "ABC") is True
        assert _symbols(db) == []
        assert "force_release(ABC)" in caplog.text
        assert PEER in caplog.text and "mode=REAL" in caplog.text

    def test_deletion_is_committed_not_just_flushed(self, db):
        _lock(db, "ABC", service=PEER)
        assert sl.force_release(db, "ABC") is True
        db.rollback()  # would resurrect the row if the delete were only flushed
        assert _symbols(db) == []

    def test_absent_symbol_returns_false(self, db):
        assert sl.force_release(db, "NOPE") is False

    def test_only_named_symbol_is_deleted(self, db):
        _lock(db, "ABC", service=US)
        _lock(db, "XYZ", service=PEER)
        assert sl.force_release(db, "abc ") is True
        assert _symbols(db) == ["XYZ"]

    def test_error_returns_false_logs_and_rolls_back(self, caplog):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert sl.force_release(db, "ABC") is False
        assert "force_release(ABC) failed" in caplog.text
        db.rollback.assert_called_once()

    def test_error_with_failing_rollback_still_returns_false(self):
        assert sl.force_release(_broken_db(), "ABC") is False


# ══════════════════════════════════════════════════════════════════════════════
# cleanup_stale
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanupStale:
    def test_releases_our_lock_with_no_position(self, db, caplog):
        _lock(db, "ABC", service=US)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            released = sl.cleanup_stale(db)
        assert released == ["ABC"]
        assert _symbols(db) == []
        assert "released stale lock for ABC" in caplog.text

    def test_keeps_lock_with_open_position(self, db):
        _lock(db, "ABC", service=US)
        _position(db, "ABC", status="OPEN")
        assert sl.cleanup_stale(db) == []
        assert _symbols(db) == ["ABC"]

    def test_keeps_lock_with_exit_legs_rejected_position(self, db):
        # Stuck position still has real shares at the broker — lock must stay.
        _lock(db, "ABC", service=US)
        _position(db, "ABC", status="EXIT_LEGS_REJECTED")
        assert sl.cleanup_stale(db) == []
        assert _symbols(db) == ["ABC"]

    @pytest.mark.parametrize("status", ["CLOSED", "TARGET_HIT", "STOP_HIT", "EOD_SQUAREOFF", "MANUAL_EXIT", "ERROR"])
    def test_releases_lock_when_only_terminal_positions_exist(self, db, status):
        _lock(db, "ABC", service=US)
        _position(db, "ABC", status=status)
        assert sl.cleanup_stale(db) == ["ABC"]
        assert _symbols(db) == []

    def test_open_position_in_a_different_symbol_does_not_protect_the_lock(self, db):
        _lock(db, "ABC", service=US)
        _position(db, "XYZ", status="OPEN", sec_id="2")
        assert sl.cleanup_stale(db) == ["ABC"]

    def test_never_touches_the_peers_locks(self, db):
        _lock(db, "PEERSYM", service=PEER, mode="REAL")  # no position of ours, but not ours
        _lock(db, "ABC", service=US)
        assert sl.cleanup_stale(db) == ["ABC"]
        assert _symbols(db) == ["PEERSYM"]

    def test_mixed_batch_releases_only_the_stale_ones(self, db):
        for sym, sec in (("LIVE1", "1"), ("STALE1", "2"), ("LIVE2", "3"), ("STALE2", "4")):
            _lock(db, sym, service=US)
        _position(db, "LIVE1", "OPEN", "1")
        _position(db, "LIVE2", "EXIT_LEGS_REJECTED", "3")
        _position(db, "STALE2", "CLOSED", "4")
        assert sorted(sl.cleanup_stale(db)) == ["STALE1", "STALE2"]
        assert _symbols(db) == ["LIVE1", "LIVE2"]

    def test_no_locks_returns_empty(self, db):
        assert sl.cleanup_stale(db) == []

    def test_commits_exactly_once_for_a_batch(self, db, monkeypatch):
        _lock(db, "A", service=US)
        _lock(db, "B", service=US)
        real_commit = db.commit
        calls = []
        monkeypatch.setattr(db, "commit", lambda: (calls.append(1), real_commit())[1])
        assert sorted(sl.cleanup_stale(db)) == ["A", "B"]
        assert len(calls) == 1

    def test_does_not_commit_when_nothing_was_released(self, db, monkeypatch):
        _lock(db, "ABC", service=US)
        _position(db, "ABC", status="OPEN")
        calls = []
        monkeypatch.setattr(db, "commit", lambda: calls.append(1))
        assert sl.cleanup_stale(db) == []
        assert calls == []

    def test_idempotent_second_run_releases_nothing(self, db):
        _lock(db, "ABC", service=US)
        assert sl.cleanup_stale(db) == ["ABC"]
        assert sl.cleanup_stale(db) == []

    def test_query_error_returns_empty_logs_and_rolls_back(self, caplog):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert sl.cleanup_stale(db) == []
        assert "cleanup_stale failed" in caplog.text
        db.rollback.assert_called_once()

    def test_error_with_failing_rollback_is_still_swallowed(self):
        assert sl.cleanup_stale(_broken_db()) == []

    def test_commit_failure_is_swallowed_and_rolled_back(self, db, monkeypatch):
        _lock(db, "ABC", service=US)
        rolled = []
        real_rollback = db.rollback
        monkeypatch.setattr(db, "commit", MagicMock(side_effect=RuntimeError("commit failed")))
        monkeypatch.setattr(db, "rollback", lambda: (rolled.append(1), real_rollback())[1])
        sl.cleanup_stale(db)  # must not raise
        assert rolled == [1]
        assert _symbols(db) == ["ABC"]  # rollback undid the pending delete
