"""
tests/test_pipeline_status.py — direct unit tests for pipeline_status.py
(coverage plan, real-trade-service round: was 91%, missing lines 79, 99,
108-110, 117).

tests/test_cycle_runner.py's TestRealPipelineStatusIntegration class
already exercises this module end-to-end through a real cycle
(start_cycle/set_stage/end_cycle/get_status), but nothing calls
set_source() or set_symbol_progress() through that path, and nothing
calls any setter for a mode with no active cycle (the "st is None" guard
clauses in set_stage/set_source/set_symbol_progress/end_cycle — these
exist specifically so a stray/late call after a cycle already ended, or
before one starts, can never raise, per the module's own "nothing in
here can affect a real cycle" docstring).

This module is pure in-memory (time, collections.deque, threading.Lock) —
no DB, no network — so these tests touch the real module directly, no
mocking needed.

Run from services/real-trade-service:
    python -m pytest tests/test_pipeline_status.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import pipeline_status as pstat


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test gets a clean slate for both modes, and leaves one behind
    for whichever test file runs next (matches test_cycle_runner.py's own
    _clean_pstat fixture)."""
    for mode in ("DEMO", "REAL"):
        pstat._STATE.pop(mode, None)
        pstat._HISTORY[mode].clear()
    yield
    for mode in ("DEMO", "REAL"):
        pstat._STATE.pop(mode, None)
        pstat._HISTORY[mode].clear()


# ── Guard clauses: setter called with no active cycle for that mode ───────

def test_set_stage_on_mode_with_no_active_cycle_is_a_no_op():
    # No start_cycle() call for DEMO — must not raise, must not create state.
    pstat.set_stage("DEMO", "entry")
    assert pstat._STATE.get("DEMO") is None


def test_set_source_on_mode_with_no_active_cycle_is_a_no_op():
    pstat.set_source("DEMO", "hot_picks")
    assert pstat._STATE.get("DEMO") is None


def test_set_symbol_progress_on_mode_with_no_active_cycle_is_a_no_op():
    pstat.set_symbol_progress("DEMO", "TCS", 1, 5)
    assert pstat._STATE.get("DEMO") is None


def test_end_cycle_on_mode_with_no_active_cycle_is_a_no_op():
    # No start_cycle() call — end_cycle must not raise and must not add
    # a spurious history entry.
    pstat.end_cycle("DEMO", {"entered": 1})
    assert list(pstat._HISTORY["DEMO"]) == []


def test_setters_on_one_mode_never_touch_the_other_modes_state():
    pstat.start_cycle("REAL", "manual")
    pstat.set_stage("DEMO", "entry")  # DEMO has no active cycle
    pstat.set_source("DEMO", "ipo")
    pstat.set_symbol_progress("DEMO", "INFY", 1, 3)

    assert pstat._STATE.get("DEMO") is None
    assert pstat._STATE["REAL"]["stage"] == "starting"  # untouched


# ── set_source / set_symbol_progress with an active cycle ─────────────────

def test_set_source_updates_current_source_when_cycle_is_active():
    pstat.start_cycle("DEMO", "manual")
    pstat.set_source("DEMO", "hot_picks")

    status = pstat.get_status("DEMO")
    assert status["current_source"] == "hot_picks"


def test_set_symbol_progress_updates_symbol_and_counts_when_cycle_is_active():
    pstat.start_cycle("DEMO", "manual")
    pstat.set_symbol_progress("DEMO", "RELIANCE", 3, 10)

    status = pstat.get_status("DEMO")
    assert status["current_symbol"] == "RELIANCE"
    assert status["symbols_done"] == 3
    assert status["symbols_total"] == 10


def test_set_stage_resets_symbol_progress_and_source_for_the_new_stage():
    pstat.start_cycle("DEMO", "manual")
    pstat.set_source("DEMO", "hot_picks")
    pstat.set_symbol_progress("DEMO", "TCS", 2, 5)

    pstat.set_stage("DEMO", "exit")

    status = pstat.get_status("DEMO")
    assert status["stage"] == "exit"
    assert status["current_symbol"] is None
    assert status["current_source"] is None
    assert status["symbols_done"] == 0
    assert status["symbols_total"] == 0


def test_set_stage_records_elapsed_time_for_the_previous_stage():
    pstat.start_cycle("DEMO", "manual")
    pstat.set_stage("DEMO", "candidates")
    pstat.set_stage("DEMO", "entry")

    status = pstat.get_status("DEMO")
    assert "candidates" in status["stage_timings_ms"]
    assert status["stage_timings_ms"]["candidates"] >= 0


# ── end_cycle with an active cycle (full record shape) ─────────────────────

def test_end_cycle_pops_state_and_appends_history():
    pstat.start_cycle("DEMO", "autopilot")
    pstat.set_stage("DEMO", "entry")
    pstat.set_source("DEMO", "hot_picks")
    pstat.set_symbol_progress("DEMO", "TCS", 1, 1)

    pstat.end_cycle("DEMO", {"entry": {"entered": 1, "waited": 0, "rejected": 0,
                                        "entry_details": [{"symbol": "TCS"}]},
                              "exit": {"full_exits": 0, "partial_exits": 0},
                              "new_candidates": 2, "fills": 0,
                              "expired_orders": 0, "auto_disarmed": False})

    assert "DEMO" not in pstat._STATE
    history = list(pstat._HISTORY["DEMO"])
    assert len(history) == 1
    rec = history[0]
    assert rec["trigger"] == "autopilot"
    assert rec["entered"] == 1
    assert rec["entry_details"] == [{"symbol": "TCS"}]
    assert rec["error"] is None


def test_end_cycle_with_none_result_does_not_raise():
    pstat.start_cycle("DEMO", "manual")
    pstat.end_cycle("DEMO", None, error="RuntimeError: boom")

    history = list(pstat._HISTORY["DEMO"])
    assert history[0]["error"] == "RuntimeError: boom"
    assert history[0]["entered"] is None


def test_end_cycle_caps_entry_details_at_twenty_rows():
    pstat.start_cycle("DEMO", "manual")
    many_details = [{"symbol": f"SYM{i}"} for i in range(30)]
    pstat.end_cycle("DEMO", {"entry": {"entry_details": many_details}})

    history = list(pstat._HISTORY["DEMO"])
    assert len(history[0]["entry_details"]) == 20


# ── get_status with no active cycle ────────────────────────────────────────

def test_get_status_with_no_active_cycle_and_no_history():
    status = pstat.get_status("DEMO")
    assert status == {"mode": "DEMO", "running": False, "last_cycle": None, "history": []}


def test_get_status_with_no_active_cycle_but_prior_history():
    pstat.start_cycle("DEMO", "manual")
    pstat.end_cycle("DEMO", {"entry": {"entered": 5}})

    status = pstat.get_status("DEMO")
    assert status["running"] is False
    assert status["last_cycle"]["entered"] == 5
    assert len(status["history"]) == 1


def test_history_is_bounded_to_history_maxlen():
    for i in range(pstat._HISTORY_MAXLEN + 5):
        pstat.start_cycle("DEMO", "manual")
        pstat.end_cycle("DEMO", {"new_candidates": i})

    history = list(pstat._HISTORY["DEMO"])
    assert len(history) == pstat._HISTORY_MAXLEN
    # Newest first — the very last cycle run (highest i) is at index 0.
    assert history[0]["new_candidates"] == pstat._HISTORY_MAXLEN + 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
