"""Phase 1 (100%-coverage plan, session83): targeted tests for every remaining
missed line in exit_engine/exit.py after the clamp-for-atr session.

Missed-line groups this file covers (confirmed via --cov-report=annotate):

  131           _trail_atr_mult: fallthrough past all schedule entries
  165-166       _load_profile: volume_shock → short-horizon profile
  176-182       _load_profile: watchlist_entry_id db path + exception fallback
  577           _send_real_sell: LIMIT order → round_to_tick branch
  688-689       CDSL fromisoformat exception → due=True → alert fires
  706           CDSL within-cooldown → logger.info silent
  724-729       insufficient-funds fromisoformat exception
  745           insufficient-funds within-cooldown silent
  839-840       oversell sync-fail fromisoformat exception
  861-868       exchange-not-allowed fromisoformat exception
  880           exchange-not-allowed within-cooldown silent
  901-906       intraday-cutoff fromisoformat exception
  934           intraday-cutoff within-cooldown silent
  975-980       security-intraday-restricted fromisoformat exception
  1001          security-intraday-restricted within-cooldown silent
  1038-1060     circuit-limit full path (save + alert + cooldown)
  1091-1092     generic-streak fromisoformat exception
  1197-1198     evaluate_mode: pstat.set_symbol_progress exception swallowed
  1300-1303     evaluate_mode: REAL emergency_gap → _send_real_sell False → held
  1371-1398     evaluate_mode: REAL target_hit_partial, both branches
  1435-1438     evaluate_mode: REAL time_stop, both branches
  1474-1482     evaluate_mode: breakeven range_pos ≥0.80 and ≥0.65 branches
  1527-1533     evaluate_mode: trail range_pos ≥0.80 and ≥0.65 branches
  1541-1548     evaluate_mode: ATR clamped → HOLD written + continue
  1579-1596     evaluate_mode: per-position exception → rollback + HOLD logged

Run from services/real-trade-service:
    python -m pytest tests/test_exit_remaining_coverage.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import exit_engine.exit as ex
from execution import dhan_client, shared_order_budget
from market_feed.feed import Tick
from resilience.local_cache import load_snapshot, save_snapshot
from tz_utils import ist_today_str

# ── shared DB engine ─────────────────────────────────────────────────────────

_engine = create_engine("sqlite:///:memory:")


def _fresh_db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    return s


def _mkpos(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTCO", status="OPEN", qty_open=10,
        avg_entry_price=100.0, current_stop=95.0, current_target=115.0,
        opened_at=datetime.now(timezone.utc), broker_imported=True,
        initial_stop_distance=5.0,
    )
    defaults.update(kw)
    pos = models.TradePosition(**defaults)
    db.add(pos); db.commit(); db.refresh(pos)
    return pos


def _demo_account(db):
    db.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0,
                               current_equity=100_000.0, cash_available=100_000.0))
    db.commit()


def _tick(price, atr=None, day_high=None, day_low=None, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc),
                atr=atr, source="test", day_high=day_high, day_low=day_low)


# ── async expire stub (expire_stale_exit_orders is async) ───────────────────

async def _noop_expire(*a, **k):
    return 0


# ── timestamp helpers ─────────────────────────────────────────────────────────

def _recent_ts():
    """Within any cooldown window."""
    return datetime.now(timezone.utc).isoformat()


def _bad_ts():
    """Guaranteed to make fromisoformat raise."""
    return "NOT_A_VALID_ISO_DATE"


# ── _send_real_sell direct call helper ───────────────────────────────────────

def _direct_sell(db, pos, order_type=None, limit_price=None):
    """Call _send_real_sell synchronously (it is not async)."""
    return ex._send_real_sell(db, pos, 10, "stop_hit",
                              order_type=order_type, limit_price=limit_price)


# ════════════════════════════════════════════════════════════════════════════════
# _trail_atr_mult: fallthrough past all schedule entries (line 131)
# ════════════════════════════════════════════════════════════════════════════════

class TestTrailAtrMultFallthrough:
    def test_held_days_beyond_all_entries_returns_last_mult(self):
        # TRAIL_ATR_SCHEDULE ends at (99, 0.9); 200 days must fall through to s[-1][1]
        assert ex._trail_atr_mult(200) == ex.TRAIL_ATR_SCHEDULE[-1][1]

    def test_custom_schedule_fallthrough(self):
        schedule = [(1, 2.0), (3, 1.5)]
        assert ex._trail_atr_mult(4, schedule=schedule) == 1.5

    def test_exactly_at_boundary_does_not_fallthrough(self):
        # held_days == 99 satisfies the last entry's ≤ 99 condition
        assert ex._trail_atr_mult(99) == ex.TRAIL_ATR_SCHEDULE[-1][1]


# ════════════════════════════════════════════════════════════════════════════════
# _load_profile (lines 165-166, 176-182)
# ════════════════════════════════════════════════════════════════════════════════

class TestLoadProfile:
    def test_volume_shock_source_tab_gets_short_horizon_profile(self):
        # lines 165-166
        db = _fresh_db()
        pos = _mkpos(db, source_tab="volume_shock")
        from watchlist_engine.decay import exit_profile_for
        expected = exit_profile_for("short")
        profile = ex._load_profile(db, pos)
        assert profile["horizon_class"] == "short"
        assert profile["max_hold_days"] == expected["max_hold_days"]

    def test_no_watchlist_entry_returns_global_defaults(self):
        db = _fresh_db()
        pos = _mkpos(db)   # watchlist_entry_id=None
        profile = ex._load_profile(db, pos)
        assert profile["horizon_class"] is None
        assert profile["max_hold_days"] == ex.MAX_HOLD_DAYS
        assert profile["trail_atr_schedule"] == ex.TRAIL_ATR_SCHEDULE

    def test_watchlist_entry_id_found_returns_horizon_class(self):
        # line 176: db.query(WatchlistEntry).get(...)
        db = _fresh_db()
        entry = models.WatchlistEntry(
            mode="REAL", symbol="TESTCO",
            catalyst_type="breakout", horizon_class="medium",
            decay_half_life_days=5, entry_band_pct=2.0,
            source_tier="A",
            expires_at=datetime.now(timezone.utc),
        )
        db.add(entry); db.commit(); db.refresh(entry)
        pos = _mkpos(db, watchlist_entry_id=entry.id)
        profile = ex._load_profile(db, pos)
        assert profile["horizon_class"] == "medium"

    def test_watchlist_entry_id_missing_row_returns_none_horizon(self):
        db = _fresh_db()
        pos = _mkpos(db, watchlist_entry_id=99999)
        profile = ex._load_profile(db, pos)
        assert profile["horizon_class"] is None

    def test_watchlist_entry_db_exception_falls_back(self):
        # lines 179-180: exception in the try block → horizon_class stays None
        db = _fresh_db()
        pos = _mkpos(db, watchlist_entry_id=1)
        orig_query = db.query

        def _boom(model):
            if model is models.WatchlistEntry:
                raise RuntimeError("DB exploded")
            return orig_query(model)

        with mock.patch.object(db, "query", side_effect=_boom):
            profile = ex._load_profile(db, pos)
        assert profile["horizon_class"] is None


# ════════════════════════════════════════════════════════════════════════════════
# _send_real_sell: LIMIT order → round_to_tick (line 577)
# ════════════════════════════════════════════════════════════════════════════════

class TestSendRealSellLimitOrder:
    def test_limit_order_calls_round_to_tick(self):
        db = _fresh_db()
        pos = _mkpos(db, broker_imported=False, entry_product_type="CNC")
        rounded = {}

        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick",
                               side_effect=lambda p: rounded.update(p=p) or round(p, 2)), \
             mock.patch.object(dhan_client, "place_order", return_value={"orderId": "L1"}), \
             mock.patch.object(shared_order_budget, "record_order_unconditional"), \
             mock.patch.object(ex, "notify_sync"):
            result = _direct_sell(db, pos, order_type="LIMIT", limit_price=102.5)

        assert result is True
        assert rounded.get("p") == 102.5


# ════════════════════════════════════════════════════════════════════════════════
# _send_real_sell: cooldown-exception branches
# Pattern: save a snapshot with a broken 'at' → fromisoformat raises → due=True → alert fires
#          OR save a valid very-recent 'at' → elapsed < cooldown → due=False → silent
# ════════════════════════════════════════════════════════════════════════════════

class TestCooldownExceptionBranches:
    """
    All six error classifiers share the same cooldown-fromisoformat pattern.
    We test:
      (a) bad timestamp → parse error → due=True  → alert IS sent
      (b) recent valid ts → due=False → alert suppressed (logger.info only)
    """

    # ── CDSL (lines 688-689, 706) ─────────────────────────────────────────────

    def _cdsl_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("Dhan API error: Validate Qty from CDSL"))

    def test_cdsl_fromisoformat_exception_still_alerts(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"cdsl_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._cdsl_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("CDSL" in a or "cdsl" in a.lower() for a in alerts)

    def test_cdsl_within_cooldown_silent(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"cdsl_alert_last_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._cdsl_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    # ── insufficient-funds (lines 724-729, 745) ──────────────────────────────

    def _funds_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("insufficient funds please add rs.500"))

    def test_insufficient_funds_fromisoformat_exception_still_alerts(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"funds_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._funds_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("funds" in a.lower() or "EXIT BLOCKED" in a for a in alerts)

    def test_insufficient_funds_within_cooldown_silent(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"funds_alert_last_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._funds_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    # ── oversell sync-fail (lines 839-840) ──────────────────────────────────
    # oversell triggers get_holdings; if that also fails, hits sync-fail branch

    def test_oversell_syncfail_fromisoformat_exception(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"oversell_sync_fail_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             mock.patch.object(dhan_client, "place_order",
                               side_effect=RuntimeError("trying to sell more than the quantity you currently hold")), \
             mock.patch.object(dhan_client, "get_holdings",
                               side_effect=RuntimeError("holdings down")), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("sync" in a.lower() or "Qty" in a or "qty" in a.lower() for a in alerts)

    # ── exchange-not-allowed (lines 861-868, 880) ─────────────────────────────

    def _exch_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("EXCH:16387 security is not allowed to trade"))

    def test_exchange_not_allowed_fromisoformat_exception(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"exch_not_allowed_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._exch_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("T+1" in a or "EXIT BLOCKED" in a for a in alerts)

    def test_exchange_not_allowed_within_cooldown_silent(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"exch_not_allowed_alert_last_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._exch_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    # ── intraday-cutoff (lines 901-906, 934) ─────────────────────────────────

    def _cutoff_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("intraday orders cannot be placed at this time"))

    def test_intraday_cutoff_fromisoformat_exception(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"intraday_cutoff_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._cutoff_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("EXIT BLOCKED" in a or "cutoff" in a.lower() or "intraday" in a.lower()
                   for a in alerts)

    def test_intraday_cutoff_within_cooldown_silent(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"intraday_cutoff_alert_last_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._cutoff_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    # ── security-intraday-restricted (lines 975-980, 1001) ───────────────────

    def _restricted_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("not allowed to be traded in intraday"))

    def test_security_intraday_restricted_fromisoformat_exception(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"intraday_restricted_alert_last_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._restricted_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("EXIT BLOCKED" in a or "ntraday" in a for a in alerts)

    def test_security_intraday_restricted_within_cooldown_silent(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"intraday_restricted_alert_last_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._restricted_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    # ── generic-streak fromisoformat exception (lines 1091-1092) ─────────────

    def test_generic_streak_fromisoformat_exception_sets_due_true(self):
        db = _fresh_db()
        pos = _mkpos(db)
        # Seed streak snap with a broken last_alert_at
        save_snapshot(db, f"exit_reject_streak_{pos.id}",
                      {"count": 0, "last_alert_at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             mock.patch.object(dhan_client, "place_order",
                               side_effect=RuntimeError("unrecognised broker error XYZZY999")), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert len(alerts) >= 1


# ════════════════════════════════════════════════════════════════════════════════
# _send_real_sell: circuit-limit full path (lines 1038-1060)
# ════════════════════════════════════════════════════════════════════════════════

class TestCircuitLimitBranch:
    def _circuit_boom(self):
        return mock.patch.object(dhan_client, "place_order",
                                 side_effect=RuntimeError("Rate Not Within Ckt Limit 100 To 120"))

    def test_circuit_saves_cutoff_key_and_alerts_on_first_hit(self):
        db = _fresh_db()
        pos = _mkpos(db)
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._circuit_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        cutoff = load_snapshot(db, f"intraday_cutoff_hit_{pos.id}_{ist_today_str()}")
        assert cutoff and cutoff.get("hit")
        assert any("CIRCUIT" in a or "circuit" in a.lower() for a in alerts)

    def test_circuit_within_cooldown_suppresses_alert(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"circuit_limit_sell_alert_{pos.id}", {"at": _recent_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._circuit_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert not alerts

    def test_circuit_fromisoformat_exception_still_alerts(self):
        db = _fresh_db()
        pos = _mkpos(db)
        save_snapshot(db, f"circuit_limit_sell_alert_{pos.id}", {"at": _bad_ts()})
        alerts = []
        with mock.patch.object(dhan_client, "get_security_id", return_value="999"), \
             mock.patch.object(dhan_client, "round_to_tick", side_effect=lambda p: p), \
             self._circuit_boom(), \
             mock.patch.object(ex, "notify_sync",
                               side_effect=lambda msg, **k: alerts.append(msg)):
            _direct_sell(db, pos)
        assert any("CIRCUIT" in a or "circuit" in a.lower() for a in alerts)


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode helpers
# ════════════════════════════════════════════════════════════════════════════════

def _run_eval(db, ticks: dict, mode: str = "REAL"):
    """Run evaluate_mode with minimal mocking - only network/broker edges."""
    async def _q(symbols): return ticks
    async def _expire(*a, **k): return 0

    with mock.patch.object(ex, "get_quotes", _q), \
         mock.patch.object(ex, "expire_stale_exit_orders", _expire), \
         mock.patch.object(ex, "notify_sync", lambda *a, **k: None):
        return asyncio.run(ex.evaluate_mode(db, mode=mode))


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: pstat exception swallowed (lines 1197-1198)
# ════════════════════════════════════════════════════════════════════════════════

class TestEvaluateModePstatError:
    def test_pstat_exception_does_not_abort_cycle(self):
        import pipeline_status as pstat
        db = _fresh_db(); _demo_account(db)
        pos = _mkpos(db, mode="DEMO")
        with mock.patch.object(pstat, "set_symbol_progress",
                               side_effect=RuntimeError("pstat broken")):
            result = _run_eval(db, {"TESTCO": _tick(98.0, atr=1.0)}, mode="DEMO")
        assert result["evaluated"] >= 1


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: REAL-mode routing (lines 1300-1303, 1371-1398, 1435-1438)
# ════════════════════════════════════════════════════════════════════════════════

class TestEvaluateModeRealRouting:

    def _gap_price(self, pos):
        return pos.avg_entry_price - ex.EMERGENCY_LOSS_MULT * pos.initial_stop_distance - 1.0

    # emergency_gap

    def test_real_emergency_gap_send_fails_counted_as_held(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True)
        ticks = {"TESTCO": _tick(self._gap_price(pos), atr=1.0)}
        with mock.patch.object(ex, "_send_real_sell", return_value=False), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["held"] >= 1
        assert result["emergency_exits"] == 0

    def test_real_emergency_gap_send_succeeds(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True)
        ticks = {"TESTCO": _tick(self._gap_price(pos), atr=1.0)}
        with mock.patch.object(ex, "_send_real_sell", return_value=True), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["emergency_exits"] == 1

    # target_hit_partial

    def test_real_target_hit_partial_send_succeeds(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True,
                     avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
                     qty_open=10, initial_stop_distance=5.0)
        ticks = {"TESTCO": _tick(112.0, atr=1.5)}
        sent = {}
        with mock.patch.object(ex, "_send_real_sell",
                               side_effect=lambda *a, **k: sent.update(called=True) or True), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["partial_exits"] == 1
        assert sent.get("called")

    def test_real_target_hit_partial_send_fails_held(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True,
                     avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
                     qty_open=10, initial_stop_distance=5.0)
        ticks = {"TESTCO": _tick(112.0, atr=1.5)}
        with mock.patch.object(ex, "_send_real_sell", return_value=False), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["partial_exits"] == 0
        assert result["held"] >= 1

    # time_stop

    def test_real_time_stop_send_succeeds(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True,
                     avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
                     qty_open=10, initial_stop_distance=5.0,
                     opened_at=datetime.now(timezone.utc) - timedelta(days=15))
        ticks = {"TESTCO": _tick(102.0, atr=1.0)}
        with mock.patch.object(ex, "_send_real_sell", return_value=True), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["time_stops"] == 1

    def test_real_time_stop_send_fails_held(self):
        db = _fresh_db()
        pos = _mkpos(db, mode="REAL", broker_imported=True,
                     avg_entry_price=100.0, current_stop=95.0, current_target=110.0,
                     qty_open=10, initial_stop_distance=5.0,
                     opened_at=datetime.now(timezone.utc) - timedelta(days=15))
        ticks = {"TESTCO": _tick(102.0, atr=1.0)}
        with mock.patch.object(ex, "_send_real_sell", return_value=False), \
             mock.patch.object(ex, "_has_pending_real_sell", return_value=False):
            result = _run_eval(db, ticks, mode="REAL")
        assert result["time_stops"] == 0
        assert result["held"] >= 1


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: breakeven range_pos branches (lines 1474-1482)
# ════════════════════════════════════════════════════════════════════════════════

class TestBreakevenRangePos:
    """
    range_pos = (ltp - day_low) / (day_high - day_low)
    ≥ 0.80 → _be_mult = 0.60  (very tight breakeven trigger)
    ≥ 0.65 → _be_mult = 0.80  (moderate tightening)

    To force breakeven: gain/atr must exceed _be_mult * BREAKEVEN_ATR_TRIGGER.
    We use a high unrealized gain relative to ATR to ensure this.
    """

    def _run(self, db, *, price, atr, day_high, day_low,
             entry=100.0, stop=90.0, target=150.0):
        _demo_account(db)
        pos = _mkpos(db, mode="DEMO", avg_entry_price=entry, current_stop=stop,
                     current_target=target, initial_stop_distance=entry - stop)
        result = _run_eval(db, {"TESTCO": _tick(price, atr=atr,
                                                 day_high=day_high, day_low=day_low)},
                           mode="DEMO")
        db.refresh(pos)
        return result, pos

    def test_range_pos_above_80_accelerates_breakeven(self):
        # ltp=108, day_low=100, day_high=110 → range_pos=0.80 → _be_mult=0.60
        # gain=8, atr=2 → gain/atr=4.0 ≥ 0.60 * BREAKEVEN_ATR_TRIGGER (default 1.0)
        db = _fresh_db()
        result, pos = self._run(db, price=108.0, atr=2.0,
                                day_high=110.0, day_low=100.0,
                                entry=100.0, stop=90.0)
        # breakeven should have moved stop to ≥ entry
        assert pos.current_stop >= 100.0 or result["trailed"] >= 1

    def test_range_pos_above_65_below_80_moderate(self):
        # ltp=107, day_low=100, day_high=110 → range_pos=0.70 → _be_mult=0.80
        db = _fresh_db()
        result, pos = self._run(db, price=107.0, atr=2.0,
                                day_high=110.0, day_low=100.0,
                                entry=100.0, stop=90.0)
        assert pos.current_stop >= 100.0 or result["trailed"] >= 1


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: trail range_pos tightening (lines 1527-1533)
# ════════════════════════════════════════════════════════════════════════════════

class TestTrailRangePos:
    """
    range_pos ≥ 0.80 → trail_mult *= 0.70  (tighter trail, higher stop)
    range_pos ≥ 0.65 → trail_mult *= 0.85

    Key: set current_stop == avg_entry_price so the breakeven guard
    (current_stop < be_level → False) is already satisfied and execution
    falls through to the ATR trail block where range_pos tightening happens.
    """

    def _run(self, db, *, price, atr, day_high, day_low, entry=100.0):
        _demo_account(db)
        # current_stop == entry → breakeven guard skipped → falls to trail block
        pos = _mkpos(db, mode="DEMO", avg_entry_price=entry, current_stop=entry,
                     current_target=None, initial_stop_distance=50.0)
        result = _run_eval(db, {"TESTCO": _tick(price, atr=atr,
                                                 day_high=day_high, day_low=day_low)},
                           mode="DEMO")
        db.refresh(pos)
        return result, pos

    def test_range_pos_above_80_tightens_trail(self):
        # ltp=108, day_low=100, day_high=110 → range_pos=0.80 → trail_mult*=0.70
        db = _fresh_db()
        result, pos = self._run(db, price=108.0, atr=2.0,
                                day_high=110.0, day_low=100.0)
        assert result["trailed"] >= 1
        assert pos.current_stop > 100.0   # stop trailed upward

    def test_range_pos_above_65_below_80(self):
        # ltp=107, day_low=100, day_high=110 → range_pos=0.70 → trail_mult*=0.85
        db = _fresh_db()
        result, pos = self._run(db, price=107.0, atr=2.0,
                                day_high=110.0, day_low=100.0)
        assert result["trailed"] >= 1
        assert pos.current_stop > 100.0


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: ATR clamped → HOLD (lines 1541-1548)
# ════════════════════════════════════════════════════════════════════════════════

class TestAtrClampedHold:
    def test_clamped_atr_writes_hold_decision_and_does_not_trail(self):
        # raw_atr_pct = atr/ltp*100. Default CORPORATE_ACTION_JUMP_THRESHOLD is 30%.
        # atr=40, ltp=110 → raw_atr_pct ≈ 36.4% > 30 → real clamp_for_atr returns None.
        # This hits lines 1541-1548: atr_pct is None → write HOLD + held += 1 + continue.
        db = _fresh_db(); _demo_account(db)
        pos = _mkpos(db, mode="DEMO", avg_entry_price=100.0, current_stop=90.0,
                     current_target=None, initial_stop_distance=10.0)
        result = _run_eval(db, {"TESTCO": _tick(110.0, atr=40.0)}, mode="DEMO")
        assert result["trailed"] == 0
        assert result["held"] >= 1
        decisions = db.query(models.TradeExitDecision).all()
        assert any(
            "clamp" in (d.reasoning or "").lower() or "ATR" in (d.reasoning or "")
            for d in decisions
        )


# ════════════════════════════════════════════════════════════════════════════════
# evaluate_mode: per-position exception → rollback + HOLD (lines 1579-1596)
# ════════════════════════════════════════════════════════════════════════════════

class TestEvaluateModePerPositionException:
    def test_exception_rolls_back_and_cycle_continues_for_remaining(self):
        db = _fresh_db(); _demo_account(db)
        crash = _mkpos(db, mode="DEMO", symbol="CRASH",
                       avg_entry_price=100.0, current_stop=95.0,
                       current_target=110.0, initial_stop_distance=5.0)
        ok    = _mkpos(db, mode="DEMO", symbol="OK",
                       avg_entry_price=100.0, current_stop=95.0,
                       current_target=110.0, initial_stop_distance=5.0)

        ticks = {
            "CRASH": _tick(98.0, atr=1.0, symbol="CRASH"),
            "OK":    _tick(98.0, atr=1.0, symbol="OK"),
        }
        original_write = ex._write_exit_decision
        call_n = {"n": 0}

        def _write_or_crash(db_, position, action, reasoning, ltp):
            call_n["n"] += 1
            if position.symbol == "CRASH" and call_n["n"] == 1:
                raise RuntimeError("simulated per-position crash")
            return original_write(db_, position, action, reasoning, ltp)

        with mock.patch.object(ex, "_write_exit_decision", _write_or_crash):
            result = _run_eval(db, ticks, mode="DEMO")

        # Must not raise; both positions processed; CRASH lands in held
        assert result["evaluated"] == 2
        assert result["held"] >= 1
