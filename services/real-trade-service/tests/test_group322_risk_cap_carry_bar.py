"""group322: per-trade risk never above the daily-loss budget; overnight carry needs a minimum gain."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import market_feed.feed as feed_module
from market_feed.feed import Tick
from execution import auto_pilot
from execution.auto_pilot import _carry_gain_ok, _select_overnight_holds
from risk_engine import engine as risk
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate

NOW = datetime(2026, 9, 21, 5, 0, 0, tzinfo=timezone.utc)


def _acct(**over):
    base = dict(
        equity=100_000.0, risk_per_trade_pct=5.0, max_daily_loss_pct=3.0, max_concurrent_positions=3,
        max_portfolio_risk_pct=50.0, stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=False,
        realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(), open_positions_total_risk=0.0,
        trading_globally_paused=False, market_is_open=True, cash_available=100_000.0, broker_cash_available=200_000.0,
        open_positions_market_value=0.0, other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def _intent(**over):
    base = dict(mode="DEMO", symbol="TESTCO", side="BUY", qty=1000, entry_price=100.0, stop_price=96.0)
    base.update(over)
    return OrderIntent(**base)


@pytest.fixture(autouse=True)
def _floor_off(monkeypatch):
    monkeypatch.setattr(risk, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(risk, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(risk, "ADAPTIVE_MAX_PRICE_STOP_PCT", 6.0)
    monkeypatch.setattr(risk, "HARD_FLOOR_LIQUIDITY", 5_000_000.0)
    monkeypatch.setattr(risk, "CAPITAL_SHARE_PCT", 100.0)
    monkeypatch.setattr(risk, "MAX_POSITION_CONCENTRATION_PCT", 100.0)


def _risk_downsized_qty(res):
    return res.approved_qty


def test_trade_risk_capped_at_daily_loss(monkeypatch):
    monkeypatch.delenv("RISK_CAP_TRADE_RISK_AT_DAILY_LOSS", raising=False)
    # 5% per trade vs 3% daily loss -> 3% of 100k = Rs 3,000 / Rs 4 per share = 750 shares
    res = evaluate(_intent(), _acct(), now=NOW)
    assert res.verdict == RiskVerdict.APPROVED
    assert res.approved_qty == 750


def test_switch_off_restores_old_cap(monkeypatch):
    monkeypatch.setenv("RISK_CAP_TRADE_RISK_AT_DAILY_LOSS", "false")
    # 5% = Rs 5,000 / 4 = 1250 -> the 1000 asked fits
    res = evaluate(_intent(), _acct(), now=NOW)
    assert res.approved_qty == 1000


def test_blank_switch_is_on(monkeypatch):
    monkeypatch.setenv("RISK_CAP_TRADE_RISK_AT_DAILY_LOSS", "  ")
    assert risk._cap_trade_risk_at_daily_loss() is True


def test_cap_not_applied_when_per_trade_already_lower(monkeypatch):
    monkeypatch.delenv("RISK_CAP_TRADE_RISK_AT_DAILY_LOSS", raising=False)
    res = evaluate(_intent(), _acct(risk_per_trade_pct=1.0), now=NOW)   # 1% = Rs 1,000 / 4 = 250
    assert res.approved_qty == 250


def test_zero_daily_loss_pct_does_not_zero_the_cap(monkeypatch):
    monkeypatch.delenv("RISK_CAP_TRADE_RISK_AT_DAILY_LOSS", raising=False)
    res = evaluate(_intent(), _acct(max_daily_loss_pct=0.0), now=NOW)
    # daily-loss check itself may react to 0; the point is the cap must not become 0% (which would size to 0)
    assert res.check_name != "per_trade_risk_cap"


# ---- carry bar -----------------------------------------------------------------------------

def test_carry_gain_bar_values(monkeypatch):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 1.5, raising=False)
    assert _carry_gain_ok(100.0, 101.5) is True
    assert _carry_gain_ok(100.0, 101.49) is False
    assert _carry_gain_ok(0, 105) is False          # unreadable entry -> fail closed
    assert _carry_gain_ok(None, 105) is False
    assert _carry_gain_ok(100.0, "x") is False


def test_carry_bar_zero_is_off(monkeypatch):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 0.0, raising=False)
    assert _carry_gain_ok(100.0, 100.01) is True
    assert _carry_gain_ok(0, 5) is True


def test_config_default_and_blank(monkeypatch):
    import importlib
    monkeypatch.setenv("OVERNIGHT_HOLD_MIN_GAIN_PCT", "")
    assert config._og_min_gain() == 1.5
    monkeypatch.setenv("OVERNIGHT_HOLD_MIN_GAIN_PCT", "abc")
    assert config._og_min_gain() == 1.5
    monkeypatch.setenv("OVERNIGHT_HOLD_MIN_GAIN_PCT", "0")
    assert config._og_min_gain() == 0.0
    monkeypatch.setenv("OVERNIGHT_HOLD_MIN_GAIN_PCT", "-3")
    assert config._og_min_gain() == 0.0


_engine = create_engine("sqlite:///:memory:")
_Session = sessionmaker(bind=_engine)


def _db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    db = _Session()
    db.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0, cash_available=100_000.0))
    db.flush()
    return db


def _select(db, symbol, entry, ltp, hi, lo):
    p = models.TradePosition(mode="REAL", symbol=symbol, status="OPEN", qty_open=10, avg_entry_price=entry,
                             opened_at=datetime.now(timezone.utc), entry_decision_label="VOLUME_SHOCK_HIGH_CONVICTION",
                             entry_conviction_score=70)
    db.add(p)
    db.flush()

    async def _q(symbols, **_kw):
        return {symbol: Tick(symbol=symbol, price=ltp, as_of=datetime.now(timezone.utc), atr=None, source="t",
                             day_high=hi, day_low=lo)}
    feed_module.get_quotes = _q
    keep, _ = asyncio.run(_select_overnight_holds(db, "REAL", [p]))
    return p, keep


def test_small_gain_squares_off_large_gain_carries(monkeypatch):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 1.5, raising=False)
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS", False, raising=False)
    db = _db()
    p, keep = _select(db, "SMALL", 100.0, 100.8, 103.0, 98.0)   # +0.8%, below the bar
    assert keep == set()
    db = _db()
    p, keep = _select(db, "BIG", 100.0, 102.0, 105.0, 98.0)     # +2.0%, range pos 0.57 -> passes the bar and the range cap
    assert p.id in keep
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 0.0, raising=False)
    db = _db()
    p, keep = _select(db, "SMALL2", 100.0, 100.4, 103.0, 98.0)
    assert p.id in keep
