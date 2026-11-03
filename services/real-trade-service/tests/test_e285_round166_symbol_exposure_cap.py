"""E285 round 166: with pyramiding ON, an add counts what the symbol already holds (ITC 6 + 8 = 14 shares on 3 Nov). Stdlib + risk_engine only."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from risk_engine import engine  # noqa: E402
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate  # noqa: E402

NOW = datetime(2026, 9, 21, 5, 0, 0, tzinfo=timezone.utc)   # a Monday, market hours


def _pin(mp):
    mp.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    mp.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    mp.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    mp.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    mp.setattr(engine, "HARD_FLOOR_LIQUIDITY", 5_000_000.0)
    mp.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    mp.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 25.0)
    mp.setattr(engine, "SYMBOL_EXPOSURE_CAP", True)


def _acct(**over) -> AccountState:
    base = dict(
        equity=100_000.0, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_concurrent_positions=5, max_portfolio_risk_pct=5.0,
        stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=True, realized_pnl_today=0.0, open_position_count=0,
        open_position_symbols=set(), open_positions_total_risk=0.0, trading_globally_paused=False, market_is_open=True,
        cash_available=100_000.0, broker_cash_available=200_000.0, open_positions_market_value=0.0, other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def _intent(**over) -> OrderIntent:
    base = dict(mode="DEMO", symbol="TESTCO", side="BUY", qty=100, entry_price=100.0, stop_price=98.0)
    base.update(over)
    return OrderIntent(**base)


def test_add_is_sized_down_to_the_room_left(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(symbol_held_value=20_000.0), now=NOW)      # cap 25k, holds 20k, wants 10k more
    assert res.verdict == RiskVerdict.APPROVED and res.check_name == "sized_down" and res.approved_qty == 50


def test_no_room_rejects(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(symbol_held_value=25_000.0), now=NOW)
    assert res.verdict == RiskVerdict.REJECTED and res.check_name == "symbol_exposure_cap" and res.approved_qty is None


def test_add_that_fits_is_unchanged(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(symbol_held_value=10_000.0), now=NOW)      # 10k + 10k = 20k <= 25k
    assert res.verdict == RiskVerdict.APPROVED and res.approved_qty == 100


def test_nothing_held_is_the_old_maths(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(), now=NOW)
    assert res.verdict == RiskVerdict.APPROVED and res.approved_qty == 100 and res.check_name == "all_checks_passed"


def test_pyramiding_off_leaves_the_step_out(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(allow_pyramiding=False, symbol_held_value=25_000.0), now=NOW)
    assert res.check_name != "symbol_exposure_cap" and res.verdict == RiskVerdict.APPROVED   # held value is only read when pyramiding is on


def test_switch_off_is_the_old_maths(monkeypatch):
    _pin(monkeypatch)
    monkeypatch.setattr(engine, "SYMBOL_EXPOSURE_CAP", False)
    res = evaluate(_intent(), _acct(symbol_held_value=25_000.0), now=NOW)
    assert res.verdict == RiskVerdict.APPROVED and res.approved_qty == 100


def test_smaller_flat_rupee_cap_wins(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(), _acct(symbol_held_value=8_000.0, max_trade_value=12_000.0), now=NOW)   # cap 12k, holds 8k, room 4k
    assert res.verdict == RiskVerdict.APPROVED and res.approved_qty == 40


def test_sell_is_never_limited(monkeypatch):
    _pin(monkeypatch)
    res = evaluate(_intent(side="SELL"), _acct(symbol_held_value=25_000.0), now=NOW)
    assert res.verdict == RiskVerdict.APPROVED and res.approved_qty == 100


def test_entry_fills_the_held_value_before_the_risk_check():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "account_state.symbol_held_value" in src
    assert src.index("account_state.symbol_held_value") < src.index("result = risk_evaluate(intent, account_state)")
