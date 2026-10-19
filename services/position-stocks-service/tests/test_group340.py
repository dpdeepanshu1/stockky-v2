"""tests/test_group340.py - offline tests for group 340 (2026-10-12): stop read-back, stop-breach watchdog, candle-history levels,
scalp target cap, share rounding, status single-source, entry cut-off.

tests/conftest.py switches these features OFF for older tests; each test here turns on what it needs.
Run from services/position-stocks-service:  python3 -m pytest tests/test_group340.py -q
"""
from __future__ import annotations

import itertools
import os
import sys
import time
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
from orders import adaptive, breakeven, calendar_gate, sizing, statuses, stop_watchdog, trailing
from tz_utils import IST

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ---------------------------------------------------------------- shared rig
@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    sent: list = []
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: sent.append(m) or True)
    s.sent = sent
    trailing.reset_state()
    breakeven.reset_state() if hasattr(breakeven, "reset_state") else None
    stop_watchdog.reset_state()
    yield s
    trailing.reset_state()
    stop_watchdog.reset_state()
    s.close()


_ids = itertools.count(1)


def mkpos(db, *, entry=100.0, qty=10, stop=99.0, target=104.0, peak=None, super_id="SO1", cnc=False, status="OPEN"):
    n = next(_ids)
    p = models.ScalpPosition(
        symbol=f"G340{n}", dhan_security_id=str(2000 + n), window_source="5m", status=status,
        entry_price=entry, quantity=qty, target_price=target, stop_price=stop,
        adaptive_target_pct=4.0, adaptive_stop_pct=1.0, capital_risked=entry * qty,
        dhan_super_order_id=super_id, max_price_seen=peak, overnight_converted_to_cnc=cnc,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(p)
    db.commit()
    return p


# ---------------------------------------------------------------- sizing
class TestSizing:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setattr(config, "SIZING_ROUNDING_ENABLED", True)

    def test_rounds_up_when_extra_share_is_within_overshoot(self):
        assert sizing.compute_quantity(1900.0, 1000.0) == 2            # 2 shares = +5.3% over budget, allowed (10%)

    def test_floor_when_extra_share_overshoots(self):
        assert sizing.compute_quantity(1500.0, 1000.0) == 1            # 2 shares = +33%

    def test_one_share_slightly_over_budget_is_allowed(self):
        assert sizing.compute_quantity(950.0, 1000.0) == 1

    def test_one_share_far_over_budget_is_skipped(self):
        assert sizing.compute_quantity(1000.0, 1307.0) == 1             # 1.3x <= 1.5x -> still bought
        assert sizing.compute_quantity(800.0, 1307.0) is None           # 1.63x > 1.5x -> skipped

    def test_disabled_restores_legacy_floor_and_force_one(self, monkeypatch):
        monkeypatch.setattr(config, "SIZING_ROUNDING_ENABLED", False)
        assert sizing.compute_quantity(1900.0, 1000.0) == 1
        assert sizing.compute_quantity(10.0, 1307.0) == 1

    def test_bad_inputs_never_raise(self):
        assert sizing.compute_quantity(1000.0, 0.0) == 1
        assert sizing.compute_quantity(0.0, 50.0) == 1
        assert sizing.compute_quantity("x", 50.0) == 1                     # falls back to the legacy floor


# ---------------------------------------------------------------- target cap
def _levels(target_pct, stop_pct, ltp=100.0):
    return adaptive.AdaptiveLevels(
        target_pct=target_pct, stop_pct=stop_pct,
        target_price=adaptive.round_to_tick(ltp * (1 + target_pct / 100)),
        stop_price=adaptive.round_to_tick(ltp * (1 - stop_pct / 100)),
    )


class TestTargetCap:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_PCT", 2.5)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_MIN_RR", 1.5)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_MIN_STOP_PCT", 0.8)

    def test_target_above_cap_is_cut_and_stop_tightened_for_rr(self):
        out = adaptive.apply_target_cap(_levels(4.0, 2.0), 100.0)
        assert out.target_pct == pytest.approx(2.5)
        assert out.stop_pct == pytest.approx(2.5 / 1.5, abs=1e-3)          # 1.6667
        assert out.target_price == pytest.approx(102.5)
        assert out.stop_price < 100.0 < out.target_price

    def test_target_under_cap_is_untouched(self):
        lv = _levels(2.0, 1.0)
        assert adaptive.apply_target_cap(lv, 100.0) is lv

    def test_stop_never_widened_and_never_under_floor(self):
        out = adaptive.apply_target_cap(_levels(4.0, 1.0), 100.0)          # rr already 2.5 -> stop kept
        assert out.stop_pct == pytest.approx(1.0)
        out2 = adaptive.apply_target_cap(_levels(4.0, 0.5), 100.0)         # already tighter than the floor -> not widened
        assert out2.stop_pct == pytest.approx(0.5)

    def test_cap_zero_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_PCT", 0.0)
        lv = _levels(4.0, 2.0)
        assert adaptive.apply_target_cap(lv, 100.0) is lv

    def test_bad_price_returns_input(self):
        lv = _levels(4.0, 2.0)
        assert adaptive.apply_target_cap(lv, 0.0) is lv


# ---------------------------------------------------------------- candle history
def _minute_bars(n=20, base=100.0, rng=1.0, t0=3000.0):
    return [(t0 + 60 * i, base, base + rng / 2, base - rng / 2, base, 1000.0) for i in range(n)]


class TestCandleHistory:
    def test_minute_bars_group_into_five_minute_ranges(self):
        r = adaptive._ranges_from_minute_bars(_minute_bars(10), 300)
        assert len(r) >= 2
        assert all(hi - lo == pytest.approx(1.0) for hi, lo in r.values())

    def test_bad_rows_are_dropped(self):
        assert adaptive._ranges_from_minute_bars([("x",), (1, 2, 1, 5, 3, 0)], 300) == {}

    def test_history_supplies_bars_when_tick_buffer_is_thin(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [])
        monkeypatch.setattr(adaptive, "_fetch_history_minute_bars", lambda s: _minute_bars(20))
        monkeypatch.setattr(config, "ADAPTIVE_HISTORY_ENABLED", True)
        atr = adaptive._bar_atr_pct("ZZZ", 100.0)
        assert atr == pytest.approx(1.0, abs=0.01)

    def test_disabled_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [])
        monkeypatch.setattr(adaptive, "_fetch_history_minute_bars", lambda s: _minute_bars(20))
        monkeypatch.setattr(config, "ADAPTIVE_HISTORY_ENABLED", False)
        assert adaptive._bar_atr_pct("ZZZ", 100.0) is None

    def test_history_failure_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [])
        monkeypatch.setattr(adaptive, "_fetch_history_minute_bars", lambda s: None)
        monkeypatch.setattr(config, "ADAPTIVE_HISTORY_ENABLED", True)
        assert adaptive._bar_atr_pct("ZZZ", 100.0) is None

    def test_fetch_failure_is_cached_and_never_raises(self, monkeypatch):
        import httpx
        from feed import bar_store
        calls = []

        class Boom:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k):
                calls.append(1)
                raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx, "Client", Boom)
        monkeypatch.setattr(bar_store, "get_bars", lambda s: [])
        adaptive.reset_history_cache()
        assert adaptive._fetch_history_minute_bars("QQQ") is None
        assert adaptive._fetch_history_minute_bars("QQQ") is None
        assert len(calls) == 1                                              # miss cached


# ---------------------------------------------------------------- stop leg read-back
def _row(trigger=101.39, status="PENDING", name="STOP_LOSS_LEG"):
    return {"orderId": "SO1", "legDetails": [{"legName": name, "triggerPrice": trigger, "orderStatus": status}]}


class TestStopLegVerdict:
    def test_price_read(self):
        assert dhan_client.read_stop_leg_price(_row(101.39)) == 101.39
        assert dhan_client.read_stop_leg_price({"orderId": "x"}) is None
        assert dhan_client.read_stop_leg_price("junk") is None

    def test_verdicts(self):
        v = dhan_client._stop_leg_verdict
        assert v(_row(101.39), 101.39, None) is True
        assert v(_row(101.38), 101.39, None) is True                        # within a tick
        assert v(_row(99.0), 101.39, None) is False                         # modify never applied
        assert v(_row(101.39, status="CANCELLED"), 101.39, None) is False  # dead leg
        assert v(None, 101.39, None) is None
        assert v({"orderId": "SO1", "legDetails": []}, 101.39, None) is None

    def test_verify_stop_leg_reads_back_and_retries_once(self, db, monkeypatch):
        monkeypatch.setattr(config, "STOP_VERIFY_ENABLED", True)
        monkeypatch.setattr(config, "STOP_VERIFY_RETRY_WAIT_S", 0.0)
        seq = iter([[_row(99.0)], [_row(101.39)]])                          # book lags once, then shows the move
        monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d, *a, **k: next(seq))
        assert dhan_client.verify_stop_leg(db, order_id="SO1", expected_price=101.39) is True

    def test_verify_false_when_still_wrong(self, db, monkeypatch):
        monkeypatch.setattr(config, "STOP_VERIFY_ENABLED", True)
        monkeypatch.setattr(config, "STOP_VERIFY_RETRY_WAIT_S", 0.0)
        monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d, *a, **k: [_row(99.0)])
        assert dhan_client.verify_stop_leg(db, order_id="SO1", expected_price=101.39) is False

    def test_verify_none_when_unreadable_or_off(self, db, monkeypatch):
        monkeypatch.setattr(config, "STOP_VERIFY_ENABLED", True)

        def boom(*a, **k):
            raise RuntimeError("dhan down")

        monkeypatch.setattr(dhan_client, "get_super_order_list", boom)
        assert dhan_client.verify_stop_leg(db, order_id="SO1", expected_price=101.39) is None
        monkeypatch.setattr(config, "STOP_VERIFY_ENABLED", False)
        assert dhan_client.verify_stop_leg(db, order_id="SO1", expected_price=101.39) is None


class TestTrailingUsesReadBack:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        for k, v in dict(USE_SUPER_ORDER=True, TRAILING_STOP_ENABLED=True, TRAIL_ACTIVATE_PCT=1.0,
                         TRAIL_DISTANCE_STOP_FRACTION=0.6, TRAIL_MIN_DISTANCE_PCT=0.4, TRAIL_MIN_STEP_PCT=0.15,
                         TRAIL_MIN_INTERVAL_S=20.0, TRAIL_RETRY_BACKOFF_S=60.0, BREAKEVEN_STOP_BUFFER_TICKS=2).items():
            monkeypatch.setattr(config, k, v)
        monkeypatch.setattr(dhan_client, "modify_super_order", lambda d, **kw: {})
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [(0, 102.0)])

    def test_unconfirmed_move_is_not_saved_and_alerts(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda *a, **k: False)
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        assert trailing.run_trailing_stop(db) == 0
        assert p.stop_price == 99.0
        assert any("not confirmed" in m for m in db.sent)

    def test_confirmed_move_is_saved(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda *a, **k: True)
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        assert trailing.run_trailing_stop(db) == 1
        assert p.stop_price == pytest.approx(101.39)

    def test_unreadable_book_fails_open_as_before(self, db, monkeypatch):
        monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda *a, **k: None)
        p = mkpos(db, entry=100.0, stop=99.0, peak=102.0)
        assert trailing.run_trailing_stop(db) == 1
        assert p.stop_price == pytest.approx(101.39)


# ---------------------------------------------------------------- stop-breach watchdog
class TestStopWatchdog:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_WATCHDOG_ENABLED", True)
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        monkeypatch.setattr(config, "STOP_WATCHDOG_GRACE_S", 20.0)
        monkeypatch.setattr(config, "STOP_WATCHDOG_MIN_BREACH_TICKS", 1)
        monkeypatch.setattr(config, "STOP_WATCHDOG_MAX_TICK_AGE_S", 15.0)
        monkeypatch.setattr(config, "STOP_WATCHDOG_RETRY_S", 30.0)
        self.closed = []
        from orders import eod_squareoff

        def _close(db_, pos, exit_reason="MANUAL_EXIT"):
            self.closed.append((pos.symbol, exit_reason))
            return {}

        monkeypatch.setattr(eod_squareoff, "close_position_now", _close)

    def _tick(self, monkeypatch, ltp, age=1.0):
        monkeypatch.setattr(stop_watchdog, "_last_tick", lambda s: (ltp, age))

    def test_is_breached_needs_a_tick_under_the_stop(self):
        assert stop_watchdog.is_breached(58.40, 58.85) is True
        assert stop_watchdog.is_breached(58.85, 58.85) is False
        assert stop_watchdog.is_breached(58.84, 58.85) is True
        assert stop_watchdog.is_breached(None, 58.85) is False
        assert stop_watchdog.is_breached(58.0, None) is False

    def test_closes_after_grace_and_alerts(self, db, monkeypatch):
        p = mkpos(db, stop=58.85)
        self._tick(monkeypatch, 58.41)
        stop_watchdog._breach_since[p.id] = time.monotonic() - 60
        assert stop_watchdog.run_stop_watchdog(db) == 1
        assert self.closed == [(p.symbol, "STOP_BREACH_EXIT")]
        assert any("Stop did not fire" in m for m in db.sent)

    def test_no_close_inside_grace(self, db, monkeypatch):
        p = mkpos(db, stop=58.85)
        self._tick(monkeypatch, 58.41)
        assert stop_watchdog.run_stop_watchdog(db) == 0                    # first sighting only starts the clock
        assert p.id in stop_watchdog._breach_since and self.closed == []

    def test_price_recovery_resets_clock(self, db, monkeypatch):
        p = mkpos(db, stop=58.85)
        stop_watchdog._breach_since[p.id] = time.monotonic() - 60
        self._tick(monkeypatch, 59.50)
        assert stop_watchdog.run_stop_watchdog(db) == 0
        assert p.id not in stop_watchdog._breach_since and self.closed == []

    def test_stale_or_missing_tick_does_not_count(self, db, monkeypatch):
        p = mkpos(db, stop=58.85)
        stop_watchdog._breach_since[p.id] = time.monotonic() - 60
        self._tick(monkeypatch, 58.0, age=120.0)
        assert stop_watchdog.run_stop_watchdog(db) == 0 and self.closed == []
        self._tick(monkeypatch, None, None)
        assert stop_watchdog.run_stop_watchdog(db) == 0 and self.closed == []

    def test_cnc_and_non_open_positions_are_skipped(self, db, monkeypatch):
        mkpos(db, stop=58.85, cnc=True)
        mkpos(db, stop=58.85, status="STOP_HIT")
        self._tick(monkeypatch, 58.0)
        for k in list(range(1, 50)):
            stop_watchdog._breach_since[k] = time.monotonic() - 60
        assert stop_watchdog.run_stop_watchdog(db) == 0 and self.closed == []

    def test_rejected_exit_backs_off(self, db, monkeypatch):
        p = mkpos(db, stop=58.85)
        from orders import eod_squareoff

        def _reject(*a, **k):
            raise eod_squareoff.ManualCloseRejected("cooldown")

        monkeypatch.setattr(eod_squareoff, "close_position_now", _reject)
        self._tick(monkeypatch, 58.41)
        stop_watchdog._breach_since[p.id] = time.monotonic() - 60
        assert stop_watchdog.run_stop_watchdog(db) == 0
        assert stop_watchdog._next_attempt[p.id] > time.monotonic()
        assert stop_watchdog.run_stop_watchdog(db) == 0                    # still backing off, no second attempt

    def test_disabled_is_a_noop(self, db, monkeypatch):
        mkpos(db, stop=58.85)
        monkeypatch.setattr(config, "STOP_WATCHDOG_ENABLED", False)
        assert stop_watchdog.run_stop_watchdog(db) == 0


# ---------------------------------------------------------------- statuses (single source)
class TestStatuses:
    def test_breach_status_is_a_flat_sell_and_terminal(self):
        assert "STOP_BREACH_EXIT" in statuses.FLAT_SELL_STATUSES
        assert set(statuses.TERMINAL_STATUSES) >= set(statuses.FLAT_SELL_STATUSES) | set(statuses.BRACKET_STATUSES)

    def test_reconcile_reads_the_shared_tuples(self):
        from orders import reconcile
        assert reconcile._FLAT_SELL_PENDING_STATUSES is statuses.FLAT_SELL_STATUSES
        assert reconcile._TERMINAL_STATUSES is statuses.TERMINAL_STATUSES

    def test_watchdog_status_matches(self):
        assert stop_watchdog.STATUS in statuses.FLAT_SELL_STATUSES

    def test_frontend_mirrors_every_flat_sell_status(self):
        api = open(os.path.join(REPO, "frontend", "src", "positionStocksApi.ts"), encoding="utf-8").read()
        tab = open(os.path.join(REPO, "frontend", "src", "components", "PositionStocksTab.tsx"), encoding="utf-8").read()
        for s in statuses.FLAT_SELL_STATUSES:
            assert f'"{s}"' in api, f"{s} missing from positionStocksApi.ts"
        assert '"STOP_BREACH_EXIT"' in tab
        assert '"CLOSE_ALL"' in tab   # group 466


# ---------------------------------------------------------------- entry cut-off
class TestScalpCutoff:
    def test_blank_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "SCALP_ENTRY_CUTOFF_IST", "")
        assert calendar_gate.scalp_cutoff_reason(datetime(2026, 10, 13, 14, 40, tzinfo=IST)) is None

    def test_before_and_after(self, monkeypatch):
        monkeypatch.setattr(config, "SCALP_ENTRY_CUTOFF_IST", "14:15")
        assert calendar_gate.scalp_cutoff_reason(datetime(2026, 10, 13, 13, 59, tzinfo=IST)) is None
        assert "SCALP_ENTRY_CUTOFF" in calendar_gate.scalp_cutoff_reason(datetime(2026, 10, 13, 14, 20, tzinfo=IST))

    def test_garbage_fails_open(self, monkeypatch):
        monkeypatch.setattr(config, "SCALP_ENTRY_CUTOFF_IST", "not-a-time")
        # an unparseable value falls back to parse_hhmm's 14:15 default - it must neither raise nor block the morning
        assert calendar_gate.scalp_cutoff_reason(datetime(2026, 10, 13, 10, 0, tzinfo=IST)) is None
        assert calendar_gate.scalp_cutoff_reason(datetime(2026, 10, 13, 14, 40, tzinfo=IST)) is not None
