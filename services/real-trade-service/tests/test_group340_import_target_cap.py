"""group 340 (2026-10-12): imported demat holdings got targets of +9.75% / +13.5% against ~1.8% stops - unreachable inside the 10-day
time stop. portfolio.import_broker_holdings now caps the imported target at config.IMPORT_TARGET_MAX_STOP_MULT x the stop distance."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import portfolio.portfolio as pf
from entry_engine import entry as entry_mod
from execution import dhan_client, shared_symbol_lock

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    s.add(models.TradeAccount(mode="REAL", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.commit()
    monkeypatch.setattr(dhan_client, "get_holdings", lambda d: [{"tradingSymbol": "WIDECO", "totalQty": 20, "avgCostPrice": 50.0}])
    monkeypatch.setattr(dhan_client, "get_positions", lambda d: [])
    monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda d, sym, mode="REAL": True)

    async def _no_quotes(symbols):
        return {}

    monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)
    monkeypatch.setattr(entry_mod, "FLAT_STOP_PCT", 2.0)
    monkeypatch.setattr(entry_mod, "FLAT_TARGET_PCT", 13.5)
    yield s
    s.close()


def _import(db):
    assert asyncio.run(pf.import_broker_holdings(db)) == 1
    return db.query(models.TradePosition).filter_by(symbol="WIDECO").one()


def test_wide_imported_target_is_capped_at_stop_multiple(db, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_TARGET_MAX_STOP_MULT", 2.5)
    pos = _import(db)
    assert pos.current_stop == pytest.approx(49.0)
    assert pos.current_target == pytest.approx(52.5)          # 50 + (50-49) * 2.5, not 56.75


def test_cap_off_keeps_the_wide_target(db, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_TARGET_MAX_STOP_MULT", 0.0)
    pos = _import(db)
    assert pos.current_target == pytest.approx(56.75)


def test_target_already_within_cap_is_untouched(db, monkeypatch):
    monkeypatch.setattr(config, "IMPORT_TARGET_MAX_STOP_MULT", 2.5)
    monkeypatch.setattr(entry_mod, "FLAT_TARGET_PCT", 4.0)     # 4% target / 2% stop = 2x < 2.5x
    pos = _import(db)
    assert pos.current_target == pytest.approx(52.0)
