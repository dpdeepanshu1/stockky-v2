"""tests/test_trailing.py - offline tests for orders/trailing.py (group 217).

Ratchets a live scalp's STOP_LOSS_LEG up behind the peak. The failure modes worth guarding: lowering a stop,
moving to a price Dhan rejects (at/above the market), hammering Dhan with modifies, letting breakeven undo a
trail, and one bad symbol aborting the pass.

Offline: in-memory SQLite, a fake `modify_super_order`, a scripted tick buffer.
Run from services/position-stocks-service:
    python3 -m pytest tests/test_trailing.py -q --cov=orders --cov-report=term-missing
"""
from __future__ import annotations

import itertools
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import notifier
from execution import dhan_client
from feed import ws_client
from orders import trailing


class Rig:
    def __init__(self):
        self.modify_calls: list[dict] = []
        self.modify_error: BaseException | None = None
        self.ticks: dict[str, float] = {}
        self.tick_raises: set[str] = set()


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    r = Rig()
    sent: list[str] = []

    def _modify(db_, **kw):
        r.modify_calls.append(kw)
        if r.modify_error:
            raise r.modify_error
        return {}

    def _ticks(sym):
        if sym in r.tick_raises:
            raise RuntimeError("ws down")
        return [(0, r.ticks[sym])] if sym in r.ticks else []

    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: sent.append(m) or True)
    monkeypatch.setattr(dhan_client, "modify_super_order", _modify)
    monkeypatch.setattr(ws_client, "get_tick_buffer", _ticks)
    for k, v in dict(
        USE_SUPER_ORDER=True, TRAILING_STOP_ENABLED=True, TRAIL_ACTIVATE_PCT=1.0,
        TRAIL_DISTANCE_STOP_FRACTION=0.6, TRAIL_MIN_DISTANCE_PCT=0.4, TRAIL_MIN_STEP_PCT=0.15,
        TRAIL_MIN_INTERVAL_S=20.0, TRAIL_RETRY_BACKOFF_S=60.0, BREAKEVEN_STOP_BUFFER_TICKS=2,
    ).items():
        monkeypatch.setattr(config, k, v)
    trailing.reset_state()
    yield db, r, sent
    trailing.reset_state()
    db.close()


_ids = itertools.count(1)


def mkpos(db, symbol=None, *, entry=100.0, qty=10, stop=99.0, target=104.0, stop_pct=1.0, peak=None,
          super_id="SO1", moved=False, cnc=False, status="OPEN"):
    n = next(_ids)
    p = models.ScalpPosition(
        symbol=symbol or f"SYM{n}", dhan_security_id=str(1000 + n), window_source="5m", status=status,
        entry_price=entry, quantity=qty, target_price=target, stop_price=stop,
        adaptive_target_pct=4.0, adaptive_stop_pct=stop_pct, capital_risked=entry * qty,
        dhan_super_order_id=super_id, stop_moved_to_breakeven=moved, max_price_seen=peak,
        overnight_converted_to_cnc=cnc, opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(p)
    db.commit()
    return p


def cts(**kw):
    base = dict(entry=100.0, peak=102.0, ltp=102.0, current_stop=99.0, target=104.0, stop_pct=1.0)
    base.update(kw)
    return trailing.compute_trailing_stop(**base)


class TestComputeTrailingStop:
    def test_below_activation_does_not_trail(self, env):
        assert cts(peak=100.99, ltp=100.99) is None

    def test_exactly_at_activation_trails_at_peak_minus_distance(self, env):
        # peak 101.0, stop_pct 1.0 -> distance max(0.4, 0.6)=0.6% -> 101*0.994 = 100.394 -> tick 100.39
        assert cts(peak=101.0, ltp=101.0) == pytest.approx(100.39)

    def test_distance_never_tighter_than_the_minimum(self, env):
        # stop_pct 0.5 -> 0.3% < 0.4% floor -> 102*(1-0.004) = 101.592 -> 101.59
        assert cts(stop_pct=0.5) == pytest.approx(101.59)

    def test_wide_stop_gets_a_wider_trail(self, env):
        # stop_pct 2.0 -> 1.2% -> 102*0.988 = 100.776 -> 100.78
        assert cts(stop_pct=2.0) == pytest.approx(100.78)

    def test_never_below_entry_plus_buffer_once_active(self, env):
        # wide trail would give 101*(1-0.03)=97.97 -> floored to entry + 2 ticks (0.01 each at 100) = 100.02
        assert cts(peak=101.0, ltp=101.0, stop_pct=5.0) == pytest.approx(100.02)

    def test_stop_is_clamped_below_the_market(self, env):
        # peak 103, price fell back to 100.3: trail level 102.38 is above the market -> one tick under the market
        assert cts(peak=103.0, ltp=100.3) == pytest.approx(100.29)

    def test_stop_never_goes_down(self, env):
        assert cts(current_stop=101.9) is None
        assert cts(current_stop=102.5) is None

    def test_tiny_improvement_is_skipped(self, env):
        # peak 102, stop_pct 1.0 -> candidate 101.39; minimum step = max(tick, 0.15% of entry) = 0.15
        assert cts(current_stop=101.30) is None                      # +0.09
        assert cts(current_stop=101.25) is None                      # +0.14
        assert cts(current_stop=101.40) is None                      # not an improvement at all
        assert cts(current_stop=101.20) == pytest.approx(101.39)     # +0.19 is worth a modify

    def test_stop_at_or_above_target_is_not_set(self, env):
        assert cts(target=101.3) is None                             # candidate 101.39 >= target
        assert cts(target=101.39) is None                            # equal also refused
        assert cts(target=101.5) == pytest.approx(101.39)            # still below target -> fine

    def test_missing_target_is_fine(self, env):
        assert cts(target=None) == pytest.approx(101.39)

    @pytest.mark.parametrize("kw", [
        dict(entry=0.0), dict(entry=None), dict(ltp=0.0), dict(ltp=None), dict(peak=None), dict(peak=100.0),
        dict(peak=99.0),
    ])
    def test_bad_inputs_return_none(self, env, kw):
        assert cts(**kw) is None

    def test_a_config_error_is_swallowed(self, env, monkeypatch):
        monkeypatch.setattr(config, "TRAIL_ACTIVATE_PCT", "bad")
        assert cts() is None


class TestRunTrailingStop:
    def test_moves_the_stop_and_marks_it_ratcheted(self, env):
        db, r, sent = env
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        r.ticks[p.symbol] = 102.0
        assert trailing.run_trailing_stop(db) == 1
        assert r.modify_calls == [{"order_id": "SO1", "order_leg": "STOP_LOSS_LEG", "stop_loss_price": 101.39}]
        assert p.stop_price == pytest.approx(101.39) and p.stop_moved_to_breakeven is True
        assert p.target_price == 104.0                                   # target leg untouched
        assert len(sent) == 1 and p.symbol in sent[0] and "Trailing stop" in sent[0]

    def test_uses_the_stored_peak_when_the_price_has_fallen_back(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, stop=99.0, peak=103.0)
        r.ticks[p.symbol] = 102.5
        trailing.run_trailing_stop(db)
        assert r.modify_calls[0]["stop_loss_price"] == pytest.approx(102.38)   # 103 * 0.994

    def test_a_new_high_in_the_live_tick_counts_before_the_tracker_catches_up(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, stop=99.0, peak=100.5)
        r.ticks[p.symbol] = 102.0
        trailing.run_trailing_stop(db)
        assert r.modify_calls[0]["stop_loss_price"] == pytest.approx(101.39)

    def test_below_activation_nothing_happens(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, peak=100.8)
        r.ticks[p.symbol] = 100.8
        assert trailing.run_trailing_stop(db) == 0 and r.modify_calls == []

    def test_per_position_throttle_between_modifies(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        r.ticks[p.symbol] = 102.0
        assert trailing.run_trailing_stop(db) == 1
        r.ticks[p.symbol] = 104.0                                        # new high straight away
        p.target_price = 110.0
        db.commit()
        assert trailing.run_trailing_stop(db) == 0 and len(r.modify_calls) == 1   # inside the 20 s window
        trailing._next_attempt[p.id] = 0.0
        assert trailing.run_trailing_stop(db) == 1 and len(r.modify_calls) == 2

    def test_telegram_notice_only_on_the_first_move(self, env):
        db, r, sent = env
        p = mkpos(db, entry=100.0, stop=99.0, target=110.0, peak=102.0)
        r.ticks[p.symbol] = 102.0
        trailing.run_trailing_stop(db)
        trailing._next_attempt[p.id] = 0.0
        r.ticks[p.symbol] = 105.0
        trailing.run_trailing_stop(db)
        assert len(r.modify_calls) == 2 and len(sent) == 1

    def test_rejected_modify_leaves_the_row_alone_and_backs_off(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        r.ticks[p.symbol] = 102.0
        r.modify_error = RuntimeError("leg not pending")
        assert trailing.run_trailing_stop(db) == 0
        assert p.stop_price == 99.0 and p.stop_moved_to_breakeven is False
        r.modify_error = None
        assert trailing.run_trailing_stop(db) == 0 and len(r.modify_calls) == 1     # still backing off
        trailing._next_attempt[p.id] = 0.0
        assert trailing.run_trailing_stop(db) == 1

    def test_switch_off_does_nothing(self, env, monkeypatch):
        db, r, _ = env
        monkeypatch.setattr(config, "TRAILING_STOP_ENABLED", False)
        p = mkpos(db, peak=103.0)
        r.ticks[p.symbol] = 103.0
        assert trailing.run_trailing_stop(db) == 0 and r.modify_calls == []

    def test_plain_order_fallback_does_nothing(self, env, monkeypatch):
        db, r, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        p = mkpos(db, peak=103.0)
        r.ticks[p.symbol] = 103.0
        assert trailing.run_trailing_stop(db) == 0 and r.modify_calls == []

    def test_positions_without_a_super_order_or_not_open_or_carried_are_skipped(self, env):
        db, r, _ = env
        a = mkpos(db, super_id=None, peak=103.0)
        b = mkpos(db, status="EXIT_LEGS_REJECTED", peak=103.0)
        c = mkpos(db, cnc=True, peak=103.0)
        for p in (a, b, c):
            r.ticks[p.symbol] = 103.0
        assert trailing.run_trailing_stop(db) == 0 and r.modify_calls == []

    def test_no_tick_or_tick_error_skips_that_position_only(self, env):
        db, r, _ = env
        a = mkpos(db, peak=103.0, super_id="A")
        b = mkpos(db, peak=103.0, super_id="B")
        c = mkpos(db, peak=103.0, super_id="C")
        r.tick_raises.add(a.symbol)
        r.ticks[c.symbol] = 103.0                                         # b has no tick at all
        assert trailing.run_trailing_stop(db) == 1
        assert [m["order_id"] for m in r.modify_calls] == ["C"]

    def test_one_failing_symbol_does_not_abort_the_pass(self, env, monkeypatch):
        db, r, _ = env
        a = mkpos(db, peak=103.0, super_id="A")
        b = mkpos(db, peak=103.0, super_id="B")
        for p in (a, b):
            r.ticks[p.symbol] = 103.0
        real = dhan_client.modify_super_order

        def _flaky(db_, **kw):
            if kw["order_id"] == "A":
                raise RuntimeError("boom")
            return real(db_, **kw)

        monkeypatch.setattr(dhan_client, "modify_super_order", _flaky)
        assert trailing.run_trailing_stop(db) == 1 and b.stop_moved_to_breakeven and not a.stop_moved_to_breakeven

    def test_notifier_failure_does_not_undo_the_move(self, env, monkeypatch):
        db, r, _ = env
        monkeypatch.setattr(notifier, "notify_fire_and_forget",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")))
        p = mkpos(db, peak=102.0)
        r.ticks[p.symbol] = 102.0
        assert trailing.run_trailing_stop(db) == 1 and p.stop_moved_to_breakeven is True

    def test_breakeven_cannot_lower_a_trailed_stop(self, env):
        from orders import breakeven
        db, r, _ = env
        g = breakeven._get_gate_state(db)
        g.breakeven_stop_enabled = True
        db.commit()
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        p.breakeven_trigger_pct = 0.5
        db.commit()
        r.ticks[p.symbol] = 102.0
        trailing.run_trailing_stop(db)
        n = len(r.modify_calls)
        assert breakeven.run_breakeven_stop(db) == 0 and len(r.modify_calls) == n
        assert p.stop_price == pytest.approx(101.39)
