"""A11 (2026-10-22): account-wide daily loss cap in the scalp pool's ledger.reserve_capital().

Run from services/position-stocks-service:  python3 -m pytest tests/test_a11_account_daily_loss.py -q
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import ledger, shared_daily_pnl
from tz_utils import ist_today_str


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(config, "ACCOUNT_MAX_DAILY_LOSS_PCT", 3.0)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)
    monkeypatch.setattr(config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 0.0, raising=False)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _pool(db, *, pnl=0.0, base=50_000.0):
    row = ledger._get_or_create(db)
    row.total_allocated_capital = base
    row.available_capital = base
    row.realized_pnl_today = pnl
    db.commit()
    return row


def _peer(db, pnl, base=50_000.0, date=None):
    db.add(models.SharedDailyPnl(service_name=shared_daily_pnl.OTHER_SERVICE_NAME,
                                 trade_date=date or ist_today_str(), realized_pnl_today=pnl, capital_base=base))
    db.commit()


def test_blocks_when_combined_loss_reaches_cap(db):
    _pool(db, pnl=-1_500.0)         # own: 3.0% of pool, under the 4% pool kill switch
    _peer(db, -1_500.0)             # peer: -1500 on 50k -> combined 3.0% of 100k
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None
    assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is False   # per-service switch untouched


def test_allows_when_combined_loss_under_cap(db):
    _pool(db, pnl=-1_000.0)
    _peer(db, -1_000.0)             # combined 2.0%
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_peer_profit_offsets_own_loss(db):
    _pool(db, pnl=-1_800.0)
    _peer(db, 1_000.0)              # net -800 / 100k = 0.8%
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_peer_row_from_earlier_day_is_ignored(db):
    _pool(db, pnl=-500.0)
    _peer(db, -50_000.0, date="2000-01-01")
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_cap_zero_disables(db, monkeypatch):
    monkeypatch.setattr(config, "ACCOUNT_MAX_DAILY_LOSS_PCT", 0.0)
    _pool(db, pnl=-1_500.0)
    _peer(db, -1_500.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_no_peer_row_behaves_like_own_cap(db):
    _pool(db, pnl=-500.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


def test_release_capital_publishes_pnl(db):
    _pool(db)
    ledger.release_capital(db, position_value=1_000.0, realized_pnl=-250.0)
    row = db.query(models.SharedDailyPnl).filter_by(service_name=shared_daily_pnl.SERVICE_NAME).one()
    assert row.realized_pnl_today == -250.0 and row.capital_base == 50_000.0
    assert row.trade_date == ist_today_str()
