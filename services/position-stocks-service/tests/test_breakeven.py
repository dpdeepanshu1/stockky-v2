"""
tests/test_breakeven.py — offline tests for orders/breakeven.py.

Moves a live position's STOP_LOSS_LEG up to (just above) entry once the trade
has run far enough toward target. The failure modes worth guarding: moving the
stop when it should not move, moving it to a price Dhan will reject (at or
above the market), moving it repeatedly, or one bad symbol aborting the pass.

Offline: in-memory SQLite, a fake `modify_super_order`, a scripted tick buffer.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_breakeven.py -q --cov=orders --cov-report=term-missing
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
from orders import breakeven


class Rig:
    def __init__(self):
        self.modify_calls: list[dict] = []
        self.modify_error: BaseException | None = None
        self.ticks: dict[str, float] = {}
        self.tick_raises: set[str] = set()
        self.fail_symbols: set[str] = set()


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    r = Rig()
    sent = {"info": [], "boom": False}

    def _info(m, *a, **k):
        if sent["boom"]:
            raise RuntimeError("telegram down")
        sent["info"].append(m)
        return True

    def _modify(db_, **kw):
        r.modify_calls.append(kw)
        if r.modify_error:
            raise r.modify_error
        return {}

    def _ticks(sym):
        if sym in r.tick_raises:
            raise RuntimeError("ws down")
        return [(0, r.ticks[sym])] if sym in r.ticks else []

    monkeypatch.setattr(notifier, "notify_sync", _info)
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: _info(m))
    monkeypatch.setattr(dhan_client, "modify_super_order", _modify)
    monkeypatch.setattr(ws_client, "get_tick_buffer", _ticks)
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
    monkeypatch.setattr(config, "BREAKEVEN_STOP_BUFFER_TICKS", 2)
    g = breakeven._get_gate_state(db)
    g.breakeven_stop_enabled = True
    db.commit()
    yield db, r, sent
    db.close()


_ids = itertools.count(1)


def mkpos(db, symbol=None, *, status="OPEN", entry=100.0, qty=10, trigger=0.5, super_id="SO1",
          moved=False, stop=99.0):
    n = next(_ids)
    p = models.ScalpPosition(
        symbol=symbol or f"SYM{n}", dhan_security_id=str(1000 + n), window_source="5m", status=status,
        entry_price=entry, quantity=qty, target_price=entry * 1.02, stop_price=stop,
        adaptive_target_pct=2.0, adaptive_stop_pct=1.0, capital_risked=entry * qty,
        dhan_super_order_id=super_id, breakeven_trigger_pct=trigger, stop_moved_to_breakeven=moved,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(p)
    db.commit()
    return p


def test_gate_row_is_created_once_and_defaults_to_off(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    g1 = breakeven._get_gate_state(db)
    g2 = breakeven._get_gate_state(db)
    assert g1.id == g2.id and g1.mode == "REAL" and not g1.breakeven_stop_enabled
    assert db.query(models.ScalpGateState).count() == 1


class TestSwitches:
    def test_off_by_default_does_nothing(self, env):
        db, r, _ = env
        g = breakeven._get_gate_state(db)
        g.breakeven_stop_enabled = False
        db.commit()
        p = mkpos(db)
        r.ticks[p.symbol] = 105.0
        assert breakeven.run_breakeven_stop(db) == 0 and r.modify_calls == []

    def test_plain_order_fallback_mode_does_nothing(self, env, monkeypatch):
        db, r, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        p = mkpos(db)
        r.ticks[p.symbol] = 105.0
        assert breakeven.run_breakeven_stop(db) == 0 and r.modify_calls == []


class TestMoveStop:
    def test_moves_stop_to_buffered_entry_once_trigger_is_reached(self, env):
        db, r, sent = env
        p = mkpos(db, entry=100.0, qty=10, trigger=0.5, stop=99.0)
        r.ticks[p.symbol] = 100.6                               # +0.6% >= 0.5%
        assert breakeven.run_breakeven_stop(db) == 1
        # tick size at ₹100 is 0.01 -> entry + 2 ticks
        assert r.modify_calls == [{"order_id": "SO1", "order_leg": "STOP_LOSS_LEG", "stop_loss_price": 100.02}]
        assert p.stop_price == pytest.approx(100.02) and p.stop_moved_to_breakeven is True
        assert any("Breakeven stop" in m and p.symbol in m and "99.00" in m for m in sent["info"])

    def test_exactly_at_trigger_moves(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[p.symbol] = 100.5
        assert breakeven.run_breakeven_stop(db) == 1

    def test_just_below_trigger_does_not_move(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[p.symbol] = 100.49
        assert breakeven.run_breakeven_stop(db) == 0
        assert r.modify_calls == [] and p.stop_price == 99.0 and p.stop_moved_to_breakeven is False

    def test_loss_never_moves_the_stop(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[p.symbol] = 95.0
        assert breakeven.run_breakeven_stop(db) == 0

    def test_zero_buffer_ticks_restores_exact_entry(self, env, monkeypatch):
        db, r, _ = env
        monkeypatch.setattr(config, "BREAKEVEN_STOP_BUFFER_TICKS", 0)
        p = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[p.symbol] = 101.0
        breakeven.run_breakeven_stop(db)
        assert p.stop_price == pytest.approx(100.0)

    def test_buffer_scales_with_the_price_band(self, env):
        db, r, _ = env
        p = mkpos(db, entry=1000.0, trigger=0.5)                # tick size 0.10 at ₹1,000
        r.ticks[p.symbol] = 1010.0
        breakeven.run_breakeven_stop(db)
        assert p.stop_price == pytest.approx(1000.2)

    def test_stop_is_clamped_below_the_market_when_the_buffer_would_touch_it(self, env, monkeypatch):
        # entry 100, ltp 100.05: a 10-tick buffer would put the stop at 100.10, above the market
        db, r, _ = env
        monkeypatch.setattr(config, "BREAKEVEN_STOP_BUFFER_TICKS", 10)
        p = mkpos(db, entry=100.0, trigger=0.04)
        r.ticks[p.symbol] = 100.05
        breakeven.run_breakeven_stop(db)
        assert r.modify_calls[0]["stop_loss_price"] == pytest.approx(100.04)     # ltp - one tick
        assert r.modify_calls[0]["stop_loss_price"] < 100.05

    def test_stop_equal_to_the_market_price_is_also_clamped(self, env):
        # buffered stop 100.02 == ltp 100.02: Dhan rejects a stop AT the market too
        db, r, _ = env
        p = mkpos(db, entry=100.0, trigger=0.01)
        r.ticks[p.symbol] = 100.02
        breakeven.run_breakeven_stop(db)
        assert r.modify_calls[0]["stop_loss_price"] == pytest.approx(100.01)

    def test_only_ever_moves_once_per_position(self, env):
        db, r, _ = env
        p = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[p.symbol] = 101.0
        assert breakeven.run_breakeven_stop(db) == 1
        assert breakeven.run_breakeven_stop(db) == 0
        assert len(r.modify_calls) == 1

    def test_several_positions_are_judged_independently(self, env):
        db, r, _ = env
        up = mkpos(db, entry=100.0, trigger=0.5)
        flat = mkpos(db, entry=100.0, trigger=0.5)
        r.ticks[up.symbol], r.ticks[flat.symbol] = 101.0, 100.1
        assert breakeven.run_breakeven_stop(db) == 1
        assert up.stop_moved_to_breakeven and not flat.stop_moved_to_breakeven


class TestEligibility:
    @pytest.mark.parametrize("kw", [
        {"status": "TARGET_HIT"}, {"status": "EXIT_LEGS_REJECTED"}, {"status": "EOD_SQUAREOFF"},
        {"moved": True}, {"trigger": None}, {"super_id": None},
    ])
    def test_ineligible_positions_are_never_touched(self, env, kw, caplog):
        db, r, _ = env
        p = mkpos(db, **kw)
        r.ticks[p.symbol] = 110.0
        with caplog.at_level("ERROR"):
            assert breakeven.run_breakeven_stop(db) == 0
        assert r.modify_calls == []
        assert not caplog.records            # filtered out by the query, not by a swallowed exception

    def test_non_positive_entry_price_is_skipped_cleanly(self, env, caplog):
        db, r, _ = env
        p = mkpos(db, entry=0.0)
        r.ticks[p.symbol] = 10.0
        with caplog.at_level("ERROR"):
            assert breakeven.run_breakeven_stop(db) == 0
        assert r.modify_calls == [] and not caplog.records    # guarded, no ZeroDivisionError swallowed

    @pytest.mark.parametrize("ltp", [None, 0.0, -2.0])
    def test_no_usable_live_price_is_skipped(self, env, ltp):
        db, r, _ = env
        p = mkpos(db)
        if ltp is not None:
            r.ticks[p.symbol] = ltp
        assert breakeven.run_breakeven_stop(db) == 0 and r.modify_calls == []

    def test_tick_feed_error_is_skipped(self, env):
        db, r, _ = env
        p = mkpos(db)
        r.tick_raises.add(p.symbol)
        assert breakeven.run_breakeven_stop(db) == 0


class TestFailureIsolation:
    def test_broker_rejection_leaves_the_position_untouched_and_retryable(self, env):
        db, r, sent = env
        p = mkpos(db, stop=99.0)
        r.ticks[p.symbol] = 101.0
        r.modify_error = RuntimeError("leg already filled")
        assert breakeven.run_breakeven_stop(db) == 0
        db.refresh(p)
        assert p.stop_price == 99.0 and p.stop_moved_to_breakeven is False and sent["info"] == []
        r.modify_error = None
        assert breakeven.run_breakeven_stop(db) == 1              # next tick simply tries again

    def test_one_bad_position_does_not_abort_the_pass(self, env, monkeypatch):
        db, r, _ = env
        bad = mkpos(db)
        good = mkpos(db)
        r.ticks[bad.symbol] = r.ticks[good.symbol] = 101.0
        real = dhan_client.modify_super_order          # the fixture's recording fake

        def selective(db_, **kw):
            if kw["order_id"] == "BAD":
                raise RuntimeError("rejected")
            return real(db_, **kw)
        bad.dhan_super_order_id = "BAD"
        db.commit()
        monkeypatch.setattr(dhan_client, "modify_super_order", selective)
        assert breakeven.run_breakeven_stop(db) == 1
        db.refresh(good)
        db.refresh(bad)
        assert good.stop_moved_to_breakeven is True and bad.stop_moved_to_breakeven is False

    def test_notification_failure_does_not_undo_the_move(self, env):
        db, r, sent = env
        sent["boom"] = True
        p = mkpos(db)
        r.ticks[p.symbol] = 101.0
        assert breakeven.run_breakeven_stop(db) == 1
        assert p.stop_moved_to_breakeven is True
