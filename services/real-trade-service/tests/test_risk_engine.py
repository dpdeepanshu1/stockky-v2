"""
tests/test_risk_engine.py — unit tests for risk_engine/engine.py.

Why this file exists: the DEMO scenario battery (POST /risk-engine/check) can
only run while the market is open — outside market hours every scenario stops
at check #2 ("market_closed") and never reaches the sizing / cap logic. These
tests inject AccountState directly (market_is_open=True, fixed `now`), so
every check runs regardless of the day or time.

Pure offline: engine.py imports only the stdlib. No DB, no network, no env.

Layout:
  - TestCheckOrder            which check wins when several would fail
  - TestGlobalPauseAndMarket  checks #1, #2
  - TestDailyLoss             check #3
  - TestConcurrentPositions   check #4
  - TestPriceBounds           checks #4a, #4a-ii (+ _adaptive_max_stock_price)
  - TestLiquidityFloor        check #4b
  - TestPerTradeRisk          check #5 (incl. adj_risk_pct)
  - TestCashCap               check #5b
  - TestCapitalShareCap       check #5b-ii
  - TestConcentrationCap      check #5c
  - TestPortfolioRisk         check #6
  - TestPyramiding            check #7
  - TestStaleData             check #8
  - TestAbnormalVolatility    check #9
  - TestSellSideBypass        exits must not be blocked by entry-only checks
  - TestKnownGaps             strict xfails: things the engine SHOULD veto but
                              currently approves. They flip to a hard failure
                              (XPASS) the moment the engine is fixed, so remove
                              the marker then.

Run from services/real-trade-service:
    python3 -m pytest tests/test_risk_engine.py -q \
        --cov=risk_engine --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from risk_engine import engine
from risk_engine.engine import (
    AccountState,
    OrderIntent,
    RiskVerdict,
    _adaptive_max_stock_price,
    evaluate,
)

NOW = datetime(2026, 9, 21, 5, 0, 0, tzinfo=timezone.utc)  # a Monday, market hours


# ── Fixtures / factories ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def pin_engine_constants(monkeypatch):
    """engine.py reads env vars at import time. Pin every tunable to its
    documented default so the suite gives the same answer on any machine,
    regardless of what RISK_* variables the VM has set."""
    monkeypatch.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(engine, "ADAPTIVE_MAX_PRICE_STOP_PCT", 6.0)
    monkeypatch.setattr(engine, "HARD_FLOOR_LIQUIDITY", 5_000_000.0)
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 25.0)


def make_account(**over) -> AccountState:
    """Roomy baseline: nothing fails unless a test overrides something.
    equity 100k, 1% per-trade risk (=> ₹1,000), 5% portfolio cap (=> ₹5,000),
    cash 100k, capital-share cap = 50% of (200k broker cash) = ₹100k."""
    base = dict(
        equity=100_000.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=3.0,
        max_concurrent_positions=3,
        max_portfolio_risk_pct=5.0,
        stale_data_seconds=30,
        max_tick_volatility_mult=2.0,
        allow_pyramiding=False,
        realized_pnl_today=0.0,
        open_position_count=0,
        open_position_symbols=set(),
        open_positions_total_risk=0.0,
        trading_globally_paused=False,
        market_is_open=True,
        cash_available=100_000.0,
        broker_cash_available=200_000.0,
        open_positions_market_value=0.0,
        other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def make_intent(**over) -> OrderIntent:
    """Baseline BUY: 100 x RELIANCE-ish @ ₹100, stop ₹98.
    risk = 2 * 100 = ₹200 (< ₹1,000), cost = ₹10,000."""
    base = dict(
        mode="DEMO",
        symbol="TESTCO",
        side="BUY",
        qty=100,
        entry_price=100.0,
        stop_price=98.0,
    )
    base.update(over)
    return OrderIntent(**base)


def run(intent=None, account=None, now=NOW):
    return evaluate(intent or make_intent(), account or make_account(), now=now)


def assert_rejected(res, check_name, verdict=RiskVerdict.REJECTED):
    assert res.verdict == verdict, f"got {res.verdict} / {res.check_name}: {res.reason}"
    assert res.check_name == check_name, f"got {res.check_name}: {res.reason}"
    assert res.approved_qty is None


def assert_approved(res, qty, check_name=None):
    assert res.verdict == RiskVerdict.APPROVED, f"got {res.verdict} / {res.check_name}: {res.reason}"
    assert res.approved_qty == qty
    if check_name:
        assert res.check_name == check_name


# ── Happy path ───────────────────────────────────────────────────────────────

def test_baseline_buy_is_approved_unchanged():
    res = run()
    assert_approved(res, 100, "all_checks_passed")


# ── Order in which checks are reported ───────────────────────────────────────

class TestCheckOrder:
    def test_global_pause_beats_market_closed(self):
        acct = make_account(trading_globally_paused=True, market_is_open=False)
        assert_rejected(run(account=acct), "global_pause", RiskVerdict.BLOCKED_GLOBAL)

    def test_market_closed_beats_daily_loss(self):
        acct = make_account(market_is_open=False, realized_pnl_today=-50_000.0)
        assert_rejected(run(account=acct), "market_closed")

    def test_daily_loss_beats_concurrent_positions(self):
        acct = make_account(realized_pnl_today=-4_000.0, open_position_count=3)
        assert_rejected(run(account=acct), "daily_loss_limit", RiskVerdict.BLOCKED_GLOBAL)

    def test_concurrent_beats_price_floor(self):
        acct = make_account(open_position_count=3)
        res = run(make_intent(entry_price=5.0, stop_price=4.9), acct)
        assert_rejected(res, "max_concurrent_positions")

    def test_price_floor_beats_liquidity_and_sizing(self):
        res = run(make_intent(entry_price=5.0, stop_price=4.9, avg_traded_value=1.0))
        assert_rejected(res, "min_price_floor")


# ── #1 / #2 ──────────────────────────────────────────────────────────────────

class TestGlobalPauseAndMarket:
    def test_paused_buy_is_blocked_global(self):
        res = run(account=make_account(trading_globally_paused=True))
        assert_rejected(res, "global_pause", RiskVerdict.BLOCKED_GLOBAL)

    def test_paused_sell_still_goes_through(self):
        res = run(make_intent(side="SELL"), make_account(trading_globally_paused=True))
        assert_approved(res, 100)

    @pytest.mark.parametrize("side", ["BUY", "SELL"])
    def test_market_closed_rejects_both_sides(self, side):
        res = run(make_intent(side=side), make_account(market_is_open=False))
        assert_rejected(res, "market_closed")


# ── #3 daily loss ────────────────────────────────────────────────────────────

class TestDailyLoss:
    def test_exactly_at_cap_blocks(self):
        # -₹3,000 on ₹100k equity == 3.00% == cap  (check is >=)
        res = run(account=make_account(realized_pnl_today=-3_000.0))
        assert_rejected(res, "daily_loss_limit", RiskVerdict.BLOCKED_GLOBAL)

    def test_just_under_cap_passes(self):
        res = run(account=make_account(realized_pnl_today=-2_999.0))
        assert_approved(res, 100)

    def test_profit_never_blocks(self):
        res = run(account=make_account(realized_pnl_today=+10_000.0))
        assert_approved(res, 100)

    def test_sell_is_never_blocked_by_daily_loss(self):
        res = run(make_intent(side="SELL"), make_account(realized_pnl_today=-50_000.0))
        assert_approved(res, 100)

    def test_zero_equity_does_not_divide_by_zero(self):
        # equity 0 -> daily-loss % treated as 0.0. The BUY still can't pass
        # (zero risk budget), but the reason must not be daily_loss_limit
        # and evaluate() must not raise.
        res = run(account=make_account(equity=0.0, realized_pnl_today=-100.0))
        assert res.check_name != "daily_loss_limit"
        assert res.verdict != RiskVerdict.APPROVED


# ── #4 concurrent positions ──────────────────────────────────────────────────

class TestConcurrentPositions:
    def test_at_cap_rejects(self):
        assert_rejected(run(account=make_account(open_position_count=3)),
                        "max_concurrent_positions")

    def test_one_below_cap_passes(self):
        assert_approved(run(account=make_account(open_position_count=2)), 100)

    def test_over_cap_rejects(self):
        assert_rejected(run(account=make_account(open_position_count=7)),
                        "max_concurrent_positions")


# ── #4a / #4a-ii price bounds ────────────────────────────────────────────────

class TestPriceBounds:
    def test_below_floor_rejected(self):
        res = run(make_intent(entry_price=19.99, stop_price=19.5))
        assert_rejected(res, "min_price_floor")

    def test_exactly_at_floor_passes(self):
        res = run(make_intent(entry_price=20.0, stop_price=19.6))
        assert_approved(res, 100)

    def test_penny_stock_rejected(self):
        res = run(make_intent(symbol="SUZLON", entry_price=8.0, stop_price=7.8, qty=1))
        assert_rejected(res, "min_price_floor")

    def test_zero_price_rejected_by_floor(self):
        res = run(make_intent(entry_price=0.0, stop_price=0.0, qty=1))
        assert_rejected(res, "min_price_floor")

    def test_negative_price_rejected_by_floor(self):
        res = run(make_intent(entry_price=-5.0, stop_price=-6.0, qty=1))
        assert_rejected(res, "min_price_floor")

    def test_adaptive_ceiling_rejects_above_derived_max(self):
        # ceiling = (100k * 1%) / 6% = ₹16,666.67
        res = run(make_intent(entry_price=20_000.0, stop_price=19_000.0, qty=1))
        assert_rejected(res, "max_price_ceiling")
        assert "scales automatically" in res.reason

    def test_adaptive_ceiling_allows_below_derived_max(self):
        res = run(make_intent(entry_price=16_000.0, stop_price=15_600.0, qty=1))
        assert_approved(res, 1, "all_checks_passed")

    def test_explicit_env_ceiling_wins_over_adaptive(self, monkeypatch):
        monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", True)
        # adaptive would allow ₹5,000 (ceiling ₹16,666); explicit ₹3,000 must win
        res = run(make_intent(entry_price=5_000.0, stop_price=4_900.0, qty=1))
        assert_rejected(res, "max_price_ceiling")
        assert "Raise RISK_MAX_STOCK_PRICE" in res.reason

    def test_adaptive_kill_switch_uses_static_ceiling(self, monkeypatch):
        monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", False)
        res = run(make_intent(entry_price=5_000.0, stop_price=4_900.0, qty=1))
        assert_rejected(res, "max_price_ceiling")
        assert "Raise RISK_MAX_STOCK_PRICE" in res.reason

    def test_ceiling_is_strictly_greater_than(self, monkeypatch):
        monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", False)
        res = run(make_intent(entry_price=3_000.0, stop_price=2_950.0, qty=1))
        assert_approved(res, 1)

    # _adaptive_max_stock_price directly
    def test_adaptive_formula(self):
        assert _adaptive_max_stock_price(make_account()) == pytest.approx(100_000 * 0.01 / 0.06)

    def test_adaptive_falls_back_to_static_on_zero_equity(self):
        assert _adaptive_max_stock_price(make_account(equity=0.0)) == 3000.0

    def test_adaptive_falls_back_to_static_on_negative_equity(self):
        assert _adaptive_max_stock_price(make_account(equity=-500.0)) == 3000.0

    def test_adaptive_falls_back_to_static_on_zero_risk_pct(self):
        assert _adaptive_max_stock_price(make_account(risk_per_trade_pct=0.0)) == 3000.0

    def test_adaptive_never_drops_below_price_floor(self):
        # equity ₹100 @ 1% -> derived ₹16.67, must clamp UP to the ₹20 floor
        assert _adaptive_max_stock_price(make_account(equity=100.0)) == 20.0


# ── #4b liquidity ────────────────────────────────────────────────────────────

class TestLiquidityFloor:
    def test_below_floor_rejected(self):
        res = run(make_intent(avg_traded_value=4_999_999.0))
        assert_rejected(res, "liquidity_floor")

    def test_at_floor_passes(self):
        assert_approved(run(make_intent(avg_traded_value=5_000_000.0)), 100)

    def test_missing_data_fails_open(self):
        assert_approved(run(make_intent(avg_traded_value=None)), 100)

    def test_zero_value_fails_open(self):
        # 0 is treated as "no data" (falsy), same as None. Pinned so a change
        # to fail-closed is a deliberate decision, not an accident.
        assert_approved(run(make_intent(avg_traded_value=0.0)), 100)


# ── #5 per-trade risk cap ────────────────────────────────────────────────────

class TestPerTradeRisk:
    """Concentration cap is opened to 100% here so it doesn't mask the cap
    under test (₹25 x 1,000 = ₹25k would otherwise sit right at the edge)."""

    @pytest.fixture(autouse=True)
    def _open_concentration(self, monkeypatch):
        monkeypatch.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 100.0)

    def test_oversize_order_downsized_to_budget(self):
        # ₹1/share risk, ₹1,000 budget -> 1,000 shares max
        res = run(make_intent(entry_price=25.0, stop_price=24.0, qty=2_000))
        assert_approved(res, 1_000, "sized_down")
        assert "per-trade risk cap" in res.reason

    def test_downsize_floors_never_rounds_up(self):
        # ₹1,000 / ₹3 = 333.33 -> 333
        res = run(make_intent(entry_price=100.0, stop_price=97.0, qty=1_000))
        assert_approved(res, 333, "sized_down")

    def test_order_exactly_at_budget_not_downsized(self):
        res = run(make_intent(entry_price=25.0, stop_price=24.0, qty=1_000))
        assert_approved(res, 1_000, "all_checks_passed")

    def test_single_share_over_budget_rejected(self):
        # 1 share risks ₹1,100 > ₹1,000 budget
        res = run(make_intent(entry_price=2_000.0, stop_price=900.0, qty=1))
        assert_rejected(res, "per_trade_risk_cap")
        assert "Even 1 share" in res.reason

    def test_never_upsizes(self):
        res = run(make_intent(entry_price=25.0, stop_price=24.0, qty=10))
        assert_approved(res, 10)

    # adj_risk_pct (conviction-adjusted sizing, 2026-09-01 fix)
    def test_adj_risk_pct_upsize_survives(self):
        # base 1% would cut 1,500 -> 1,000; adj 2% (₹2,000 budget) keeps 1,500
        intent = make_intent(entry_price=25.0, stop_price=24.0, qty=1_500)
        assert_approved(run(intent), 1_000, "sized_down")  # without adj: clawed back
        intent = make_intent(entry_price=25.0, stop_price=24.0, qty=1_500, adj_risk_pct=2.0)
        assert_approved(run(intent), 1_500, "all_checks_passed")

    def test_adj_risk_pct_downsize_applies(self):
        intent = make_intent(entry_price=25.0, stop_price=24.0, qty=1_000, adj_risk_pct=0.5)
        assert_approved(run(intent), 500, "sized_down")

    def test_adj_risk_pct_zero_is_honoured_not_treated_as_unset(self):
        # `is not None` check: 0.0 means a zero budget, not "use the default".
        intent = make_intent(entry_price=25.0, stop_price=24.0, qty=10, adj_risk_pct=0.0)
        assert_rejected(run(intent), "per_trade_risk_cap")

    def test_stop_equals_entry_branch_only_reachable_with_negative_equity(self):
        # Documents the one way the "Stop price equals entry price" branch
        # fires today: order_risk (0) > max_trade_risk requires a negative budget.
        acct = make_account(equity=-1_000.0)
        res = run(make_intent(entry_price=100.0, stop_price=100.0), acct)
        assert_rejected(res, "per_trade_risk_cap")
        assert "equals entry" in res.reason


# ── #5b cash cap ─────────────────────────────────────────────────────────────

class TestCashCap:
    def test_downsized_to_available_cash(self):
        # ₹5,000 cash @ ₹100 -> 50 shares
        res = run(account=make_account(cash_available=5_000.0))
        assert_approved(res, 50, "sized_down")
        assert "cash available" in res.reason

    def test_cash_floor_division(self):
        res = run(account=make_account(cash_available=5_099.0))
        assert_approved(res, 50, "sized_down")

    def test_not_enough_for_one_share_rejected(self):
        res = run(account=make_account(cash_available=99.0))
        assert_rejected(res, "cash_available_cap")

    def test_zero_cash_rejected(self):
        res = run(account=make_account(cash_available=0.0))
        assert_rejected(res, "cash_available_cap")

    def test_cash_exactly_equal_to_cost_not_downsized(self):
        res = run(account=make_account(cash_available=10_000.0))
        assert_approved(res, 100, "all_checks_passed")

    def test_cash_cap_applies_after_risk_cap(self, monkeypatch):
        monkeypatch.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 100.0)
        # risk cap: 2,000 -> 1,000 shares (₹25k); cash ₹10k -> 400 shares
        intent = make_intent(entry_price=25.0, stop_price=24.0, qty=2_000)
        res = run(intent, make_account(cash_available=10_000.0))
        assert_approved(res, 400, "sized_down")
        assert "cash available" in res.reason


# ── #5b-ii capital-share cap ─────────────────────────────────────────────────

class TestCapitalShareCap:
    def test_rejects_when_projected_exposure_exceeds_share(self):
        # total = 50k + 40k = 90k -> 50% = 45k; projected = 40k + 10k = 50k
        acct = make_account(broker_cash_available=50_000.0, open_positions_market_value=40_000.0)
        assert_rejected(run(account=acct), "capital_share_cap")

    def test_other_service_holdings_count_toward_total(self):
        # Regression for the 2026-09-20 audit fix: same numbers as above, but
        # position-stocks-service holds ₹60k -> total 150k, cap 75k, 50k fits.
        acct = make_account(
            broker_cash_available=50_000.0,
            open_positions_market_value=40_000.0,
            other_service_open_positions_market_value=60_000.0,
        )
        assert_approved(run(account=acct), 100)

    def test_projected_exactly_at_share_passes(self):
        # total 100k -> cap 50k; open 40k + cost 10k = 50k (not >)
        acct = make_account(broker_cash_available=60_000.0, open_positions_market_value=40_000.0)
        assert_approved(run(account=acct), 100)

    def test_rejects_rather_than_downsizes(self):
        acct = make_account(broker_cash_available=50_000.0, open_positions_market_value=40_000.0)
        res = run(account=acct)
        assert res.verdict == RiskVerdict.REJECTED and res.approved_qty is None

    def test_broker_cash_unpopulated_with_open_positions_fails_closed(self):
        # broker_cash defaults to 0.0: total = open MV only, so any new order
        # pushes exposure past 50% of that -> reject.
        acct = make_account(broker_cash_available=0.0, open_positions_market_value=10_000.0)
        assert_rejected(run(account=acct), "capital_share_cap")

    def test_zero_total_shared_value_skips_check__CURRENT_BEHAVIOUR(self):
        # AccountState's comment says unpopulated capital fields "fail closed",
        # but when broker cash, own positions and other-service positions are
        # ALL zero the `total > 0` guard skips the check entirely (fail OPEN).
        # cash_available (5b) still applies, so it is bounded — but it is not
        # what the comment promises. Pinned here so changing it is deliberate.
        acct = make_account(
            broker_cash_available=0.0,
            open_positions_market_value=0.0,
            other_service_open_positions_market_value=0.0,
        )
        assert_approved(run(account=acct), 100)


# ── #5c concentration cap ────────────────────────────────────────────────────

class TestConcentrationCap:
    def test_downsized_to_25_percent_of_equity(self):
        # ₹100 x 300 = ₹30k > ₹25k cap -> 250 shares
        res = run(make_intent(entry_price=100.0, stop_price=99.0, qty=300))
        assert_approved(res, 250, "sized_down")
        assert "single-position cap" in res.reason

    def test_exactly_at_cap_passes(self):
        res = run(make_intent(entry_price=100.0, stop_price=99.0, qty=250))
        assert_approved(res, 250, "all_checks_passed")

    def test_single_share_over_cap_rejected(self, monkeypatch):
        # Lift the price ceiling so ₹26k reaches check 5c (cap is ₹25k).
        monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", True)
        monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 100_000.0)
        res = run(make_intent(entry_price=26_000.0, stop_price=25_900.0, qty=1))
        assert_rejected(res, "position_concentration_cap")

    def test_order_risk_recomputed_after_concentration_downsize(self):
        # Original order risk ₹300, downsized ₹250. Open risk ₹4,740:
        #   4,740 + 300 = 5,040 > ₹5,000 cap  (stale risk would reject)
        #   4,740 + 250 = 4,990 <= ₹5,000     (recomputed risk passes)
        acct = make_account(open_positions_total_risk=4_740.0)
        res = run(make_intent(entry_price=100.0, stop_price=99.0, qty=300), acct)
        assert_approved(res, 250, "sized_down")


# ── #6 portfolio risk ────────────────────────────────────────────────────────

class TestPortfolioRisk:
    def test_over_cap_rejected(self):
        # 4,900 open + ₹200 new = 5,100 > ₹5,000
        res = run(account=make_account(open_positions_total_risk=4_900.0))
        assert_rejected(res, "max_portfolio_risk")

    def test_exactly_at_cap_passes(self):
        res = run(account=make_account(open_positions_total_risk=4_800.0))
        assert_approved(res, 100)


# ── #7 pyramiding ────────────────────────────────────────────────────────────

class TestPyramiding:
    def test_second_entry_in_same_symbol_rejected(self):
        acct = make_account(open_position_symbols={"TESTCO"}, open_position_count=1)
        assert_rejected(run(account=acct), "no_pyramiding")

    def test_allowed_when_enabled(self):
        acct = make_account(open_position_symbols={"TESTCO"}, open_position_count=1,
                            allow_pyramiding=True)
        assert_approved(run(account=acct), 100)

    def test_other_symbol_unaffected(self):
        acct = make_account(open_position_symbols={"OTHERCO"}, open_position_count=1)
        assert_approved(run(account=acct), 100)


# ── #8 stale data ────────────────────────────────────────────────────────────

class TestStaleData:
    def test_older_than_cap_rejected(self):
        ts = NOW - timedelta(seconds=31)
        assert_rejected(run(make_intent(market_data_timestamp=ts)), "stale_market_data")

    def test_exactly_at_cap_passes(self):
        ts = NOW - timedelta(seconds=30)
        assert_approved(run(make_intent(market_data_timestamp=ts)), 100)

    def test_fresh_passes(self):
        assert_approved(run(make_intent(market_data_timestamp=NOW)), 100)

    def test_missing_timestamp_skips_check(self):
        assert_approved(run(make_intent(market_data_timestamp=None)), 100)

    def test_future_timestamp_passes(self):
        ts = NOW + timedelta(seconds=5)
        assert_approved(run(make_intent(market_data_timestamp=ts)), 100)

    def test_sell_ignores_stale_data(self):
        ts = NOW - timedelta(hours=1)
        assert_approved(run(make_intent(side="SELL", market_data_timestamp=ts)), 100)


# ── #9 abnormal tick volatility ──────────────────────────────────────────────

class TestAbnormalVolatility:
    def test_spike_beyond_multiple_rejected(self):
        # threshold = 2.0% ATR x 2.0 = 4.0%
        res = run(make_intent(recent_atr_pct=2.0, latest_tick_move_pct=5.0))
        assert_rejected(res, "abnormal_volatility")

    def test_negative_spike_rejected(self):
        res = run(make_intent(recent_atr_pct=2.0, latest_tick_move_pct=-5.0))
        assert_rejected(res, "abnormal_volatility")

    def test_exactly_at_threshold_passes(self):
        assert_approved(run(make_intent(recent_atr_pct=2.0, latest_tick_move_pct=4.0)), 100)

    def test_no_atr_skips_check(self):
        assert_approved(run(make_intent(recent_atr_pct=None, latest_tick_move_pct=50.0)), 100)
        assert_approved(run(make_intent(recent_atr_pct=0.0, latest_tick_move_pct=50.0)), 100)

    def test_no_tick_move_skips_check(self):
        assert_approved(run(make_intent(recent_atr_pct=2.0, latest_tick_move_pct=None)), 100)

    def test_zero_tick_move_is_evaluated_not_skipped(self):
        assert_approved(run(make_intent(recent_atr_pct=2.0, latest_tick_move_pct=0.0)), 100)

    def test_still_applies_to_sell(self):
        res = run(make_intent(side="SELL", recent_atr_pct=2.0, latest_tick_move_pct=9.0))
        assert_rejected(res, "abnormal_volatility")


# ── SELL side: exits must not be blocked by entry-only checks ────────────────

class TestSellSideBypass:
    def test_sell_bypasses_every_entry_only_check_at_once(self):
        """One SELL that would trip nearly every BUY check still approves:
        paused, over daily-loss cap, at position cap, sub-floor price, illiquid,
        oversize, no cash, over capital share, already-held symbol."""
        acct = make_account(
            trading_globally_paused=True,
            realized_pnl_today=-50_000.0,
            open_position_count=9,
            open_position_symbols={"TESTCO"},
            open_positions_total_risk=99_999.0,
            cash_available=0.0,
            broker_cash_available=0.0,
            open_positions_market_value=500_000.0,
        )
        intent = make_intent(side="SELL", entry_price=5.0, stop_price=4.0, qty=50_000,
                             avg_traded_value=1.0,
                             market_data_timestamp=NOW - timedelta(days=1))
        assert_approved(run(intent, acct), 50_000, "all_checks_passed")

    def test_sell_qty_never_altered(self):
        res = run(make_intent(side="SELL", entry_price=25.0, stop_price=24.0, qty=999_999))
        assert_approved(res, 999_999)

    def test_sell_above_price_ceiling_allowed(self):
        res = run(make_intent(side="SELL", entry_price=90_000.0, stop_price=89_000.0, qty=1))
        assert_approved(res, 1)


# ── Result object ────────────────────────────────────────────────────────────

def test_default_now_is_used_when_not_supplied():
    # Exercises the `now or datetime.now(...)` default path.
    res = evaluate(make_intent(market_data_timestamp=datetime.now(timezone.utc)), make_account())
    assert_approved(res, 100)


def test_verdict_enum_values_are_stable():
    # Serialised into API responses / DB rows — a rename is a breaking change.
    assert RiskVerdict.APPROVED.value == "approved"
    assert RiskVerdict.REJECTED.value == "rejected"
    assert RiskVerdict.BLOCKED_GLOBAL.value == "blocked_global"


# ── Known gaps (strict xfail) ────────────────────────────────────────────────

class TestKnownGaps:
    """The engine calls itself the "absolute veto authority", but it takes
    entry_price/stop_price/qty on trust: manual_engine.py validates them first
    (qty > 0, stop < entry), and entry_engine derives the stop from a % so it
    is always below entry — so live order flow is currently protected UPSTREAM.
    Anything that calls evaluate() directly (the admin POST /risk-engine/check
    endpoint, a future caller) gets no such protection.

    Each test states the CORRECT behaviour and is xfail(strict=True): it passes
    as an expected failure today, and turns red (XPASS) as soon as the engine
    is fixed — at which point delete the marker."""

    @pytest.mark.xfail(strict=True, reason="BUY with stop ABOVE entry is approved: risk uses abs()")
    def test_buy_with_stop_above_entry_should_be_rejected(self):
        res = run(make_intent(entry_price=100.0, stop_price=105.0))
        assert res.verdict != RiskVerdict.APPROVED

    @pytest.mark.xfail(strict=True, reason="BUY with stop == entry is approved (zero risk distance)")
    def test_buy_with_stop_equal_to_entry_should_be_rejected(self):
        res = run(make_intent(entry_price=100.0, stop_price=100.0))
        assert res.verdict != RiskVerdict.APPROVED

    @pytest.mark.xfail(strict=True, reason="qty=0 BUY is approved with approved_qty=0")
    def test_zero_qty_buy_should_be_rejected(self):
        res = run(make_intent(qty=0))
        assert res.verdict != RiskVerdict.APPROVED

    @pytest.mark.xfail(strict=True, reason="negative qty BUY is approved")
    def test_negative_qty_buy_should_be_rejected(self):
        res = run(make_intent(qty=-10))
        assert res.verdict != RiskVerdict.APPROVED
