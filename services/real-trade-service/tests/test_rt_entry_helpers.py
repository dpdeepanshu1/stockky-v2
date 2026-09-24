"""
tests/test_rt_entry_helpers.py — offline tests for the pricing / sizing / gating helpers
and the order-lifecycle functions in entry_engine/entry.py (real-trade-service).

NOT covered here: evaluate_mode() itself (the ~830-line candidate-to-order pipeline) — see
AUDIT_REPORT.md. What IS covered is every number that decides an order's stop, target, size,
drift tolerance and ranking, plus the functions that turn stale entry orders into EXPIRED
(which for REAL money means cancelling at Dhan first).

Sections:
  TestAtrStopTarget / TestRangeAdjusted / TestRewardRisk / TestConviction / TestDrift / TestComposite
  TestCostGateKnobs      admin-editable cost-gate overrides
  TestMarketRegime       regime fetch, fail-open, cache
  TestAccountState       AccountState assembled for the risk engine
  TestCheckPendingFills  DEMO simulated fills
  TestExpireStaleOrders  no-chase expiry (REAL cancels at Dhan first)
  TestKnownGaps          strict xfail — needs a design decision

Run from services/real-trade-service:
    python3 -m pytest tests/test_rt_entry_helpers.py -q --cov=entry_engine --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import time as _time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry
from execution import dhan_client, equity_sync, shared_symbol_lock
from market_feed.feed import Tick
from tz_utils import ist_today_str


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    for k, v in dict(ENTRY_MIN_REWARD_RISK=2.0, ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65,
                     ENTRY_COMPOSITE_WEIGHT_RR=0.10, ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25,
                     ENTRY_COMPOSITE_RR_CEILING=4.0, MIN_TRADE_VALUE=3000.0,
                     MIN_EDGE_TO_COST_RATIO=3.0, API_GATEWAY_URL="http://gw").items():
        monkeypatch.setattr(config, k, v)
    for k, v in dict(MIN_REWARD_RISK_RATIO=2.0, MAX_ENTRY_DRIFT_ATR=0.75, CONVICTION_MIDPOINT=65.0,
                     CONVICTION_MAX_SCALE=0.25, REGIME_MIN_SCORE_STATIC=38).items():
        monkeypatch.setattr(entry, k, v)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def tick(day_high=None, day_low=None):
    return types.SimpleNamespace(day_high=day_high, day_low=day_low)


# ── ATR stop / target ────────────────────────────────────────────────────────
class TestAtrStopTarget:
    @pytest.mark.parametrize("atr", [None, 0, 0.0, -1.5])
    def test_missing_or_bad_atr_gives_the_flat_fallback(self, atr):
        assert entry._atr_stop_target_pct(atr) == (3.2, 6.5)

    @pytest.mark.parametrize("atr,stop,target", [
        (0.5, 2.0, 4.0),        # floor: 1.5*0.5 = 0.75 -> 2.0
        (1.333, 2.0, 4.0),      # just under the floor
        (2.0, 3.0, 6.0),
        (3.0, 4.5, 9.0),
        (4.0, 6.0, 12.0),       # ceiling reached exactly
        (12.0, 6.0, 12.0),      # ceiling: 18 -> 6.0
    ])
    def test_stop_is_atr_times_1_5_clamped_and_target_is_2x_stop(self, atr, stop, target):
        assert entry._atr_stop_target_pct(atr) == (stop, target)

    def test_results_are_rounded_to_two_places(self):
        stop, target = entry._atr_stop_target_pct(2.1234)
        assert stop == round(stop, 2) and target == round(target, 2)

    @pytest.mark.parametrize("atr", [0.1, 0.9, 1.7, 2.6, 3.3, 5.0, 9.9])
    def test_reward_risk_is_always_exactly_two(self, atr):
        stop, target = entry._atr_stop_target_pct(atr)
        assert target / stop == pytest.approx(2.0, abs=0.01)


# ── range-adjusted stop / target ─────────────────────────────────────────────
class TestRangeAdjusted:
    @pytest.mark.parametrize("t,entry_price", [
        (tick(None, None), 100.0), (tick(110.0, None), 100.0), (tick(None, 100.0), 100.0),
        (tick(100.0, 100.0), 100.0),           # zero span
        (tick(110.0, 100.0), 0.0),             # dead entry price
        (tick(110.0, 100.0), -5.0),
    ])
    def test_unusable_range_data_changes_nothing(self, t, entry_price):
        assert entry._range_adjusted_stop_target(3.0, 6.0, t, entry_price) == (3.0, 6.0)

    def test_a_bare_object_without_day_fields_changes_nothing(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, object(), 100.0) == (3.0, 6.0)

    def test_mid_range_is_neutral(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 105.0) == (3.0, 6.0)

    def test_near_high_tightens_stop_and_target_and_restores_two_to_one(self):
        # rpos 0.9: target 6*0.75=4.5, stop 3*0.8=2.4 -> 1.875:1 -> target bumped to 2.4*2 = 4.8
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 109.0) == (2.4, 4.8)

    def test_near_high_boundary_is_inclusive(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 108.0) == (2.4, 4.8)   # rpos 0.80
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 107.9) == (3.0, 6.0)   # 0.79

    def test_near_low_widens_the_target_only(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 101.0) == (3.0, 6.6)

    def test_near_low_boundary_is_inclusive(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 102.0)[1] == 6.6      # rpos 0.20
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 102.1)[1] == 6.0      # 0.21

    def test_near_high_never_goes_below_the_minimum_stop_or_target(self):
        # stop 2.0*0.8 = 1.6 -> floored 2.0 ; target 4.0*0.75 = 3.0 -> floored 4.0
        assert entry._range_adjusted_stop_target(2.0, 4.0, tick(110.0, 100.0), 110.0) == (2.0, 4.0)

    def test_near_low_target_is_capped(self):
        assert entry._range_adjusted_stop_target(6.0, 12.0, tick(110.0, 100.0), 100.0) == (6.0, 12.0)

    def test_entry_outside_the_days_range_is_clamped_not_extrapolated(self):
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 150.0) == (2.4, 4.8)
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 50.0) == (3.0, 6.6)

    def test_reward_risk_floor_is_re_enforced_for_a_thin_input(self):
        # neutral range but a caller-supplied 1.5:1 -> lifted to 2:1
        assert entry._range_adjusted_stop_target(4.0, 6.0, tick(110.0, 100.0), 105.0) == (4.0, 8.0)

    def test_floor_follows_the_configured_ratio(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 3.0)
        assert entry._range_adjusted_stop_target(3.0, 6.0, tick(110.0, 100.0), 105.0) == (3.0, 9.0)

    def test_result_is_never_below_the_floor_for_any_input(self):
        for stop in (2.0, 3.3, 4.7, 6.0):
            for rpos_entry in (100.0, 105.0, 109.0):
                s, t = entry._range_adjusted_stop_target(stop, stop * 2.0, tick(110.0, 100.0), rpos_entry)
                assert t / s >= 2.0 - 1e-6


# ── reward:risk ──────────────────────────────────────────────────────────────
class TestRewardRisk:
    def test_ratio(self):
        assert entry._reward_risk_ratio(100.0, 97.0, 106.0) == 2.0

    def test_rounded_to_two_places(self):
        assert entry._reward_risk_ratio(100.0, 97.0, 107.0) == 2.33

    @pytest.mark.parametrize("stop", [100.0, 101.0])
    def test_stop_at_or_above_entry_scores_zero(self, stop):
        assert entry._reward_risk_ratio(100.0, stop, 110.0) == 0.0

    def test_target_below_entry_is_negative(self):
        assert entry._reward_risk_ratio(100.0, 97.0, 99.0) < 0


# ── conviction-scaled risk ───────────────────────────────────────────────────
class TestConviction:
    def test_no_score_keeps_the_base(self):
        assert entry._conviction_adjusted_risk_pct(1.0, None) == 1.0

    @pytest.mark.parametrize("score,expected", [
        (65.0, 1.0), (100.0, 1.25), (0.0, 0.75), (82.5, 1.125), (32.5, 0.875),
    ])
    def test_scales_plus_minus_25_percent(self, score, expected):
        assert entry._conviction_adjusted_risk_pct(1.0, score) == pytest.approx(expected)

    @pytest.mark.parametrize("score,expected", [(250.0, 1.25), (-40.0, 0.75)])
    def test_scores_are_clamped_to_0_100(self, score, expected):
        assert entry._conviction_adjusted_risk_pct(1.0, score) == pytest.approx(expected)

    def test_scales_the_base_and_rounds_to_four_places(self):
        assert entry._conviction_adjusted_risk_pct(2.0, 100.0) == 2.5
        assert entry._conviction_adjusted_risk_pct(1.0, 70.0) == round(1.0 * (1 + (5 / 35) * 0.25), 4)

    def test_numeric_strings_are_accepted(self):
        assert entry._conviction_adjusted_risk_pct(1.0, "100") == pytest.approx(1.25)

    def test_max_scale_is_respected(self, monkeypatch):
        monkeypatch.setattr(entry, "CONVICTION_MAX_SCALE", 0.10)
        assert entry._conviction_adjusted_risk_pct(1.0, 100.0) == pytest.approx(1.10)


# ── drift gate ───────────────────────────────────────────────────────────────
class TestDrift:
    @pytest.mark.parametrize("cur,sig", [(100.0, None), (100.0, 0.0), (100.0, -5.0), (0.0, 100.0), (-1.0, 100.0)])
    def test_no_usable_signal_price_means_no_gate(self, cur, sig):
        assert entry._entry_drift_ok(cur, sig, 2.0) == (True, "", 0.0, 0.0)

    def test_small_drift_passes_and_reports_the_numbers(self):
        ok, why, drift, mx = entry._entry_drift_ok(100.5, 100.0, 2.0)
        assert ok and why == "" and drift == pytest.approx(0.5) and mx == pytest.approx(1.5)

    def test_drift_exactly_at_the_limit_still_passes(self):
        assert entry._entry_drift_ok(101.5, 100.0, 2.0)[0] is True          # limit = 2.0 * 0.75

    def test_chasing_is_rejected(self):
        ok, why, drift, mx = entry._entry_drift_ok(102.0, 100.0, 2.0)
        assert not ok and "Chasing" in why and "+2.0%" in why and "limit 1.5%" in why

    def test_a_big_fall_is_rejected_with_its_own_message(self):
        ok, why, drift, _ = entry._entry_drift_ok(98.0, 100.0, 2.0)
        assert not ok and "fell" in why and "re-evaluate" in why and drift == pytest.approx(-2.0)

    def test_a_fall_exactly_at_the_limit_passes(self):
        assert entry._entry_drift_ok(98.5, 100.0, 2.0)[0] is True

    @pytest.mark.parametrize("atr", [None, 0, -1.0])
    def test_missing_atr_uses_a_three_percent_atr(self, atr):
        _, _, _, mx = entry._entry_drift_ok(100.0, 100.0, atr)
        assert mx == pytest.approx(3.0 * 0.75)

    def test_limit_follows_the_configured_multiple(self, monkeypatch):
        monkeypatch.setattr(entry, "MAX_ENTRY_DRIFT_ATR", 0.25)
        assert entry._entry_drift_ok(101.0, 100.0, 2.0)[0] is False         # limit now 0.5%


# ── composite quality score ──────────────────────────────────────────────────
class TestComposite:
    def test_documented_example(self):
        # conviction 80, rr 3.0 (halfway 2->4), drift half-way to the limit
        assert entry._composite_quality_score(80.0, 3.0, 0.375, 0.75) == pytest.approx(80 * 0.65 + 50 * 0.10 + 50 * 0.25)

    def test_no_conviction_counts_as_fifty(self):
        assert entry._composite_quality_score(None, 2.0, 0.0, 0.0) == pytest.approx(50 * 0.65 + 0 + 100 * 0.25)

    def test_conviction_is_clamped(self):
        hi = entry._composite_quality_score(500.0, 2.0, 0.0, 0.0)
        assert hi == pytest.approx(100 * 0.65 + 0 + 25.0)
        lo = entry._composite_quality_score(-50.0, 2.0, 0.0, 0.0)
        assert lo == pytest.approx(0 + 0 + 25.0)

    @pytest.mark.parametrize("rr,norm", [(2.0, 0.0), (3.0, 50.0), (4.0, 100.0), (9.0, 100.0), (1.0, 0.0)])
    def test_reward_risk_scales_from_the_floor_to_the_ceiling(self, rr, norm):
        assert entry._composite_quality_score(0.0, rr, 0.0, 0.0) == pytest.approx(norm * 0.10 + 100 * 0.25)

    @pytest.mark.parametrize("drift,safety", [(0.0, 100.0), (0.375, 50.0), (0.75, 0.0), (-0.375, 50.0), (2.0, 0.0)])
    def test_drift_safety_is_symmetric_and_clamped(self, drift, safety):
        assert entry._composite_quality_score(0.0, 2.0, drift, 0.75) == pytest.approx(safety * 0.25)

    def test_no_signal_price_is_treated_as_safe(self):
        assert entry._composite_quality_score(0.0, 2.0, 0.0, 0.0) == pytest.approx(25.0)

    def test_degenerate_ceiling_does_not_divide_by_zero(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_COMPOSITE_RR_CEILING", 2.0)
        assert entry._composite_quality_score(0.0, 3.0, 0.0, 0.0) == pytest.approx(100 * 0.10 + 25.0)

    def test_better_setups_always_rank_higher(self):
        weak = entry._composite_quality_score(50.0, 2.0, 0.7, 0.75)
        strong = entry._composite_quality_score(90.0, 3.5, 0.05, 0.75)
        assert strong > weak

    def test_result_is_rounded_to_two_places(self):
        v = entry._composite_quality_score(77.7777, 2.3333, 0.1234, 0.75)
        assert v == round(v, 2)


# ── cost-gate knobs ──────────────────────────────────────────────────────────
class TestCostGateKnobs:
    def cfg(self, db, **kw):
        db.add(models.TradeRiskConfig(mode="REAL", **kw))
        db.commit()

    def test_no_row_falls_back_to_the_env_defaults(self, db):
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (3000.0, 3.0)

    def test_null_columns_fall_back(self, db):
        self.cfg(db)
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (3000.0, 3.0)

    def test_each_override_is_independent(self, db):
        self.cfg(db, min_trade_value=5000.0)
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (5000.0, 3.0)
        db.query(models.TradeRiskConfig).update({"min_trade_value": None, "min_edge_to_cost_ratio": 4.5})
        db.commit()
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (3000.0, 4.5)

    def test_a_zero_override_is_honoured_not_treated_as_unset(self, db):
        self.cfg(db, min_trade_value=0.0, min_edge_to_cost_ratio=0.0)
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (0.0, 0.0)

    def test_other_modes_row_is_ignored(self, db):
        self.cfg(db, min_trade_value=9999.0)
        assert entry._resolve_cost_gate_knobs(db, "DEMO") == (3000.0, 3.0)

    def test_a_db_error_falls_back(self, db, monkeypatch):
        monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert entry._resolve_cost_gate_knobs(db, "REAL") == (3000.0, 3.0)


# ── market regime ────────────────────────────────────────────────────────────
class FakeHttp:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error, self.calls = status, body if body is not None else {}, error, 0

    def client(self):
        outer = self

        class C:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

            async def get(s, url, params=None, timeout=None):
                outer.calls += 1
                if outer.error:
                    raise outer.error
                return types.SimpleNamespace(status_code=outer.status, json=lambda: outer.body)
        return C()


@pytest.fixture()
def regime(monkeypatch):
    import adaptive_thresholds as at
    monkeypatch.setattr(entry, "_regime_cache", {"score": None, "threshold": None, "source": None, "ts": 0.0})
    state = types.SimpleNamespace(http=FakeHttp(body={"market_score": 60}), recorded=[], thr=(40, "adaptive_20d_p20"),
                                  adaptive_error=None)
    monkeypatch.setattr(entry.httpx, "AsyncClient", lambda *a, **k: state.http.client())
    monkeypatch.setattr(at, "record_market_score", lambda db, s: state.recorded.append(s))

    def thr(db):
        if state.adaptive_error:
            raise state.adaptive_error
        return state.thr
    monkeypatch.setattr(at, "adaptive_regime_threshold", thr)
    return state


class TestMarketRegime:
    def test_score_above_the_adaptive_threshold_is_ok(self, db, regime):
        assert run(entry._get_market_regime(db)) == (True, 60, 40, "adaptive_20d_p20")
        assert regime.recorded == [60]

    def test_score_below_the_threshold_blocks(self, db, regime):
        regime.http.body = {"market_score": 30}
        ok, score, thr, _ = run(entry._get_market_regime(db))
        assert (ok, score, thr) == (False, 30, 40)

    def test_score_equal_to_the_threshold_passes(self, db, regime):
        regime.http.body = {"market_score": 40}
        assert run(entry._get_market_regime(db))[0] is True

    def test_fetch_failure_fails_open_with_a_neutral_score(self, db, regime):
        regime.http.error = RuntimeError("gateway down")
        ok, score, _, _ = run(entry._get_market_regime(db))
        assert score == 50 and ok is True

    def test_non_200_is_treated_like_a_failed_fetch(self, db, regime):
        regime.http.status, regime.http.body = 503, {"market_score": 5}
        assert run(entry._get_market_regime(db))[1] == 50

    def test_missing_score_defaults_to_neutral(self, db, regime):
        regime.http.body = {}
        assert run(entry._get_market_regime(db))[1] == 50

    def test_adaptive_failure_falls_back_to_the_static_threshold(self, db, regime):
        regime.adaptive_error = RuntimeError("no history")
        assert run(entry._get_market_regime(db)) == (True, 60, 38, "static")

    def test_result_is_cached_for_the_ttl(self, db, regime, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(_time, "time", lambda: now[0])
        run(entry._get_market_regime(db))
        regime.http.body = {"market_score": 10}
        now[0] += 119.0
        assert run(entry._get_market_regime(db))[1] == 60 and regime.http.calls == 1

    def test_cache_expires_after_the_ttl(self, db, regime, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(_time, "time", lambda: now[0])
        run(entry._get_market_regime(db))
        regime.http.body = {"market_score": 10}
        now[0] += 121.0
        assert run(entry._get_market_regime(db))[1] == 10 and regime.http.calls == 2

    def test_a_reported_score_of_zero_is_returned_as_zero__BUG_FIXED(self, db, regime):
        # FIXED (item C.12): `int(data.get("market_score") or 50)` was treating a genuine 0 (worst possible
        # market) as falsy and returning 50 (neutral). Now uses explicit is-None check so 0 passes through.
        regime.http.body = {"market_score": 0}
        assert run(entry._get_market_regime(db))[1] == 0


# ── AccountState ─────────────────────────────────────────────────────────────
@pytest.fixture()
def acct(db, monkeypatch):
    calls = []
    monkeypatch.setattr(equity_sync, "sync_real_equity", lambda d: calls.append("sync"))
    monkeypatch.setattr(entry, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure", lambda d: 12_345.0)
    for mode in ("REAL", "DEMO"):
        db.add(models.TradeAccount(mode=mode, starting_capital=100_000.0, current_equity=90_000.0,
                                   cash_available=60_000.0, broker_cash_available=75_000.0,
                                   realized_pnl_today=-500.0, realized_pnl_total=0.0,
                                   pnl_last_reset_date=ist_today_str()))      # else get_account() lazily zeroes today's P&L
        db.add(models.TradeRiskConfig(mode=mode, risk_per_trade_pct=1.5, max_daily_loss_pct=2.5,
                                      max_concurrent_positions=4, max_portfolio_risk_pct=6.0,
                                      stale_data_seconds=20, max_tick_volatility_mult=1.8,
                                      allow_pyramiding=True))
    db.commit()
    return calls


def add_pos(db, symbol, qty, avg, stop, status="OPEN", mode="REAL"):
    db.add(models.TradePosition(mode=mode, symbol=symbol, status=status, qty_open=qty, avg_entry_price=avg,
                                current_stop=stop, realized_pnl=0.0, opened_at=datetime.now(timezone.utc)))
    db.commit()


class TestAccountState:
    def test_risk_config_and_account_are_mapped_through(self, db, acct):
        a = entry._account_state(db, "REAL", gate_armed=True)
        assert (a.equity, a.risk_per_trade_pct, a.max_daily_loss_pct, a.max_concurrent_positions) == (90_000.0, 1.5, 2.5, 4)
        assert (a.max_portfolio_risk_pct, a.stale_data_seconds, a.max_tick_volatility_mult) == (6.0, 20, 1.8)
        assert a.allow_pyramiding is True and a.realized_pnl_today == -500.0
        assert a.cash_available == 60_000.0 and a.broker_cash_available == 75_000.0 and a.market_is_open is True

    def test_real_mode_syncs_equity_and_includes_the_other_services_exposure(self, db, acct):
        a = entry._account_state(db, "REAL", gate_armed=True)
        assert acct == ["sync"] and a.other_service_open_positions_market_value == 12_345.0

    def test_demo_mode_neither_syncs_nor_counts_the_other_service(self, db, acct):
        a = entry._account_state(db, "DEMO", gate_armed=True)
        assert acct == [] and a.other_service_open_positions_market_value == 0.0

    def test_disarmed_gate_means_globally_paused(self, db, acct):
        assert entry._account_state(db, "REAL", gate_armed=False).trading_globally_paused is True
        assert entry._account_state(db, "REAL", gate_armed=True).trading_globally_paused is False

    def test_market_hours_flag_comes_from_the_ist_clock(self, db, acct, monkeypatch):
        monkeypatch.setattr(entry, "is_market_open_ist", lambda: False)
        assert entry._account_state(db, "REAL", True).market_is_open is False

    def test_position_exposure_is_summed(self, db, acct):
        add_pos(db, "AAA", 10, 100.0, 97.0)                 # risk 30, value 1,000
        add_pos(db, "BBB", 5, 200.0, 190.0)                 # risk 50, value 1,000
        a = entry._account_state(db, "REAL", True)
        assert a.open_position_count == 2 and a.open_position_symbols == {"AAA", "BBB"}
        assert a.open_positions_total_risk == pytest.approx(80.0) and a.open_positions_market_value == pytest.approx(2_000.0)

    def test_a_stop_above_entry_contributes_zero_risk_not_negative(self, db, acct):
        add_pos(db, "AAA", 10, 100.0, 105.0)
        assert entry._account_state(db, "REAL", True).open_positions_total_risk == 0.0

    def test_a_missing_stop_contributes_zero_risk(self, db, acct):
        add_pos(db, "AAA", 10, 100.0, None)
        assert entry._account_state(db, "REAL", True).open_positions_total_risk == 0.0

    def test_pending_exit_still_counts_as_exposure(self, db, acct):
        add_pos(db, "AAA", 10, 100.0, 97.0, status="PENDING_EXIT")
        a = entry._account_state(db, "REAL", True)
        assert a.open_position_symbols == {"AAA"} and a.open_position_count == 1

    def test_closed_positions_and_other_modes_are_ignored(self, db, acct):
        add_pos(db, "AAA", 10, 100.0, 97.0, status="CLOSED")
        add_pos(db, "BBB", 10, 100.0, 97.0, mode="DEMO")
        assert entry._account_state(db, "REAL", True).open_position_count == 0

    def test_reserved_cash_is_deducted_and_floored_at_zero(self, db, acct):
        assert entry._account_state(db, "REAL", True, reserved_cash=10_000.0).cash_available == 50_000.0
        assert entry._account_state(db, "REAL", True, reserved_cash=999_999.0).cash_available == 0.0


# ── DEMO simulated fills ─────────────────────────────────────────────────────
def demo_order(db, symbol="ABC", limit=100.0, decision=True, status="PLACED", mode="DEMO", side="BUY", qty=10):
    dec_id = None
    if decision:
        d = models.TradeDecision(mode=mode, symbol=symbol, decision_type="ENTRY", action="BUY",
                                 proposed_stop=97.0, proposed_target=106.0)
        db.add(d)
        db.commit()
        dec_id = d.id
    o = models.TradeOrder(mode=mode, decision_id=dec_id, symbol=symbol, side=side, qty=qty, order_type="LIMIT",
                          limit_price=limit, status=status)
    db.add(o)
    db.commit()
    return o


class TestCheckPendingFills:
    @pytest.fixture(autouse=True)
    def quotes(self, db, monkeypatch):
        db.add(models.TradeAccount(mode="DEMO", starting_capital=100_000.0, current_equity=100_000.0,
                                   cash_available=100_000.0, broker_cash_available=0.0,
                                   realized_pnl_today=0.0, realized_pnl_total=0.0))
        db.commit()
        self.ticks = {}
        self.asked = []

        async def get_quotes(symbols):
            self.asked.append(sorted(symbols))
            return dict(self.ticks)
        monkeypatch.setattr(entry, "get_quotes", get_quotes)

    def T(self, price):
        return Tick("ABC", price, datetime.now(timezone.utc), None, "test")

    def test_only_demo_is_simulated(self, db):
        demo_order(db, mode="REAL")
        assert run(entry.check_pending_fills(db, "REAL")) == 0 and self.asked == []

    def test_no_pending_orders_makes_no_quote_call(self, db):
        assert run(entry.check_pending_fills(db, "DEMO")) == 0 and self.asked == []

    def test_price_inside_the_entry_zone_fills_with_the_decisions_levels(self, db):
        o = demo_order(db)
        self.ticks["ABC"] = self.T(99.5)
        assert run(entry.check_pending_fills(db, "DEMO")) == 1
        pos = db.query(models.TradePosition).one()
        assert o.status == "FILLED" and (pos.avg_entry_price, pos.current_stop, pos.current_target) == (99.5, 97.0, 106.0)

    def test_price_above_the_limit_does_not_fill(self, db):
        o = demo_order(db)
        self.ticks["ABC"] = self.T(100.5)
        assert run(entry.check_pending_fills(db, "DEMO")) == 0 and o.status == "PLACED"

    def test_symbol_without_a_quote_is_skipped(self, db):
        demo_order(db)
        assert run(entry.check_pending_fills(db, "DEMO")) == 0

    def test_only_buy_orders_in_placed_state_are_checked(self, db):
        demo_order(db, status="FILLED")
        demo_order(db, side="SELL")
        assert run(entry.check_pending_fills(db, "DEMO")) == 0 and self.asked == []

    def test_quotes_are_requested_once_for_the_distinct_symbols(self, db):
        demo_order(db, "AAA")
        demo_order(db, "AAA")
        demo_order(db, "BBB")
        run(entry.check_pending_fills(db, "DEMO"))
        assert self.asked == [["AAA", "BBB"]]

    def test_no_decision_falls_back_to_the_flat_percentages(self, db, monkeypatch):
        import portfolio.portfolio as P
        seen = {}
        monkeypatch.setattr(P, "try_fill_entry", lambda d, o, t, stop, tgt: seen.update(stop=stop, tgt=tgt) or True)
        demo_order(db, decision=False, limit=200.0)
        self.ticks["ABC"] = self.T(199.0)
        assert run(entry.check_pending_fills(db, "DEMO")) == 1
        assert seen["stop"] == pytest.approx(200.0 * (1 - 3.2 / 100)) and seen["tgt"] == pytest.approx(200.0 * (1 + 6.5 / 100))

    def test_multiple_fills_are_counted(self, db):
        demo_order(db, "AAA")
        demo_order(db, "BBB")
        self.ticks = {"AAA": Tick("AAA", 99.0, datetime.now(timezone.utc), None, "t"),
                      "BBB": Tick("BBB", 99.0, datetime.now(timezone.utc), None, "t")}
        assert run(entry.check_pending_fills(db, "DEMO")) == 2


# ── stale-order expiry ───────────────────────────────────────────────────────
class TestExpireStaleOrders:
    @pytest.fixture(autouse=True)
    def rig(self, db, monkeypatch):
        self.cancels, self.sent, self.cancel_error = [], [], None

        def cancel(db_, *, is_armed, dhan_order_id):
            self.cancels.append((is_armed, dhan_order_id))
            if self.cancel_error:
                raise self.cancel_error
            return {}

        async def notify(t):
            self.sent.append(t)
        monkeypatch.setattr(dhan_client, "cancel_order", cancel)
        monkeypatch.setattr(entry, "notify_async", notify)

    def stale(self, db, mode="REAL", status="PLACED", dhan_id="D1", symbol="ABC", minutes=5, filled=0, **kw):
        o = models.TradeOrder(mode=mode, symbol=symbol, side="BUY", qty=10, order_type="LIMIT", status=status,
                              dhan_order_id=dhan_id, filled_qty_so_far=filled,
                              valid_until=datetime.now(timezone.utc) - timedelta(minutes=minutes), **kw)
        db.add(o)
        db.commit()
        return o

    def held(self, db, symbol="ABC"):
        return db.query(models.SharedSymbolLock).filter_by(symbol=symbol).first() is not None

    def test_real_order_is_cancelled_at_dhan_first_then_expired_and_unlocked(self, db):
        shared_symbol_lock.try_claim(db, "ABC")
        o = self.stale(db)
        assert run(entry.expire_stale_orders(db, "REAL")) == 1
        assert self.cancels == [(True, "D1")]                       # cancelling is never gated by arm state
        assert o.status == "EXPIRED" and not self.held(db)
        ev = db.query(models.TradeOrderEvent).filter_by(order_id=o.id).one()
        assert ev.event_type == "EXPIRED" and ev.detail.endswith("Dhan order cancelled.") and "unfilled" in ev.detail

    def test_failed_cancel_leaves_the_order_placed_and_the_lock_held(self, db):
        shared_symbol_lock.try_claim(db, "ABC")
        o = self.stale(db)
        self.cancel_error = RuntimeError("already filled")
        assert run(entry.expire_stale_orders(db, "REAL")) == 0
        assert o.status == "PLACED" and self.held(db)
        assert len(self.sent) == 1 and "cancel failed" in self.sent[0] and "already filled" in self.sent[0]
        assert db.query(models.TradeOrderEvent).count() == 0

    def test_partially_filled_order_expires_but_keeps_the_lock(self, db):
        shared_symbol_lock.try_claim(db, "ABC")
        o = self.stale(db, status="PARTIAL", filled=4)
        assert run(entry.expire_stale_orders(db, "REAL")) == 1
        assert o.status == "EXPIRED" and self.held(db)              # 4 real shares are still held
        detail = db.query(models.TradeOrderEvent).one().detail
        assert "partially filled" in detail and "4/10 had already filled" in detail

    def test_demo_orders_expire_without_touching_the_broker(self, db):
        o = self.stale(db, mode="DEMO", dhan_id=None)
        assert run(entry.expire_stale_orders(db, "DEMO")) == 1
        assert o.status == "EXPIRED" and self.cancels == []
        assert "Dhan order cancelled" not in db.query(models.TradeOrderEvent).one().detail

    def test_real_order_without_a_broker_id_expires_without_a_cancel(self, db):
        o = self.stale(db, dhan_id=None)
        assert run(entry.expire_stale_orders(db, "REAL")) == 1 and o.status == "EXPIRED" and self.cancels == []

    def test_orders_inside_their_window_or_without_one_are_untouched(self, db):
        fresh = self.stale(db, minutes=-10, dhan_id="D2", symbol="AAA")
        none = models.TradeOrder(mode="REAL", symbol="BBB", side="BUY", qty=1, order_type="LIMIT", status="PLACED",
                                 dhan_order_id="D3", valid_until=None)
        db.add(none)
        db.commit()
        assert run(entry.expire_stale_orders(db, "REAL")) == 0
        assert fresh.status == "PLACED" and none.status == "PLACED" and self.cancels == []

    @pytest.mark.parametrize("status", ["FILLED", "REJECTED", "CANCELLED", "EXPIRED"])
    def test_finished_orders_are_never_touched(self, db, status):
        self.stale(db, status=status)
        assert run(entry.expire_stale_orders(db, "REAL")) == 0 and self.cancels == []

    def test_only_the_requested_mode_is_processed(self, db):
        self.stale(db, mode="DEMO", dhan_id=None)
        assert run(entry.expire_stale_orders(db, "REAL")) == 0

    def test_one_failed_cancel_does_not_stop_the_others(self, db):
        a = self.stale(db, dhan_id="D1", symbol="AAA")
        b = self.stale(db, dhan_id="D2", symbol="BBB")

        def cancel(db_, *, is_armed, dhan_order_id):
            if dhan_order_id == "D1":
                raise RuntimeError("gone")
        dhan_client.cancel_order = cancel
        assert run(entry.expire_stale_orders(db, "REAL")) == 1 and a.status == "PLACED" and b.status == "EXPIRED"

    def test_count_and_audit_log(self, db):
        self.stale(db, symbol="AAA", dhan_id="D1")
        self.stale(db, symbol="BBB", dhan_id="D2")
        assert run(entry.expire_stale_orders(db, "REAL")) == 2
        assert db.query(models.TradeAuditLog).filter_by(action="ORDERS_EXPIRED").count() == 1

    def test_nothing_stale_writes_no_audit_row(self, db):
        run(entry.expire_stale_orders(db, "REAL"))
        assert db.query(models.TradeAuditLog).count() == 0


# ── item 10 fixed: shares filled before stale-order cancel are now booked ─────
class TestKnownGaps:
    def test_shares_filled_just_before_the_cancel_should_still_be_booked(self, db, monkeypatch):
        """FIXED (item 10): expire_stale_orders now reads Dhan's order state after a successful
        cancel and books any fills that landed before the cancel reached the exchange."""
        import asyncio as _a
        from execution import reconcile as R
        db.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0,
                                   cash_available=100_000.0, broker_cash_available=100_000.0,
                                   realized_pnl_today=0.0, realized_pnl_total=0.0))
        dec = models.TradeDecision(mode="REAL", symbol="ABC", decision_type="ENTRY", action="BUY",
                                   proposed_stop=97.0, proposed_target=106.0)
        db.add(dec)
        db.commit()
        o = models.TradeOrder(mode="REAL", decision_id=dec.id, symbol="ABC", side="BUY", qty=10, order_type="LIMIT",
                              status="PLACED", dhan_order_id="D1", filled_qty_so_far=0,
                              valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.add(o)
        db.commit()
        monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
        book = [{"orderId": "D1", "orderStatus": "CANCELLED", "orderType": "LIMIT",
                 "averageTradedPrice": 100.0, "filledQty": 3}]          # 3 shares filled before the cancel landed
        monkeypatch.setattr(dhan_client, "get_order_list", lambda d: list(book))

        async def _noop(*a, **k):
            return 0
        monkeypatch.setattr(R, "notify_async", _noop)
        monkeypatch.setattr(R, "import_broker_holdings", _noop)
        monkeypatch.setattr(R, "holdings_sync_reconcile", lambda d: {"closed": 0, "symbols": []})
        monkeypatch.setattr(entry, "notify_async", _noop)

        run(entry.expire_stale_orders(db, "REAL"))                        # same order as cycle_runner: expire first...
        run(R.reconcile_real_orders(db))                                  # ...then reconcile
        pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
        assert pos is not None and pos.qty_open == 3


class TestLateFillAfterEarlierPartial:
    """session110: the post-cancel late-fill booking in expire_stale_orders is a
    second caller of reconcile._book_fill_delta. When the order had ALREADY been
    partially booked, the late shares must be booked at their own price, not at
    the broker's cumulative average of the whole order."""

    def test_late_fill_after_a_partial_is_booked_at_its_own_price(self, db, monkeypatch):
        import asyncio as _a
        from execution import reconcile as R
        db.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0,
                                   cash_available=100_000.0, broker_cash_available=100_000.0,
                                   realized_pnl_today=0.0, realized_pnl_total=0.0))
        dec = models.TradeDecision(mode="REAL", symbol="ABC", decision_type="ENTRY", action="BUY",
                                   proposed_stop=97.0, proposed_target=106.0)
        db.add(dec)
        db.commit()
        # 5 shares already booked at the broker's cumulative 100.00 (baseline = 500)
        o = models.TradeOrder(mode="REAL", decision_id=dec.id, symbol="ABC", side="BUY", qty=10, order_type="LIMIT",
                              status="PARTIAL", dhan_order_id="D1", filled_qty_so_far=5,
                              broker_fill_notional=500.0,
                              valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.add(o)
        db.commit()
        monkeypatch.setattr(dhan_client, "cancel_order", lambda *a, **k: {})
        # 5 more filled before the cancel landed -> cumulative 10 @ 101.00, i.e. the late 5 were @ 102.00
        monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [
            {"orderId": "D1", "orderStatus": "CANCELLED", "orderType": "LIMIT",
             "averageTradedPrice": 101.0, "filledQty": 10}])

        async def _noop(*a, **k):
            return 0
        monkeypatch.setattr(R, "notify_async", _noop)
        monkeypatch.setattr(entry, "notify_async", _noop)

        run(entry.expire_stale_orders(db, "REAL"))
        pos = db.query(models.TradePosition).filter_by(symbol="ABC").first()
        assert pos is not None and pos.qty_open == 5
        assert pos.avg_entry_price == pytest.approx(102.0)
        assert o.broker_fill_notional == pytest.approx(1010.0)

