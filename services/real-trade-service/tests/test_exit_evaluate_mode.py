"""
tests/test_exit_evaluate_mode.py — offline tests for exit_engine.exit.evaluate_mode(),
the per-cycle decision-tree dispatcher that decides whether every open REAL/DEMO
position holds, trails, partial-exits, full-exits, time-stops, or emergency-exits.

Previously untested (27% file coverage per AUDIT_REPORT.md) despite being the
function that actually decides when real positions are sold. This suite exercises
the decision tree end-to-end in DEMO mode (so real position/account state mutations
run for real, not mocked) plus a few REAL-mode routing checks (mocking only the
Dhan-facing edge, `_send_real_sell`, which already has its own dedicated coverage in
test_exit_placement_backoff.py / test_exit_backoff_escalation.py).

Priority order under test (see evaluate_mode's own docstring):
  emergency_gap > stop_hit > target_hit > time_stop > breakeven_stop > trail_stop > hold

Run from services/real-trade-service:
    python3 -m pytest tests/test_exit_evaluate_mode.py -q --cov=exit_engine.exit --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import exit_engine.exit as ex
from market_feed.feed import Tick


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    # DEMO account row — refresh_unrealized/close_position both require one.
    s.add(models.TradeAccount(
        mode="DEMO", starting_capital=100000.0, current_equity=100000.0,
        cash_available=100000.0,
    ))
    s.commit()
    yield s
    s.close()


def make_position(db, *, symbol="TESTCO", qty=10, entry=100.0, stop=95.0, target=110.0,
                   opened_days_ago=0, initial_stop_distance=None, mode="DEMO",
                   status="OPEN"):
    pos = models.TradePosition(
        mode=mode, symbol=symbol, status=status, qty_open=qty,
        avg_entry_price=entry, current_stop=stop, current_target=target,
        initial_stop_distance=initial_stop_distance if initial_stop_distance is not None else (entry - stop) if stop else None,
        opened_at=datetime.now(timezone.utc) - timedelta(days=opened_days_ago),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def tick(price, atr=None, day_high=None, day_low=None, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=atr,
                source="test", day_high=day_high, day_low=day_low)


def quotes(monkeypatch, mapping: dict):
    async def _q(symbols):
        return dict(mapping)
    monkeypatch.setattr(ex, "get_quotes", _q)


class TestEmptyAndMissingTick:
    def test_no_open_positions_returns_zeroed_tally(self, db):
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally == {
            "evaluated": 0, "held": 0, "trailed": 0,
            "partial_exits": 0, "full_exits": 0,
            "time_stops": 0, "emergency_exits": 0,
        }

    def test_missing_tick_holds_without_crashing(self, db, monkeypatch):
        make_position(db)
        quotes(monkeypatch, {})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["held"] == 1
        assert tally["evaluated"] == 1


class TestStopHit:
    def test_stop_hit_closes_full_position_in_demo(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(94.0)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["full_exits"] == 1
        db.refresh(pos)
        assert pos.status == "CLOSED"
        assert pos.qty_open == 0
        assert pos.realized_pnl == pytest.approx((94.0 - 100.0) * 10, abs=0.01)

    def test_stop_hit_takes_priority_over_a_simultaneously_valid_target(self, db, monkeypatch):
        # Pathological but should never let a position ride a target hit
        # when it's also breached its stop — capital protection first.
        pos = make_position(db, qty=10, entry=100.0, stop=95.0, target=90.0)  # inverted on purpose
        quotes(monkeypatch, {"TESTCO": tick(92.0)})  # <= target(90)? no. <= stop(95)? yes
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.status == "CLOSED"  # stop-hit branch, not target branch

    def test_price_exactly_at_stop_triggers_exit(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(95.0)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["full_exits"] == 1

    def test_no_stop_set_never_stop_exits(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=None, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(50.0)})  # would've hit almost any real stop
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["full_exits"] == 0


class TestTargetHitPartial:
    def test_target_hit_locks_60pct_and_trails_remainder(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(111.0)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["partial_exits"] == 1
        db.refresh(pos)
        assert pos.qty_open == 4          # 10 - round(10*0.60) = 4
        assert pos.status == "PARTIALLY_CLOSED"
        assert pos.current_stop == 100.0  # raised to breakeven
        assert pos.current_target is None  # nullified so it can't re-trigger
        assert pos.realized_pnl == pytest.approx((111.0 - 100.0) * 6, abs=0.01)

    def test_after_partial_exit_same_price_does_not_retrigger_target(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(111.0)})
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        qty_after_first = pos.qty_open
        # Next cycle, price still above the OLD target — should NOT partial-exit again
        # because current_target was nullified.
        tally = run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert tally["partial_exits"] == 0
        assert pos.qty_open == qty_after_first  # untouched this cycle


class TestEmergencyGapDown:
    def test_gap_through_stop_fires_emergency_exit_not_ordinary_stop(self, db, monkeypatch):
        # original_risk = entry - stop = 5.0; EMERGENCY_LOSS_MULT default 1.5
        # so unrealized_loss_per_share must exceed 7.5 to trigger emergency.
        pos = make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(90.0)})  # loss/share = 10 > 7.5
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["emergency_exits"] == 1
        assert tally["full_exits"] == 0
        db.refresh(pos)
        assert pos.status == "CLOSED"

    def test_loss_just_under_the_emergency_multiple_falls_to_ordinary_stop_hit(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {"TESTCO": tick(94.9)})  # loss/share ~5.1, < 7.5 -> not emergency
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["emergency_exits"] == 0
        assert tally["full_exits"] == 1  # still exits, just via the ordinary stop-hit branch


class TestTimeStop:
    def test_held_past_max_days_below_target_exits_full(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=None,
                             opened_days_ago=ex.MAX_HOLD_DAYS + 1)
        # below 1.005x entry benchmark used when target is None
        quotes(monkeypatch, {"TESTCO": tick(100.1)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["time_stops"] == 1
        db.refresh(pos)
        assert pos.status == "CLOSED"

    def test_held_past_max_days_but_above_target_does_not_time_stop(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=90.0, target=105.0,
                       opened_days_ago=ex.MAX_HOLD_DAYS + 1)
        quotes(monkeypatch, {"TESTCO": tick(106.0)})  # above target -> target-hit branch instead
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["time_stops"] == 0
        assert tally["partial_exits"] == 1

    def test_not_yet_at_max_days_does_not_time_stop(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=90.0, target=None,
                       opened_days_ago=ex.MAX_HOLD_DAYS - 1)
        quotes(monkeypatch, {"TESTCO": tick(100.1)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["time_stops"] == 0

    def test_time_stop_writes_full_exit_action_not_mislabeled_emergency(self, db, monkeypatch):
        # Regression guard for the 2026-09-01 BUG FIX documented inline in exit.py:
        # a time-stop close must never be logged with action="EMERGENCY_EXIT".
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=None,
                             opened_days_ago=ex.MAX_HOLD_DAYS + 1)
        quotes(monkeypatch, {"TESTCO": tick(100.1)})
        run(ex.evaluate_mode(db, "DEMO"))
        last_decision = (
            db.query(models.TradeExitDecision)
            .filter_by(position_id=pos.id)
            .order_by(models.TradeExitDecision.id.desc())
            .first()
        )
        assert last_decision.action == "FULL_EXIT"


class TestEarlyWarning:
    def test_early_warn_day_logs_hold_with_no_exit(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=200.0,
                             opened_days_ago=ex.EARLY_WARN_DAYS)
        quotes(monkeypatch, {"TESTCO": tick(99.0)})  # still at/below entry
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["held"] == 1
        last_decision = (
            db.query(models.TradeExitDecision)
            .filter_by(position_id=pos.id)
            .order_by(models.TradeExitDecision.id.desc())
            .first()
        )
        assert last_decision.action == "HOLD"
        assert f"Day {ex.EARLY_WARN_DAYS} review" in last_decision.reasoning


class TestBreakevenStop:
    def test_gain_past_1atr_moves_stop_to_breakeven(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=200.0)
        # gain = 3 >= 1.0 * atr(2.0) -> breakeven triggers
        quotes(monkeypatch, {"TESTCO": tick(103.0, atr=2.0)})
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["trailed"] == 1
        db.refresh(pos)
        assert pos.current_stop == 100.0

    def test_gain_under_1atr_does_not_move_stop_to_breakeven_yet(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=200.0)
        quotes(monkeypatch, {"TESTCO": tick(101.0, atr=5.0)})  # gain 1 < 1.0*5.0 -> no breakeven
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.current_stop != 100.0  # breakeven (entry) was NOT set
        # falls through to the ATR trail branch instead (still ratcheted, not held flat)
        assert pos.current_stop >= 90.0

    def test_breakeven_never_lowers_an_already_higher_stop(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=101.0, target=200.0)  # already above breakeven
        quotes(monkeypatch, {"TESTCO": tick(103.0, atr=2.0)})
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.current_stop == 101.0  # unchanged, not pulled down to 100


class TestAtrTrailingStop:
    def test_trail_only_ratchets_up_never_down(self, db, monkeypatch):
        # Force past breakeven already so this cycle falls through to the trail
        # branch (current_stop already >= avg_entry_price, so breakeven is a no-op).
        pos = make_position(db, qty=10, entry=100.0, stop=100.0, target=200.0)
        quotes(monkeypatch, {"TESTCO": tick(120.0, atr=2.0)})  # well past breakeven trigger too
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.current_stop >= 100.0  # never loosened
        assert pos.current_stop < 120.0   # still below live price (a stop, not a sell-here)

    def test_never_trails_a_losing_position(self, db, monkeypatch):
        pos = make_position(db, qty=10, entry=100.0, stop=90.0, target=200.0)
        quotes(monkeypatch, {"TESTCO": tick(95.0, atr=2.0)})  # below entry -> no trail, no breakeven
        run(ex.evaluate_mode(db, "DEMO"))
        db.refresh(pos)
        assert pos.current_stop == 90.0


class TestHold:
    def test_no_condition_met_holds(self, db, monkeypatch):
        make_position(db, qty=10, entry=100.0, stop=90.0, target=200.0)
        quotes(monkeypatch, {"TESTCO": tick(100.0)})  # flat, no ATR -> no trail/breakeven either
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["held"] == 1


class TestMultiplePositionsSameCycle:
    def test_each_position_evaluated_independently(self, db, monkeypatch):
        make_position(db, symbol="STOPCO", qty=10, entry=100.0, stop=95.0, target=110.0)
        make_position(db, symbol="HOLDCO", qty=5, entry=50.0, stop=45.0, target=60.0)
        quotes(monkeypatch, {
            "STOPCO": tick(94.0, symbol="STOPCO"),
            "HOLDCO": tick(50.0, symbol="HOLDCO"),
        })
        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["evaluated"] == 2
        assert tally["full_exits"] == 1
        assert tally["held"] == 1


class TestRealModePendingSellGuard:
    def test_real_position_with_pending_sell_is_held_not_reevaluated(self, db, monkeypatch):
        pos = make_position(db, mode="REAL", qty=10, entry=100.0, stop=95.0, target=110.0)
        db.add(models.TradeOrder(
            mode="REAL", symbol=pos.symbol, side="SELL", qty=10,
            order_type="MARKET", status="PLACED",
        ))
        db.commit()
        quotes(monkeypatch, {"TESTCO": tick(50.0)})  # would obviously stop-hit if evaluated
        tally = run(ex.evaluate_mode(db, "REAL"))
        assert tally["held"] == 1
        assert tally["full_exits"] == 0
        db.refresh(pos)
        assert pos.status == "OPEN"  # untouched — reconcile owns this, not evaluate_mode

    def test_real_stop_hit_routes_through_send_real_sell(self, db, monkeypatch):
        make_position(db, mode="REAL", qty=10, entry=100.0, stop=95.0, target=110.0)
        calls = []

        def _fake_send(db_, position, qty, reason, **kw):
            calls.append((position.symbol, qty, reason))
            return True
        monkeypatch.setattr(ex, "_send_real_sell", _fake_send)
        quotes(monkeypatch, {"TESTCO": tick(94.0)})
        tally = run(ex.evaluate_mode(db, "REAL"))
        assert tally["full_exits"] == 1
        assert calls == [("TESTCO", 10, "stop_hit")]

    def test_real_send_failure_leaves_position_held_for_retry(self, db, monkeypatch):
        make_position(db, mode="REAL", qty=10, entry=100.0, stop=95.0, target=110.0)
        monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: False)
        quotes(monkeypatch, {"TESTCO": tick(94.0)})
        tally = run(ex.evaluate_mode(db, "REAL"))
        assert tally["full_exits"] == 0
        assert tally["held"] == 1
