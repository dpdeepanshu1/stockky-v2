"""
tests/test_auto_pilot_orchestration.py

100%-coverage-plan follow-up, round 2: execution/auto_pilot.py -- 31% (after
round 1's helper-function tests, see test_auto_pilot_helpers.py) -> this
round targets the cycle-orchestration layer round 1 deliberately left out:
the worker-thread lock wrappers, the three tick bodies (fast-exit, full-cycle,
schedule), the scheduled-automation functions they call (pre-pick, enter-at-
open, eDIS morning check, EOD square-off, EOD signal scan), the after-hours
scan tick's remaining branches, the five background loops, and start().

Run from services/real-trade-service:
    python3 -m pytest tests/test_auto_pilot_orchestration.py -q \
        --cov=execution.auto_pilot --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import auto_pilot as ap

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _fresh_session_factory(monkeypatch):
    """Every tick body under test calls get_session_factory() itself and
    opens/closes its own Session — point that at our in-memory engine so
    the body's own db.close() doesn't touch a fixture-owned session."""
    factory = sessionmaker(bind=_engine)
    monkeypatch.setattr(ap, "get_session_factory", lambda: factory)
    yield


def _gate(mode="REAL", **kw):
    defaults = dict(mode=mode, armed=True, auto_pilot_enabled=True)
    defaults.update(kw)
    return models.TradeGateState(**defaults)


def _position(**kw):
    defaults = dict(
        mode="REAL", symbol="TESTCO", status="OPEN", qty_open=10,
        avg_entry_price=100.0, opened_at=datetime.now(timezone.utc),
        realized_pnl=0.0,
    )
    defaults.update(kw)
    return models.TradePosition(**defaults)


class _FakeTick:
    def __init__(self, price=None, day_high=None, day_low=None):
        self.price = price
        self.day_high = day_high
        self.day_low = day_low


class _account:
    def __init__(self, equity):
        self.current_equity = equity


def _async_return(value=None):
    async def _fake(*a, **kw):
        return value
    return _fake


def _async_raise(exc):
    async def _fake(*a, **kw):
        raise exc
    return _fake


def _async_recorder(sink: list, retval=None):
    async def _fake(*a, **kw):
        sink.append((a, kw))
        return retval
    return _fake


# ---------------------------------------------------------------------------
# Worker-thread lock wrappers
# ---------------------------------------------------------------------------

class TestRunExitTickSync:
    def test_skips_when_lock_already_held(self, monkeypatch):
        lock = ap._get_exit_lock("SKIPTEST")
        lock.acquire()
        try:
            calls = []
            monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda *a, **kw: calls.append(a))
            ap._run_exit_tick_sync("SKIPTEST")
            assert calls == []
        finally:
            lock.release()

    def test_runs_body_and_releases_lock_on_success(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda fn, mode: calls.append((fn, mode)))
        ap._run_exit_tick_sync("RUNTEST")
        assert calls == [(ap._exit_only_tick_body, "RUNTEST")]
        assert ap._get_exit_lock("RUNTEST").acquire(blocking=False) is True
        ap._get_exit_lock("RUNTEST").release()

    def test_releases_lock_even_if_body_raises(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("body blew up")
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", _boom)
        with pytest.raises(RuntimeError):
            ap._run_exit_tick_sync("RAISETEST")
        assert ap._get_exit_lock("RAISETEST").acquire(blocking=False) is True
        ap._get_exit_lock("RAISETEST").release()


class TestRunFullTickSync:
    def test_waits_for_lock_then_runs_body(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda fn, mode: calls.append((fn, mode)))
        ap._run_full_tick_sync("FULLTEST")
        assert calls == [(ap._full_tick_body, "FULLTEST")]
        # lock released after the `with` block
        assert ap._get_lock("FULLTEST").acquire(blocking=False) is True
        ap._get_lock("FULLTEST").release()


class TestRunScheduleTickSync:
    def test_skips_when_entry_lock_already_held(self, monkeypatch):
        lock = ap._get_lock("SCHEDSKIP")
        lock.acquire()
        try:
            calls = []
            monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda *a, **kw: calls.append(a))
            ap._run_schedule_tick_sync("SCHEDSKIP")
            assert calls == []
        finally:
            lock.release()

    def test_runs_body_when_lock_free(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda fn, mode: calls.append((fn, mode)))
        ap._run_schedule_tick_sync("SCHEDRUN")
        assert calls == [(ap._schedule_tick_body, "SCHEDRUN")]


class TestTopLevelAsyncWrappers:
    def test_exit_only_tick_delegates_to_thread(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap.asyncio, "to_thread", _async_recorder(calls))
        run(ap._exit_only_tick("DEMO"))
        assert calls == [((ap._run_exit_tick_sync, "DEMO"), {})]

    def test_full_tick_delegates_to_thread(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap.asyncio, "to_thread", _async_recorder(calls))
        run(ap._full_tick("DEMO"))
        assert calls == [((ap._run_full_tick_sync, "DEMO"), {})]

    def test_schedule_tick_skips_on_non_weekday(self, monkeypatch):
        monkeypatch.setattr(ap, "is_ist_weekday", lambda: False)
        calls = []
        monkeypatch.setattr(ap.asyncio, "to_thread", _async_recorder(calls))
        run(ap._schedule_tick("DEMO"))
        assert calls == []

    def test_schedule_tick_delegates_to_thread_on_weekday(self, monkeypatch):
        monkeypatch.setattr(ap, "is_ist_weekday", lambda: True)
        calls = []
        monkeypatch.setattr(ap.asyncio, "to_thread", _async_recorder(calls))
        run(ap._schedule_tick("DEMO"))
        assert calls == [((ap._run_schedule_tick_sync, "DEMO"), {})]


# ---------------------------------------------------------------------------
# _exit_only_tick_body
# ---------------------------------------------------------------------------

class TestExitOnlyTickBody:
    def test_noop_when_market_closed(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        calls = []
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_recorder(calls, {}))
        run(ap._exit_only_tick_body("REAL"))
        assert calls == []

    def test_runs_exit_evaluate_and_notifies_on_activity(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(
            "exit_engine.exit.evaluate_mode",
            _async_return({"full_exits": 1, "partial_exits": 0, "time_stops": 0, "emergency_exits": 0}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._exit_only_tick_body("REAL"))
        assert len(notes) == 1
        assert "Fast exit tick" in notes[0][0][0]
        assert "gate off" not in notes[0][0][0]

    def test_no_notify_when_nothing_happened(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._exit_only_tick_body("REAL"))
        assert notes == []

    def test_demo_mode_skips_reconcile(self, db, monkeypatch):
        db.add(_gate(mode="DEMO"))
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        calls = []
        monkeypatch.setattr("execution.reconcile.reconcile_real_orders", _async_recorder(calls))
        run(ap._exit_only_tick_body("DEMO"))
        assert calls == []

    def test_real_mode_reconciles_when_due(self, db, monkeypatch):
        db.add(_gate(mode="REAL"))
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        monkeypatch.setattr(ap, "_reconcile_due", lambda mode: True)
        marked = []
        monkeypatch.setattr(ap, "_mark_reconciled", lambda mode: marked.append(mode))
        calls = []
        monkeypatch.setattr("execution.reconcile.reconcile_real_orders", _async_recorder(calls))
        run(ap._exit_only_tick_body("REAL"))
        assert len(calls) == 1
        assert marked == ["REAL"]

    def test_real_mode_skips_reconcile_when_not_due(self, db, monkeypatch):
        db.add(_gate(mode="REAL"))
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        monkeypatch.setattr(ap, "_reconcile_due", lambda mode: False)
        calls = []
        monkeypatch.setattr("execution.reconcile.reconcile_real_orders", _async_recorder(calls))
        run(ap._exit_only_tick_body("REAL"))
        assert calls == []

    def test_alerts_when_gate_off_and_open_positions(self, db, monkeypatch):
        db.add(_gate(armed=False))
        db.add(_position())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        alert_calls = []
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_recorder(alert_calls))
        run(ap._exit_only_tick_body("REAL"))
        assert len(alert_calls) == 1

    def test_no_alert_call_when_gate_armed(self, db, monkeypatch):
        db.add(_gate(armed=True, auto_pilot_enabled=True))
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_return({}))
        alert_calls = []
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_recorder(alert_calls))
        run(ap._exit_only_tick_body("REAL"))
        assert alert_calls == []

    def test_gate_note_appended_to_notify_when_disarmed_with_activity(self, db, monkeypatch):
        db.add(_gate(armed=False))
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(
            "exit_engine.exit.evaluate_mode", _async_return({"emergency_exits": 1}),
        )
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_return(None))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._exit_only_tick_body("REAL"))
        assert "gate off — protective exit only" in notes[0][0][0]

    def test_exception_logged_and_notified_never_raises(self, db, monkeypatch, caplog):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("exit_engine.exit.evaluate_mode", _async_raise(RuntimeError("boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            run(ap._exit_only_tick_body("REAL"))  # must not raise
        assert len(notes) == 1
        assert "Fast exit tick error" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _full_tick_body
# ---------------------------------------------------------------------------

class TestFullTickBody:
    def test_alerts_and_returns_when_not_armed(self, db, monkeypatch):
        db.add(_gate(armed=False))
        db.add(_position())
        db.commit()
        alert_calls = []
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_recorder(alert_calls))
        run(ap._full_tick_body("REAL"))
        assert len(alert_calls) == 1

    def test_returns_when_no_gate_row(self, db, monkeypatch):
        alert_calls = []
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_recorder(alert_calls))
        run(ap._full_tick_body("REAL"))
        assert len(alert_calls) == 1

    def test_returns_when_auto_pilot_disabled(self, db, monkeypatch):
        db.add(_gate(armed=True, auto_pilot_enabled=False))
        db.commit()
        alert_calls = []
        monkeypatch.setattr(ap, "_alert_if_open_positions_while_gate_off", _async_recorder(alert_calls))
        run(ap._full_tick_body("REAL"))
        assert len(alert_calls) == 1

    def test_noop_when_market_closed(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        cycle_calls = []
        monkeypatch.setattr("cycle_runner.run_cycle_core", _async_recorder(cycle_calls, {}))
        run(ap._full_tick_body("REAL"))
        assert cycle_calls == []

    def test_auto_disarmed_notifies_and_returns(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(
            "cycle_runner.run_cycle_core",
            _async_return({"auto_disarmed": "Dhan token expired"}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._full_tick_body("REAL"))
        assert len(notes) == 1
        assert "disarmed" in notes[0][0][0]

    def test_notifies_summary_on_activity(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(
            "cycle_runner.run_cycle_core",
            _async_return({"entry": {"entered": 1, "rejected": 0}}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._full_tick_body("REAL"))
        assert len(notes) == 1

    def test_no_notify_on_no_activity_without_heartbeat(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("cycle_runner.run_cycle_core", _async_return({}))
        monkeypatch.setattr(config, "AUTO_PILOT_NOTIFY_HEARTBEAT", False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._full_tick_body("REAL"))
        assert notes == []

    def test_heartbeat_notify_even_with_no_activity(self, db, monkeypatch):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("cycle_runner.run_cycle_core", _async_return({}))
        monkeypatch.setattr(config, "AUTO_PILOT_NOTIFY_HEARTBEAT", True)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._full_tick_body("REAL"))
        assert len(notes) == 1

    def test_exception_logged_and_notified_never_raises(self, db, monkeypatch, caplog):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr("cycle_runner.run_cycle_core", _async_raise(RuntimeError("cycle boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            run(ap._full_tick_body("REAL"))
        assert len(notes) == 1
        assert "Auto-Pilot error" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _select_overnight_holds — net-of-costs profitability branch (round 1
# pinned OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS=False throughout; this round
# covers the True branch, i.e. cost_model.estimate_round_trip_cost's path).
# ---------------------------------------------------------------------------

class TestSelectOvernightHoldsNetOfCosts:
    def _pin(self, monkeypatch, **overrides):
        values = dict(
            OVERNIGHT_HOLD_ELIGIBLE_LABELS={"HIGH_CONVICTION"},
            OVERNIGHT_HOLD_REQUIRE_PROFITABLE=True,
            OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS=True,
            OVERNIGHT_HOLD_MAX_RANGE_POS=0.80,
            OVERNIGHT_HOLD_MAX_EXPOSURE_PCT=40.0,
            OVERNIGHT_HOLD_MAX_POSITIONS=3,
            OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT=15.0,
            OVERNIGHT_HOLD_MAX_PER_SECTOR=1,
            OVERNIGHT_HOLD_ENABLED=True,
        )
        values.update(overrides)
        for k, v in values.items():
            monkeypatch.setattr(config, k, v)

    def test_excludes_position_whose_gross_pnl_does_not_clear_round_trip_cost(self, db, monkeypatch):
        self._pin(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="THINMARGIN", avg_entry_price=100.0)
        # gross pnl tiny; real round-trip cost model will exceed it
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"THINMARGIN": _FakeTick(price=100.01, day_high=110.0, day_low=95.0)}),
        )
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_keeps_position_whose_gross_pnl_clears_round_trip_cost(self, db, monkeypatch):
        self._pin(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="FATMARGIN",
                         avg_entry_price=100.0, qty_open=10, entry_conviction_score=70.0)
        db.add(pos)
        db.commit()
        # large gross move well clear of round-trip costs and range-position cap
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"FATMARGIN": _FakeTick(price=110.0, day_high=130.0, day_low=100.0)}),
        )
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=1_000_000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == {pos.id}


# ---------------------------------------------------------------------------
# _requeue_overnight_priority_candidates
# ---------------------------------------------------------------------------

class TestRequeueOvernightPriorityCandidates:
    def test_returns_zero_and_logs_on_snapshot_read_exception(self, db, monkeypatch):
        monkeypatch.setattr("resilience.local_cache.load_snapshot",
                             lambda db, key: (_ for _ in ()).throw(RuntimeError("cache down")))
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_returns_zero_when_no_snapshot(self, db, monkeypatch):
        monkeypatch.setattr("resilience.local_cache.load_snapshot", lambda db, key: None)
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_returns_zero_when_already_consumed(self, db, monkeypatch):
        monkeypatch.setattr("resilience.local_cache.load_snapshot",
                             lambda db, key: {"consumed": True})
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_returns_zero_when_trading_date_missing(self, db, monkeypatch):
        monkeypatch.setattr("resilience.local_cache.load_snapshot",
                             lambda db, key: {"consumed": False})
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_returns_zero_when_trading_date_is_today_or_later(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "resilience.local_cache.load_snapshot",
            lambda db, key: {"consumed": False, "trading_date": "2026-09-23"},
        )
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_returns_zero_when_no_picks(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "resilience.local_cache.load_snapshot",
            lambda db, key: {"consumed": False, "trading_date": "2026-09-22", "candidates": []},
        )
        assert run(ap._requeue_overnight_priority_candidates(db, "REAL")) == 0

    def test_adds_new_candidates_and_marks_snapshot_consumed(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        snap = {
            "consumed": False, "trading_date": "2026-09-22",
            "candidates": [
                {"symbol": "AAA", "decision_label": "BUY NOW", "conviction_score": 80,
                 "signal_price": 100.0, "raw_payload": "{}"},
                {"symbol": None},  # no symbol -> skipped
            ],
        }
        monkeypatch.setattr("resilience.local_cache.load_snapshot", lambda db, key: snap)
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        added = run(ap._requeue_overnight_priority_candidates(db, "REAL"))
        assert added == 1
        assert saved[0]["consumed"] is True
        row = db.query(models.TradeCandidate).filter_by(symbol="AAA").first()
        assert row is not None
        assert row.overnight_priority is True

    def test_skips_symbol_already_queued(self, db, monkeypatch):
        db.add(models.TradeCandidate(mode="REAL", symbol="DUP", consumed=False))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        snap = {
            "consumed": False, "trading_date": "2026-09-22",
            "candidates": [{"symbol": "DUP", "decision_label": "BUY NOW", "conviction_score": 80}],
        }
        monkeypatch.setattr("resilience.local_cache.load_snapshot", lambda db, key: snap)
        monkeypatch.setattr("resilience.local_cache.save_snapshot", lambda db, key, payload: None)
        added = run(ap._requeue_overnight_priority_candidates(db, "REAL"))
        assert added == 0

    def test_save_snapshot_failure_is_swallowed(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        snap = {
            "consumed": False, "trading_date": "2026-09-22",
            "candidates": [{"symbol": "AAA", "decision_label": "BUY NOW", "conviction_score": 80}],
        }
        monkeypatch.setattr("resilience.local_cache.load_snapshot", lambda db, key: snap)
        monkeypatch.setattr(
            "resilience.local_cache.save_snapshot",
            lambda db, key, payload: (_ for _ in ()).throw(RuntimeError("save failed")),
        )
        added = run(ap._requeue_overnight_priority_candidates(db, "REAL"))  # must not raise
        assert added == 1


# ---------------------------------------------------------------------------
# _inject_nextday_watchlist_candidates
# ---------------------------------------------------------------------------

class TestInjectNextdayWatchlistCandidates:
    def test_returns_zero_when_no_rows(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        assert run(ap._inject_nextday_watchlist_candidates(db, "REAL")) == 0

    def test_injects_row_above_threshold_with_preview_price(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(config, "AFTERHOURS_SCAN_MIN_INJECT_SCORE", 50.0)
        db.add(models.NextDayWatchlistEntry(
            mode="REAL", symbol="NEWSCO", catalyst_type="results",
            priority_score=70.0, market_date="2026-09-23", consumed=False,
        ))
        db.commit()
        monkeypatch.setattr(
            "market_feed.feed.get_preview_quotes",
            _async_return({"NEWSCO": _FakeTick(price=55.5)}),
        )
        injected = run(ap._inject_nextday_watchlist_candidates(db, "REAL"))
        assert injected == 1
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="NEWSCO").first()
        assert row.consumed is True
        cand = db.query(models.TradeCandidate).filter_by(symbol="NEWSCO").first()
        assert cand.signal_price == 55.5
        assert cand.overnight_priority is True

    def test_row_below_threshold_marked_consumed_not_injected(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(config, "AFTERHOURS_SCAN_MIN_INJECT_SCORE", 50.0)
        db.add(models.NextDayWatchlistEntry(
            mode="REAL", symbol="LOWSCORE", catalyst_type="results",
            priority_score=10.0, market_date="2026-09-23", consumed=False,
        ))
        db.commit()
        injected = run(ap._inject_nextday_watchlist_candidates(db, "REAL"))
        assert injected == 0
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="LOWSCORE").first()
        assert row.consumed is True

    def test_row_already_queued_marked_consumed_not_reinjected(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(config, "AFTERHOURS_SCAN_MIN_INJECT_SCORE", 50.0)
        db.add(models.TradeCandidate(mode="REAL", symbol="ALREADY", consumed=False))
        db.add(models.NextDayWatchlistEntry(
            mode="REAL", symbol="ALREADY", catalyst_type="results",
            priority_score=90.0, market_date="2026-09-23", consumed=False,
        ))
        db.commit()
        injected = run(ap._inject_nextday_watchlist_candidates(db, "REAL"))
        assert injected == 0
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="ALREADY").first()
        assert row.consumed is True

    def test_preview_price_lookup_failure_is_non_fatal(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(config, "AFTERHOURS_SCAN_MIN_INJECT_SCORE", 50.0)
        db.add(models.NextDayWatchlistEntry(
            mode="REAL", symbol="NOPRICE", catalyst_type="results",
            priority_score=90.0, market_date="2026-09-23", consumed=False,
        ))
        db.commit()
        monkeypatch.setattr(
            "market_feed.feed.get_preview_quotes",
            _async_raise(RuntimeError("feed down")),
        )
        injected = run(ap._inject_nextday_watchlist_candidates(db, "REAL"))
        assert injected == 1
        cand = db.query(models.TradeCandidate).filter_by(symbol="NOPRICE").first()
        assert cand.signal_price is None

    def test_db_add_failure_rolled_back_and_row_left_for_retry(self, db, monkeypatch):
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(config, "AFTERHOURS_SCAN_MIN_INJECT_SCORE", 50.0)
        db.add(models.NextDayWatchlistEntry(
            mode="REAL", symbol="BADROW", catalyst_type="results",
            priority_score=90.0, market_date="2026-09-23", consumed=False,
        ))
        db.commit()
        monkeypatch.setattr("market_feed.feed.get_preview_quotes", _async_return({}))

        real_commit = db.commit
        state = {"calls": 0}

        def _flaky_commit():
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("flush failed")
            return real_commit()

        monkeypatch.setattr(db, "commit", _flaky_commit)
        injected = run(ap._inject_nextday_watchlist_candidates(db, "REAL"))
        assert injected == 0

    def test_outer_exception_rolls_back_and_returns_zero(self, db, monkeypatch):
        monkeypatch.setattr(
            ap, "ist_today_str",
            lambda: (_ for _ in ()).throw(RuntimeError("clock broken")),
        )
        assert run(ap._inject_nextday_watchlist_candidates(db, "REAL")) == 0


# ---------------------------------------------------------------------------
# _prepick
# ---------------------------------------------------------------------------

class TestPrepick:
    def test_basic_prepick_notifies_with_counts(self, db, monkeypatch):
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(3))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))
        assert len(notes) == 1
        assert "Queued 3 candidate" in notes[0][0][0]

    def test_lists_top_symbols_with_overnight_tag(self, db, monkeypatch):
        db.add(models.TradeCandidate(mode="REAL", symbol="TOPPICK", consumed=False,
                                      signal_price=50.0, overnight_priority=True))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(1))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))
        assert "TOPPICK" in notes[0][0][0]
        assert "🌙" in notes[0][0][0]

    def test_overnight_added_line_included_when_nonzero(self, db, monkeypatch):
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(2))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))
        assert "carried over" in notes[0][0][0]

    def test_us_sector_signal_applied_when_enabled(self, db, monkeypatch):
        db.add(models.TradeCandidate(mode="REAL", symbol="SECTORED", consumed=False))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(
            "market_context.sector_signal.refresh_us_sector_snapshot",
            lambda db: {"IT": 1.5},
        )
        monkeypatch.setattr(
            "market_context.sector_signal.sector_bonus_for_symbol",
            lambda symbol, returns: 0.5,
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))
        row = db.query(models.TradeCandidate).filter_by(symbol="SECTORED").first()
        assert row.us_sector_bonus == 0.5

    def test_us_sector_signal_failure_is_non_fatal(self, db, monkeypatch):
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(
            "market_context.sector_signal.refresh_us_sector_snapshot",
            lambda db: (_ for _ in ()).throw(RuntimeError("sector feed down")),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))  # must not raise
        assert len(notes) == 1

    def test_more_than_ten_candidates_shows_overflow_line(self, db, monkeypatch):
        for i in range(12):
            db.add(models.TradeCandidate(mode="REAL", symbol=f"SYM{i}", consumed=False))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(12))
        monkeypatch.setattr(ap, "_requeue_overnight_priority_candidates", _async_return(0))
        monkeypatch.setattr(ap, "_inject_nextday_watchlist_candidates", _async_return(0))
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._prepick(db, "REAL"))
        assert "...and 2 more" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _enter_at_open
# ---------------------------------------------------------------------------

class TestEnterAtOpen:
    def test_auto_disarmed_notifies_and_returns(self, db, monkeypatch):
        monkeypatch.setattr(
            "cycle_runner.run_cycle_core",
            _async_return({"auto_disarmed": "token expired"}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._enter_at_open(db, "REAL", True))
        assert len(notes) == 1
        assert "disarmed" in notes[0][0][0]

    def test_notifies_entry_summary(self, db, monkeypatch):
        monkeypatch.setattr(
            "cycle_runner.run_cycle_core",
            _async_return({"entry": {"entered": 2, "rejected": 1, "waited": 3}}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._enter_at_open(db, "REAL", True))
        assert "Entries sent: 2" in notes[0][0][0]
        assert "waited: 3" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _edis_morning_check
# ---------------------------------------------------------------------------

class TestEdisMorningCheck:
    def test_noop_for_demo_mode(self, db, monkeypatch):
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "DEMO"))
        assert notes == []

    def test_noop_when_no_cnc_pending_positions(self, db, monkeypatch):
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [])
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "REAL"))
        assert notes == []

    def test_summary_exception_logged_and_swallowed(self, db, monkeypatch):
        pos = _position(broker_imported=True)
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(
            "execution.dhan_client.edis_verification_summary",
            lambda db: (_ for _ in ()).throw(RuntimeError("dhan down")),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "REAL"))  # must not raise
        assert notes == []

    def test_noop_when_already_verified_today(self, db, monkeypatch):
        pos = _position(broker_imported=True)
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(
            "execution.dhan_client.edis_verification_summary",
            lambda db: {"verified_today": True},
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "REAL"))
        assert notes == []

    def test_alerts_when_not_verified(self, db, monkeypatch):
        pos = _position(broker_imported=True, symbol="PENDCO")
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(
            "execution.dhan_client.edis_verification_summary",
            lambda db: {"verified_today": False, "pending_symbols": ["PENDCO"]},
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "REAL"))
        assert len(notes) == 1
        assert "not yet verified" in notes[0][0][0]
        assert "PENDCO" in notes[0][0][0]

    def test_alerts_with_unknown_status_when_summary_ambiguous(self, db, monkeypatch):
        pos = _position(broker_imported=True, symbol="UNKCO")
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(
            "execution.dhan_client.edis_verification_summary",
            lambda db: {"verified_today": None, "detail": "inquiry failed"},
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._edis_morning_check(db, "REAL"))
        assert len(notes) == 1
        assert "status unknown" in notes[0][0][0]
        assert "inquiry failed" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _eod_squareoff
# ---------------------------------------------------------------------------

class TestEodSquareoff:
    def test_noop_when_no_open_positions(self, db, monkeypatch):
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [])
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))
        assert notes == []

    def test_demo_closes_at_live_tick(self, db, monkeypatch):
        pos = _position(mode="DEMO", symbol="DEMOCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return({"DEMOCO": _FakeTick(105.0)}))
        closed_calls = []
        monkeypatch.setattr(
            "portfolio.portfolio.close_position",
            lambda db, p, tick, qty, reason: closed_calls.append((p.symbol, qty, reason)),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "DEMO"))
        assert closed_calls == [("DEMOCO", 5, "eod_squareoff")]
        assert "Closed 1 position" in notes[0][0][0]

    def test_demo_close_failure_counts_as_failed(self, db, monkeypatch):
        pos = _position(mode="DEMO", symbol="FAILCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return({"FAILCO": _FakeTick(105.0)}))
        monkeypatch.setattr(
            "portfolio.portfolio.close_position",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("close failed")),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "DEMO"))  # must not raise
        assert "could not be closed" in notes[0][0][0]

    def test_demo_missing_tick_counts_as_failed(self, db, monkeypatch):
        pos = _position(mode="DEMO", symbol="NOTICK", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return({}))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "DEMO"))
        assert "could not be closed" in notes[0][0][0]

    def test_real_sends_sell_for_each_position(self, db, monkeypatch):
        pos = _position(mode="REAL", symbol="REALCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda db, sym: False)
        monkeypatch.setattr("exit_engine.exit._send_real_sell", lambda db, p, qty, reason, full: True)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))
        assert "Sent 1 sell order" in notes[0][0][0]

    def test_real_skips_position_with_pending_sell(self, db, monkeypatch):
        pos = _position(mode="REAL", symbol="PENDING", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda db, sym: True)
        sell_calls = []
        monkeypatch.setattr(
            "exit_engine.exit._send_real_sell",
            lambda *a, **kw: sell_calls.append(a),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))
        assert sell_calls == []
        assert "1 skipped" in notes[0][0][0]

    def test_real_send_sell_returns_false_counts_as_failed(self, db, monkeypatch):
        pos = _position(mode="REAL", symbol="REJECTCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda db, sym: False)
        monkeypatch.setattr("exit_engine.exit._send_real_sell", lambda *a, **kw: False)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))
        assert "could not be closed" in notes[0][0][0]

    def test_real_send_sell_exception_counts_as_failed(self, db, monkeypatch):
        pos = _position(mode="REAL", symbol="EXCCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(ap, "_select_overnight_holds", _async_return((set(), {})))
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda db, sym: False)
        monkeypatch.setattr(
            "exit_engine.exit._send_real_sell",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("broker down")),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))  # must not raise
        assert "could not be closed" in notes[0][0][0]

    def test_overnight_holds_excluded_and_stamped_with_reason(self, db, monkeypatch):
        pos = _position(mode="REAL", symbol="HELDCO", qty_open=5)
        db.add(pos)
        db.commit()
        monkeypatch.setattr("portfolio.portfolio.open_positions", lambda db, mode: [pos])
        monkeypatch.setattr(
            ap, "_select_overnight_holds",
            _async_return(({pos.id}, {pos.id: "overnight hold: HIGH_CONVICTION"})),
        )
        sell_calls = []
        monkeypatch.setattr(
            "exit_engine.exit._send_real_sell",
            lambda *a, **kw: sell_calls.append(a),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_squareoff(db, "REAL"))
        assert sell_calls == []
        db.refresh(pos)
        assert pos.overnight_hold_reason == "overnight hold: HIGH_CONVICTION"
        events = db.query(models.TradePositionEvent).filter_by(position_id=pos.id).all()
        assert len(events) == 1
        assert events[0].event_type == "OVERNIGHT_HOLD"
        assert "held overnight" in notes[0][0][0]


# ---------------------------------------------------------------------------
# _eod_signal_scan
# ---------------------------------------------------------------------------

def _pin_eod_scan_config(monkeypatch, **overrides):
    values = dict(
        EOD_SIGNAL_SCAN_MIN_CONVICTION=50.0,
        EOD_SIGNAL_SCAN_MAX_CANDIDATES=5,
        EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION=80.0,
        EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES=2,
    )
    values.update(overrides)
    for k, v in values.items():
        monkeypatch.setattr(config, k, v)


class TestEodSignalScan:
    def test_no_candidates_notifies_nothing_queued(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr("resilience.local_cache.save_snapshot", lambda db, key, payload: None)
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        assert "nothing queued" in notes[0][0][0]

    def test_below_conviction_candidate_excluded(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        db.add(models.TradeCandidate(mode="REAL", symbol="WEAK", consumed=False,
                                      decision_label="BUY NOW", conviction_score=10))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr("resilience.local_cache.save_snapshot", lambda db, key, payload: None)
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        row = db.query(models.TradeCandidate).filter_by(symbol="WEAK").first()
        assert row.consumed is True
        assert "nothing queued" in notes[0][0][0]

    def test_wrong_label_excluded(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        db.add(models.TradeCandidate(mode="REAL", symbol="OTHERLABEL", consumed=False,
                                      decision_label="AVOID", conviction_score=90))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr("resilience.local_cache.save_snapshot", lambda db, key, payload: None)
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        assert "nothing queued" in notes[0][0][0]

    def test_queue_only_candidate_saved_to_snapshot(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        db.add(models.TradeCandidate(mode="REAL", symbol="QUEUEONLY", consumed=False,
                                      decision_label="BUY NOW", conviction_score=60))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        assert saved[0]["candidates"][0]["symbol"] == "QUEUEONLY"
        assert "Queued 1 more" in notes[0][0][0]

    def test_high_conviction_entered_today(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        c = models.TradeCandidate(mode="REAL", symbol="HOTPICK", consumed=False,
                                   decision_label="BUY NOW", conviction_score=95)
        db.add(c)
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        monkeypatch.setattr("resilience.local_cache.save_snapshot", lambda db, key, payload: None)
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "entry_engine.entry.evaluate_mode",
            _async_return({"entry_details": [{"symbol": "HOTPICK", "action": "ENTER"}]}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        assert "entered today" in notes[0][0][0]

    def test_high_conviction_not_filled_falls_back_to_queue(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        c = models.TradeCandidate(mode="REAL", symbol="MISSFILL", consumed=False,
                                   decision_label="BUY NOW", conviction_score=95)
        db.add(c)
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "entry_engine.entry.evaluate_mode",
            _async_return({"entry_details": []}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        assert "carried to tomorrow" in notes[0][0][0]
        assert saved[0]["candidates"][0]["symbol"] == "MISSFILL"

    def test_entry_evaluate_exception_falls_back_to_queue(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        c = models.TradeCandidate(mode="REAL", symbol="EVALEXC", consumed=False,
                                   decision_label="BUY NOW", conviction_score=95)
        db.add(c)
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "entry_engine.entry.evaluate_mode",
            _async_raise(RuntimeError("entry engine down")),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))  # must not raise
        assert saved[0]["candidates"][0]["symbol"] == "EVALEXC"

    def test_disarmed_skips_same_day_entry_falls_back_to_queue(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch)
        c = models.TradeCandidate(mode="REAL", symbol="DISARMEDPICK", consumed=False,
                                   decision_label="BUY NOW", conviction_score=95)
        db.add(c)
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        entry_calls = []
        monkeypatch.setattr(
            "entry_engine.entry.evaluate_mode",
            _async_recorder(entry_calls, {"entry_details": []}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", False))
        assert entry_calls == []
        assert saved[0]["candidates"][0]["symbol"] == "DISARMEDPICK"

    def test_candidates_capped_at_max_candidates(self, db, monkeypatch):
        _pin_eod_scan_config(monkeypatch, EOD_SIGNAL_SCAN_MAX_CANDIDATES=1)
        db.add(models.TradeCandidate(mode="REAL", symbol="HIGH1", consumed=False,
                                      decision_label="BUY NOW", conviction_score=95))
        db.add(models.TradeCandidate(mode="REAL", symbol="HIGH2", consumed=False,
                                      decision_label="BUY NOW", conviction_score=90))
        db.commit()
        monkeypatch.setattr("candidate_engine.candidates.refresh_candidates", _async_return(0))
        saved = []
        monkeypatch.setattr("resilience.local_cache.save_snapshot",
                             lambda db, key, payload: saved.append(payload))
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(
            "entry_engine.entry.evaluate_mode",
            _async_return({"entry_details": []}),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._eod_signal_scan(db, "REAL", True))
        # only 1 candidate should have made it into the pick list (HIGH1, higher conviction)
        assert len(saved[0]["candidates"]) == 1
        assert saved[0]["candidates"][0]["symbol"] == "HIGH1"


# ---------------------------------------------------------------------------
# _schedule_tick_body
# ---------------------------------------------------------------------------

class TestScheduleTickBody:
    def test_returns_when_no_gate_row(self, db, monkeypatch):
        prepick_calls = []
        monkeypatch.setattr(ap, "_prepick", _async_recorder(prepick_calls))
        run(ap._schedule_tick_body("REAL"))
        assert prepick_calls == []

    def test_returns_when_not_armed(self, db, monkeypatch):
        db.add(_gate(armed=False))
        db.commit()
        prepick_calls = []
        monkeypatch.setattr(ap, "_prepick", _async_recorder(prepick_calls))
        run(ap._schedule_tick_body("REAL"))
        assert prepick_calls == []

    def test_prepick_fires_when_enabled_and_time_reached(self, db, monkeypatch):
        db.add(_gate(prepick_enabled=True, prepick_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        prepick_calls = []
        monkeypatch.setattr(ap, "_prepick", _async_recorder(prepick_calls))
        run(ap._schedule_tick_body("REAL"))
        assert len(prepick_calls) == 1
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert gate.prepick_last_run == "2026-09-23"

    def test_prepick_skipped_when_already_run_today(self, db, monkeypatch):
        db.add(_gate(prepick_enabled=True, prepick_last_run="2026-09-23"))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        prepick_calls = []
        monkeypatch.setattr(ap, "_prepick", _async_recorder(prepick_calls))
        run(ap._schedule_tick_body("REAL"))
        assert prepick_calls == []

    def test_prepick_exception_logged_and_notified(self, db, monkeypatch, caplog):
        db.add(_gate(prepick_enabled=True, prepick_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        monkeypatch.setattr(ap, "_prepick", _async_raise(RuntimeError("prepick boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            run(ap._schedule_tick_body("REAL"))
        assert any("Pre-pick error" in n[0][0] for n in notes)

    def test_edis_check_fires_when_enabled(self, db, monkeypatch):
        db.add(_gate(edis_morning_check_enabled=True, edis_check_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        edis_calls = []
        monkeypatch.setattr(ap, "_edis_morning_check", _async_recorder(edis_calls))
        run(ap._schedule_tick_body("REAL"))
        assert len(edis_calls) == 1

    def test_edis_check_exception_logged_and_notified(self, db, monkeypatch):
        db.add(_gate(edis_morning_check_enabled=True, edis_check_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        monkeypatch.setattr(ap, "_edis_morning_check", _async_raise(RuntimeError("edis boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._schedule_tick_body("REAL"))
        assert any("eDIS morning check error" in n[0][0] for n in notes)

    def test_enter_at_open_requires_market_open(self, db, monkeypatch):
        db.add(_gate(enter_at_open_enabled=True, enter_at_open_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: False)
        enter_calls = []
        monkeypatch.setattr(ap, "_enter_at_open", _async_recorder(enter_calls))
        run(ap._schedule_tick_body("REAL"))
        assert enter_calls == []

    def test_enter_at_open_fires_when_market_open(self, db, monkeypatch):
        db.add(_gate(enter_at_open_enabled=True, enter_at_open_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        enter_calls = []
        monkeypatch.setattr(ap, "_enter_at_open", _async_recorder(enter_calls))
        run(ap._schedule_tick_body("REAL"))
        assert len(enter_calls) == 1

    def test_enter_at_open_exception_logged_and_notified(self, db, monkeypatch):
        db.add(_gate(enter_at_open_enabled=True, enter_at_open_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(ap, "_enter_at_open", _async_raise(RuntimeError("boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._schedule_tick_body("REAL"))
        assert any("Enter-at-open error" in n[0][0] for n in notes)

    def test_eod_squareoff_fires_and_uses_exit_lock(self, db, monkeypatch):
        db.add(_gate(eod_squareoff_enabled=True, eod_squareoff_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        squareoff_calls = []
        monkeypatch.setattr(ap, "_eod_squareoff", _async_recorder(squareoff_calls))
        run(ap._schedule_tick_body("REAL"))
        assert len(squareoff_calls) == 1
        # lock must have been released afterward
        assert ap._get_exit_lock("REAL").acquire(blocking=False) is True
        ap._get_exit_lock("REAL").release()

    def test_eod_squareoff_exception_logged_and_lock_released(self, db, monkeypatch):
        db.add(_gate(eod_squareoff_enabled=True, eod_squareoff_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(ap, "_eod_squareoff", _async_raise(RuntimeError("squareoff boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._schedule_tick_body("REAL"))
        assert any("EOD square-off error" in n[0][0] for n in notes)
        assert ap._get_exit_lock("REAL").acquire(blocking=False) is True
        ap._get_exit_lock("REAL").release()

    def test_eod_signal_scan_fires(self, db, monkeypatch):
        db.add(_gate(eod_signal_scan_enabled=True, eod_signal_scan_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        scan_calls = []
        monkeypatch.setattr(ap, "_eod_signal_scan", _async_recorder(scan_calls))
        run(ap._schedule_tick_body("REAL"))
        assert len(scan_calls) == 1

    def test_eod_signal_scan_exception_logged_and_notified(self, db, monkeypatch):
        db.add(_gate(eod_signal_scan_enabled=True, eod_signal_scan_last_run=None))
        db.commit()
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: True)
        monkeypatch.setattr(ap, "is_market_open_ist", lambda: True)
        monkeypatch.setattr(ap, "_eod_signal_scan", _async_raise(RuntimeError("scan boom")))
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._schedule_tick_body("REAL"))
        assert any("EOD signal scan error" in n[0][0] for n in notes)

    def test_outer_exception_logged_and_swallowed(self, db, monkeypatch, caplog):
        db.add(_gate())
        db.commit()
        monkeypatch.setattr(
            ap, "ist_today_str", lambda: (_ for _ in ()).throw(RuntimeError("clock broken")),
        )
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            run(ap._schedule_tick_body("REAL"))  # must not raise


# ---------------------------------------------------------------------------
# Background loops — break out after N sleeps via a sentinel exception
# ---------------------------------------------------------------------------

class _StopLoop(Exception):
    pass


def _sleep_after(n_calls: int):
    state = {"n": 0}

    async def _fake_sleep(_seconds):
        state["n"] += 1
        if state["n"] >= n_calls:
            raise _StopLoop()

    return _fake_sleep


class TestScheduleLoop:
    def test_calls_schedule_tick_for_both_modes_then_sleeps(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        calls = []
        monkeypatch.setattr(ap, "_schedule_tick", _async_recorder(calls))
        with pytest.raises(_StopLoop):
            run(ap._schedule_loop())
        assert [c[0][0] for c in calls] == ["DEMO", "REAL"]

    def test_exception_in_one_mode_does_not_stop_the_other(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))

        async def _flaky(mode):
            if mode == "DEMO":
                raise RuntimeError("demo tick boom")

        calls = []

        async def _tracking(mode):
            calls.append(mode)
            await _flaky(mode)

        monkeypatch.setattr(ap, "_schedule_tick", _tracking)
        with pytest.raises(_StopLoop):
            run(ap._schedule_loop())
        assert calls == ["DEMO", "REAL"]


class TestFastExitLoop:
    def test_calls_exit_only_tick_for_both_modes(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        calls = []
        monkeypatch.setattr(ap, "_exit_only_tick", _async_recorder(calls))
        with pytest.raises(_StopLoop):
            run(ap._fast_exit_loop())
        assert [c[0][0] for c in calls] == ["DEMO", "REAL"]

    def test_exception_in_one_mode_does_not_stop_the_other(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))

        calls = []

        async def _flaky(mode):
            calls.append(mode)
            if mode == "DEMO":
                raise RuntimeError("demo exit tick boom")

        monkeypatch.setattr(ap, "_exit_only_tick", _flaky)
        with pytest.raises(_StopLoop):
            run(ap._fast_exit_loop())  # must not propagate the RuntimeError
        assert calls == ["DEMO", "REAL"]


class TestFullCycleLoop:
    def test_calls_full_tick_for_both_modes(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        calls = []
        monkeypatch.setattr(ap, "_full_tick", _async_recorder(calls))
        with pytest.raises(_StopLoop):
            run(ap._full_cycle_loop())
        assert [c[0][0] for c in calls] == ["DEMO", "REAL"]

    def test_exception_in_one_mode_does_not_stop_the_other(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))

        calls = []

        async def _flaky(mode):
            calls.append(mode)
            if mode == "DEMO":
                raise RuntimeError("demo full tick boom")

        monkeypatch.setattr(ap, "_full_tick", _flaky)
        with pytest.raises(_StopLoop):
            run(ap._full_cycle_loop())  # must not propagate the RuntimeError
        assert calls == ["DEMO", "REAL"]


class TestTotpRefreshLoop:
    def test_noop_when_totp_disabled(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)
        with pytest.raises(_StopLoop):
            run(ap._totp_refresh_loop())

    def test_refreshes_when_needed_and_enabled(self, monkeypatch, db):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr("auth.dhan_credentials.token_needs_refresh", lambda db: True)
        refresh_calls = []
        monkeypatch.setattr(
            "auth.dhan_credentials.refresh_if_totp_enabled",
            lambda db: refresh_calls.append(1) or True,
        )
        with pytest.raises(_StopLoop):
            run(ap._totp_refresh_loop())
        assert refresh_calls == [1]

    def test_refresh_failure_logged_not_raised(self, monkeypatch, db):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr("auth.dhan_credentials.token_needs_refresh", lambda db: True)
        monkeypatch.setattr("auth.dhan_credentials.refresh_if_totp_enabled", lambda db: False)
        with pytest.raises(_StopLoop):
            run(ap._totp_refresh_loop())  # must not raise from the False path

    def test_no_refresh_needed_skips_call(self, monkeypatch, db):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr("auth.dhan_credentials.token_needs_refresh", lambda db: False)
        refresh_calls = []
        monkeypatch.setattr(
            "auth.dhan_credentials.refresh_if_totp_enabled",
            lambda db: refresh_calls.append(1),
        )
        with pytest.raises(_StopLoop):
            run(ap._totp_refresh_loop())
        assert refresh_calls == []

    def test_exception_inside_tick_logged_and_swallowed(self, monkeypatch, db):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr(
            "auth.dhan_credentials.token_needs_refresh",
            lambda db: (_ for _ in ()).throw(RuntimeError("db boom")),
        )
        with pytest.raises(_StopLoop):
            run(ap._totp_refresh_loop())  # must not propagate the RuntimeError


class TestAfterhoursScanLoop:
    def test_calls_tick_sync_via_to_thread_for_both_modes(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))
        calls = []
        monkeypatch.setattr(ap.asyncio, "to_thread", _async_recorder(calls))
        with pytest.raises(_StopLoop):
            run(ap._afterhours_scan_loop())
        assert [c[0] for c in calls] == [(ap._run_afterhours_tick_sync, "DEMO"),
                                          (ap._run_afterhours_tick_sync, "REAL")]

    def test_exception_in_to_thread_does_not_stop_loop(self, monkeypatch):
        monkeypatch.setattr(ap.asyncio, "sleep", _sleep_after(2))

        async def _boom(*a, **kw):
            raise RuntimeError("thread boom")

        monkeypatch.setattr(ap.asyncio, "to_thread", _boom)
        with pytest.raises(_StopLoop):
            run(ap._afterhours_scan_loop())  # must not propagate the RuntimeError


# ---------------------------------------------------------------------------
# _afterhours_scan_body — remaining branches not covered by round 1
# ---------------------------------------------------------------------------

class TestAfterhoursScanBody:
    def test_gate_not_found_returns_reason(self, db, monkeypatch):
        result = run(ap._afterhours_scan_body("REAL"))
        assert result["reason"] == "gate_not_found"
        assert result["ran"] is False

    def test_feature_disabled_returns_reason(self, db, monkeypatch):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=False))
        db.commit()
        result = run(ap._afterhours_scan_body("REAL"))
        assert result["reason"] == "feature_disabled"

    def test_outside_window_returns_reason(self, db, monkeypatch):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=True))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: False)
        result = run(ap._afterhours_scan_body("REAL"))
        assert result["reason"] == "outside_window"

    def test_manual_bypasses_toggle_and_window_check(self, db, monkeypatch):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=False))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: False)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-09-24")
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: datetime(2026, 9, 23, 20, 0))
        monkeypatch.setattr(
            "watchlist_engine.afterhours_scan.run_afterhours_scan", _async_return(3)
        )
        result = run(ap._afterhours_scan_body("REAL", manual=True))
        assert result["ran"] is True
        assert result["written"] == 3

    def test_finalize_pass_fires_and_notifies_on_shortlist(self, db, monkeypatch):
        db.add(models.TradeGateState(
            mode="REAL", afterhours_news_scan_enabled=True,
            afterhours_finalize_last_run=None,
        ))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: True)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-09-23")
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        fixed_now = datetime(2026, 9, 23, 9, 0)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed_now)
        monkeypatch.setattr(config, "AFTERHOURS_FINALIZE_TIME_IST", "08:45")
        monkeypatch.setattr(
            "watchlist_engine.afterhours_scan.finalize_nextday_watchlist",
            _async_return(["SYMA", "SYMB"]),
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        result = run(ap._afterhours_scan_body("REAL"))
        assert result["ran"] is True
        assert result["finalized"] is True
        assert len(notes) == 1
        assert "SYMA" in notes[0][0][0]

    def test_finalize_pass_no_shortlist_no_notify(self, db, monkeypatch):
        db.add(models.TradeGateState(
            mode="REAL", afterhours_news_scan_enabled=True,
            afterhours_finalize_last_run=None,
        ))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: True)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-09-23")
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        fixed_now = datetime(2026, 9, 23, 9, 0)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed_now)
        monkeypatch.setattr(config, "AFTERHOURS_FINALIZE_TIME_IST", "08:45")
        monkeypatch.setattr(
            "watchlist_engine.afterhours_scan.finalize_nextday_watchlist", _async_return([])
        )
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        run(ap._afterhours_scan_body("REAL"))
        assert notes == []

    def test_regular_scan_pass_runs_and_records_success(self, db, monkeypatch):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=True))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: True)
        monkeypatch.setattr(ap, "_compute_afterhours_market_date", lambda now_t: "2026-09-24")
        monkeypatch.setattr(ap, "ist_today_str", lambda: "2026-09-23")
        fixed_now = datetime(2026, 9, 23, 20, 0)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed_now)
        monkeypatch.setattr(config, "AFTERHOURS_FINALIZE_TIME_IST", "08:45")
        monkeypatch.setattr(
            "watchlist_engine.afterhours_scan.run_afterhours_scan", _async_return(5)
        )
        result = run(ap._afterhours_scan_body("REAL"))
        assert result["ran"] is True
        assert result["written"] == 5
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert gate.afterhours_scan_last_run_ok is True

    def test_exception_records_failure_and_returns_reason(self, db, monkeypatch):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=True))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: True)
        monkeypatch.setattr(
            ap, "_compute_afterhours_market_date",
            lambda now_t: (_ for _ in ()).throw(RuntimeError("date calc boom")),
        )
        fixed_now = datetime(2026, 9, 23, 20, 0)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed_now)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))
        result = run(ap._afterhours_scan_body("REAL"))
        assert "date calc boom" in result["reason"]
        assert len(notes) == 1
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert gate.afterhours_scan_last_run_ok is False

    def test_exception_also_failing_to_record_is_swallowed(self, db, monkeypatch, caplog):
        db.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=True))
        db.commit()
        monkeypatch.setattr(ap, "_is_afterhours_window_active", lambda: True)
        monkeypatch.setattr(
            ap, "_compute_afterhours_market_date",
            lambda now_t: (_ for _ in ()).throw(RuntimeError("date calc boom")),
        )
        fixed_now = datetime(2026, 9, 23, 20, 0)
        monkeypatch.setattr("tz_utils.ist_now", lambda now=None: fixed_now)
        notes = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(notes))

        # Force the recovery block's own db.query to blow up too.
        orig_query = db.query
        state = {"n": 0}

        def _flaky_query(*a, **kw):
            state["n"] += 1
            if state["n"] >= 2:
                raise RuntimeError("recovery query also broken")
            return orig_query(*a, **kw)

        monkeypatch.setattr(db, "query", _flaky_query)
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            result = run(ap._afterhours_scan_body("REAL"))  # must not raise
        assert "date calc boom" in result["reason"]


# ---------------------------------------------------------------------------
# after-hours lock + manual trigger
# ---------------------------------------------------------------------------

class TestGetAfterhoursLock:
    def test_creates_and_reuses_same_lock_per_mode(self):
        l1 = ap._get_afterhours_lock("AHTEST")
        l2 = ap._get_afterhours_lock("AHTEST")
        assert l1 is l2


class TestRunAfterhoursTickSync:
    def test_skips_when_lock_held(self, monkeypatch):
        lock = ap._get_afterhours_lock("AHSKIP")
        lock.acquire()
        try:
            calls = []
            monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda *a, **kw: calls.append(a))
            ap._run_afterhours_tick_sync("AHSKIP")
            assert calls == []
        finally:
            lock.release()

    def test_runs_and_releases_lock(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda fn, mode: calls.append((fn, mode)))
        ap._run_afterhours_tick_sync("AHRUN")
        assert calls == [(ap._afterhours_scan_body, "AHRUN")]
        assert ap._get_afterhours_lock("AHRUN").acquire(blocking=False) is True
        ap._get_afterhours_lock("AHRUN").release()


class TestRunAfterhoursScanManualSync:
    def test_returns_already_in_progress_when_locked(self, monkeypatch):
        lock = ap._get_afterhours_lock("AHMANUALSKIP")
        lock.acquire()
        try:
            result = ap.run_afterhours_scan_manual_sync("AHMANUALSKIP")
            assert result == {"mode": "AHMANUALSKIP", "ran": False, "reason": "already_in_progress"}
        finally:
            lock.release()

    def test_runs_scan_body_and_releases_lock(self, monkeypatch):
        monkeypatch.setattr(
            ap, "_afterhours_scan_body",
            lambda mode, manual=False: _fake_coro({"mode": mode, "ran": True, "manual": manual}),
        )
        result = ap.run_afterhours_scan_manual_sync("AHMANUALRUN")
        assert result == {"mode": "AHMANUALRUN", "ran": True, "manual": True}
        assert ap._get_afterhours_lock("AHMANUALRUN").acquire(blocking=False) is True
        ap._get_afterhours_lock("AHMANUALRUN").release()


async def _fake_coro(value):
    return value


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    def test_creates_all_five_background_tasks(self, monkeypatch):
        created = []

        def _fake_create_task(coro):
            created.append(coro)
            coro.close()  # never actually run it
            return _FakeTask()

        monkeypatch.setattr(ap, "_full_task", None)
        monkeypatch.setattr(ap, "_exit_task", None)
        monkeypatch.setattr(ap, "_schedule_task", None)
        monkeypatch.setattr(ap, "_totp_task", None)
        monkeypatch.setattr(ap, "_afterhours_task", None)
        monkeypatch.setattr(ap.asyncio, "create_task", _fake_create_task)

        async def _runner():
            ap.start()

        run(_runner())
        assert len(created) == 5

    def test_idempotent_when_tasks_already_running(self, monkeypatch):
        running_task = _FakeTask(done=False)
        monkeypatch.setattr(ap, "_full_task", running_task)
        monkeypatch.setattr(ap, "_exit_task", running_task)
        monkeypatch.setattr(ap, "_schedule_task", running_task)
        monkeypatch.setattr(ap, "_totp_task", running_task)
        monkeypatch.setattr(ap, "_afterhours_task", running_task)
        created = []
        monkeypatch.setattr(ap.asyncio, "create_task", lambda coro: created.append(coro))

        async def _runner():
            ap.start()

        run(_runner())
        assert created == []

    def test_recreates_task_that_finished(self, monkeypatch):
        finished_task = _FakeTask(done=True)
        created = []

        def _fake_create_task(coro):
            created.append(coro)
            coro.close()
            return _FakeTask()

        monkeypatch.setattr(ap, "_full_task", finished_task)
        monkeypatch.setattr(ap, "_exit_task", finished_task)
        monkeypatch.setattr(ap, "_schedule_task", finished_task)
        monkeypatch.setattr(ap, "_totp_task", finished_task)
        monkeypatch.setattr(ap, "_afterhours_task", finished_task)
        monkeypatch.setattr(ap.asyncio, "create_task", _fake_create_task)

        async def _runner():
            ap.start()

        run(_runner())
        assert len(created) == 5


class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done
