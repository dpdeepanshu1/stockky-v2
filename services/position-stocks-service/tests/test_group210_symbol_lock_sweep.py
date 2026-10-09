"""Group 210 (item 14): periodic sweep of stale symbol locks (capital/shared_symbol_lock.sweep_stale)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import shared_symbol_lock as sl

US = "position-stocks-service"
PEER = "real-trade-service"
OLD = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    sl.reset_sweep_throttle()
    yield session
    session.close()
    sl.reset_sweep_throttle()


def _lock(db, symbol, service=US, claimed_at=OLD):
    row = models.SharedSymbolLock(symbol=symbol, held_by_service=service, held_by_mode=None)
    row.claimed_at = claimed_at
    db.add(row)
    db.commit()


def _pos(db, symbol, status="OPEN", err=None, sec="1"):
    db.add(models.ScalpPosition(
        symbol=symbol, window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        status=status, quantity=1, entry_price=100.0, capital_risked=100.0,
        overnight_converted_to_cnc=False, dhan_security_id=sec, target_price=102.0, stop_price=98.0,
        opened_at=datetime.now(timezone.utc), error_message=err,
    ))
    db.commit()


def _symbols(db):
    return sorted(r.symbol for r in db.query(models.SharedSymbolLock).all())


def test_releases_old_lock_behind_an_error_row_the_avalon_case(db):
    _lock(db, "AVALON")
    _pos(db, "AVALON", status="ERROR", err="Entry leg REJECTED on Dhan (reconciled)")
    assert sl.sweep_stale(db) == ["AVALON"]
    assert _symbols(db) == []


def test_young_claim_is_left_alone_even_with_no_position_row(db):
    _lock(db, "NEWBUY", claimed_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    assert sl.sweep_stale(db) == []
    assert _symbols(db) == ["NEWBUY"]


def test_young_claim_naive_timestamp_is_also_protected(db):
    # SQLite hands back naive datetimes
    _lock(db, "NEWBUY", claimed_at=datetime.utcnow() - timedelta(seconds=30))
    assert sl.sweep_stale(db) == []


def test_open_and_exit_legs_rejected_positions_keep_their_lock(db):
    _lock(db, "A")
    _lock(db, "B")
    _pos(db, "A", "OPEN", sec="1")
    _pos(db, "B", "EXIT_LEGS_REJECTED", sec="2")
    assert sl.sweep_stale(db) == []
    assert _symbols(db) == ["A", "B"]


def test_dead_exit_sell_error_keeps_its_lock_but_startup_cleanup_does_not(db):
    _lock(db, "DEAD")
    _pos(db, "DEAD", "ERROR", err="EOD_SQUAREOFF_SELL_DEAD: order 1 came back REJECTED with zero fill")
    assert sl.sweep_stale(db) == []
    assert _symbols(db) == ["DEAD"]
    # startup behaviour is unchanged
    assert sl.cleanup_stale(db) == ["DEAD"]


def test_peer_locks_are_never_touched(db):
    _lock(db, "PEERSYM", service=PEER)
    assert sl.sweep_stale(db) == []
    assert _symbols(db) == ["PEERSYM"]


def test_throttled_to_one_pass_per_interval(db, monkeypatch):
    monkeypatch.setattr(config, "SYMBOL_LOCK_SWEEP_INTERVAL_S", 3600.0, raising=False)
    _lock(db, "A")
    assert sl.sweep_stale(db) == ["A"]
    _lock(db, "B")
    assert sl.sweep_stale(db) == []          # inside the interval
    assert sl.sweep_stale(db, force=True) == ["B"]


def test_interval_zero_turns_the_periodic_sweep_off(db, monkeypatch):
    monkeypatch.setattr(config, "SYMBOL_LOCK_SWEEP_INTERVAL_S", 0.0, raising=False)
    _lock(db, "A")
    assert sl.sweep_stale(db) == []
    assert _symbols(db) == ["A"]


def test_min_age_is_configurable(db, monkeypatch):
    monkeypatch.setattr(config, "SYMBOL_LOCK_SWEEP_MIN_AGE_S", 0.0, raising=False)
    _lock(db, "NEWBUY", claimed_at=datetime.now(timezone.utc))
    assert sl.sweep_stale(db) == ["NEWBUY"]


def test_db_error_is_swallowed(monkeypatch):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    sl.reset_sweep_throttle()
    assert sl.sweep_stale(Boom()) == []


def test_first_sweep_is_not_throttled_on_a_freshly_booted_host(db, monkeypatch):
    """group 272: time.monotonic() is host uptime; a first sweep must run even when uptime is below the interval."""
    monkeypatch.setattr(config, "SYMBOL_LOCK_SWEEP_INTERVAL_S", 60.0, raising=False)
    monkeypatch.setattr(sl.time, "monotonic", lambda: 5.0)          # host up for 5 seconds
    _lock(db, "A")
    assert sl.sweep_stale(db) == ["A"]
    _lock(db, "B")
    assert sl.sweep_stale(db) == []                                 # still throttled right after
