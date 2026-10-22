"""A12 (2026-10-22): max entries per day (risk_engine check 4-day + portfolio.today_entry_count)
and A9: structure gate "log" mode (config._sg_mode + structure_gate.mode_for).

Run from services/real-trade-service:  python3 -m pytest tests/test_a12_max_entries_per_day.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import portfolio.portfolio as pf
from entry_engine import structure_gate
from risk_engine import engine
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate

NOW = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)  # 10:30 IST, market hours


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "LOSS_STREAK_MAX", 3)
    monkeypatch.setattr(engine, "MAX_ENTRIES_PER_DAY", 6)


def _account(**over):
    base = dict(
        equity=100_000.0, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_concurrent_positions=3,
        max_portfolio_risk_pct=5.0, stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=False,
        realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(), open_positions_total_risk=0.0,
        trading_globally_paused=False, market_is_open=True, cash_available=100_000.0,
        broker_cash_available=200_000.0, open_positions_market_value=0.0,
        other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def _intent(**over):
    base = dict(mode="REAL", symbol="TESTCO", side="BUY", qty=100, entry_price=100.0, stop_price=98.0)
    base.update(over)
    return OrderIntent(**base)


class TestEngineEntryCap:
    def test_default_account_state_never_triggers(self):
        assert evaluate(_intent(), _account(), now=NOW).verdict == RiskVerdict.APPROVED

    @pytest.mark.parametrize("n", [0, 3, 5])
    def test_below_cap_passes(self, n):
        assert evaluate(_intent(), _account(entries_today=n), now=NOW).verdict == RiskVerdict.APPROVED

    @pytest.mark.parametrize("n", [6, 9])
    def test_at_or_above_cap_rejects(self, n):
        res = evaluate(_intent(), _account(entries_today=n), now=NOW)
        assert res.verdict == RiskVerdict.REJECTED and res.check_name == "max_entries_per_day"

    def test_sell_is_never_capped(self):
        res = evaluate(_intent(side="SELL"), _account(entries_today=50, open_position_count=1, open_position_symbols={"TESTCO"}), now=NOW)
        assert res.check_name != "max_entries_per_day"

    def test_zero_disables_cap(self, monkeypatch):
        monkeypatch.setattr(engine, "MAX_ENTRIES_PER_DAY", 0)
        assert evaluate(_intent(), _account(entries_today=99), now=NOW).verdict == RiskVerdict.APPROVED


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _pos(db, opened, mode="REAL", imported=False, status="OPEN"):
    db.add(models.TradePosition(
        mode=mode, symbol="X", status=status, qty_open=1, avg_entry_price=100.0,
        opened_at=opened.replace(tzinfo=None), broker_imported=imported,
    ))
    db.commit()


class TestTodayEntryCount:
    def test_counts_today_open_and_closed_for_the_mode_only(self, db):
        _pos(db, NOW - timedelta(hours=1))
        _pos(db, NOW - timedelta(hours=2), status="CLOSED")
        _pos(db, NOW - timedelta(hours=1), mode="DEMO")
        assert pf.today_entry_count(db, "REAL", NOW) == 2

    def test_ignores_yesterday_and_broker_imports(self, db):
        _pos(db, NOW - timedelta(days=1))
        _pos(db, NOW - timedelta(hours=1), imported=True)
        assert pf.today_entry_count(db, "REAL", NOW) == 0

    def test_error_means_no_cap(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert pf.today_entry_count(Boom(), "REAL", NOW) == 0


class TestStructureGateLogMode:
    def test_shipped_default_is_log(self, monkeypatch):
        monkeypatch.delenv("STRUCTURE_GATE_MODE", raising=False)
        assert config._sg_mode("STRUCTURE_GATE_MODE", "log") == "log"

    @pytest.mark.parametrize("raw,expected", [("log", "log"), ("OFF", "off"), ("enforce", "enforce"), ("shadow", "enforce")])
    def test_env_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv("STRUCTURE_GATE_MODE", raw)
        assert config._sg_mode("STRUCTURE_GATE_MODE", "log") == expected

    @pytest.mark.parametrize("val,expected", [("log", "log"), ("off", "off"), ("enforce", "enforce")])
    def test_mode_for(self, monkeypatch, val, expected):
        monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", val)
        monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", val)
        assert structure_gate.mode_for("REAL") == expected and structure_gate.mode_for("DEMO") == expected
