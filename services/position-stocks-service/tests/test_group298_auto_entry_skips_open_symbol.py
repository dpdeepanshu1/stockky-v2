"""group298: the automatic scalp entry itself refuses a symbol this service already holds."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import models
from orders import entry
from test_entry import (LEDGER_AVAILABLE, available, cand, env, last_log, lock_held, mkpos,  # noqa: F401
                        n_positions)


@pytest.mark.parametrize("status", ["OPEN", "EXIT_LEGS_REJECTED"])
def test_a_symbol_already_held_here_is_skipped_cleanly(env, status):
    db, b, _ = env
    held = mkpos(db, "ABC", status=status)
    before = n_positions(db)
    assert entry.attempt_entry(db, cand("ABC")) is None
    log = last_log(db)
    assert log.decision == "SKIPPED" and log.reason == f"ALREADY_OPEN_HERE:id={held.id},status={status}"
    assert n_positions(db) == before and available(db) == pytest.approx(LEDGER_AVAILABLE)
    assert not lock_held(db, "ABC")
    assert b.of("place_super_order") == [] and b.of("place_order") == []


@pytest.mark.parametrize("status", ["TARGET_HIT", "STOP_HIT", "ERROR", "CLOSED"])
def test_a_finished_position_does_not_block(env, status):
    db, _, _ = env
    mkpos(db, "ABC", status=status)
    assert entry.attempt_entry(db, cand("ABC")) is not None


def test_another_symbol_open_does_not_block(env):
    db, _, _ = env
    mkpos(db, "XYZ")
    assert entry.attempt_entry(db, cand("ABC")) is not None


def test_the_guard_runs_before_the_symbol_lock_is_claimed(env):
    db, _, _ = env
    mkpos(db, "ABC")
    entry.attempt_entry(db, cand("ABC"))
    assert db.query(models.SharedSymbolLock).count() == 0


def test_the_slot_cap_message_still_wins_when_full_and_symbol_not_held(env):
    db, _, _ = env
    for _ in range(5):
        mkpos(db, status="OPEN")
    entry.attempt_entry(db, cand("ABC"))
    assert last_log(db).reason == "MAX_POSITIONS:5"
