"""
tests/test_auto_pilot_helpers.py

100%-coverage-plan follow-up: execution/auto_pilot.py -- 19% -> incremental
progress toward 100%. This is the largest remaining coverage gap in the
service (750 statements, 611 missing before this file). Given the module's
size, this first round targets the self-contained helper functions that
don't require standing up the full cycle_runner/entry/exit orchestration
(that's _full_tick_body/_prepick/_eod_squareoff/_eod_signal_scan/the
background loops -- left for a dedicated follow-up round, see
AUDIT_REPORT.md).

Covered this round:
  - _get_lock / _get_exit_lock       per-mode threading.Lock lazy-init
  - _reconcile_due / _mark_reconciled  throttle-window bookkeeping
  - _run_coro_in_new_loop            trivial asyncio.run wrapper
  - _summarize                       cycle-result -> Telegram message text
  - _overnight_hold_enabled          gate-row toggle read + fail-safe default
  - _edis_check_enabled              same pattern, edis_morning_check_enabled
  - _needs_cnc_sell                  CNC vs INTRADAY product-type inference
  - _alert_if_open_positions_while_gate_off   throttled Telegram alert
  - _is_afterhours_window_active     midnight-spanning time-window check
  - _compute_afterhours_market_date  next-trading-date calc, weekend/holiday skip
  - _select_overnight_holds          the full eligibility/ranking/cap pipeline
    (profitability, range-position, exposure/position/single-symbol/sector caps)

Run from services/real-trade-service:
    python3 -m pytest tests/test_auto_pilot_helpers.py -q \
        --cov=execution.auto_pilot --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
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


def _position(**kw):
    defaults = dict(
        mode="REAL", symbol="TESTCO", status="OPEN", qty_open=10,
        avg_entry_price=100.0, opened_at=datetime.now(timezone.utc),
        realized_pnl=0.0,
    )
    defaults.update(kw)
    return models.TradePosition(**defaults)


class _FakeTick:
    def __init__(self, price, day_high=None, day_low=None):
        self.price = price
        self.day_high = day_high
        self.day_low = day_low


# ---------------------------------------------------------------------------
# _get_lock / _get_exit_lock
# ---------------------------------------------------------------------------

class TestLocks:
    def test_get_lock_creates_and_reuses_same_object_per_mode(self):
        l1 = ap._get_lock("DEMO")
        l2 = ap._get_lock("DEMO")
        assert l1 is l2
        assert isinstance(l1, type(threading.Lock()))

    def test_get_lock_different_modes_get_different_locks(self):
        assert ap._get_lock("REAL") is not ap._get_lock("DEMO")

    def test_get_exit_lock_creates_and_reuses_same_object_per_mode(self):
        l1 = ap._get_exit_lock("DEMO")
        l2 = ap._get_exit_lock("DEMO")
        assert l1 is l2

    def test_entry_lock_and_exit_lock_are_independent(self):
        assert ap._get_lock("REAL") is not ap._get_exit_lock("REAL")


# ---------------------------------------------------------------------------
# _reconcile_due / _mark_reconciled
# ---------------------------------------------------------------------------

class TestReconcileDue:
    def test_due_when_never_reconciled(self, monkeypatch):
        monkeypatch.setattr(ap, "_last_real_reconcile_at", {})
        assert ap._reconcile_due("REAL") is True

    def test_not_due_immediately_after_mark(self, monkeypatch):
        monkeypatch.setattr(ap, "_last_real_reconcile_at", {})
        ap._mark_reconciled("REAL")
        assert ap._reconcile_due("REAL") is False

    def test_due_again_after_interval_elapses(self, monkeypatch):
        past = datetime.now(timezone.utc) - timedelta(
            seconds=ap.REAL_RECONCILE_MIN_INTERVAL_SECONDS + 5
        )
        monkeypatch.setattr(ap, "_last_real_reconcile_at", {"REAL": past})
        assert ap._reconcile_due("REAL") is True

    def test_mark_reconciled_sets_a_fresh_timestamp(self, monkeypatch):
        store = {}
        monkeypatch.setattr(ap, "_last_real_reconcile_at", store)
        ap._mark_reconciled("DEMO")
        assert "DEMO" in store
        assert (datetime.now(timezone.utc) - store["DEMO"]) < timedelta(seconds=2)


# ---------------------------------------------------------------------------
# _run_coro_in_new_loop
# ---------------------------------------------------------------------------

class TestRunCoroInNewLoop:
    def test_runs_coro_to_completion_on_its_own_loop(self):
        results = []

        async def _coro(x, y):
            results.append(x + y)

        ap._run_coro_in_new_loop(_coro, 2, 3)
        assert results == [5]


# ---------------------------------------------------------------------------
# _summarize
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_no_activity_message(self):
        text, activity = ap._summarize("DEMO", {})
        assert activity is False
        assert "Nothing actionable this cycle." in text
        assert "Auto-Pilot — DEMO" in text

    def test_entries_and_fills_marked_as_activity(self):
        result = {"entry": {"entered": 2, "rejected": 1}, "fills": 3, "new_candidates": 5}
        text, activity = ap._summarize("REAL", result)
        assert activity is True
        assert "Candidates: 5 new" in text
        assert "Entries sent: 2 (1 risk-rejected)" in text
        assert "Fills: 3" in text
        assert "Nothing actionable" not in text

    def test_exit_activity_line_included_when_present(self):
        result = {"exit": {"partial_exits": 1, "full_exits": 2, "time_stops": 1,
                            "trailed": 4, "emergency_exits": 0}}
        text, activity = ap._summarize("REAL", result)
        assert activity is True
        assert "partial: 1, full: 2, time-stop: 1, trailed: 4, emergency: 0" in text

    def test_emergency_exits_alone_counts_as_activity(self):
        result = {"exit": {"emergency_exits": 1}}
        _, activity = ap._summarize("REAL", result)
        assert activity is True

    def test_market_regime_line_included_when_score_present(self):
        result = {"entry": {"regime": {"score": 62, "gate": True, "source": "live"}}}
        text, _ = ap._summarize("REAL", result)
        assert "Market score: 62 (gate=True,live)" in text

    def test_market_regime_line_omitted_when_no_score(self):
        result = {"entry": {"regime": {}}}
        text, _ = ap._summarize("REAL", result)
        assert "Market score" not in text


# ---------------------------------------------------------------------------
# _overnight_hold_enabled / _edis_check_enabled
# ---------------------------------------------------------------------------

class TestOvernightHoldEnabled:
    def test_reads_true_from_gate_row(self, db):
        db.add(models.TradeGateState(mode="REAL", overnight_hold_enabled=True))
        db.commit()
        assert ap._overnight_hold_enabled(db, "REAL") is True

    def test_reads_false_from_gate_row(self, db):
        db.add(models.TradeGateState(mode="REAL", overnight_hold_enabled=False))
        db.commit()
        assert ap._overnight_hold_enabled(db, "REAL") is False

    def test_falls_back_to_config_default_when_no_gate_row(self, db, monkeypatch):
        monkeypatch.setattr(config, "OVERNIGHT_HOLD_ENABLED", False)
        assert ap._overnight_hold_enabled(db, "REAL") is False

    def test_falls_back_to_config_default_on_query_exception(self, db, monkeypatch):
        monkeypatch.setattr(db, "query", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(config, "OVERNIGHT_HOLD_ENABLED", True)
        assert ap._overnight_hold_enabled(db, "REAL") is True


class TestEdisCheckEnabled:
    def test_reads_true_from_gate_row(self, db):
        db.add(models.TradeGateState(mode="REAL", edis_morning_check_enabled=True))
        db.commit()
        assert ap._edis_check_enabled(db, "REAL") is True

    def test_reads_false_from_gate_row(self, db):
        db.add(models.TradeGateState(mode="REAL", edis_morning_check_enabled=False))
        db.commit()
        assert ap._edis_check_enabled(db, "REAL") is False

    def test_falls_back_to_config_default_when_no_gate_row(self, db, monkeypatch):
        monkeypatch.setattr(config, "EDIS_MORNING_CHECK_ENABLED", False)
        assert ap._edis_check_enabled(db, "REAL") is False

    def test_falls_back_to_config_default_on_query_exception(self, db, monkeypatch):
        monkeypatch.setattr(db, "query", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(config, "EDIS_MORNING_CHECK_ENABLED", True)
        assert ap._edis_check_enabled(db, "REAL") is True


# ---------------------------------------------------------------------------
# _needs_cnc_sell
# ---------------------------------------------------------------------------

class TestNeedsCncSell:
    def test_broker_imported_always_cnc(self):
        pos = _position(broker_imported=True, entry_product_type="INTRADAY")
        assert ap._needs_cnc_sell(pos) is True

    def test_intraday_product_type_is_not_cnc(self):
        pos = _position(broker_imported=False, entry_product_type="INTRADAY")
        assert ap._needs_cnc_sell(pos) is False

    def test_mis_product_type_is_not_cnc(self):
        pos = _position(broker_imported=False, entry_product_type="MIS")
        assert ap._needs_cnc_sell(pos) is False

    def test_explicit_cnc_product_type_is_cnc(self):
        pos = _position(broker_imported=False, entry_product_type="CNC")
        assert ap._needs_cnc_sell(pos) is True

    def test_no_product_type_same_day_position_is_not_cnc(self):
        pos = _position(broker_imported=False, entry_product_type=None,
                         opened_at=datetime.now(timezone.utc))
        assert ap._needs_cnc_sell(pos) is False

    def test_no_product_type_carried_position_is_cnc(self):
        pos = _position(broker_imported=False, entry_product_type=None,
                         opened_at=datetime.now(timezone.utc) - timedelta(days=2))
        assert ap._needs_cnc_sell(pos) is True


# ---------------------------------------------------------------------------
# _alert_if_open_positions_while_gate_off
# ---------------------------------------------------------------------------

class TestAlertIfOpenPositionsWhileGateOff:
    def test_noop_when_no_open_positions(self, db, monkeypatch):
        calls = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(calls))
        run(ap._alert_if_open_positions_while_gate_off(db, "REAL"))
        assert calls == []

    def test_sends_alert_when_open_positions_and_gate_off(self, db, monkeypatch):
        db.add(_position(status="OPEN"))
        db.commit()
        monkeypatch.setattr(ap, "_gate_off_alert_last_sent", {})
        calls = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(calls))
        run(ap._alert_if_open_positions_while_gate_off(db, "REAL"))
        assert len(calls) == 1
        assert "Auto-Pilot not evaluating exits — REAL" in calls[0]
        assert "Re-authenticate and re-arm" in calls[0]

    def test_demo_mode_uses_different_hint_text(self, db, monkeypatch):
        db.add(_position(mode="DEMO", status="PARTIALLY_CLOSED"))
        db.commit()
        monkeypatch.setattr(ap, "_gate_off_alert_last_sent", {})
        calls = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(calls))
        run(ap._alert_if_open_positions_while_gate_off(db, "DEMO"))
        assert "Re-arm and re-enable Auto-Pilot" in calls[0]

    def test_throttled_within_cooldown_window(self, db, monkeypatch):
        db.add(_position(status="OPEN"))
        db.commit()
        monkeypatch.setattr(ap, "_gate_off_alert_last_sent", {"REAL": datetime.now(timezone.utc)})
        calls = []
        monkeypatch.setattr(ap, "notify_async", _async_recorder(calls))
        run(ap._alert_if_open_positions_while_gate_off(db, "REAL"))
        assert calls == []

    def test_never_raises_on_db_error(self, db, monkeypatch, caplog):
        monkeypatch.setattr(db, "query", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        with caplog.at_level(logging.ERROR, logger="real-trade-autopilot"):
            run(ap._alert_if_open_positions_while_gate_off(db, "REAL"))  # must not raise


def _async_recorder(sink: list):
    async def _fake(msg):
        sink.append(msg)
    return _fake


# ---------------------------------------------------------------------------
# _is_afterhours_window_active
# ---------------------------------------------------------------------------

class TestIsAfterhoursWindowActive:
    def test_active_late_evening(self, monkeypatch):
        import tz_utils
        from zoneinfo import ZoneInfo
        fixed = datetime(2026, 9, 23, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        monkeypatch.setattr(tz_utils, "ist_now", lambda now=None: fixed)
        # start default 15:45, end default 08:45 -- 20:00 is >= start -> active
        assert ap._is_afterhours_window_active() is True

    def test_active_early_morning_before_open(self, monkeypatch):
        import tz_utils
        from zoneinfo import ZoneInfo
        fixed = datetime(2026, 9, 23, 3, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        monkeypatch.setattr(tz_utils, "ist_now", lambda now=None: fixed)
        assert ap._is_afterhours_window_active() is True

    def test_inactive_during_market_hours(self, monkeypatch):
        import tz_utils
        from zoneinfo import ZoneInfo
        fixed = datetime(2026, 9, 23, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        monkeypatch.setattr(tz_utils, "ist_now", lambda now=None: fixed)
        assert ap._is_afterhours_window_active() is False


# ---------------------------------------------------------------------------
# _compute_afterhours_market_date
# ---------------------------------------------------------------------------

class _FixedDatetime(datetime):
    """Subclass whose .now() ignores the real wall clock, so weekend/holiday
    skip logic can be tested deterministically regardless of when this
    suite actually runs."""
    _fixed: "datetime" = None

    @classmethod
    def now(cls, tz=None):
        base = cls._fixed
        if tz is not None:
            return base.astimezone(tz)
        return base


class TestComputeAfterhoursMarketDate:
    def test_after_close_targets_next_weekday(self, monkeypatch):
        # Wed 2026-09-23, 20:00 UTC ~= Thu 01:30 IST -- doesn't matter here,
        # we control now_t directly and only need the *date* arithmetic.
        from zoneinfo import ZoneInfo
        fixed = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)  # a Wednesday in UTC
        _FixedDatetime._fixed = fixed
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        monkeypatch.setattr("tz_utils.is_nse_holiday", lambda dt: False)

        start_t = ap.parse_hhmm(config.AFTERHOURS_SCAN_START_IST, 15, 45)
        after_close = (datetime.min + timedelta(hours=20)).time()  # any time >= 15:45
        assert after_close >= start_t
        result = ap._compute_afterhours_market_date(now_t=after_close)
        expected = (fixed + timedelta(days=1)).strftime("%Y-%m-%d")
        assert result == expected

    def test_after_close_skips_weekend(self, monkeypatch):
        # Friday -> after-close target should skip to Monday, not Saturday.
        friday = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        assert friday.weekday() == 4  # sanity: Friday
        _FixedDatetime._fixed = friday
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        monkeypatch.setattr("tz_utils.is_nse_holiday", lambda dt: False)

        after_close = (datetime.min + timedelta(hours=20)).time()
        result = ap._compute_afterhours_market_date(now_t=after_close)
        monday = friday + timedelta(days=3)
        assert result == monday.strftime("%Y-%m-%d")

    def test_after_close_skips_holiday(self, monkeypatch):
        thursday = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        _FixedDatetime._fixed = thursday
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        holiday_date = (thursday + timedelta(days=1)).strftime("%Y-%m-%d")
        monkeypatch.setattr(
            "tz_utils.is_nse_holiday",
            lambda dt: dt.strftime("%Y-%m-%d") == holiday_date,
        )
        after_close = (datetime.min + timedelta(hours=20)).time()
        result = ap._compute_afterhours_market_date(now_t=after_close)
        expected = (thursday + timedelta(days=4)).strftime("%Y-%m-%d")
        assert result == expected

    def test_before_open_targets_today(self, monkeypatch):
        wednesday = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        _FixedDatetime._fixed = wednesday
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        monkeypatch.setattr("tz_utils.is_nse_holiday", lambda dt: False)

        before_open = (datetime.min + timedelta(hours=3)).time()  # < 08:45
        result = ap._compute_afterhours_market_date(now_t=before_open)
        assert result == wednesday.strftime("%Y-%m-%d")

    def test_before_open_on_weekend_skips_to_next_weekday(self, monkeypatch):
        saturday = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        assert saturday.weekday() == 5
        _FixedDatetime._fixed = saturday
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        monkeypatch.setattr("tz_utils.is_nse_holiday", lambda dt: False)

        before_open = (datetime.min + timedelta(hours=3)).time()
        result = ap._compute_afterhours_market_date(now_t=before_open)
        monday = saturday + timedelta(days=2)
        assert result == monday.strftime("%Y-%m-%d")

    def test_defaults_now_t_when_not_passed(self, monkeypatch):
        """now_t=None must compute it fresh via tz_utils.ist_now() rather
        than raising -- covers the default-parameter branch."""
        import tz_utils
        wednesday = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        _FixedDatetime._fixed = wednesday
        monkeypatch.setattr(ap, "datetime", _FixedDatetime)
        monkeypatch.setattr(tz_utils, "ist_now", lambda now=None: wednesday)
        monkeypatch.setattr("tz_utils.is_nse_holiday", lambda dt: False)
        result = ap._compute_afterhours_market_date()  # no now_t passed
        assert isinstance(result, str)
        assert len(result) == 10  # YYYY-MM-DD


# ---------------------------------------------------------------------------
# _select_overnight_holds
# ---------------------------------------------------------------------------

def _pin_overnight_config(monkeypatch, **overrides):
    values = dict(
        OVERNIGHT_HOLD_ELIGIBLE_LABELS={"HIGH_CONVICTION", "UPPER_CIRCUIT"},
        OVERNIGHT_HOLD_REQUIRE_PROFITABLE=True,
        OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS=False,  # simple ltp>=entry check for this round
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


class TestSelectOvernightHolds:
    def test_returns_empty_when_disabled(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_ENABLED=False)
        pos = _position(entry_decision_label="HIGH_CONVICTION")
        result = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert result == (set(), {})

    def test_returns_empty_when_no_positions(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        result = run(ap._select_overnight_holds(db, "REAL", []))
        assert result == (set(), {})

    def test_returns_empty_when_no_position_matches_eligible_label(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="VOLUME_SHOCK")  # not eligible
        result = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert result == (set(), {})

    def test_excludes_position_with_no_live_tick(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="NOQUOTE")
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return({}))
        keep, reasons = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_excludes_position_below_entry_price_when_profitability_required(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="LOSER", avg_entry_price=100.0)
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"LOSER": _FakeTick(price=95.0, day_high=100, day_low=90)}),
        )
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_excludes_position_missing_day_range_data(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="NORANGE", avg_entry_price=100.0)
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"NORANGE": _FakeTick(price=105.0, day_high=None, day_low=None)}),
        )
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_excludes_position_extended_near_day_high(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="EXTENDED", avg_entry_price=100.0)
        # range_pos = (109-100)/(110-100) = 0.9 >= 0.80 cap -> excluded
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"EXTENDED": _FakeTick(price=109.0, day_high=110.0, day_low=100.0)}),
        )
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=100000))
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_keeps_a_single_eligible_position(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="GOODONE",
                         avg_entry_price=100.0, qty_open=10, entry_conviction_score=70.0)
        db.add(pos)
        db.commit()
        # range_pos = (105-100)/(120-100) = 0.25 -- well clear of the 0.80 cap
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"GOODONE": _FakeTick(price=105.0, day_high=120.0, day_low=100.0)}),
        )
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=1000000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})
        keep, reasons = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == {pos.id}
        assert "overnight hold: HIGH_CONVICTION" in reasons[pos.id]

    def test_max_positions_cap_enforced(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_MAX_POSITIONS=1)
        positions = []
        quotes = {}
        for i, sym in enumerate(["AAA", "BBB"]):
            p = _position(entry_decision_label="HIGH_CONVICTION", symbol=sym,
                           avg_entry_price=100.0, qty_open=1,
                           entry_conviction_score=90.0 - i * 10)  # AAA ranks higher
            db.add(p)
            positions.append(p)
            quotes[sym] = _FakeTick(price=105.0, day_high=120.0, day_low=100.0)
        db.commit()
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return(quotes))
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=10_000_000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})
        keep, _ = run(ap._select_overnight_holds(db, "REAL", positions))
        assert len(keep) == 1
        assert positions[0].id in keep  # higher conviction wins the single slot

    def test_single_symbol_exposure_cap_excludes_oversized_position(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT=1.0)
        pos = _position(entry_decision_label="HIGH_CONVICTION", symbol="HUGE",
                         avg_entry_price=100.0, qty_open=1000, entry_conviction_score=80.0)
        db.add(pos)
        db.commit()
        monkeypatch.setattr(
            "market_feed.feed.get_quotes",
            _async_return({"HUGE": _FakeTick(price=105.0, day_high=120.0, day_low=100.0)}),
        )
        # equity small enough that 1% cap << position_value (105 * 1000)
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=100000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})
        keep, _ = run(ap._select_overnight_holds(db, "REAL", [pos]))
        assert keep == set()

    def test_aggregate_exposure_cap_stops_accepting_further_positions(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_MAX_EXPOSURE_PCT=1.0,
                               OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT=100.0,
                               OVERNIGHT_HOLD_MAX_POSITIONS=5)
        positions = []
        quotes = {}
        for i, sym in enumerate(["FIRST", "SECOND"]):
            p = _position(entry_decision_label="HIGH_CONVICTION", symbol=sym,
                           avg_entry_price=100.0, qty_open=50,
                           entry_conviction_score=90.0 - i * 10)
            db.add(p)
            positions.append(p)
            quotes[sym] = _FakeTick(price=105.0, day_high=120.0, day_low=100.0)
        db.commit()
        # equity=10,000 -> cap = 1% = 100 rupees; one position (105*50=5250) already blows it
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return(quotes))
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=10_000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})
        keep, _ = run(ap._select_overnight_holds(db, "REAL", positions))
        assert keep == set()

    def test_sector_cap_blocks_second_position_in_same_sector(self, db, monkeypatch):
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_MAX_PER_SECTOR=1,
                               OVERNIGHT_HOLD_MAX_POSITIONS=5)
        positions = []
        quotes = {}
        for i, sym in enumerate(["BANKA", "BANKB"]):
            p = _position(entry_decision_label="HIGH_CONVICTION", symbol=sym,
                           avg_entry_price=100.0, qty_open=1,
                           entry_conviction_score=90.0 - i * 10)
            db.add(p)
            positions.append(p)
            quotes[sym] = _FakeTick(price=105.0, day_high=120.0, day_low=100.0)
        db.commit()
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return(quotes))
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=10_000_000))
        monkeypatch.setattr(
            "market_context.sector_signal.NSE_SECTOR_MAP",
            {"BANKA": "BANKING", "BANKB": "BANKING"},
        )
        keep, reasons = run(ap._select_overnight_holds(db, "REAL", positions))
        assert len(keep) == 1
        assert positions[0].id in keep  # higher-conviction BANKA takes the sector's single slot
        assert "sector=BANKING" in reasons[positions[0].id]

    def test_unmapped_sector_symbols_never_compete_against_each_other(self, db, monkeypatch):
        """A symbol absent from NSE_SECTOR_MAP must never be blocked by the
        per-sector cap -- each unmapped symbol gets its own private slot."""
        _pin_overnight_config(monkeypatch, OVERNIGHT_HOLD_MAX_PER_SECTOR=1,
                               OVERNIGHT_HOLD_MAX_POSITIONS=5)
        positions = []
        quotes = {}
        for i, sym in enumerate(["OBSCURE1", "OBSCURE2"]):
            p = _position(entry_decision_label="HIGH_CONVICTION", symbol=sym,
                           avg_entry_price=100.0, qty_open=1,
                           entry_conviction_score=90.0 - i * 10)
            db.add(p)
            positions.append(p)
            quotes[sym] = _FakeTick(price=105.0, day_high=120.0, day_low=100.0)
        db.commit()
        monkeypatch.setattr("market_feed.feed.get_quotes", _async_return(quotes))
        monkeypatch.setattr("portfolio.portfolio.get_account", lambda db, mode: _account(equity=10_000_000))
        monkeypatch.setattr("market_context.sector_signal.NSE_SECTOR_MAP", {})  # neither mapped
        keep, _ = run(ap._select_overnight_holds(db, "REAL", positions))
        assert len(keep) == 2


def _async_return(value):
    async def _fake(*a, **kw):
        return value
    return _fake


class _account:
    def __init__(self, equity):
        self.current_equity = equity
