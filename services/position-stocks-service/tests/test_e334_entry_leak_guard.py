"""
E334 - position-stocks: an unexpected error between the symbol claim / capital reservation and the BUY call must not leave them held.

attempt_entry and attempt_manual_entry take the shared symbol claim, reserve capital and only then call Dhan. Every planned rejection releases both, but an
unexpected exception in between (live_anchor, a gate, a DB call, sizing ...) skipped those releases: the claim and the capital stayed held for the session.
Each entry now records what it holds (the _hold_* wrappers) and the outer wrapper releases the remainder when an exception escapes BEFORE the order call.
After the order call nothing is released (the order may be live). The original exception is always re-raised unchanged.

Run from services/position-stocks-service:  python -m pytest tests/test_e334_entry_leak_guard.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capital import ledger  # noqa: E402
from orders import entry  # noqa: E402
from tests.test_entry import (  # noqa: E402,F401  (env is a fixture)
    LEDGER_AVAILABLE, POSITION_VALUE, available, cand, env, lock_held, n_positions,
)


def _boom(*a, **k):
    raise RuntimeError("unexpected")


class TestAutoEntry:
    def test_crash_before_the_order_releases_capital_and_claim(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(entry.live_anchor, "live_price", _boom)        # runs after the capital is reserved, before the BUY
        with pytest.raises(RuntimeError, match="unexpected"):
            entry.attempt_entry(db, cand("ABC"))
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert not lock_held(db, "ABC")
        assert b.of("place_super_order") == [] and b.of("place_order") == []
        assert entry._hold_cur() is None                                    # state cleared

    def test_crash_after_the_order_keeps_capital_and_claim(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(entry.position_save, "save_new_position", _boom)
        with pytest.raises(RuntimeError):
            entry.attempt_entry(db, cand("ABC"))
        assert len(b.of("place_super_order")) == 1
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - POSITION_VALUE)
        assert lock_held(db, "ABC")

    def test_planned_rejection_is_not_released_twice(self, env):
        db, b, _ = env
        b.super_script = [RuntimeError("gateway timeout talking to broker")]   # BUY refused: capital + claim released by the planned branch
        assert entry.attempt_entry(db, cand("ABC")) is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)                # exactly once
        assert not lock_held(db, "ABC")

    def test_release_failure_during_cleanup_still_tries_the_claim_and_reraises_the_original(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(entry.live_anchor, "live_price", _boom)
        monkeypatch.setattr(ledger, "release_capital", lambda *a, **k: (_ for _ in ()).throw(ValueError("ledger busy")))
        with pytest.raises(RuntimeError, match="unexpected"):
            entry.attempt_entry(db, cand("ABC"))
        assert not lock_held(db, "ABC")

    def test_a_crash_before_anything_is_held_is_a_no_op(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(entry, "_get_gate_state", _boom)
        with pytest.raises(RuntimeError):
            entry.attempt_entry(db, cand("ABC"))
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")


# ── manual BUY ──────────────────────────────────────────────────────────────
class TestManualEntry:
    def test_crash_after_the_claim_releases_it(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(entry, "compute_levels", _boom)
        with pytest.raises(RuntimeError):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_crash_after_the_reservation_releases_capital_and_claim(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(entry.sizing, "compute_quantity", _boom)
        with pytest.raises(RuntimeError):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert b.of("place_super_order") == []

    def test_crash_after_the_order_keeps_everything(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(entry.position_save, "save_new_position", _boom)
        with pytest.raises(RuntimeError):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert len(b.of("place_super_order")) == 1 and lock_held(db, "ABC") and available(db) < LEDGER_AVAILABLE

    def test_planned_rejection_after_reserve_is_not_released_twice(self, env, monkeypatch):
        db, b, _ = env
        b.super_script = [RuntimeError("gateway timeout talking to broker")]
        with pytest.raises(entry.ManualEntryRejected):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")


# ── the tracker itself ──────────────────────────────────────────────────────
def test_partial_release_then_cleanup_returns_only_the_remainder(monkeypatch):
    rel = []
    monkeypatch.setattr(ledger, "reserve_capital", lambda db, **k: 100.0)
    monkeypatch.setattr(ledger, "release_capital", lambda db, position_value, realized_pnl: rel.append(position_value))
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db, s: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db, s: rel.append(("lock", s)))
    db = type("D", (), {"rollback": lambda self: None})()
    entry._hold_begin()
    try:
        assert entry._hold_claim(db, "XYZ") and entry._hold_reserve(db, adaptive_stop_pct=1.0) == 100.0
        entry._hold_release(db, 30.0)
        entry._hold_cleanup(db, RuntimeError("x"))
    finally:
        entry._hold_end()
    assert rel == [30.0, 70.0, ("lock", "XYZ")]


def test_failed_reservation_and_failed_claim_hold_nothing(monkeypatch):
    monkeypatch.setattr(ledger, "reserve_capital", lambda db, **k: None)
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db, s: False)
    entry._hold_begin()
    try:
        assert not entry._hold_claim(None, "XYZ") and entry._hold_reserve(None) is None
        h = entry._hold_cur()
        assert not h.claimed and not h.value
    finally:
        entry._hold_end()


def test_helpers_work_without_a_begun_hold(monkeypatch):
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db, s: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db, s: None)
    monkeypatch.setattr(ledger, "release_capital", lambda db, position_value, realized_pnl: None)
    assert entry._hold_cur() is None
    assert entry._hold_claim(None, "A") and entry._hold_unlock(None, "A") is None and entry._hold_release(None, 5.0) is None
    entry._hold_cleanup(None, RuntimeError("x"))      # no-op, no raise
