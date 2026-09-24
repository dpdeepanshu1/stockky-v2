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


# ── session110: exact timers for overlapping stages ───────────────────────

class _Clock:
    """Deterministic replacement for pstat._now (seconds, monotonic)."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000.0


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(pstat, "_now", c)
    return c


def test_concurrent_stages_report_their_real_durations(clock):
    """The session94 probe: dynamic_universe 50 ms -> watchlist chain 100 ms
    running alongside candidates 300 ms. Before the fix the single current-stage
    slot reported candidates=50.7 and watchlist=249.5."""
    pstat.start_cycle("DEMO", "autopilot")
    # t=0: both branches of the gather start
    pstat.set_stage("DEMO", "dynamic_universe"); pstat.stage_started("DEMO", "dynamic_universe")
    pstat.set_stage("DEMO", "candidates");       pstat.stage_started("DEMO", "candidates")
    clock.advance_ms(50)                          # t=50: universe done, watchlist begins
    pstat.stage_finished("DEMO", "dynamic_universe")
    pstat.set_stage("DEMO", "watchlist");        pstat.stage_started("DEMO", "watchlist")
    clock.advance_ms(100)                         # t=150: watchlist done
    pstat.stage_finished("DEMO", "watchlist")
    clock.advance_ms(150)                         # t=300: candidates done
    pstat.stage_finished("DEMO", "candidates")
    pstat.set_stage("DEMO", "entry")              # gather finished -> sequential stages resume

    timings = pstat.get_status("DEMO")["stage_timings_ms"]
    assert timings["dynamic_universe"] == 50.0
    assert timings["watchlist"] == 100.0
    assert timings["candidates"] == 300.0


def test_set_stage_never_overwrites_an_exact_timing_and_end_cycle_keeps_it(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.set_stage("DEMO", "candidates"); pstat.stage_started("DEMO", "candidates")
    clock.advance_ms(300)
    pstat.stage_finished("DEMO", "candidates")
    clock.advance_ms(40)                          # slot still says "candidates" for 40 more ms
    pstat.set_stage("DEMO", "entry")              # legacy path would have written 340.0 here
    assert pstat.get_status("DEMO")["stage_timings_ms"]["candidates"] == 300.0
    clock.advance_ms(10)
    pstat.end_cycle("DEMO", {})
    rec = pstat.get_status("DEMO")["last_cycle"]
    assert rec["stage_timings_ms"]["candidates"] == 300.0
    assert rec["stage_timings_ms"]["entry"] == 10.0     # sequential stages are still timed the old way


def test_stages_without_explicit_timers_are_still_timed_by_set_stage(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.set_stage("DEMO", "entry")
    clock.advance_ms(70)
    pstat.set_stage("DEMO", "fills")
    assert pstat.get_status("DEMO")["stage_timings_ms"]["entry"] == 70.0


def test_stage_timer_calls_with_no_active_cycle_are_no_ops():
    pstat.stage_started("DEMO", "candidates")
    pstat.stage_finished("DEMO", "candidates")
    assert "DEMO" not in pstat._STATE


def test_stage_finished_without_a_matching_start_is_ignored():
    pstat.start_cycle("DEMO", "manual")
    pstat.stage_finished("DEMO", "candidates")
    assert pstat.get_status("DEMO")["stage_timings_ms"] == {}


def test_stage_finished_twice_keeps_the_first_measurement(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.stage_started("DEMO", "watchlist")
    clock.advance_ms(25)
    pstat.stage_finished("DEMO", "watchlist")
    clock.advance_ms(500)
    pstat.stage_finished("DEMO", "watchlist")          # second call: no timer left -> ignored
    assert pstat.get_status("DEMO")["stage_timings_ms"]["watchlist"] == 25.0


def test_timers_are_per_mode(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.start_cycle("REAL", "manual")
    pstat.stage_started("DEMO", "candidates")
    clock.advance_ms(10)
    pstat.stage_started("REAL", "candidates")
    clock.advance_ms(30)
    pstat.stage_finished("DEMO", "candidates")
    pstat.stage_finished("REAL", "candidates")
    assert pstat.get_status("DEMO")["stage_timings_ms"]["candidates"] == 40.0
    assert pstat.get_status("REAL")["stage_timings_ms"]["candidates"] == 30.0


def test_internal_timer_bookkeeping_is_not_leaked_into_the_status_or_history(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.stage_started("DEMO", "candidates")
    clock.advance_ms(5)
    pstat.stage_finished("DEMO", "candidates")
    live = pstat.get_status("DEMO")
    assert not any(k.startswith("_") for k in live)
    pstat.end_cycle("DEMO", {})
    assert not any(k.startswith("_") for k in pstat.get_status("DEMO")["last_cycle"])


def test_restarting_a_stage_timer_measures_from_the_latest_start(clock):
    pstat.start_cycle("DEMO", "manual")
    pstat.stage_started("DEMO", "watchlist")
    clock.advance_ms(200)
    pstat.stage_started("DEMO", "watchlist")           # e.g. a retry of the stage
    clock.advance_ms(30)
    pstat.stage_finished("DEMO", "watchlist")
    assert pstat.get_status("DEMO")["stage_timings_ms"]["watchlist"] == 30.0
