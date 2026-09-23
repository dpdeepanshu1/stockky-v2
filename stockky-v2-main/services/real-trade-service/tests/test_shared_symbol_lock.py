"""
tests/test_shared_symbol_lock.py

100%-coverage-plan follow-up: execution/shared_symbol_lock.py -- 41% -> target 100%.

This is the cross-service claim that stops real-trade-service and
position-stocks-service from both holding a live broker position in the
same symbol at once (the AEGISVOPAK double-buy incident, session60).
Existing indirect coverage (via entry.py / portfolio.py / exit.py tests)
only ever exercised the "claim a fresh, unheld symbol" happy path. This
file directly tests every other branch:

  - try_claim(): already-ours (no-op True), already held by the OTHER
    service (blocked, False), the IntegrityError race-lost-to-other-
    service branch, the IntegrityError race-but-it-was-actually-ours
    branch, and the generic fail-open exception branch.
  - release(): the non-blocking exception branch (existing tests only
    cover the normal delete-and-commit path).
  - status(): not exercised anywhere before this file -- both the
    normal snapshot shape and its fail-safe (return []) exception branch.

Run from services/real-trade-service:
    python3 -m pytest tests/test_shared_symbol_lock.py -q \
        --cov=execution.shared_symbol_lock --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import models
from execution import shared_symbol_lock as ssl

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


class _FakeQueryResult:
    """Stand-in for the db.query(...).filter_by(...).first() chain, so
    IntegrityError-race tests can control exactly what each successive
    query call sees without depending on real concurrent-connection
    semantics (sqlite in-memory engines don't reliably share state across
    connections here, so a genuine two-connection race isn't practical to
    simulate deterministically)."""
    def __init__(self, result):
        self._result = result

    def filter_by(self, **kw):
        return self

    def first(self):
        return self._result


def _racy_commit():
    raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


class TestTryClaim:
    def test_claims_fresh_symbol(self, db):
        assert ssl.try_claim(db, "reliance", mode="REAL") is True
        row = db.query(models.SharedSymbolLock).filter_by(symbol="RELIANCE").first()
        assert row is not None
        assert row.held_by_service == "real-trade-service"
        assert row.held_by_mode == "REAL"

    def test_reclaiming_own_symbol_is_a_noop_true(self, db):
        db.add(models.SharedSymbolLock(symbol="TCS", held_by_service="real-trade-service", held_by_mode="REAL"))
        db.commit()
        assert ssl.try_claim(db, "tcs") is True
        # still exactly one row -- not duplicated
        assert db.query(models.SharedSymbolLock).filter_by(symbol="TCS").count() == 1

    def test_blocked_when_held_by_other_service(self, db, caplog):
        db.add(models.SharedSymbolLock(
            symbol="AEGISVOPAK", held_by_service="position-stocks-service",
            held_by_mode=None, claimed_at=datetime.now(timezone.utc),
        ))
        db.commit()
        with caplog.at_level(logging.WARNING, logger="real-trade-shared-symbol-lock"):
            result = ssl.try_claim(db, "aegisvopak")
        assert result is False
        assert any("symbol lock BLOCKED buy of AEGISVOPAK" in r.message for r in caplog.records)

    def test_integrity_error_race_lost_to_other_service(self, db, monkeypatch, caplog):
        """Simulates position-stocks-service winning a concurrent insert
        race for the same symbol between our SELECT and our INSERT: our
        initial existence check sees nothing, our own insert then hits the
        unique constraint, and the post-rollback re-SELECT finds the
        competitor's row."""
        other_row = models.SharedSymbolLock(
            symbol="NEWSYMBOL", held_by_service="position-stocks-service",
            held_by_mode=None, claimed_at=datetime.now(timezone.utc),
        )
        calls = {"n": 0}

        def fake_query(model):
            calls["n"] += 1
            return _FakeQueryResult(None if calls["n"] == 1 else other_row)

        monkeypatch.setattr(db, "query", fake_query)
        monkeypatch.setattr(db, "commit", _racy_commit)
        with caplog.at_level(logging.WARNING, logger="real-trade-shared-symbol-lock"):
            result = ssl.try_claim(db, "newsymbol")
        assert result is False
        assert any("lost race to position-stocks-service" in r.message for r in caplog.records)

    def test_integrity_error_race_but_row_is_actually_ours(self, db, monkeypatch):
        """If the re-SELECT after an IntegrityError finds a row already
        owned by us (e.g. a retried request racing itself), try_claim must
        still return True rather than falsely blocking."""
        our_row = models.SharedSymbolLock(
            symbol="INFY", held_by_service="real-trade-service", held_by_mode="REAL",
            claimed_at=datetime.now(timezone.utc),
        )
        calls = {"n": 0}

        def fake_query(model):
            calls["n"] += 1
            return _FakeQueryResult(None if calls["n"] == 1 else our_row)

        monkeypatch.setattr(db, "query", fake_query)
        monkeypatch.setattr(db, "commit", _racy_commit)
        assert ssl.try_claim(db, "infy") is True

    def test_integrity_error_and_reselect_itself_fails_still_returns_true(self, db, monkeypatch):
        """If the re-SELECT inside the except IntegrityError block also
        raises, that inner exception is swallowed and the function still
        fails open (True) -- a secondary error must never escape."""
        calls = {"n": 0}

        def flaky_query(model):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeQueryResult(None)  # initial existence check
            raise RuntimeError("db connection dropped mid-race")  # re-check also fails

        monkeypatch.setattr(db, "query", flaky_query)
        monkeypatch.setattr(db, "commit", _racy_commit)
        assert ssl.try_claim(db, "wipro") is True

    def test_fails_open_on_generic_exception(self, db, monkeypatch, caplog):
        def boom(*a, **kw):
            raise RuntimeError("db is down")
        monkeypatch.setattr(db, "query", boom)
        with caplog.at_level(logging.ERROR, logger="real-trade-shared-symbol-lock"):
            result = ssl.try_claim(db, "hdfc")
        assert result is True
        assert any("try_claim(HDFC) failed" in r.message for r in caplog.records)

    def test_generic_exception_rollback_failure_is_swallowed(self, db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db is down")
        monkeypatch.setattr(db, "query", boom)
        monkeypatch.setattr(db, "rollback", lambda: (_ for _ in ()).throw(RuntimeError("rollback broken too")))
        assert ssl.try_claim(db, "hdfc") is True


class TestRelease:
    def test_releases_own_row(self, db):
        db.add(models.SharedSymbolLock(symbol="ITC", held_by_service="real-trade-service", held_by_mode="REAL"))
        db.commit()
        ssl.release(db, "itc")
        assert db.query(models.SharedSymbolLock).filter_by(symbol="ITC").first() is None

    def test_does_not_touch_row_held_by_other_service(self, db):
        db.add(models.SharedSymbolLock(symbol="WIPRO", held_by_service="position-stocks-service", held_by_mode=None))
        db.commit()
        ssl.release(db, "wipro")  # must be a no-op -- not ours to release
        assert db.query(models.SharedSymbolLock).filter_by(symbol="WIPRO").first() is not None

    def test_noop_when_symbol_not_locked_at_all(self, db):
        ssl.release(db, "ghost")  # must not raise
        assert db.query(models.SharedSymbolLock).count() == 0

    def test_non_blocking_on_exception(self, db, monkeypatch, caplog):
        def boom(*a, **kw):
            raise RuntimeError("db is down")
        monkeypatch.setattr(db, "query", boom)
        with caplog.at_level(logging.ERROR, logger="real-trade-shared-symbol-lock"):
            ssl.release(db, "itc")  # must not raise
        assert any("release(ITC) failed" in r.message for r in caplog.records)

    def test_rollback_failure_inside_exception_handler_is_swallowed(self, db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db is down")
        monkeypatch.setattr(db, "query", boom)
        monkeypatch.setattr(db, "rollback", lambda: (_ for _ in ()).throw(RuntimeError("rollback broken too")))
        ssl.release(db, "itc")  # must not raise


class TestStatus:
    def test_returns_snapshot_of_all_locked_symbols(self, db):
        db.add(models.SharedSymbolLock(symbol="ITC", held_by_service="real-trade-service", held_by_mode="REAL"))
        db.add(models.SharedSymbolLock(symbol="WIPRO", held_by_service="position-stocks-service", held_by_mode=None))
        db.commit()
        result = ssl.status(db)
        assert len(result) == 2
        by_symbol = {r["symbol"]: r for r in result}
        assert by_symbol["ITC"]["held_by_service"] == "real-trade-service"
        assert by_symbol["ITC"]["held_by_mode"] == "REAL"
        assert by_symbol["ITC"]["claimed_at"] is not None  # isoformat string, not a datetime
        assert isinstance(by_symbol["ITC"]["claimed_at"], str)
        assert by_symbol["WIPRO"]["held_by_mode"] is None

    def test_returns_empty_list_when_no_locks(self, db):
        assert ssl.status(db) == []

    def test_returns_empty_list_on_exception_instead_of_raising(self, db, monkeypatch, caplog):
        def boom(*a, **kw):
            raise RuntimeError("db is down")
        monkeypatch.setattr(db, "query", boom)
        with caplog.at_level(logging.ERROR, logger="real-trade-shared-symbol-lock"):
            assert ssl.status(db) == []
        assert any("shared_symbol_lock.status failed" in r.message for r in caplog.records)
