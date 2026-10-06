"""
tests/test_group193_share_cap_total.py — group 193: the 50% share-cap total no longer shrinks.

Symptom: almost every real-trade candidate was held at WAIT by "capital_share_cap". The cap compares this
service's exposure with 50% of (Dhan free cash + this service's exposure + position-stocks-service's exposure).
Two things made that total too small, so the cap too tight:

  1. A BUY that Dhan is still working is not a TradePosition yet (reconcile books it on fill), but Dhan already
     blocks its cash, so free cash fell while the order was counted nowhere — total shrank, exposure was
     undercounted. AccountState.in_flight_buy_value / shared_exposure.get_in_flight_buy_value close that.
  2. position-stocks-service's published exposure could be missing or stale (covered in that service's tests).
     The reject message now shows every component and the peer figure's age so this is visible in the log.

    cd services/real-trade-service
    python -m pytest tests/test_group193_share_cap_total.py -v
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from execution import shared_exposure as se
from models import SharedServiceExposure, TradeOrder
from risk_engine import engine
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate

NOW = datetime(2026, 9, 21, 5, 0, 0, tzinfo=timezone.utc)
LOGGER = "real-trade-shared-exposure"


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 100.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 100_000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", False)
    monkeypatch.setattr(engine, "HARD_FLOOR_LIQUIDITY", 0.0)
    monkeypatch.delenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES", raising=False)
    monkeypatch.setattr(se, "_last_stale_warn_ts", None)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _acct(**over) -> AccountState:
    base = dict(
        equity=1_000_000.0, risk_per_trade_pct=5.0, max_daily_loss_pct=50.0, max_concurrent_positions=50,
        max_portfolio_risk_pct=100.0, stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=False,
        realized_pnl_today=0.0, open_position_count=0, open_position_symbols=set(), open_positions_total_risk=0.0,
        trading_globally_paused=False, market_is_open=True, cash_available=1_000_000.0,
        broker_cash_available=0.0, open_positions_market_value=0.0, other_service_open_positions_market_value=0.0,
    )
    base.update(over)
    return AccountState(**base)


def _buy(cost: float) -> OrderIntent:
    # 10 shares so cost = 10 * price; tight 1% stop keeps the per-trade risk checks out of the way.
    price = cost / 10.0
    return OrderIntent(mode="REAL", symbol="TESTCO", side="BUY", qty=10, entry_price=price, stop_price=price * 0.99)


def _run(cost, **acct):
    return evaluate(_buy(cost), _acct(**acct), now=NOW)


# ───────────────────────── engine: in-flight BUYs count ─────────────────────────

class TestInFlightBuysInShareCap:
    def test_default_zero_keeps_old_behaviour(self):
        # free 10,000 + own 10,072 + peer 8,000 = 28,072 -> cap 14,036; 10,072 + 1,500 fits.
        res = _run(1_500, broker_cash_available=10_000.0, open_positions_market_value=10_072.0,
                   other_service_open_positions_market_value=8_000.0)
        assert res.verdict == RiskVerdict.APPROVED

    def test_in_flight_buys_are_counted_as_exposure(self):
        # Same account, but 6,000 of this service's BUYs are still working at Dhan (cash already blocked).
        # exposure 16,072; total 10,000 + 16,072 + 8,000 = 34,072 -> cap 17,036; 16,072 + 1,500 = 17,572 > cap.
        res = _run(1_500, broker_cash_available=10_000.0, open_positions_market_value=10_072.0,
                   other_service_open_positions_market_value=8_000.0, in_flight_buy_value=6_000.0)
        assert res.verdict == RiskVerdict.REJECTED and res.check_name == "capital_share_cap"

    def test_in_flight_value_restores_the_total_so_a_small_order_still_fits(self):
        # 4,000 in flight: exposure 14,072, total 32,072 -> cap 16,036; 14,072 + 1,000 = 15,072 fits.
        # Without crediting the in-flight cash back the total would be 28,072 (cap 14,036) and this would reject.
        res = _run(1_000, broker_cash_available=10_000.0, open_positions_market_value=10_072.0,
                   other_service_open_positions_market_value=8_000.0, in_flight_buy_value=4_000.0)
        assert res.verdict == RiskVerdict.APPROVED
        shrunk = _run(1_000, broker_cash_available=10_000.0, open_positions_market_value=14_072.0,
                      other_service_open_positions_market_value=8_000.0)
        assert shrunk.verdict == RiskVerdict.APPROVED  # 14,072 own counted via positions behaves the same

    def test_reject_message_shows_every_component_and_peer_age(self):
        res = _run(5_000, broker_cash_available=10_000.0, open_positions_market_value=10_072.0,
                   other_service_open_positions_market_value=0.0, in_flight_buy_value=1_000.0,
                   other_service_exposure_age_s=42.4)
        assert res.check_name == "capital_share_cap"
        r = res.reason
        assert "₹10,072.00 open" in r and "₹1,000.00 in-flight BUYs" in r
        assert "₹10,000.00 free cash" in r and "₹0.00 position-stocks-service" in r
        assert "published 42s ago" in r

    def test_reject_message_flags_a_missing_peer_figure(self):
        res = _run(5_000, broker_cash_available=10_000.0, open_positions_market_value=10_072.0,
                   other_service_exposure_age_s=None)
        assert "no published figure" in res.reason

    def test_sell_side_is_never_blocked_by_in_flight_buys(self):
        sell = OrderIntent(mode="REAL", symbol="TESTCO", side="SELL", qty=10, entry_price=100.0, stop_price=99.0)
        res = evaluate(sell, _acct(broker_cash_available=1.0, open_positions_market_value=10_000.0,
                                   in_flight_buy_value=50_000.0), now=NOW)
        assert res.check_name != "capital_share_cap"


# ─────────────────────── shared_exposure.get_in_flight_buy_value ───────────────────────

def _order(db, *, side="BUY", status="PLACED", qty=10, price=100.0, filled=0, mode="REAL", age_min=1.0):
    db.add(TradeOrder(
        mode=mode, symbol="X", side=side, qty=qty, limit_price=price, status=status,
        filled_qty_so_far=filled, created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    ))
    db.commit()


class TestGetInFlightBuyValue:
    def test_counts_placed_pending_and_partial_buys(self, db):
        _order(db, status="PLACED", qty=10, price=100.0)                 # 1,000
        _order(db, status="PENDING", qty=5, price=200.0)                 # 1,000
        _order(db, status="PARTIAL", qty=10, price=50.0, filled=4)       # 6 remaining -> 300
        assert se.get_in_flight_buy_value(db, "REAL") == pytest.approx(2_300.0)

    def test_terminal_sell_demo_and_unpriced_orders_are_ignored(self, db):
        _order(db, status="FILLED")
        _order(db, status="CANCELLED")
        _order(db, status="REJECTED")
        _order(db, status="EXPIRED")
        _order(db, side="SELL")
        _order(db, mode="DEMO")
        _order(db, price=0.0)
        _order(db, status="PARTIAL", qty=10, price=100.0, filled=10)     # nothing left to fill
        assert se.get_in_flight_buy_value(db, "REAL") == 0.0

    def test_non_real_mode_is_zero(self, db):
        _order(db)
        assert se.get_in_flight_buy_value(db, "DEMO") == 0.0

    def test_orders_older_than_the_window_are_ignored(self, db):
        _order(db, age_min=121.0)
        _order(db, age_min=119.0, qty=10, price=10.0)                    # 100
        assert se.get_in_flight_buy_value(db, "REAL") == pytest.approx(100.0)

    def test_window_is_configurable_and_zero_disables(self, db, monkeypatch):
        _order(db, age_min=30.0)
        monkeypatch.setenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES", "10")
        assert se.get_in_flight_buy_value(db, "REAL") == 0.0
        monkeypatch.setenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES", "0")
        assert se.get_in_flight_buy_value(db, "REAL") == 0.0
        monkeypatch.setenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES", "60")
        assert se.get_in_flight_buy_value(db, "REAL") == pytest.approx(1_000.0)

    @pytest.mark.parametrize("raw", ["abc", "-5", " "])
    def test_bad_window_values_fall_back_to_default(self, db, monkeypatch, raw):
        _order(db, age_min=30.0)
        monkeypatch.setenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES", raw)
        assert se.get_in_flight_buy_value(db, "REAL") == pytest.approx(1_000.0)

    def test_db_error_is_fail_open_and_logged(self, caplog):
        bad = MagicMock()
        bad.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_in_flight_buy_value(bad, "REAL") == 0.0
        assert "in-flight BUY value" in caplog.text


# ───────────────────── shared_exposure.get_other_service_exposure_age ─────────────────────

class TestPeerExposureAge:
    def _seed(self, db, age_s):
        db.add(SharedServiceExposure(
            service_name=se.OTHER_SERVICE_NAME, open_positions_market_value=5.0,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        ))
        db.commit()

    def test_no_row_is_none_and_warns_once(self, db, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure_age(db) is None
            assert se.get_other_service_exposure_age(db) is None
        assert caplog.text.count("exposure is missing") == 1  # throttled

    def test_fresh_row_returns_age_without_warning(self, db, caplog):
        self._seed(db, 30)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            age = se.get_other_service_exposure_age(db)
        assert 29 <= age <= 40
        assert "exposure is" not in caplog.text

    def test_old_row_warns(self, db, caplog):
        self._seed(db, 3_600)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            age = se.get_other_service_exposure_age(db)
        assert age >= 3_599 and "s old" in caplog.text

    def test_warning_repeats_after_the_throttle_window(self, db, caplog, monkeypatch):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            se.get_other_service_exposure_age(db)
            monkeypatch.setattr(se, "_last_stale_warn_ts", se.time.monotonic() - 10_000)
            se.get_other_service_exposure_age(db)
        assert caplog.text.count("exposure is missing") == 2

    def test_row_without_timestamp_is_none(self):
        # updated_at is NOT NULL in the schema, so only a non-DB-backed row can lack it.
        fake = MagicMock()
        fake.query.return_value.filter_by.return_value.first.return_value = MagicMock(updated_at=None)
        assert se.get_other_service_exposure_age(fake) is None

    def test_db_error_is_none_and_logged(self, caplog):
        bad = MagicMock()
        bad.query.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert se.get_other_service_exposure_age(bad) is None
        assert "exposure age" in caplog.text


# ───────────────────────── publish heartbeat (this service's copy) ─────────────────────────

def test_publish_refreshes_updated_at_even_when_the_value_is_unchanged(db):
    se.publish_own_exposure(db, 100.0)
    row = db.query(SharedServiceExposure).filter_by(service_name=se.SERVICE_NAME).first()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    row.updated_at = old
    db.commit()
    se.publish_own_exposure(db, 100.0)  # same value
    row = db.query(SharedServiceExposure).filter_by(service_name=se.SERVICE_NAME).first()
    ts = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
    assert ts > old + timedelta(hours=1)


# ───────────────────────── call sites pass the new fields ─────────────────────────

def test_entry_account_state_carries_in_flight_value_and_peer_age(db, monkeypatch):
    from entry_engine import entry
    monkeypatch.setattr("execution.equity_sync.sync_real_equity", lambda d: None)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure", lambda d: 321.0)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure_age", lambda d: 12.5)
    monkeypatch.setattr(entry.shared_exposure, "get_in_flight_buy_value", lambda d, m: 777.0)
    db.add(models.TradeGateState(mode="REAL"))
    db.add(models.TradeRiskConfig(mode="REAL"))
    db.add(models.TradeAccount(mode="REAL"))
    db.commit()
    a = entry._account_state(db, "REAL", gate_armed=True)
    assert a.in_flight_buy_value == 777.0
    assert a.other_service_exposure_age_s == 12.5
    assert a.other_service_open_positions_market_value == 321.0


def test_demo_mode_never_reads_the_shared_exposure_fields(db, monkeypatch):
    from entry_engine import entry

    def boom(*a, **k):
        raise AssertionError("must not be called for DEMO")

    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure_age", boom)
    monkeypatch.setattr(entry.shared_exposure, "get_other_service_exposure", boom)
    db.add(models.TradeGateState(mode="DEMO"))
    db.add(models.TradeRiskConfig(mode="DEMO"))
    db.add(models.TradeAccount(mode="DEMO"))
    db.commit()
    a = entry._account_state(db, "DEMO", gate_armed=True)
    assert a.in_flight_buy_value == 0.0 and a.other_service_exposure_age_s is None
