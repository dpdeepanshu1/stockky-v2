"""group 264: entry product routing (CNC vs INTRADAY), DP-aware / MIS-aware cost gate, same-day re-entry guard."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import cost_model
import entry_product as ep
import models

MORNING = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)      # 10:30 IST
LATE = datetime(2026, 10, 9, 9, 40, tzinfo=timezone.utc)        # 15:10 IST


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_PRODUCT_MODE", "auto")           # the shipped default is "cnc" since group 266


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_same_day_exit_label_goes_to_mis(db):
    assert ep.choose_entry_product(db, "REAL", "ABC", "VOLUME_SHOCK", MORNING)[0] == "INTRADAY"


def test_overnight_eligible_label_stays_cnc(db):
    assert ep.choose_entry_product(db, "REAL", "ABC", "VOLUME_SHOCK_UPPER_CIRCUIT", MORNING)[0] == "CNC"


def test_late_entry_stays_cnc(db):
    assert ep.choose_entry_product(db, "REAL", "ABC", "VOLUME_SHOCK", LATE)[0] == "CNC"


def test_restricted_symbol_stays_cnc(db):
    db.add(models.IntradayRestrictedSecurity(symbol="T2T"))
    db.commit()
    assert ep.choose_entry_product(db, "REAL", "T2T", "VOLUME_SHOCK", MORNING)[0] == "CNC"


def test_mode_cnc_restores_old_behaviour(db, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_PRODUCT_MODE", "cnc")
    assert ep.choose_entry_product(db, "REAL", "ABC", "VOLUME_SHOCK", MORNING)[0] == "CNC"


def test_shipped_default_is_cnc():
    import importlib
    os.environ.pop("ENTRY_PRODUCT_MODE", None)
    assert importlib.reload(config).ENTRY_PRODUCT_MODE == "cnc"


def test_choose_never_raises(db, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MIS_LAST_TIME_IST", "garbage")
    assert ep.choose_entry_product(db, "REAL", "ABC", "VOLUME_SHOCK", MORNING)[0] in ("CNC", "INTRADAY")
    assert ep.choose_entry_product(None, "REAL", "ABC", "VOLUME_SHOCK", MORNING)[0] in ("CNC", "INTRADAY")


def test_same_day_cnc_round_trip_costs_the_same_as_mis():
    # group 266: Dhan charges a CNC buy sold the same day as intraday, so the estimate must not treat it as delivery
    mis = cost_model.estimate_round_trip_cost(100.0, 30, 101.0, product_type="INTRADAY")
    cnc_same_day = cost_model.estimate_round_trip_cost(100.0, 30, 101.0, product_type="CNC")
    assert cnc_same_day.total == mis.total and mis.dp_charge == 0.0 and mis.brokerage > 0


def test_carried_cnc_sale_is_much_dearer_than_a_same_day_round_trip():
    same_day = cost_model.estimate_round_trip_cost(100.0, 30, 101.0, product_type="CNC", is_delivery_sell=False)
    carried = cost_model.estimate_round_trip_cost(100.0, 30, 101.0, product_type="CNC", is_delivery_sell=True)
    assert carried.total > same_day.total + 10 and carried.dp_charge > 0 and carried.brokerage == 0.0


def test_gate_includes_dp_only_when_asked():
    base = cost_model.evaluate_entry_cost_gate(100.0, 30, 1.0, product_type="CNC")
    dp = cost_model.evaluate_entry_cost_gate(100.0, 30, 1.0, product_type="CNC", include_dp=True)
    assert dp.estimated_cost > base.estimated_cost + config.DP_CHARGE_FLAT * (1 + config.GST_PCT / 100) - 0.02
    mis = cost_model.evaluate_entry_cost_gate(100.0, 30, 1.0, product_type="INTRADAY", include_dp=True)
    assert mis.estimated_cost == base.estimated_cost                       # MIS never carries DP


def _closed(db, symbol, when):
    db.add(models.TradePosition(mode="REAL", symbol=symbol, status="CLOSED", qty_open=0, avg_entry_price=10.0,
                                opened_at=when - timedelta(hours=1), closed_at=when))
    db.commit()


def test_closed_today_symbols_only_today_and_only_closed(db):
    now = datetime.now(timezone.utc)
    _closed(db, "TODAY", now - timedelta(minutes=1))
    _closed(db, "OLD", now - timedelta(days=3))
    db.add(models.TradePosition(mode="REAL", symbol="OPENP", status="OPEN", qty_open=1, avg_entry_price=10.0))
    db.commit()
    got = ep.closed_today_symbols(db, "REAL")
    assert "TODAY" in got and "OLD" not in got and "OPENP" not in got
    assert ep.closed_today_symbols(db, "DEMO") == set()


def test_closed_today_never_raises():
    assert ep.closed_today_symbols(None, "REAL") == set()
