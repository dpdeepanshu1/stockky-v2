"""
tests/test_local_cache.py

100%-coverage-plan round for resilience/local_cache.py (was 45% — only the
happy paths of save_snapshot/load_snapshot were exercised, indirectly, by
tests that use them as a fixture; snapshot_open_positions and
reconcile_on_startup had no direct tests at all).

This module is the last-known-good cache the whole service leans on for
"don't spam / don't forget" state (exit-alert cooldowns, the per-position
circuit-limit resend-suppression flag, the rejection streak counters, the
ATR cache, the after-hours overnight-priority snapshot) AND the per-cycle
open-position snapshot that reconcile_on_startup compares against after a
restart. It is also where the session46 / 2026-09-16 "snapshot froze forever
once open positions hit zero" bug lived.

Everything here runs against a REAL in-memory SQLite database (real
ResilienceCache / TradePosition / TradeAuditLog tables, real audit logger) —
no mocking of the code under test. The only fakes are:
  * _RacyDb — a thin Session proxy that hides the existing row from the
    check-then-act SELECT once, so the classic two-writers race (both see "no
    row", both INSERT) really happens against the database and raises a
    genuine IntegrityError from the PRIMARY KEY constraint;
  * caplog for the log-message assertions.

What is covered:
  * save_snapshot — insert, upsert-in-place, default=str serialisation of
    non-JSON types, the 2026-09-12 IntegrityError race fallback to UPDATE
    (with the row genuinely ending up holding the loser's payload), the
    retry-after-conflict itself failing, the generic failure path
    (rollback + warning, never raises, nothing persisted), and the one
    thing it does NOT swallow (an unserialisable payload — programmer error).
  * load_snapshot — miss, hit, corrupt JSON, DB failure (all -> None/dict,
    never raises).
  * snapshot_open_positions — payload shape, per-mode key isolation, and the
    2026-09-16 regression (an empty position list must still WRITE a fresh
    snapshot, replacing the stale non-empty one).
  * reconcile_on_startup — no snapshot / match / mismatch in both
    directions with the exact RECONCILE_MISMATCH audit detail, the
    2026-09-12 PARTIALLY_CLOSED fix, CLOSED positions not counting as live,
    per-mode independence, a falsy ({}) snapshot being skipped, a snapshot
    with no "positions" key, an end-to-end check that the 2026-09-16 fix
    really removes the spurious post-restart mismatch, and the whole-function
    failure being swallowed.
  * Key-length invariant: every key this module writes fits the
    trade_resilience_cache.key String(64) column (SQLite ignores the limit,
    Postgres/Oracle do not — and save_snapshot swallows the error).

Run from services/real-trade-service:
    python3 -m pytest tests/test_local_cache.py -q \\
        --cov=resilience.local_cache --cov-report=term-missing
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from resilience import local_cache as lc

_engine = create_engine("sqlite:///:memory:")
_Session = sessionmaker(bind=_engine)

LOGGER = "real-trade-local-cache"


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = _Session()
    yield s
    s.close()


def _cache_rows(db):
    db.expire_all()
    return db.query(models.ResilienceCache).all()


def _add_position(db, symbol, mode="REAL", status="OPEN", qty=10):
    p = models.TradePosition(
        mode=mode, symbol=symbol, status=status, qty_open=qty,
        avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
    )
    db.add(p)
    db.commit()
    return p


def _audit_rows(db, action="RECONCILE_MISMATCH"):
    db.expire_all()
    return db.query(models.TradeAuditLog).filter_by(action=action).all()


class _Q:
    def __init__(self, q, owner):
        self._q, self._owner = q, owner

    def filter_by(self, **kw):
        return _Q(self._q.filter_by(**kw), self._owner)

    def first(self):
        if self._owner.hide_first > 0:
            self._owner.hide_first -= 1
            return None
        return self._q.first()

    def update(self, *a, **k):
        if self._owner.update_raises:
            raise RuntimeError("update failed")
        return self._q.update(*a, **k)


class _RacyDb:
    """Session proxy: the first `hide_first` SELECT ...first() calls report
    'no row' even though one exists — i.e. the other writer's row committed
    between our check and our INSERT."""

    def __init__(self, real, hide_first=1, update_raises=False):
        self._real = real
        self.hide_first = hide_first
        self.update_raises = update_raises
        self.rollbacks = 0

    def query(self, *a):
        return _Q(self._real.query(*a), self)

    def rollback(self):
        self.rollbacks += 1
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ===========================================================================
# save_snapshot
# ===========================================================================

class TestSaveSnapshot:
    def test_inserts_a_new_row(self, db):
        lc.save_snapshot(db, "k1", {"a": 1, "b": [1, 2]})
        rows = _cache_rows(db)
        assert [r.key for r in rows] == ["k1"]
        assert json.loads(rows[0].payload_json) == {"a": 1, "b": [1, 2]}
        assert rows[0].updated_at is not None

    def test_second_save_updates_in_place_instead_of_adding_a_row(self, db):
        lc.save_snapshot(db, "k1", {"v": 1})
        first_ts = _cache_rows(db)[0].updated_at
        # Pass-through proxy (hide_first=0) so we can also assert the ordinary
        # upsert is a clean SELECT->UPDATE: NO failed INSERT and NO rollback.
        # (An "always INSERT" regression would still end with the right data —
        # the IntegrityError fallback quietly repairs it — but would burn a
        # failed INSERT + rollback on every routine update.)
        spy = _RacyDb(db, hide_first=0)
        lc.save_snapshot(spy, "k1", {"v": 2})
        assert spy.rollbacks == 0
        rows = _cache_rows(db)
        assert len(rows) == 1
        assert json.loads(rows[0].payload_json) == {"v": 2}
        assert rows[0].updated_at >= first_ts

    def test_different_keys_are_independent(self, db):
        lc.save_snapshot(db, "a", {"x": 1})
        lc.save_snapshot(db, "b", {"x": 2})
        assert lc.load_snapshot(db, "a") == {"x": 1}
        assert lc.load_snapshot(db, "b") == {"x": 2}

    def test_non_json_types_are_stringified_not_fatal(self, db):
        ts = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
        lc.save_snapshot(db, "k", {"when": ts, "n": 3})
        loaded = lc.load_snapshot(db, "k")
        assert loaded["n"] == 3
        assert loaded["when"] == str(ts)

    def test_concurrent_writer_race_falls_back_to_update(self, db):
        """2026-09-12 fix. Two writers, same key: the other writer's row is
        committed AFTER our SELECT saw nothing, so our INSERT hits the PRIMARY
        KEY constraint (a genuine IntegrityError). The write must not be
        dropped — it is retried as an UPDATE and the row ends up holding OUR
        payload."""
        other = _Session()
        try:
            lc.save_snapshot(other, "market_feed:atr_cache", {"atrs": {"OLD": 1}})
        finally:
            other.close()

        racy = _RacyDb(db, hide_first=1)
        lc.save_snapshot(racy, "market_feed:atr_cache", {"atrs": {"NEW": 2}})

        assert racy.rollbacks == 1                       # the failed INSERT was rolled back
        rows = _cache_rows(db)
        assert len(rows) == 1
        assert json.loads(rows[0].payload_json) == {"atrs": {"NEW": 2}}   # loser's write NOT lost

    def test_retry_after_conflict_failing_is_logged_and_swallowed(self, db, caplog):
        other = _Session()
        try:
            lc.save_snapshot(other, "k", {"v": "theirs"})
        finally:
            other.close()

        racy = _RacyDb(db, hide_first=1, update_raises=True)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            lc.save_snapshot(racy, "k", {"v": "ours"})     # must not raise
        assert "retry-after-conflict failed" in caplog.text
        assert "[k]" in caplog.text
        assert racy.rollbacks == 2                          # once after IntegrityError, once after failed retry
        assert lc.load_snapshot(db, "k") == {"v": "theirs"}  # their row is untouched

    def test_generic_failure_rolls_back_warns_and_never_raises(self, db, caplog):
        def boom():
            raise RuntimeError("connection lost")

        rolled = []
        db.commit = boom                                   # instance-level override
        real_rollback = db.rollback
        db.rollback = lambda: (rolled.append(1), real_rollback())
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            lc.save_snapshot(db, "k", {"v": 1})
        assert "local_cache.save_snapshot[k]" in caplog.text and "connection lost" in caplog.text
        assert rolled == [1]
        del db.commit                                      # restore real commit
        assert lc.load_snapshot(db, "k") is None           # nothing was persisted

    def test_unserialisable_payload_raises_and_writes_nothing(self, db):
        """json.dumps runs BEFORE the try-block, so — unlike every DB failure —
        a payload json can't encode even with default=str (non-string dict
        keys) is a loud programmer error, not a swallowed warning. No current
        call site can hit this (all payloads are str-keyed dicts); pinned so a
        future change to swallow OR to widen it is a conscious decision."""
        with pytest.raises(TypeError):
            lc.save_snapshot(db, "k", {(1, 2): "tuple key"})
        assert _cache_rows(db) == []


# ===========================================================================
# load_snapshot
# ===========================================================================

class TestLoadSnapshot:
    def test_missing_key_returns_none(self, db):
        assert lc.load_snapshot(db, "nope") is None

    def test_roundtrip(self, db):
        lc.save_snapshot(db, "k", {"a": [1, 2, 3]})
        assert lc.load_snapshot(db, "k") == {"a": [1, 2, 3]}

    def test_corrupt_json_returns_none_and_warns(self, db, caplog):
        db.add(models.ResilienceCache(key="bad", payload_json="{not json"))
        db.commit()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert lc.load_snapshot(db, "bad") is None
        assert "local_cache.load_snapshot[bad]" in caplog.text

    def test_db_failure_returns_none_and_warns(self, caplog):
        class Boom:
            def query(self, *a):
                raise RuntimeError("db down")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert lc.load_snapshot(Boom(), "k") is None
        assert "db down" in caplog.text


# ===========================================================================
# snapshot_open_positions
# ===========================================================================

def _pos(id_, symbol, qty=5, avg=100.0, stop=95.0, target=110.0, status="OPEN"):
    return SimpleNamespace(id=id_, symbol=symbol, qty_open=qty, avg_entry_price=avg,
                           current_stop=stop, current_target=target, status=status)


class TestSnapshotOpenPositions:
    def test_payload_shape(self, db):
        lc.snapshot_open_positions(db, "REAL", [_pos(7, "TCS", qty=3, avg=3500.5, stop=3400.0,
                                                     target=3700.0, status="PARTIALLY_CLOSED")])
        snap = lc.load_snapshot(db, "open_positions_last_known_good_REAL")
        assert snap["positions"] == [{
            "id": 7, "symbol": "TCS", "qty_open": 3, "avg_entry_price": 3500.5,
            "current_stop": 3400.0, "current_target": 3700.0, "status": "PARTIALLY_CLOSED",
        }]
        as_of = datetime.fromisoformat(snap["as_of"])
        assert as_of.tzinfo is not None and as_of.utcoffset().total_seconds() == 0

    def test_none_stop_and_target_are_preserved(self, db):
        lc.snapshot_open_positions(db, "REAL", [_pos(1, "INFY", stop=None, target=None)])
        row = lc.load_snapshot(db, "open_positions_last_known_good_REAL")["positions"][0]
        assert row["current_stop"] is None and row["current_target"] is None

    def test_demo_and_real_snapshots_do_not_overwrite_each_other(self, db):
        lc.snapshot_open_positions(db, "DEMO", [_pos(1, "AAA")])
        lc.snapshot_open_positions(db, "REAL", [_pos(2, "BBB")])
        demo = lc.load_snapshot(db, "open_positions_last_known_good_DEMO")
        real = lc.load_snapshot(db, "open_positions_last_known_good_REAL")
        assert [p["symbol"] for p in demo["positions"]] == ["AAA"]
        assert [p["symbol"] for p in real["positions"]] == ["BBB"]

    def test_empty_position_list_still_writes_a_fresh_snapshot(self, db):
        """2026-09-16 regression: an empty list used to early-return with NO
        write, so the snapshot froze at the last non-empty cycle forever."""
        lc.snapshot_open_positions(db, "REAL", [_pos(1, "TCS"), _pos(2, "INFY")])
        stale = lc.load_snapshot(db, "open_positions_last_known_good_REAL")
        assert len(stale["positions"]) == 2

        lc.snapshot_open_positions(db, "REAL", [])

        fresh = lc.load_snapshot(db, "open_positions_last_known_good_REAL")
        assert fresh["positions"] == []                    # stale positions gone
        assert fresh["as_of"] >= stale["as_of"]            # and as_of advanced

    def test_empty_list_on_a_brand_new_mode_writes_a_row(self, db):
        lc.snapshot_open_positions(db, "REAL", [])
        assert lc.load_snapshot(db, "open_positions_last_known_good_REAL")["positions"] == []

    def test_every_key_this_module_writes_fits_the_column(self):
        """SQLite ignores String(64); Postgres/Oracle enforce it and
        save_snapshot swallows the error — so an over-long key would silently
        never persist in production while passing every test on SQLite."""
        limit = models.ResilienceCache.key.type.length
        assert limit == 64
        for mode in ("DEMO", "REAL"):
            assert len(f"open_positions_last_known_good_{mode}") <= limit


# ===========================================================================
# reconcile_on_startup
# ===========================================================================

def _snapshot(db, mode, symbols, as_of="2026-09-24T09:32:49+00:00"):
    lc.save_snapshot(db, f"open_positions_last_known_good_{mode}", {
        "as_of": as_of,
        "positions": [{"id": i, "symbol": s} for i, s in enumerate(symbols)],
    })


class TestReconcileOnStartup:
    def test_no_snapshots_is_a_silent_noop(self, db, caplog):
        _add_position(db, "TCS")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            lc.reconcile_on_startup(db)
        assert _audit_rows(db) == []
        assert "MISMATCH" not in caplog.text and "OK (" not in caplog.text

    def test_matching_state_logs_ok_and_writes_no_audit_row(self, db, caplog):
        _add_position(db, "TCS")
        _add_position(db, "INFY")
        _snapshot(db, "REAL", ["TCS", "INFY"])
        with caplog.at_level(logging.INFO, logger=LOGGER):
            lc.reconcile_on_startup(db)
        assert "REAL OK (2 open positions match snapshot)" in caplog.text
        assert _audit_rows(db) == []

    def test_position_only_in_db_is_a_mismatch(self, db, caplog):
        _add_position(db, "TCS")
        _add_position(db, "WIPRO")
        _snapshot(db, "REAL", ["TCS"])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            lc.reconcile_on_startup(db)
        rows = _audit_rows(db)
        assert len(rows) == 1
        assert (rows[0].actor, rows[0].mode) == ("system", "REAL")
        assert rows[0].detail == (
            "mode=REAL snap_as_of=2026-09-24T09:32:49+00:00 "
            "in_live_not_snap=['WIPRO'] in_snap_not_live=[]"
        )
        assert "MISMATCH detected" in caplog.text

    def test_position_only_in_snapshot_is_a_mismatch(self, db):
        _add_position(db, "TCS")
        _snapshot(db, "REAL", ["TCS", "GHOST"])
        lc.reconcile_on_startup(db)
        detail = _audit_rows(db)[0].detail
        assert "in_live_not_snap=[]" in detail and "in_snap_not_live=['GHOST']" in detail

    def test_mismatch_symbol_lists_are_sorted_and_both_directions_reported(self, db):
        for s in ("ZED", "ALPHA"):
            _add_position(db, s)
        _snapshot(db, "REAL", ["MID", "BETA"])
        lc.reconcile_on_startup(db)
        detail = _audit_rows(db)[0].detail
        assert "in_live_not_snap=['ALPHA', 'ZED']" in detail
        assert "in_snap_not_live=['BETA', 'MID']" in detail

    def test_partially_closed_position_counts_as_live(self, db):
        """2026-09-12 fix: the snapshot is fed from open_positions(), which
        includes PARTIALLY_CLOSED. Comparing against status=OPEN only meant a
        guaranteed spurious mismatch on every boot."""
        _add_position(db, "TCS", status="PARTIALLY_CLOSED")
        _snapshot(db, "REAL", ["TCS"])
        lc.reconcile_on_startup(db)
        assert _audit_rows(db) == []

    def test_closed_position_does_not_count_as_live(self, db):
        _add_position(db, "TCS", status="CLOSED")
        _snapshot(db, "REAL", ["TCS"])
        lc.reconcile_on_startup(db)
        assert "in_snap_not_live=['TCS']" in _audit_rows(db)[0].detail

    def test_other_modes_positions_do_not_leak_into_the_comparison(self, db):
        _add_position(db, "TCS", mode="DEMO")               # DEMO holds TCS...
        _snapshot(db, "REAL", [])                            # ...REAL snapshot is empty
        lc.reconcile_on_startup(db)
        assert _audit_rows(db) == []                         # REAL sees no live positions -> match

    def test_modes_are_checked_independently(self, db):
        _add_position(db, "AAA", mode="DEMO")
        _snapshot(db, "DEMO", ["AAA"])                       # DEMO matches
        _add_position(db, "BBB", mode="REAL")
        _snapshot(db, "REAL", [])                            # REAL drifted
        lc.reconcile_on_startup(db)
        rows = _audit_rows(db)
        assert [r.mode for r in rows] == ["REAL"]

    def test_falsy_empty_dict_snapshot_is_skipped(self, db):
        """`if not snap: continue` — a stored {} is treated like 'no snapshot'
        (nothing to compare against), so live positions raise no mismatch."""
        _add_position(db, "TCS")
        lc.save_snapshot(db, "open_positions_last_known_good_REAL", {})
        lc.reconcile_on_startup(db)
        assert _audit_rows(db) == []

    def test_snapshot_without_positions_key_is_treated_as_empty(self, db):
        _add_position(db, "TCS")
        lc.save_snapshot(db, "open_positions_last_known_good_REAL", {"as_of": "2026-09-24T00:00:00+00:00"})
        lc.reconcile_on_startup(db)
        assert "in_live_not_snap=['TCS']" in _audit_rows(db)[0].detail

    def test_zero_position_state_matches_after_the_2026_09_16_fix(self, db):
        """End-to-end for the session46 bug: positions existed, then all
        closed; the per-cycle snapshot (now written even when empty) tracks
        that, so a restart compares 0 live vs 0 snapshotted -> no spurious
        RECONCILE_MISMATCH. With the old skip-on-empty behaviour the snapshot
        would still list TCS and this would flag a mismatch on every boot."""
        p = _add_position(db, "TCS")
        lc.snapshot_open_positions(db, "REAL", [p])          # cycle N: 1 open
        p.status = "CLOSED"
        db.commit()
        lc.snapshot_open_positions(db, "REAL", [])           # cycle N+1: 0 open (must still write)
        lc.reconcile_on_startup(db)                          # restart
        assert _audit_rows(db) == []

    def test_positions_snapshotted_from_real_rows_reconcile_cleanly(self, db):
        rows = [_add_position(db, "TCS"), _add_position(db, "INFY", status="PARTIALLY_CLOSED")]
        lc.snapshot_open_positions(db, "REAL", rows)
        lc.reconcile_on_startup(db)
        assert _audit_rows(db) == []

    def test_any_failure_is_swallowed_with_a_warning(self, db, monkeypatch, caplog):
        def boom(db_, key):
            raise RuntimeError("cache unreadable")

        monkeypatch.setattr(lc, "load_snapshot", boom)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            lc.reconcile_on_startup(db)                      # must not raise
        assert "reconcile_on_startup failed (non-fatal)" in caplog.text
        assert "cache unreadable" in caplog.text
