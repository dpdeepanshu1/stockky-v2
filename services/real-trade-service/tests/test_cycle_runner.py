"""
tests/test_cycle_runner.py

100%-coverage-plan round for cycle_runner.py (was 7% — 114 of 122 statements
never executed by any test; every caller in the suite mocks run_cycle_core
out at its own boundary, so the function itself had no direct coverage).

cycle_runner.py is the single implementation of "run one evaluation cycle
for a mode" — the manual Run Cycle route, Auto-Pilot's background timer and
the enter-at-open scheduler all funnel through it — so this file pins its
OWN control flow, not the stages it calls. Every collaborator (candidates,
entry, exit, watchlist, dynamic universe, reconcile, credentials, local
cache, the shared exit lock, pipeline_status, notifier, tz_utils) is
replaced with a recording fake; the fakes share one ordered call log so the
tests can assert exact sequencing, not just "was called".

What is covered:
  * run_cycle_core wrapper — manual-trigger market-hours warning (closed /
    open / non-manual triggers skip the check entirely / notify failure and
    clock failure both non-fatal), mode upper-casing, best-effort
    pipeline_status calls (a raising pstat can never break a cycle), and
    the error path (end_cycle(error=...) then re-raise; a raising end_cycle
    can't mask the original exception).
  * REAL token pre-flight — token_needs_refresh gating (2026-09-01 fix: was
    ~130 Dhan generateAccessToken calls/day), TOTP failure non-fatal,
    enforce_live_token rejection → early auto_disarmed result with NOTHING
    downstream executed, sync_real_equity before candidates; DEMO skips all
    of it.
  * Stage ordering — dynamic_universe→watchlist chain vs candidates refresh
    run concurrently (session48b), both must finish before entry, then
    entry→fills→expire→snapshot→[exit lock: exit→reconcile]→result.
    Concurrency is proven with asyncio.Events (a regression back to
    sequential execution deadlocks the event and fails the test), not just
    by call order.
  * Dynamic-universe stage (snapshot save, None result, failure non-fatal),
    watchlist stage (each of its three steps failing returns {"error": ...}
    and skips the remaining steps but never blocks the cycle),
    position snapshot (mode passed explicitly — 2026-09-16 fix — and
    failure non-fatal), exit lock (acquired before exit, released after
    reconcile, released on exception, a real threading.Lock actually held
    during exit evaluation), REAL fills coming from reconcile not from
    check_pending_fills, and one end-to-end pass through the REAL
    pipeline_status module.

Run from services/real-trade-service:
    python3 -m pytest tests/test_cycle_runner.py -q \\
        --cov=cycle_runner --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import cycle_runner
import pipeline_status as pstat
import tz_utils
import notifier
from auth import dhan_credentials as creds
from candidate_engine import candidates as cand_mod
from entry_engine import entry as entry_mod
from exit_engine import exit as exit_mod
from execution import auto_pilot as ap
from execution import equity_sync
from execution import reconcile as reconcile_mod
from portfolio import portfolio as portfolio_mod
from resilience import local_cache
from watchlist_engine import dynamic_universe as du_mod
from watchlist_engine import watchlist as wl_mod

_DB = object()  # every collaborator is faked — the session is only passed through


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Rig: installs recording fakes for every collaborator
# ---------------------------------------------------------------------------

class _RecLock:
    """Stand-in for the per-mode exit lock that logs acquire/release."""

    def __init__(self, calls):
        self._calls = calls
        self.held = False

    def acquire(self):
        self._calls.append("exit_lock_acquire")
        self.held = True

    def release(self):
        self._calls.append("exit_lock_release")
        self.held = False


class Rig:
    def __init__(self):
        self.calls: list = []
        self.started: list = []   # pstat.start_cycle(mode, trigger, warning)
        self.ended: list = []     # pstat.end_cycle(mode, result, error)
        self.lock = _RecLock(self.calls)
        self.args: dict = {}      # last call args per collaborator


@pytest.fixture()
def rig(monkeypatch):
    r = Rig()
    calls = r.calls

    # ---- pipeline_status (recorders; the real module is used in the last class)
    def start_cycle(mode, trigger, warning=None):
        r.started.append((mode, trigger, warning))
        calls.append("pstat_start")

    def set_stage(mode, stage):
        calls.append(f"stage:{stage}")

    def end_cycle(mode, result, error=None):
        r.ended.append((mode, result, error))
        calls.append("pstat_end")

    monkeypatch.setattr(pstat, "start_cycle", start_cycle)
    monkeypatch.setattr(pstat, "set_stage", set_stage)
    monkeypatch.setattr(pstat, "end_cycle", end_cycle)

    # ---- tz_utils / notifier (manual-trigger market-hours warning)
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: True)
    r.notified = []

    async def notify_async(text):
        r.notified.append(text)
        return True

    monkeypatch.setattr(notifier, "notify_async", notify_async)

    # ---- REAL token pre-flight
    monkeypatch.setattr(creds, "token_needs_refresh", lambda db: calls.append("token_needs_refresh") or False)
    monkeypatch.setattr(creds, "refresh_if_totp_enabled", lambda db: calls.append("totp_refresh"))

    def enforce_live_token(db, mode):
        calls.append("enforce_live_token")
        r.args["enforce_live_token"] = (db, mode)
        return True, None

    monkeypatch.setattr(creds, "enforce_live_token", enforce_live_token)
    monkeypatch.setattr(equity_sync, "sync_real_equity", lambda db: calls.append("sync_equity"))

    # ---- dynamic universe + watchlist chain
    async def refresh_dynamic_universe(db):
        calls.append("du")
        return None

    async def refresh_watchlist(db, mode):
        calls.append("wl_refresh")
        r.args["refresh_watchlist"] = (db, mode)

    def expire_stale_entries(db, mode):
        calls.append("wl_expire")
        r.args["expire_stale_entries"] = (db, mode)

    async def evaluate_watchlist_entries(db, mode):
        calls.append("wl_eval")
        r.args["evaluate_watchlist_entries"] = (db, mode)
        return {"triggered": 1}

    monkeypatch.setattr(du_mod, "refresh_dynamic_universe", refresh_dynamic_universe)
    monkeypatch.setattr(wl_mod, "refresh_watchlist", refresh_watchlist)
    monkeypatch.setattr(wl_mod, "expire_stale_entries", expire_stale_entries)
    monkeypatch.setattr(entry_mod, "evaluate_watchlist_entries", evaluate_watchlist_entries)

    # ---- candidates
    async def refresh_candidates(db, mode):
        calls.append("candidates")
        r.args["refresh_candidates"] = (db, mode)
        return 3

    monkeypatch.setattr(cand_mod, "refresh_candidates", refresh_candidates)

    # ---- entry / fills / expire
    async def entry_evaluate(db, mode, gate_armed):
        calls.append("entry")
        r.args["entry_evaluate"] = (db, mode, gate_armed)
        return {"evaluated": 2, "entered": 1, "waited": 0, "rejected": 1, "entry_details": []}

    async def check_pending_fills(db, mode):
        calls.append("fills")
        return 4

    async def expire_stale_orders(db, mode):
        calls.append("expire")
        return 2

    monkeypatch.setattr(entry_mod, "evaluate_mode", entry_evaluate)
    monkeypatch.setattr(entry_mod, "check_pending_fills", check_pending_fills)
    monkeypatch.setattr(entry_mod, "expire_stale_orders", expire_stale_orders)

    # ---- position snapshot
    monkeypatch.setattr(portfolio_mod, "open_positions", lambda db, mode: ["POS-A", "POS-B"])

    def snapshot_open_positions(db, mode, positions):
        calls.append("snapshot")
        r.args["snapshot"] = (db, mode, positions)

    def save_snapshot(db, key, payload):
        calls.append("save_snapshot")
        r.args["save_snapshot"] = (db, key, payload)

    monkeypatch.setattr(local_cache, "snapshot_open_positions", snapshot_open_positions)
    monkeypatch.setattr(local_cache, "save_snapshot", save_snapshot)

    # ---- exit lock + exit evaluation + reconcile
    monkeypatch.setattr(ap, "_get_exit_lock", lambda mode: r.lock)
    monkeypatch.setattr(ap, "_mark_reconciled", lambda mode: calls.append(f"mark_reconciled:{mode}"))

    async def exit_evaluate(db, mode):
        calls.append("exit")
        return {"full_exits": 1, "partial_exits": 0}

    async def reconcile_real_orders(db):
        calls.append("reconcile")
        return {"entries_filled": 7, "checked": 9}

    monkeypatch.setattr(exit_mod, "evaluate_mode", exit_evaluate)
    monkeypatch.setattr(reconcile_mod, "reconcile_real_orders", reconcile_real_orders)

    return r


def _cycle(mode="REAL", armed=True, trigger="autopilot"):
    return run(cycle_runner.run_cycle_core(_DB, mode, armed, trigger=trigger))


def _stub_stages(monkeypatch, **overrides):
    """Neutral fakes for every pipeline stage, WITHOUT touching pipeline_status
    or the exit lock — for tests that want the real ones. Any stage can be
    overridden by keyword (entry_evaluate, exit_evaluate, refresh_candidates)."""
    async def noop(*a, **k):
        return None

    async def zero(*a, **k):
        return 0

    monkeypatch.setattr(du_mod, "refresh_dynamic_universe", noop)
    monkeypatch.setattr(wl_mod, "refresh_watchlist", noop)
    monkeypatch.setattr(wl_mod, "expire_stale_entries", lambda db, m: None)
    monkeypatch.setattr(entry_mod, "evaluate_watchlist_entries", noop)
    monkeypatch.setattr(entry_mod, "evaluate_mode", overrides.get("entry_evaluate", noop))
    monkeypatch.setattr(entry_mod, "check_pending_fills", zero)
    monkeypatch.setattr(entry_mod, "expire_stale_orders", zero)
    monkeypatch.setattr(exit_mod, "evaluate_mode", overrides.get("exit_evaluate", noop))
    monkeypatch.setattr(cand_mod, "refresh_candidates", overrides.get("refresh_candidates", zero))
    monkeypatch.setattr(portfolio_mod, "open_positions", lambda db, m: [])
    monkeypatch.setattr(local_cache, "snapshot_open_positions", lambda db, m, p: None)


# ===========================================================================
# run_cycle_core — wrapper behaviour
# ===========================================================================

class TestMarketHoursWarning:
    def test_manual_cycle_outside_market_hours_warns_and_notifies(self, rig, monkeypatch):
        monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: False)
        result = _cycle("REAL", trigger="manual")
        warning = result["pre_market_warning"]
        assert "outside market hours" in warning
        assert " IST)" in warning
        # dashboard gets the same warning the caller does
        assert rig.started == [("REAL", "manual", warning)]
        assert len(rig.notified) == 1
        assert "Manual Run Cycle (REAL)" in rig.notified[0]
        # ...and it is informational only: the cycle still ran end to end
        assert "entry" in rig.calls and "exit" in rig.calls

    def test_manual_cycle_during_market_hours_has_no_warning(self, rig):
        result = _cycle("REAL", trigger="manual")
        assert "pre_market_warning" not in result
        assert rig.started == [("REAL", "manual", None)]
        assert rig.notified == []

    @pytest.mark.parametrize("trigger", ["autopilot", "enter_at_open"])
    def test_non_manual_triggers_never_check_market_hours(self, rig, monkeypatch, trigger):
        seen = []
        monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: seen.append(1) or False)
        result = _cycle("REAL", trigger=trigger)
        assert seen == []                       # gate not even consulted
        assert "pre_market_warning" not in result
        assert rig.notified == []
        assert rig.started == [("REAL", trigger, None)]

    def test_notify_failure_is_non_fatal_but_warning_still_reported(self, rig, monkeypatch):
        monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: False)

        async def boom(text):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(notifier, "notify_async", boom)
        result = _cycle("REAL", trigger="manual")
        assert "outside market hours" in result["pre_market_warning"]
        assert "exit" in rig.calls

    def test_market_hours_check_failure_is_non_fatal(self, rig, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("holiday table missing")

        monkeypatch.setattr(tz_utils, "is_market_open_ist", broken)
        result = _cycle("REAL", trigger="manual")
        assert "pre_market_warning" not in result   # no warning computed...
        assert "exit" in rig.calls                  # ...but the cycle proceeded

    def test_early_token_reject_result_also_carries_the_warning(self, rig, monkeypatch):
        monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda *a, **k: False)
        monkeypatch.setattr(creds, "enforce_live_token", lambda db, mode: (False, "expired"))
        result = _cycle("REAL", trigger="manual")
        assert result["auto_disarmed"].endswith("expired")
        assert "outside market hours" in result["pre_market_warning"]


class TestModeNormalisation:
    def test_lowercase_mode_is_uppercased_everywhere(self, rig):
        result = _cycle("demo")
        assert result["mode"] == "DEMO"
        assert rig.started[0][0] == "DEMO"
        assert rig.args["entry_evaluate"][1] == "DEMO"
        assert rig.args["snapshot"][1] == "DEMO"
        assert "reconcile" not in rig.calls     # DEMO never reconciles against Dhan

    def test_lowercase_real_still_takes_the_real_path(self, rig):
        result = _cycle("real")
        assert result["mode"] == "REAL"
        assert "enforce_live_token" in rig.calls
        assert "reconcile" in rig.calls


class TestOverlappingStageTimings:
    """session110: dynamic_universe -> watchlist and candidates run concurrently
    (session48b) but pipeline_status only has one current-stage slot, so their
    reported timings overwrote each other (candidates 300 ms real -> ~50 ms
    reported). Uses the REAL pipeline_status with real asyncio sleeps; only
    LOWER bounds are asserted, so a slow CI box can't make it flaky."""

    @pytest.fixture(autouse=True)
    def _clean_pstat(self):
        pstat._STATE.pop("DEMO", None)
        pstat._HISTORY["DEMO"].clear()
        yield
        pstat._STATE.pop("DEMO", None)
        pstat._HISTORY["DEMO"].clear()

    def test_each_overlapping_stage_reports_its_own_real_duration(self, monkeypatch):
        async def slow_universe(db):
            await asyncio.sleep(0.02)

        async def slow_watchlist(db, mode):
            await asyncio.sleep(0.05)

        async def slow_candidates(db, mode):
            await asyncio.sleep(0.50)
            return 3

        _stub_stages(monkeypatch, refresh_candidates=slow_candidates)
        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", slow_universe)
        monkeypatch.setattr(wl_mod, "refresh_watchlist", slow_watchlist)

        run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="autopilot"))

        t = pstat.get_status("DEMO")["last_cycle"]["stage_timings_ms"]
        # Lower bounds are each stage's own sleep (a slow box can only make a
        # sleep LONGER). The generous upper bounds on the two short stages are
        # what catch the old bug: the single current-stage slot charged the
        # watchlist for the whole wait on candidates (~480 ms) and gave
        # dynamic_universe ~0.
        assert t["candidates"] >= 490
        assert 48 <= t["watchlist"] < 300
        assert 19 <= t["dynamic_universe"] < 300
        # sequential stages are unaffected and still present
        for stage in ("entry", "fills", "expire", "exit"):
            assert stage in t

    def test_timers_are_closed_even_when_a_stage_fails(self, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("candidates 500")

        _stub_stages(monkeypatch, refresh_candidates=boom)
        with pytest.raises(RuntimeError, match="candidates 500"):
            run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="manual"))
        assert "candidates" in pstat.get_status("DEMO")["last_cycle"]["stage_timings_ms"]

    def test_a_failing_watchlist_stage_still_records_its_time(self, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("catalyst source down")

        _stub_stages(monkeypatch)
        monkeypatch.setattr(wl_mod, "refresh_watchlist", boom)
        run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="manual"))   # non-fatal by design
        assert "watchlist" in pstat.get_status("DEMO")["last_cycle"]["stage_timings_ms"]


class TestStatusTrackingIsBestEffort:
    def test_stage_timer_failure_never_blocks_the_cycle(self, rig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pstat broken")

        monkeypatch.setattr(pstat, "stage_started", boom)
        monkeypatch.setattr(pstat, "stage_finished", boom)
        result = _cycle("REAL")
        assert result["new_candidates"] == 3
        for step in ("du", "wl_eval", "candidates", "entry", "exit", "reconcile"):
            assert step in rig.calls


    def test_start_cycle_failure_never_blocks_the_cycle(self, rig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pstat broken")

        monkeypatch.setattr(pstat, "start_cycle", boom)
        result = _cycle("DEMO")
        assert result["new_candidates"] == 3

    def test_end_cycle_failure_on_success_path_still_returns_result(self, rig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pstat broken")

        monkeypatch.setattr(pstat, "end_cycle", boom)
        result = _cycle("DEMO")
        assert result["fills"] == 4

    def test_set_stage_failure_never_blocks_the_cycle(self, rig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pstat broken")

        monkeypatch.setattr(pstat, "set_stage", boom)
        result = _cycle("REAL")
        # every stage ran despite every _stage() call raising
        for step in ("du", "wl_eval", "candidates", "entry", "fills", "expire", "exit", "reconcile"):
            assert step in rig.calls
        assert result["exit"] == {"full_exits": 1, "partial_exits": 0}

    def test_failure_closes_the_status_record_with_the_error_then_reraises(self, rig, monkeypatch):
        async def boom(db, mode, gate_armed):
            raise RuntimeError("entry exploded")

        monkeypatch.setattr(entry_mod, "evaluate_mode", boom)
        with pytest.raises(RuntimeError, match="entry exploded"):
            _cycle("DEMO")
        assert rig.ended == [("DEMO", {}, "RuntimeError: entry exploded")]

    def test_failing_end_cycle_cannot_mask_the_original_exception(self, rig, monkeypatch):
        async def boom(db, mode, gate_armed):
            raise ValueError("the real problem")

        def bad_end(*a, **k):
            raise RuntimeError("pstat also broken")

        monkeypatch.setattr(entry_mod, "evaluate_mode", boom)
        monkeypatch.setattr(pstat, "end_cycle", bad_end)
        with pytest.raises(ValueError, match="the real problem"):
            _cycle("DEMO")

    def test_success_closes_the_status_record_once_with_the_result(self, rig):
        result = _cycle("DEMO")
        assert len(rig.ended) == 1
        mode, ended_result, err = rig.ended[0]
        assert (mode, err) == ("DEMO", None)
        assert ended_result is result


# ===========================================================================
# REAL-mode token pre-flight
# ===========================================================================

class TestRealTokenPreflight:
    def test_totp_refresh_runs_before_liveness_check_when_token_needs_it(self, rig, monkeypatch):
        monkeypatch.setattr(creds, "token_needs_refresh", lambda db: rig.calls.append("token_needs_refresh") or True)
        _cycle("REAL")
        i = rig.calls
        assert i.index("token_needs_refresh") < i.index("totp_refresh") < i.index("enforce_live_token")

    def test_totp_refresh_skipped_when_token_still_fresh(self, rig):
        """Regression pin for the 2026-09-01 fix: refreshing on EVERY cycle
        meant ~130 generateAccessToken calls/day + a Telegram every 3 min."""
        _cycle("REAL")
        assert "token_needs_refresh" in rig.calls
        assert "totp_refresh" not in rig.calls

    def test_token_needs_refresh_raising_is_non_fatal(self, rig, monkeypatch):
        def boom(db):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(creds, "token_needs_refresh", boom)
        _cycle("REAL")
        assert "enforce_live_token" in rig.calls and "reconcile" in rig.calls

    def test_totp_refresh_raising_is_non_fatal(self, rig, monkeypatch):
        monkeypatch.setattr(creds, "token_needs_refresh", lambda db: True)

        def boom(db):
            raise RuntimeError("totp secret invalid")

        monkeypatch.setattr(creds, "refresh_if_totp_enabled", boom)
        _cycle("REAL")
        assert "enforce_live_token" in rig.calls and "reconcile" in rig.calls

    def test_enforce_live_token_receives_db_and_mode(self, rig):
        _cycle("REAL")
        assert rig.args["enforce_live_token"] == (_DB, "REAL")

    def test_rejected_token_returns_early_result_and_runs_nothing_downstream(self, rig, monkeypatch):
        monkeypatch.setattr(creds, "enforce_live_token", lambda db, mode: (False, "invalid_token 807"))
        result = _cycle("REAL")
        assert result == {
            "mode": "REAL", "new_candidates": 0,
            "entry": {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0, "entry_details": []},
            "fills": 0, "expired_orders": 0, "exit": {}, "reconcile": None,
            "auto_disarmed": "Dhan token rejected: invalid_token 807",
        }
        # Nothing that touches the broker, the DB pipeline or the exit lock ran.
        for step in ("sync_equity", "du", "wl_refresh", "candidates", "entry", "fills",
                     "expire", "snapshot", "exit_lock_acquire", "exit", "reconcile"):
            assert step not in rig.calls
        assert not any(c.startswith("stage:") for c in rig.calls)

    def test_rejected_token_closes_the_status_record_with_the_early_result(self, rig, monkeypatch):
        monkeypatch.setattr(creds, "enforce_live_token", lambda db, mode: (False, "expired"))
        result = _cycle("REAL")
        assert rig.ended == [("REAL", result, None)]

    def test_rejected_token_end_cycle_failure_is_swallowed(self, rig, monkeypatch):
        monkeypatch.setattr(creds, "enforce_live_token", lambda db, mode: (False, "expired"))

        def boom(*a, **k):
            raise RuntimeError("pstat broken")

        monkeypatch.setattr(pstat, "end_cycle", boom)
        result = _cycle("REAL")
        assert result["auto_disarmed"] == "Dhan token rejected: expired"

    def test_equity_is_synced_from_dhan_before_any_candidate_work(self, rig):
        _cycle("REAL")
        c = rig.calls
        assert c.index("enforce_live_token") < c.index("sync_equity") < c.index("candidates")
        assert c.index("sync_equity") < c.index("du")

    def test_demo_mode_skips_the_whole_preflight(self, rig):
        _cycle("DEMO")
        for step in ("token_needs_refresh", "totp_refresh", "enforce_live_token", "sync_equity"):
            assert step not in rig.calls


# ===========================================================================
# Stage ordering, results and concurrency
# ===========================================================================

class TestPipelineOrderingAndResult:
    def test_real_cycle_full_tail_order(self, rig):
        """entry -> fills -> expire -> snapshot -> exit lock [exit -> reconcile
        -> mark_reconciled] -> pstat_end, with nothing interleaved."""
        _cycle("REAL")
        c = rig.calls
        tail = [x for x in c[c.index("entry"):] if not x.startswith("stage:")]
        assert tail == [
            "entry", "fills", "expire", "snapshot",
            "exit_lock_acquire", "exit", "reconcile", "mark_reconciled:REAL", "exit_lock_release",
            "pstat_end",
        ]

    def test_demo_cycle_has_no_reconcile_and_uses_check_pending_fills(self, rig):
        result = _cycle("DEMO")
        c = rig.calls
        assert "reconcile" not in c and not any(x.startswith("mark_reconciled") for x in c)
        assert result["reconcile"] is None
        assert result["fills"] == 4          # from check_pending_fills
        # exit stage still runs under the exit lock in DEMO
        assert c.index("exit_lock_acquire") < c.index("exit") < c.index("exit_lock_release")

    def test_real_fills_come_from_reconcile_not_check_pending_fills(self, rig):
        """REAL 'fills' only ever means broker-confirmed (cycle_runner's own
        comment) — the DEMO-simulated check_pending_fills count is replaced."""
        result = _cycle("REAL")
        assert result["fills"] == 7
        assert result["reconcile"] == {"entries_filled": 7, "checked": 9}

    def test_result_shape_and_values(self, rig):
        result = _cycle("REAL")
        assert result == {
            "mode": "REAL",
            "watchlist": {"triggered": 1},
            "new_candidates": 3,
            "entry": {"evaluated": 2, "entered": 1, "waited": 0, "rejected": 1, "entry_details": []},
            "fills": 7,
            "expired_orders": 2,
            "exit": {"full_exits": 1, "partial_exits": 0},
            "reconcile": {"entries_filled": 7, "checked": 9},
        }

    @pytest.mark.parametrize("armed", [True, False])
    def test_gate_armed_is_passed_through_to_entry_unchanged(self, rig, armed):
        _cycle("DEMO", armed=armed)
        assert rig.args["entry_evaluate"] == (_DB, "DEMO", armed)

    def test_stage_labels_real(self, rig):
        _cycle("REAL")
        stages = [c[6:] for c in rig.calls if c.startswith("stage:")]
        assert set(stages[:3]) == {"dynamic_universe", "watchlist", "candidates"}
        assert stages[3:] == ["entry", "fills", "expire", "exit", "reconcile"]

    def test_stage_labels_demo_omit_reconcile(self, rig):
        _cycle("DEMO")
        stages = [c[6:] for c in rig.calls if c.startswith("stage:")]
        assert stages[3:] == ["entry", "fills", "expire", "exit"]

    def test_candidate_and_watchlist_stages_receive_db_and_mode(self, rig):
        _cycle("DEMO")
        assert rig.args["refresh_candidates"] == (_DB, "DEMO")
        assert rig.args["refresh_watchlist"] == (_DB, "DEMO")
        assert rig.args["expire_stale_entries"] == (_DB, "DEMO")
        assert rig.args["evaluate_watchlist_entries"] == (_DB, "DEMO")

    def test_entry_failure_stops_the_cycle_before_exit_or_the_exit_lock(self, rig, monkeypatch):
        async def boom(db, mode, gate_armed):
            raise RuntimeError("entry exploded")

        monkeypatch.setattr(entry_mod, "evaluate_mode", boom)
        with pytest.raises(RuntimeError):
            _cycle("REAL")
        for step in ("fills", "expire", "snapshot", "exit_lock_acquire", "exit", "reconcile"):
            assert step not in rig.calls

    def test_candidate_refresh_failure_aborts_the_cycle(self, rig, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("candidate db error")

        monkeypatch.setattr(cand_mod, "refresh_candidates", boom)
        with pytest.raises(RuntimeError, match="candidate db error"):
            _cycle("DEMO")
        assert "entry" not in rig.calls and "exit_lock_acquire" not in rig.calls
        assert rig.ended[0][2] == "RuntimeError: candidate db error"


class TestConcurrency:
    """session48b: dynamic_universe→watchlist chain and the candidates refresh
    run concurrently, and BOTH must finish before entry_evaluate starts."""

    def test_candidates_start_while_the_dynamic_universe_chain_is_still_running(self, rig, monkeypatch):
        cand_started = asyncio.Event()
        state = {}

        async def du(db):
            try:
                await asyncio.wait_for(cand_started.wait(), 0.5)
                state["overlap"] = True
            except asyncio.TimeoutError:  # pragma: no cover - only reached if the stages regress to sequential
                state["overlap"] = False   # sequential execution would land here

        async def cands(db, mode):
            cand_started.set()
            return 1

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", du)
        monkeypatch.setattr(cand_mod, "refresh_candidates", cands)
        _cycle("DEMO")
        assert state["overlap"] is True

    def test_dynamic_universe_chain_starts_while_candidates_are_still_running(self, rig, monkeypatch):
        du_started = asyncio.Event()
        state = {}

        async def du(db):
            du_started.set()

        async def cands(db, mode):
            try:
                await asyncio.wait_for(du_started.wait(), 0.5)
                state["overlap"] = True
            except asyncio.TimeoutError:  # pragma: no cover - only reached if the stages regress to sequential
                state["overlap"] = False
            return 1

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", du)
        monkeypatch.setattr(cand_mod, "refresh_candidates", cands)
        _cycle("DEMO")
        assert state["overlap"] is True

    def test_entry_waits_for_a_slow_candidates_refresh(self, rig, monkeypatch):
        async def slow_cands(db, mode):
            await asyncio.sleep(0.05)
            rig.calls.append("cands_end")
            return 1

        monkeypatch.setattr(cand_mod, "refresh_candidates", slow_cands)
        _cycle("DEMO")
        assert rig.calls.index("cands_end") < rig.calls.index("entry")

    def test_entry_waits_for_a_slow_watchlist_chain(self, rig, monkeypatch):
        async def slow_eval(db, mode):
            await asyncio.sleep(0.05)
            rig.calls.append("wl_eval_end")
            return {"triggered": 0}

        monkeypatch.setattr(entry_mod, "evaluate_watchlist_entries", slow_eval)
        _cycle("DEMO")
        assert rig.calls.index("wl_eval_end") < rig.calls.index("entry")

    def test_watchlist_stage_starts_only_after_dynamic_universe_finishes(self, rig, monkeypatch):
        async def slow_du(db):
            await asyncio.sleep(0.05)
            rig.calls.append("du_end")

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", slow_du)
        _cycle("DEMO")
        assert rig.calls.index("du_end") < rig.calls.index("wl_refresh")

    def test_chain_internal_order_du_then_refresh_expire_eval(self, rig):
        _cycle("DEMO")
        chain = [c for c in rig.calls if c in ("du", "wl_refresh", "wl_expire", "wl_eval")]
        assert chain == ["du", "wl_refresh", "wl_expire", "wl_eval"]


# ===========================================================================
# Dynamic-universe stage
# ===========================================================================

class TestDynamicUniverseStage:
    def test_result_is_snapshotted_with_mode_and_utc_timestamp(self, rig, monkeypatch):
        async def du(db):
            return {"subscribed": 42, "added": 3}

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", du)
        _cycle("REAL")
        db, key, payload = rig.args["save_snapshot"]
        assert (db, key) == (_DB, "dynamic_universe_last")
        assert payload["subscribed"] == 42 and payload["added"] == 3
        assert payload["mode"] == "REAL"
        ts = datetime.fromisoformat(payload["synced_at"])
        assert ts.tzinfo is not None and ts.utcoffset().total_seconds() == 0

    def test_none_result_means_throttled_no_snapshot_written(self, rig):
        _cycle("REAL")            # default fake returns None
        assert "save_snapshot" not in rig.calls

    def test_refresh_failure_is_non_fatal_and_watchlist_still_runs(self, rig, monkeypatch, caplog):
        async def du(db):
            raise RuntimeError("angelone subscribe failed")

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", du)
        with caplog.at_level(logging.WARNING, logger="real-trade-cycle"):
            result = _cycle("DEMO")
        assert "dynamic universe refresh failed" in caplog.text
        assert "wl_refresh" in rig.calls and "wl_eval" in rig.calls
        assert result["watchlist"] == {"triggered": 1}

    def test_snapshot_save_failure_is_non_fatal(self, rig, monkeypatch, caplog):
        async def du(db):
            return {"subscribed": 1}

        def boom(db, key, payload):
            raise RuntimeError("kv write failed")

        monkeypatch.setattr(du_mod, "refresh_dynamic_universe", du)
        monkeypatch.setattr(local_cache, "save_snapshot", boom)
        with caplog.at_level(logging.WARNING, logger="real-trade-cycle"):
            result = _cycle("DEMO")
        assert "dynamic universe refresh failed" in caplog.text
        assert result["watchlist"] == {"triggered": 1}


# ===========================================================================
# Watchlist stage
# ===========================================================================

class TestWatchlistStage:
    def test_refresh_watchlist_failure_returns_error_and_skips_remaining_steps(self, rig, monkeypatch, caplog):
        async def boom(db, mode):
            raise RuntimeError("source fetch failed")

        monkeypatch.setattr(wl_mod, "refresh_watchlist", boom)
        with caplog.at_level(logging.WARNING, logger="real-trade-cycle"):
            result = _cycle("DEMO")
        assert result["watchlist"] == {"error": "source fetch failed"}
        assert "watchlist stage failed" in caplog.text
        assert "wl_expire" not in rig.calls and "wl_eval" not in rig.calls
        # the cycle itself carried on — entry/exit still ran
        assert "entry" in rig.calls and "exit" in rig.calls

    def test_expire_stale_entries_failure_returns_error_and_skips_evaluation(self, rig, monkeypatch):
        def boom(db, mode):
            raise RuntimeError("expire failed")

        monkeypatch.setattr(wl_mod, "expire_stale_entries", boom)
        result = _cycle("DEMO")
        assert result["watchlist"] == {"error": "expire failed"}
        assert "wl_eval" not in rig.calls
        assert "entry" in rig.calls

    def test_evaluate_watchlist_entries_failure_returns_error(self, rig, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("band check failed")

        monkeypatch.setattr(entry_mod, "evaluate_watchlist_entries", boom)
        result = _cycle("DEMO")
        assert result["watchlist"] == {"error": "band check failed"}
        assert "entry" in rig.calls

    def test_a_watchlist_failure_does_not_touch_candidates_count(self, rig, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("x")

        monkeypatch.setattr(wl_mod, "refresh_watchlist", boom)
        assert _cycle("DEMO")["new_candidates"] == 3


# ===========================================================================
# Position snapshot
# ===========================================================================

class TestPositionSnapshot:
    def test_snapshot_gets_mode_explicitly_and_the_live_open_positions(self, rig):
        """2026-09-16 fix: mode is passed explicitly so the snapshot is still
        written when open positions are empty (old code inferred mode from
        positions[0] and silently skipped writing, freezing the snapshot)."""
        _cycle("REAL")
        assert rig.args["snapshot"] == (_DB, "REAL", ["POS-A", "POS-B"])

    def test_snapshot_still_written_when_there_are_no_open_positions(self, rig, monkeypatch):
        monkeypatch.setattr(portfolio_mod, "open_positions", lambda db, mode: [])
        _cycle("REAL")
        assert rig.args["snapshot"] == (_DB, "REAL", [])

    def test_snapshot_happens_before_exit_evaluation(self, rig):
        _cycle("REAL")
        assert rig.calls.index("snapshot") < rig.calls.index("exit_lock_acquire") < rig.calls.index("exit")

    def test_snapshot_failure_is_non_fatal(self, rig, monkeypatch, caplog):
        def boom(db, mode, positions):
            raise RuntimeError("kv unavailable")

        monkeypatch.setattr(local_cache, "snapshot_open_positions", boom)
        with caplog.at_level(logging.WARNING, logger="real-trade-cycle"):
            result = _cycle("REAL")
        assert "position snapshot failed" in caplog.text
        assert result["exit"] == {"full_exits": 1, "partial_exits": 0}

    def test_open_positions_lookup_failure_is_non_fatal(self, rig, monkeypatch, caplog):
        def boom(db, mode):
            raise RuntimeError("db locked")

        monkeypatch.setattr(portfolio_mod, "open_positions", boom)
        with caplog.at_level(logging.WARNING, logger="real-trade-cycle"):
            _cycle("REAL")
        assert "position snapshot failed" in caplog.text
        assert "exit" in rig.calls and "reconcile" in rig.calls


# ===========================================================================
# Exit lock
# ===========================================================================

class TestExitLock:
    def test_lock_wraps_exit_and_reconcile_and_nothing_else(self, rig):
        _cycle("REAL")
        c = rig.calls
        a, r = c.index("exit_lock_acquire"), c.index("exit_lock_release")
        inside = [x for x in c[a + 1:r] if not x.startswith("stage:")]
        assert inside == ["exit", "reconcile", "mark_reconciled:REAL"]
        # entry-side work happens strictly outside (before) the exit lock
        for outside in ("entry", "fills", "expire", "snapshot"):
            assert c.index(outside) < a
        assert rig.lock.held is False

    def test_lock_is_requested_for_the_cycles_own_mode(self, rig, monkeypatch):
        seen = []
        monkeypatch.setattr(ap, "_get_exit_lock", lambda mode: seen.append(mode) or rig.lock)
        _cycle("DEMO")
        assert seen == ["DEMO"]

    def test_lock_released_when_exit_evaluation_raises(self, rig, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("exit exploded")

        monkeypatch.setattr(exit_mod, "evaluate_mode", boom)
        with pytest.raises(RuntimeError, match="exit exploded"):
            _cycle("REAL")
        assert rig.lock.held is False
        assert rig.calls.count("exit_lock_release") == 1
        assert "reconcile" not in rig.calls

    def test_lock_released_when_reconcile_raises(self, rig, monkeypatch):
        async def boom(db):
            raise RuntimeError("dhan orderbook down")

        monkeypatch.setattr(reconcile_mod, "reconcile_real_orders", boom)
        with pytest.raises(RuntimeError, match="dhan orderbook down"):
            _cycle("REAL")
        assert rig.lock.held is False
        assert "mark_reconciled:REAL" not in rig.calls   # only marked on success

    def test_a_real_threading_lock_is_actually_held_during_exit_evaluation(self, monkeypatch):
        """No rig here: the module's real per-mode threading.Lock is used, so
        this proves the lock is genuinely held while exit evaluation runs and
        genuinely free again afterwards (a manual Close Position from another
        thread contends with exactly this window)."""
        held_during = {}

        async def exit_evaluate(db, mode):
            held_during["locked"] = ap._get_exit_lock("DEMO").locked()
            return {}

        _stub_stages(monkeypatch, exit_evaluate=exit_evaluate)
        assert ap._get_exit_lock("DEMO").locked() is False
        run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="autopilot"))
        assert held_during["locked"] is True
        assert ap._get_exit_lock("DEMO").locked() is False

    def test_real_lock_is_released_even_when_exit_evaluation_raises(self, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("exit exploded")

        _stub_stages(monkeypatch, exit_evaluate=boom)
        with pytest.raises(RuntimeError):
            run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="autopilot"))
        assert ap._get_exit_lock("DEMO").locked() is False


# ===========================================================================
# End-to-end through the REAL pipeline_status module
# ===========================================================================

class TestRealPipelineStatusIntegration:
    """Uses the REAL pipeline_status module (no recorders) so the contract
    between cycle_runner and the dashboard's Pipeline tab is exercised for
    real: a live record while running, a history entry afterwards."""

    @pytest.fixture(autouse=True)
    def _clean_pstat(self):
        pstat._STATE.pop("DEMO", None)
        pstat._HISTORY["DEMO"].clear()
        yield
        pstat._STATE.pop("DEMO", None)
        pstat._HISTORY["DEMO"].clear()

    def test_a_completed_cycle_is_recorded_in_the_dashboard_history(self, monkeypatch):
        async def entry_ok(db, mode, armed):
            return {"evaluated": 5, "entered": 2, "waited": 1, "rejected": 2,
                    "entry_details": [{"symbol": "TCS"}]}

        async def exit_ok(db, mode):
            return {"full_exits": 1, "partial_exits": 3}

        async def cands(db, mode):
            return 6

        _stub_stages(monkeypatch, entry_evaluate=entry_ok, exit_evaluate=exit_ok, refresh_candidates=cands)

        before = pstat.get_status("DEMO")
        assert before["running"] is False and before["last_cycle"] is None

        run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="autopilot"))

        after = pstat.get_status("DEMO")
        assert after["running"] is False           # end_cycle closed the live record
        rec = after["last_cycle"]
        assert rec["trigger"] == "autopilot"
        assert rec["new_candidates"] == 6
        assert (rec["entered"], rec["waited"], rec["rejected"]) == (2, 1, 2)
        assert rec["entry_details"] == [{"symbol": "TCS"}]
        assert (rec["full_exits"], rec["partial_exits"]) == (1, 3)
        assert rec["error"] is None

    def test_cycle_is_visible_as_running_at_the_entry_stage_while_it_executes(self, monkeypatch):
        seen = {}

        async def entry_spy(db, mode, armed):
            seen["status"] = pstat.get_status("DEMO")
            return {}

        _stub_stages(monkeypatch, entry_evaluate=entry_spy)
        run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="manual"))
        assert seen["status"]["running"] is True
        assert seen["status"]["stage"] == "entry"
        assert seen["status"]["trigger"] == "manual"

    def test_a_failed_cycle_is_recorded_with_its_error(self, monkeypatch):
        async def boom(db, mode):
            raise RuntimeError("candidates 500")

        _stub_stages(monkeypatch, refresh_candidates=boom)
        with pytest.raises(RuntimeError, match="candidates 500"):
            run(cycle_runner.run_cycle_core(_DB, "DEMO", True, trigger="manual"))
        status = pstat.get_status("DEMO")
        assert status["running"] is False
        assert status["last_cycle"]["error"] == "RuntimeError: candidates 500"
