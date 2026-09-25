"""
tests/test_pipeline_status.py

Covers pipeline_status.py (session112 round 13) — previously 31%, missing
lines 43-50 (start), 60-64 (set_stage), 68-71 (finish), 75 (snapshot):
i.e. every function body, since nothing in this service calls this module
directly under test — main.py's _run_cycle/_stage wire it in, but no test
exercises those call sites against the real module either.

Unlike real-trade-service's much larger pipeline_status.py (per-mode
_STATE dict, a Lock, a bounded _HISTORY deque, exact per-stage timing),
this service's version is deliberately the simplest possible thing: one
module-level dict, no lock, no history — see the module's own docstring
("single-process, module-level state is enough here ... never
concurrently with itself"). So this file is NOT a port of
real-trade-service's tests/test_pipeline_status.py; the two modules don't
share a shape. Written fresh for this module's actual four functions.

Pure stdlib (only `datetime`) and mutates module-level global state, so
every test resets `_state` to a known baseline first — this file owns
that reset rather than relying on test order.

Run from services/position-stocks-service:
    python -m pytest tests/test_pipeline_status.py -v --cov=pipeline_status --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import pipeline_status as ps

_BASELINE = {
    "running": False,
    "trigger": None,
    "started_at": None,
    "stage": None,
    "stage_label": None,
    "stage_started_at": None,
    "candidates": [],
    "last_cycle": None,
}


@pytest.fixture(autouse=True)
def reset_state():
    """This module is bare global state — reset it before AND after every
    test so no test's leftovers leak into the next one, and so a test that
    fails mid-way doesn't poison the rest of the run."""
    ps._state.clear()
    ps._state.update(dict(_BASELINE))
    yield
    ps._state.clear()
    ps._state.update(dict(_BASELINE))


def _is_iso_utc_now(s, *, within_seconds=5):
    dt = datetime.fromisoformat(s)
    assert dt.tzinfo is not None
    delta = abs((datetime.now(timezone.utc) - dt).total_seconds())
    return delta < within_seconds


# ══════════════════════════════════════════════════════════════════════════
# start
# ══════════════════════════════════════════════════════════════════════════

class TestStart:
    def test_sets_running_and_trigger(self):
        ps.start("MANUAL")
        assert ps._state["running"] is True
        assert ps._state["trigger"] == "MANUAL"

    def test_sets_started_at_to_a_real_iso_utc_timestamp(self):
        ps.start("AUTO")
        assert _is_iso_utc_now(ps._state["started_at"])

    def test_sets_initial_stage_to_starting(self):
        ps.start("AUTO")
        assert ps._state["stage"] == "starting"
        assert ps._state["stage_label"] == "Starting…"

    def test_stage_started_at_matches_started_at(self):
        ps.start("AUTO")
        assert ps._state["stage_started_at"] == ps._state["started_at"]

    def test_clears_candidates_from_any_previous_cycle(self):
        ps._state["candidates"] = ["OLDSTOCK"]
        ps.start("AUTO")
        assert ps._state["candidates"] == []

    def test_does_not_touch_last_cycle(self):
        ps._state["last_cycle"] = {"symbols_scanned": 10}
        ps.start("AUTO")
        assert ps._state["last_cycle"] == {"symbols_scanned": 10}

    def test_accepts_either_documented_trigger_value(self):
        ps.start("AUTO")
        assert ps._state["trigger"] == "AUTO"
        ps.start("MANUAL")
        assert ps._state["trigger"] == "MANUAL"


# ══════════════════════════════════════════════════════════════════════════
# set_stage
# ══════════════════════════════════════════════════════════════════════════

class TestSetStage:
    def test_updates_stage_and_label(self):
        ps.start("AUTO")
        ps.set_stage("scan", "Scan (all 4 windows)")
        assert ps._state["stage"] == "scan"
        assert ps._state["stage_label"] == "Scan (all 4 windows)"

    def test_updates_stage_started_at_to_a_fresh_timestamp(self):
        ps.start("AUTO")
        first = ps._state["stage_started_at"]
        ps.set_stage("scan", "Scan")
        assert _is_iso_utc_now(ps._state["stage_started_at"])
        # not asserting != first: a fast machine can complete both calls
        # within the same microsecond-truncated ISO string in rare cases;
        # freshness (within 5s of now) is the actual contract that matters.

    def test_candidates_none_leaves_existing_candidates_untouched(self):
        ps.start("AUTO")
        ps._state["candidates"] = ["RELIANCE"]
        ps.set_stage("quality_gate", "Quality Gate")
        assert ps._state["candidates"] == ["RELIANCE"]

    def test_candidates_provided_replaces_the_list(self):
        ps.start("AUTO")
        ps._state["candidates"] = ["OLD"]
        ps.set_stage("scan", "Scan (all 4 windows)", candidates=["TCS", "INFY"])
        assert ps._state["candidates"] == ["TCS", "INFY"]

    def test_candidates_empty_list_is_provided_not_none_and_still_replaces(self):
        """`candidates=[]` must clear the list — the check is `is not None`,
        not truthiness, so an empty-but-provided list must NOT be treated
        the same as omitting the argument."""
        ps.start("AUTO")
        ps._state["candidates"] = ["OLD"]
        ps.set_stage("scan", "Scan (all 4 windows)", candidates=[])
        assert ps._state["candidates"] == []

    def test_can_be_called_multiple_times_across_a_cycle(self):
        ps.start("AUTO")
        ps.set_stage("scan", "Scan (all 4 windows)", candidates=["A"])
        ps.set_stage("quality_gate", "Quality Gate")
        ps.set_stage("entry", "Entry")
        assert ps._state["stage"] == "entry"
        assert ps._state["stage_label"] == "Entry"
        assert ps._state["candidates"] == ["A"]  # last-set survives untouched

    def test_does_not_change_running_flag(self):
        ps.start("AUTO")
        ps.set_stage("scan", "Scan")
        assert ps._state["running"] is True


# ══════════════════════════════════════════════════════════════════════════
# finish
# ══════════════════════════════════════════════════════════════════════════

class TestFinish:
    def test_clears_running_and_stage(self):
        ps.start("AUTO")
        ps.set_stage("scan", "Scan")
        ps.finish({"symbols_scanned": 42})
        assert ps._state["running"] is False
        assert ps._state["stage"] is None
        assert ps._state["stage_label"] is None

    def test_stores_the_summary_as_last_cycle(self):
        summary = {"symbols_scanned": 42, "candidates_found": 3}
        ps.finish(summary)
        assert ps._state["last_cycle"] == summary

    def test_does_not_clear_candidates_or_started_at(self):
        """finish() only clears running/stage/stage_label — candidates and
        started_at are left as-is (the frontend can still show what the
        just-finished cycle surfaced until the next start() call)."""
        ps.start("AUTO")
        ps.set_stage("scan", "Scan", candidates=["WIPRO"])
        started_at = ps._state["started_at"]
        ps.finish({"ok": True})
        assert ps._state["candidates"] == ["WIPRO"]
        assert ps._state["started_at"] == started_at

    def test_overwrites_a_previous_last_cycle(self):
        ps.finish({"run": 1})
        ps.finish({"run": 2})
        assert ps._state["last_cycle"] == {"run": 2}


# ══════════════════════════════════════════════════════════════════════════
# snapshot
# ══════════════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_returns_a_dict_matching_current_state(self):
        ps.start("MANUAL")
        ps.set_stage("scan", "Scan", candidates=["HDFC"])
        snap = ps.snapshot()
        assert snap == ps._state

    def test_returns_a_shallow_copy_not_the_live_dict(self):
        """The frontend route (main.py's pipeline_status_route) returns this
        straight to FastAPI as a response body — it must be a point-in-time
        copy, not a live reference an in-flight cycle could mutate out from
        under a response already being serialized."""
        snap = ps.snapshot()
        assert snap is not ps._state
        snap["running"] = True
        assert ps._state["running"] is False

    def test_reflects_the_not_running_baseline(self):
        assert ps.snapshot() == dict(_BASELINE)

    def test_reflects_a_completed_cycle(self):
        ps.start("AUTO")
        ps.set_stage("scan", "Scan", candidates=["ITC"])
        ps.finish({"symbols_scanned": 5})
        snap = ps.snapshot()
        assert snap["running"] is False
        assert snap["stage"] is None
        assert snap["last_cycle"] == {"symbols_scanned": 5}
        assert snap["candidates"] == ["ITC"]  # survives finish(), per above
