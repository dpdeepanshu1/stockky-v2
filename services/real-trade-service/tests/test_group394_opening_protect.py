"""tests/test_group394_opening_protect.py - entry_engine/opening_protect.py (group 394): the real-trade opening size cut and stop-out pause.

Off for the rest of the suite (tests/conftest.py); this file turns it on and pins the clock.
Run from services/real-trade-service:
    python3 -m pytest tests/test_group394_opening_protect.py -q --cov=entry_engine.opening_protect --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, opening_protect as op
from risk_engine.engine import RiskResult, RiskVerdict
from tz_utils import IST

# reuse the fixtures / helpers of the evaluate_mode suite (autouse pin, db, make_candidate, tick, quotes)
from tests.test_entry_evaluate_mode import (  # noqa: F401
    pin, db, make_candidate, tick, quotes, run,
)

WED = (2026, 10, 14)      # an ordinary trading day
SAT = (2026, 10, 17)
HOLIDAY = (2026, 10, 20)  # Dussehra


def at(h, m, day=WED):
    return datetime(*day, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def protect_on(monkeypatch):
    for k, v in dict(OPENING_PROTECT_ENABLED=True, OPENING_PROTECT_UNTIL_IST="09:45", OPENING_SIZE_MULT=0.5,
                     OPENING_PAUSE_STOPOUTS=2, OPENING_PROTECT_MODES=("REAL", "DEMO")).items():
        monkeypatch.setattr(config, k, v, raising=False)


def clock(monkeypatch, h, m, day=WED):
    """Pin the clock the module reads (and the guard throttle's neighbours stay untouched)."""
    monkeypatch.setattr(op, "ist_now", lambda now=None: datetime(*day, h, m, tzinfo=IST))


def add_close(db, *, mode="DEMO", reason="stop_hit", pnl=-50.0, when=None, symbol="AAA", event_type="CLOSED"):
    """A position with one close event. `when` is an aware IST datetime; stored as naive UTC, like Oracle returns it."""
    when = when or at(9, 25)
    pos = models.TradePosition(mode=mode, symbol=symbol, status="CLOSED", qty_open=0, avg_entry_price=100.0, realized_pnl=pnl,
                               closed_at=when.astimezone(timezone.utc).replace(tzinfo=None))
    db.add(pos)
    db.flush()
    db.add(models.TradePositionEvent(position_id=pos.id, event_type=event_type, detail=f"{reason}: 10 @ 99.0 (pnl {pnl:+.2f})",
                                     occurred_at=when.astimezone(timezone.utc).replace(tzinfo=None)))
    db.commit()


class TestWindow:
    @pytest.mark.parametrize("hh,mm,expected", [(9, 14, False), (9, 15, True), (9, 44, True), (9, 45, False), (11, 0, False)])
    def test_bounds(self, monkeypatch, hh, mm, expected):
        clock(monkeypatch, hh, mm)
        assert op.active("REAL") is expected

    def test_weekend_and_holiday_are_not_active(self, monkeypatch):
        clock(monkeypatch, 9, 20, SAT)
        assert op.active("REAL") is False
        clock(monkeypatch, 9, 20, HOLIDAY)
        assert op.active("REAL") is False

    def test_mode_coverage_and_switch(self, monkeypatch):
        clock(monkeypatch, 9, 20)
        monkeypatch.setattr(config, "OPENING_PROTECT_MODES", ("REAL",))
        assert op.active("REAL") is True and op.active("DEMO") is False and op.covers("demo") is False
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
        assert op.active("REAL") is False and op.covers("REAL") is False

    def test_custom_end_time_and_bad_end_time(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_PROTECT_UNTIL_IST", "10:00")
        clock(monkeypatch, 9, 50)
        assert op.active("REAL") is True
        monkeypatch.setattr(config, "OPENING_PROTECT_UNTIL_IST", "garbage")      # parse_hhmm default 09:45
        assert op.active("REAL") is False

    def test_clock_error_fails_open(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("clock")
        monkeypatch.setattr(op, "ist_now", boom)
        assert op.active("REAL") is False and op.size_mult("REAL") == 1.0

    def test_covers_survives_bad_config(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_PROTECT_MODES", None)
        assert op.covers("REAL") is False


class TestSize:
    def test_half_inside_the_window_and_full_outside(self, monkeypatch):
        clock(monkeypatch, 9, 20)
        assert op.size_mult("REAL") == 0.5
        clock(monkeypatch, 10, 0)
        assert op.size_mult("REAL") == 1.0

    def test_mult_is_clamped(self, monkeypatch):
        clock(monkeypatch, 9, 20)
        monkeypatch.setattr(config, "OPENING_SIZE_MULT", 0.01)
        assert op.size_mult("REAL") == 0.1
        monkeypatch.setattr(config, "OPENING_SIZE_MULT", 3.0)
        assert op.size_mult("REAL") == 1.0
        monkeypatch.setattr(config, "OPENING_SIZE_MULT", "x")
        assert op.size_mult("REAL") == 1.0

    @pytest.mark.parametrize("qty,mult,expected", [(10, 0.5, 5), (7, 0.5, 4), (3, 0.5, 2), (2, 0.5, 1), (1, 0.5, 1), (10, 1.0, 10), (0, 0.5, 0), (5, 0.1, 1)])
    def test_scaled_qty_matches_the_replay_rule(self, qty, mult, expected):
        assert op.scaled_qty(qty, mult) == expected

    def test_scaled_qty_bad_input_is_unchanged(self):
        assert op.scaled_qty("x", 0.5) == "x"


class TestStopouts:
    def test_counts_losing_stops_of_all_three_kinds(self, db, monkeypatch):
        clock(monkeypatch, 9, 40)
        for i, reason in enumerate(("stop_hit", "broker_stop", "emergency_gap_down")):
            add_close(db, reason=reason, symbol=f"S{i}")
        assert op.stopouts_since_open(db, "DEMO") == 3

    def test_ignores_other_exits_profits_partials_other_modes_and_yesterday(self, db, monkeypatch):
        clock(monkeypatch, 9, 40)
        add_close(db, reason="target_hit_partial", symbol="T")
        add_close(db, reason="time_stop", symbol="U")
        add_close(db, reason="structure_exit", symbol="V")
        add_close(db, reason="stop_hit", pnl=25.0, symbol="W")                                    # trailed stop in profit
        add_close(db, reason="stop_hit", event_type="PARTIAL_EXIT", symbol="X")                   # not a full close
        add_close(db, reason="stop_hit", mode="REAL", symbol="Y")                                 # other mode
        add_close(db, reason="stop_hit", when=at(9, 10), symbol="Z")                              # before the bell
        add_close(db, reason="stop_hit", when=at(9, 25) - timedelta(days=1), symbol="Q")          # yesterday
        assert op.stopouts_since_open(db, "DEMO") == 0
        assert op.stopouts_since_open(db, "REAL") == 1

    def test_a_close_at_the_bell_counts_and_a_missing_detail_does_not(self, db, monkeypatch):
        clock(monkeypatch, 9, 40)
        add_close(db, when=at(9, 15), symbol="A")
        pos = models.TradePosition(mode="DEMO", symbol="B", status="CLOSED", qty_open=0, avg_entry_price=100.0, realized_pnl=-5.0)
        db.add(pos)
        db.flush()
        db.add(models.TradePositionEvent(position_id=pos.id, event_type="CLOSED", detail=None,
                                         occurred_at=at(9, 30).astimezone(timezone.utc).replace(tzinfo=None)))
        db.commit()
        assert op.stopouts_since_open(db, "DEMO") == 1

    def test_db_error_means_zero(self):
        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert op.stopouts_since_open(Broken(), "DEMO") == 0


class TestPause:
    def test_pauses_at_the_limit_and_names_when_it_lifts(self, db, monkeypatch):
        clock(monkeypatch, 9, 40)
        add_close(db, symbol="A")
        assert op.pause_reason(db, "DEMO") is None
        add_close(db, symbol="B")
        r = op.pause_reason(db, "DEMO")
        assert r and r.startswith("OPENING_PROTECT:PAUSED:2 stop-outs") and "09:45" in r

    def test_no_pause_outside_the_window_or_when_limit_is_zero_or_mode_uncovered(self, db, monkeypatch):
        add_close(db, symbol="A")
        add_close(db, symbol="B")
        clock(monkeypatch, 9, 45)
        assert op.pause_reason(db, "DEMO") is None
        clock(monkeypatch, 9, 40)
        monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", 0)
        assert op.pause_reason(db, "DEMO") is None
        monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", 2)
        monkeypatch.setattr(config, "OPENING_PROTECT_MODES", ("REAL",))
        assert op.pause_reason(db, "DEMO") is None

    def test_bad_limit_config_fails_open(self, db, monkeypatch):
        clock(monkeypatch, 9, 40)
        monkeypatch.setattr(config, "OPENING_PAUSE_STOPOUTS", "x")
        assert op.pause_reason(db, "DEMO") is None


def _capture_qty(monkeypatch):
    seen = []

    def _risk(intent, state):
        seen.append(intent.qty)
        return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty)
    monkeypatch.setattr(entry, "risk_evaluate", _risk)
    return seen


def _evaluate_at(db, monkeypatch, h, m, mode="DEMO"):
    clock(monkeypatch, h, m)
    return run(entry.evaluate_mode(db, mode, gate_armed=True))


class TestEvaluateModeWiring:
    def _queue(self, db, monkeypatch, symbol="AAA"):
        make_candidate(db, symbol=symbol, conviction=80.0)
        quotes(monkeypatch, {symbol: tick(100.0, symbol=symbol)})
        return _capture_qty(monkeypatch)

    def test_two_stopouts_leave_candidates_queued_and_decide_nothing(self, db, monkeypatch):
        seen = self._queue(db, monkeypatch)
        add_close(db, symbol="S1")
        add_close(db, symbol="S2")
        tally = _evaluate_at(db, monkeypatch, 9, 30)
        assert tally["entered"] == 0 and tally["evaluated"] == 0 and tally["rejected"] == 0 and tally["waited"] == 1
        assert tally["entry_details"] == [] and tally["opening_protect"].startswith("OPENING_PROTECT:PAUSED:2")
        assert seen == []
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 1       # still queued
        assert db.query(models.TradeDecision).count() == 0

    def test_the_same_candidate_enters_once_the_window_ends(self, db, monkeypatch):
        self._queue(db, monkeypatch)
        add_close(db, symbol="S1")
        add_close(db, symbol="S2")
        _evaluate_at(db, monkeypatch, 9, 30)
        tally = _evaluate_at(db, monkeypatch, 9, 46)
        assert tally["entered"] == 1 and "opening_protect" not in tally

    def test_one_stopout_does_not_pause(self, db, monkeypatch):
        self._queue(db, monkeypatch)
        add_close(db, symbol="S1")
        tally = _evaluate_at(db, monkeypatch, 9, 30)
        assert tally["entered"] == 1 and "opening_protect" not in tally

    def test_uncovered_mode_is_not_paused(self, db, monkeypatch):
        self._queue(db, monkeypatch)
        monkeypatch.setattr(config, "OPENING_PROTECT_MODES", ("REAL",))
        add_close(db, symbol="S1")
        add_close(db, symbol="S2")
        assert _evaluate_at(db, monkeypatch, 9, 30)["entered"] == 1

    def test_switch_off_enters_as_before(self, db, monkeypatch):
        self._queue(db, monkeypatch)
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
        add_close(db, symbol="S1")
        add_close(db, symbol="S2")
        assert _evaluate_at(db, monkeypatch, 9, 30)["entered"] == 1

    def test_quantity_is_halved_inside_the_window_and_full_after_it(self, db, monkeypatch):
        seen = self._queue(db, monkeypatch, "AAA")
        assert _evaluate_at(db, monkeypatch, 9, 30)["entered"] == 1
        make_candidate(db, symbol="BBB", conviction=80.0)
        quotes(monkeypatch, {"BBB": tick(100.0, symbol="BBB")})
        assert _evaluate_at(db, monkeypatch, 10, 30)["entered"] == 1
        inside, outside = seen
        assert outside > 1 and inside == opening_expected(outside)

    def test_size_cut_off_gives_the_full_quantity(self, db, monkeypatch):
        seen = self._queue(db, monkeypatch, "AAA")
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
        _evaluate_at(db, monkeypatch, 9, 30)
        full = seen[0]
        make_candidate(db, symbol="BBB", conviction=80.0)
        quotes(monkeypatch, {"BBB": tick(100.0, symbol="BBB")})
        monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", True)
        _evaluate_at(db, monkeypatch, 9, 31)
        assert seen[1] == opening_expected(full)


def opening_expected(full_qty):
    return op.scaled_qty(full_qty, 0.5)


class TestConfig:
    def test_defaults_and_env_parsing(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()
        for needle in ('OPENING_PROTECT_ENABLED = ((os.getenv("OPENING_PROTECT_ENABLED")', 'OPENING_PROTECT_UNTIL_IST = (os.getenv("OPENING_PROTECT_UNTIL_IST")',
                       'OPENING_SIZE_MULT = _og_float("OPENING_SIZE_MULT", 0.5)', 'OPENING_PAUSE_STOPOUTS = _op_int("OPENING_PAUSE_STOPOUTS", 2)',
                       '"REAL,DEMO"'):
            assert needle in src

    def test_op_int_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("OPENING_PAUSE_STOPOUTS", "nope")
        assert config._op_int("OPENING_PAUSE_STOPOUTS", 2) == 2
        monkeypatch.setenv("OPENING_PAUSE_STOPOUTS", "3")
        assert config._op_int("OPENING_PAUSE_STOPOUTS", 2) == 3
        monkeypatch.delenv("OPENING_PAUSE_STOPOUTS")
        assert config._op_int("OPENING_PAUSE_STOPOUTS", 2) == 2
