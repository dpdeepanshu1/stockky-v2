"""group157: watchlist trigger holds back ETFs and sub-floor-priced stocks before they are queued.
Run: python3 -m pytest tests/test_group157_watchlist_penny_etf_hold.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from entry_engine import entry
from market_feed.feed import Tick


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_ETF_SYMBOLS", "CANDIDATE_MIN_STOCK_PRICE",
              "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()


def make_row(db, *, symbol, catalyst_price=100.0, source_tier=1):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type="results", catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=3.0,
        entry_band_pct=0.07, source_tier=source_tier, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def patch_quote(monkeypatch, symbol, price):
    t = Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=1.5, source="test")

    async def fake(symbols, **kw):
        return {symbol: t}
    monkeypatch.setattr(entry, "get_quotes", fake)


def n_candidates(db):
    return db.query(models.TradeCandidate).count()


@pytest.mark.parametrize("sym", ["MASPTOP50", "MAFANG", "NIFTYBEES", "GOLDBEES", "MASPTOP50.NS", "maspTop50",
                                 "SOMEETF", "NIFTY1ETF"])
def test_etf_names_are_detected(sym):
    assert entry._wl_is_etf_symbol(sym) is True


@pytest.mark.parametrize("sym", ["RELIANCE", "INFY", "TATASTEEL", "HARDWYN", "JHS", "UCOBANK", "BETA", "", None])
def test_normal_names_are_not_etfs(sym):
    assert entry._wl_is_etf_symbol(sym) is False


def test_extra_etf_symbols_from_env(monkeypatch):
    assert entry._wl_is_etf_symbol("ABCFUND") is False
    monkeypatch.setenv("WATCHLIST_ETF_SYMBOLS", " abcfund , XYZFUND ")
    assert entry._wl_is_etf_symbol("ABCFUND") is True
    assert entry._wl_is_etf_symbol("xyzfund.ns") is True


def test_etf_row_not_queued_and_stays_active(db, monkeypatch):
    row = make_row(db, symbol="MAFANG", catalyst_price=100.0)
    patch_quote(monkeypatch, "MAFANG", 100.5)  # inside the band, would have queued before
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1 and tally["queued"] == 0 and tally["band_ok"] == 0
    assert n_candidates(db) == 0
    db.refresh(row)
    assert row.status == "active"


def test_penny_stock_not_queued(db, monkeypatch):
    row = make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quote(monkeypatch, "HARDWYN", 12.3)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1 and tally["queued"] == 0
    assert n_candidates(db) == 0
    db.refresh(row)
    assert row.status == "active"


def test_price_at_floor_is_queued(db, monkeypatch):
    make_row(db, symbol="OKCO", catalyst_price=20.0)
    patch_quote(monkeypatch, "OKCO", 20.0)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["queued"] == 1 and tally["adverse"] == 0


def test_normal_priced_stock_still_queued(db, monkeypatch):
    make_row(db, symbol="INFY", catalyst_price=1500.0)
    patch_quote(monkeypatch, "INFY", 1505.0)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["queued"] == 1 and tally["adverse"] == 0


def test_floor_follows_env(db, monkeypatch):
    monkeypatch.setenv("CANDIDATE_MIN_STOCK_PRICE", "5")
    make_row(db, symbol="CHEAPCO", catalyst_price=12.0)
    patch_quote(monkeypatch, "CHEAPCO", 12.0)
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_bad_floor_env_falls_back_to_20(db, monkeypatch):
    monkeypatch.setenv("CANDIDATE_MIN_STOCK_PRICE", "abc")
    make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quote(monkeypatch, "HARDWYN", 12.0)
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["adverse"] == 1


def test_off_switch_restores_old_behaviour(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    make_row(db, symbol="MAFANG", catalyst_price=100.0)
    patch_quote(monkeypatch, "MAFANG", 100.0)
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_instrument_reason_never_raises():
    assert entry._watchlist_instrument_reason("TESTCO", "not-a-number") is None
    assert entry._watchlist_instrument_reason("TESTCO", None) is None


def test_skip_is_logged_once_per_window(db, monkeypatch, caplog):
    make_row(db, symbol="MASPTOP50", catalyst_price=100.0)
    patch_quote(monkeypatch, "MASPTOP50", 100.0)
    with caplog.at_level("INFO"):
        run(entry.evaluate_watchlist_entries(db, "DEMO"))
        run(entry.evaluate_watchlist_entries(db, "DEMO"))
    lines = [r for r in caplog.records if "SKIPPED" in r.getMessage() and "MASPTOP50" in r.getMessage()]
    assert len(lines) == 1
    assert "ETF" in lines[0].getMessage()
