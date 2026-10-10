"""group297: a BUY order still working at the broker counts as exposure for the no-pyramiding check."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import manual_engine
import models
from entry_engine import entry
from execution import equity_sync
from portfolio import portfolio
from tz_utils import ist_today_str


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENTRY_BLOCK_WORKING_BUY_DUP", raising=False)
    monkeypatch.delenv("ENTRY_WORKING_BUY_MAX_AGE_HOURS", raising=False)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def order(db, symbol="AAA", side="BUY", status="PLACED", mode="REAL", age_h=0.1):
    db.add(models.TradeOrder(mode=mode, symbol=symbol, side=side, qty=5, status=status,
                             created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=age_h),
                             updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()


@pytest.mark.parametrize("status", ["PLACED", "PARTIAL"])
def test_a_working_buy_counts(db, status):
    order(db, status=status)
    assert portfolio.working_buy_symbols(db, "REAL") == {"AAA"}


@pytest.mark.parametrize("status", ["FILLED", "REJECTED", "EXPIRED", "CANCELLED"])
def test_a_finished_buy_does_not(db, status):
    order(db, status=status)
    assert portfolio.working_buy_symbols(db, "REAL") == set()


def test_a_sell_other_mode_and_old_orders_do_not(db):
    order(db, "S", side="SELL")
    order(db, "D", mode="DEMO")
    order(db, "OLD", age_h=30)
    order(db, "NEW", age_h=2)
    assert portfolio.working_buy_symbols(db, "REAL") == {"NEW"}
    assert portfolio.working_buy_symbols(db, "DEMO") == {"D"}


def test_the_age_limit_is_configurable_and_blank_safe(db, monkeypatch):
    order(db, "OLD", age_h=30)
    monkeypatch.setenv("ENTRY_WORKING_BUY_MAX_AGE_HOURS", "48")
    assert portfolio.working_buy_symbols(db, "REAL") == {"OLD"}
    for raw in ("", "  ", "x", "-1", "0"):
        monkeypatch.setenv("ENTRY_WORKING_BUY_MAX_AGE_HOURS", raw)
        assert portfolio.working_buy_symbols(db, "REAL") == set()          # falls back to 24 h
        assert portfolio._working_buy_max_age_hours() == 24.0


@pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
def test_switch_off_returns_nothing(db, monkeypatch, raw):
    order(db)
    monkeypatch.setenv("ENTRY_BLOCK_WORKING_BUY_DUP", raw)
    assert portfolio.working_buy_symbols(db, "REAL") == set()


@pytest.mark.parametrize("raw", ["", "1", "yes"])
def test_blank_or_on_keeps_it_on(db, monkeypatch, raw):
    order(db)
    monkeypatch.setenv("ENTRY_BLOCK_WORKING_BUY_DUP", raw)
    assert portfolio.working_buy_symbols(db, "REAL") == {"AAA"}


def test_a_db_error_fails_open():
    assert portfolio.working_buy_symbols(object(), "REAL") == set()


@pytest.fixture()
def acct(db, monkeypatch):
    monkeypatch.setattr(equity_sync, "sync_real_equity", lambda d: None)
    monkeypatch.setattr(entry, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure", lambda d: 0.0)
    for mode in ("REAL", "DEMO"):
        db.add(models.TradeAccount(mode=mode, starting_capital=100_000.0, current_equity=90_000.0,
                                   cash_available=60_000.0, broker_cash_available=75_000.0,
                                   realized_pnl_today=0.0, realized_pnl_total=0.0, pnl_last_reset_date=ist_today_str()))
        db.add(models.TradeRiskConfig(mode=mode, allow_pyramiding=False))
    db.commit()


def test_entry_account_state_includes_a_working_buy_and_a_held_position(db, acct):
    order(db, "AAA")
    db.add(models.TradePosition(mode="REAL", symbol="BBB", status="OPEN", qty_open=1, avg_entry_price=10.0,
                                current_stop=9.0, realized_pnl=0.0, opened_at=datetime.now(timezone.utc)))
    db.commit()
    a = entry._account_state(db, "REAL", gate_armed=True)
    assert a.open_position_symbols == {"AAA", "BBB"}
    assert a.open_position_count == 1                      # only real positions count towards the position cap


def test_manual_account_state_includes_a_working_buy(db, acct, monkeypatch):
    monkeypatch.setattr(manual_engine, "is_market_open_ist", lambda: True, raising=False)
    order(db, "AAA")
    assert "AAA" in manual_engine._account_state(db, "REAL", gate_armed=True).open_position_symbols


def _intent(symbol):
    from risk_engine.engine import OrderIntent
    return OrderIntent(mode="REAL", symbol=symbol, side="BUY", qty=1, entry_price=100.0, stop_price=98.0,
                       target_price=105.0, market_data_timestamp=datetime.now(timezone.utc))


def test_the_risk_engine_rejects_a_second_buy_while_the_first_is_working(db, acct):
    from risk_engine.engine import RiskVerdict, evaluate
    order(db, "AAA")
    a = entry._account_state(db, "REAL", gate_armed=True)
    res = evaluate(_intent("AAA"), a)
    assert res.verdict == RiskVerdict.REJECTED and res.check_name == "no_pyramiding"
    assert "BUY order still working" in res.reason
    other = evaluate(_intent("BBB"), a)                    # a different symbol is not stopped by this check
    assert other.check_name != "no_pyramiding"


def test_with_pyramiding_allowed_a_working_buy_does_not_reject(db, acct):
    from risk_engine.engine import evaluate
    order(db, "AAA")
    db.query(models.TradeRiskConfig).update({"allow_pyramiding": True})
    db.commit()
    a = entry._account_state(db, "REAL", gate_armed=True)
    assert evaluate(_intent("AAA"), a).check_name != "no_pyramiding"


def test_once_the_order_is_filled_or_expired_the_symbol_is_free(db, acct):
    from risk_engine.engine import evaluate
    order(db, "AAA")
    db.query(models.TradeOrder).update({"status": "EXPIRED"})
    db.commit()
    a = entry._account_state(db, "REAL", gate_armed=True)
    assert "AAA" not in a.open_position_symbols
    assert evaluate(_intent("AAA"), a).check_name != "no_pyramiding"


def test_the_dry_run_route_sees_a_working_buy(monkeypatch):
    from unittest import mock
    import main
    from test_main_routes_core import _fresh_db, _run, _seed
    db = _fresh_db()
    _seed(db)
    order(db, "RELIANCE", mode="DEMO")
    captured = {}

    def _fake_eval(intent, state):
        captured["symbols"] = state.open_position_symbols
        return mock.Mock(check_name="ok", verdict=mock.Mock(value="APPROVED"), reason="", approved_qty=1)
    with mock.patch.object(main, "risk_evaluate", side_effect=_fake_eval), \
         mock.patch.object(main, "is_market_open_ist", return_value=True):
        _run(main.risk_engine_check(main.RiskCheckRequest(mode="DEMO", symbol="reliance", side="buy", qty=1,
                                                          entry_price=100.0, stop_price=98.0), authorization="", db=db))
    assert captured["symbols"] == {"RELIANCE"}
