"""
tests/test_capital_share.py — unit tests for the CAPITAL_SHARE_PCT cap in
execution/equity_sync.py (session72 open-issue #27 / #30).

Verifies that sync_real_equity() stores only the service's configured share
of the full Dhan balance as cash_available/current_equity, and that the raw
broker figure is kept separately in broker_cash_available so that the
risk_engine's total-exposure check can still see the full account.

Patch strategy:
  - dhan_client.get_funds is patched on the dhan_client module object, which
    is the same object equity_sync imported at module level.
  - equity_sync._open_positions_market_value is patched directly on the
    equity_sync module namespace (where the name lives after the
    `from portfolio.portfolio import _open_positions_market_value` import).
  - get_account is imported from portfolio.portfolio (where it actually lives)
    to read back DB state after sync.

Pure offline tests — no Dhan API calls, no live DB (SQLite in-memory).

Run from services/real-trade-service:
    python -m pytest tests/test_capital_share.py -v
or:
    python tests/test_capital_share.py
"""
from __future__ import annotations

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
from execution import dhan_client, equity_sync
from portfolio.portfolio import get_account

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def fresh_db(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    Session = sessionmaker(bind=_engine)
    db = Session()

    # Seed a REAL account row so sync_real_equity has something to update.
    acct = models.TradeAccount(
        mode="REAL",
        cash_available=0.0,
        current_equity=0.0,
        broker_cash_available=0.0,
        starting_capital=0.0,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(acct)
    db.commit()

    # Reset equity_sync's module-level _last_balance_key between tests so
    # "new balance field" warnings don't bleed across test cases.
    monkeypatch.setattr(equity_sync, "_last_balance_key", None)

    # Default: no open positions market value.
    monkeypatch.setattr(
        equity_sync, "_open_positions_market_value", lambda db_, mode: 0.0
    )

    yield db
    db.close()


def _mock_funds(balance: float, monkeypatch, key: str = "availabelBalance"):
    monkeypatch.setattr(
        dhan_client, "get_funds", lambda db_: {key: balance}
    )


# ── Tests ────────────────────────────────────────────────────────────────────

def test_cash_available_is_capped_to_capital_share_pct(monkeypatch, fresh_db):
    """cash_available must be CAPITAL_SHARE_PCT% of the live Dhan balance."""
    _mock_funds(100_000.0, monkeypatch)
    equity = equity_sync.sync_real_equity(fresh_db)

    expected = round(100_000.0 * (config.CAPITAL_SHARE_PCT / 100.0), 2)
    acct = get_account(fresh_db, "REAL")

    assert acct.cash_available == expected
    assert equity == expected


def test_broker_cash_available_is_full_balance(monkeypatch, fresh_db):
    """broker_cash_available must reflect the FULL Dhan balance, not the cap."""
    full_balance = 200_000.0
    _mock_funds(full_balance, monkeypatch)
    equity_sync.sync_real_equity(fresh_db)

    acct = get_account(fresh_db, "REAL")
    assert acct.broker_cash_available == full_balance


def test_current_equity_is_capped_cash_plus_market_value(monkeypatch, fresh_db):
    """current_equity = capped_cash + open-positions market value."""
    full_balance = 50_000.0
    market_value = 8_000.0
    monkeypatch.setattr(
        equity_sync, "_open_positions_market_value", lambda db_, mode: market_value
    )
    _mock_funds(full_balance, monkeypatch)
    equity = equity_sync.sync_real_equity(fresh_db)

    capped = round(full_balance * (config.CAPITAL_SHARE_PCT / 100.0), 2)
    assert equity == round(capped + market_value, 2)


def test_50_pct_default_gives_half_the_balance(monkeypatch, fresh_db):
    """Sanity: with 50% share, cash_available == balance / 2."""
    monkeypatch.setattr(config, "CAPITAL_SHARE_PCT", 50.0)
    _mock_funds(80_000.0, monkeypatch)
    equity_sync.sync_real_equity(fresh_db)

    acct = get_account(fresh_db, "REAL")
    assert acct.cash_available == 40_000.0


def test_100_pct_share_gives_full_balance(monkeypatch, fresh_db):
    """If CAPITAL_SHARE_PCT is 100, cash_available equals the full balance."""
    monkeypatch.setattr(config, "CAPITAL_SHARE_PCT", 100.0)
    _mock_funds(60_000.0, monkeypatch)
    equity_sync.sync_real_equity(fresh_db)

    acct = get_account(fresh_db, "REAL")
    assert acct.cash_available == 60_000.0


def test_get_funds_failure_returns_none_and_leaves_db_unchanged(monkeypatch, fresh_db):
    """If Dhan's /funds call raises, sync returns None; DB is NOT updated."""
    acct = get_account(fresh_db, "REAL")
    acct.cash_available = 12_345.0
    fresh_db.commit()

    monkeypatch.setattr(
        dhan_client, "get_funds",
        lambda db_: (_ for _ in ()).throw(RuntimeError("Dhan down")),
    )
    result = equity_sync.sync_real_equity(fresh_db)

    assert result is None
    fresh_db.expire_all()
    acct = get_account(fresh_db, "REAL")
    assert acct.cash_available == 12_345.0


def test_dhan_not_connected_returns_none(monkeypatch, fresh_db):
    monkeypatch.setattr(
        dhan_client, "get_funds",
        lambda db_: (_ for _ in ()).throw(dhan_client.DhanNotConnectedError("not connected")),
    )
    result = equity_sync.sync_real_equity(fresh_db)
    assert result is None


def test_unrecognized_funds_shape_returns_none(monkeypatch, fresh_db):
    """A funds response with none of the known balance keys must not crash."""
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"someRandomKey": 99999.0})
    result = equity_sync.sync_real_equity(fresh_db)
    assert result is None


def test_starting_capital_auto_corrected_when_zero(monkeypatch, fresh_db):
    """starting_capital=0 must be auto-corrected to current_equity on first sync."""
    _mock_funds(40_000.0, monkeypatch)
    equity_sync.sync_real_equity(fresh_db)

    acct = get_account(fresh_db, "REAL")
    expected_equity = round(40_000.0 * (config.CAPITAL_SHARE_PCT / 100.0), 2)
    assert acct.starting_capital == expected_equity


def test_both_services_share_full_broker_balance(monkeypatch, fresh_db):
    """broker_cash_available + position-stocks' implied half must sum to the full balance.
    This verifies the contract: both services each hold half of Dhan's available cash."""
    monkeypatch.setattr(config, "CAPITAL_SHARE_PCT", 50.0)
    full_balance = 120_000.0
    _mock_funds(full_balance, monkeypatch)
    equity_sync.sync_real_equity(fresh_db)

    acct = get_account(fresh_db, "REAL")
    # This service's half
    assert acct.cash_available == 60_000.0
    # Full balance is stored so risk_engine can see the real account size
    assert acct.broker_cash_available == full_balance
    # Implied: position-stocks-service has the other half (60k) — untestable here
    # but broker_cash_available gives risk_engine the data to enforce it
    assert acct.broker_cash_available == acct.cash_available * 2
