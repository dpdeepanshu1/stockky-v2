"""group360 (14 Oct log item 10: GERMAN queued at `move=0.00%`): the watchlist trigger does not store a baseline from, or
queue on, a price that is not live.

Run: python3 -m pytest tests/test_group360_stale_price_gate.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from entry_engine import entry

_ENV = ("WATCHLIST_STALE_PRICE_GATE", "WATCHLIST_BASELINE_AGE_UNKNOWN", "WATCHLIST_NO_TRADE_HOLD",
        "WATCHLIST_MAX_TICK_AGE_S", "WATCHLIST_REQUEUE_COOLDOWN_S")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def T(**kw):
    base = dict(price=100.0, source="dhan", age_known=True, volume=None, prev_close=None,
                as_of=datetime.now(timezone.utc))
    base.update(kw)
    return NS(**base)


def old(secs):
    return datetime.now(timezone.utc) - timedelta(seconds=secs)


# ── baseline from a not-live price ────────────────────────────────────────────────
def test_fresh_known_age_baseline_is_fine():
    assert entry._watchlist_stale_price_reason(T(), baseline=True) is None


def test_old_tick_cannot_set_a_baseline():
    r = entry._watchlist_stale_price_reason(T(as_of=old(100)), baseline=True)
    assert r and "100s old" in r


def test_stale_last_good_copy_cannot_set_a_baseline_even_if_recent():
    r = entry._watchlist_stale_price_reason(T(source="stale_last_good(dhan)"), baseline=True)
    assert r and "stale last-good" in r


def test_unknown_age_cannot_set_a_baseline_by_default():
    r = entry._watchlist_stale_price_reason(T(age_known=False), baseline=True)
    assert r and "age unknown" in r


def test_unknown_age_baseline_can_be_allowed(monkeypatch):
    monkeypatch.setenv("WATCHLIST_BASELINE_AGE_UNKNOWN", "allow")
    assert entry._watchlist_stale_price_reason(T(age_known=False), baseline=True) is None


def test_blank_unknown_age_switch_uses_default(monkeypatch):
    monkeypatch.setenv("WATCHLIST_BASELINE_AGE_UNKNOWN", "  ")
    assert entry._watchlist_stale_price_reason(T(age_known=False), baseline=True)


def test_existing_baseline_keeps_the_old_age_path():
    # an old tick for a row that already has a baseline is left to _watchlist_adverse_reason (unchanged)
    assert entry._watchlist_stale_price_reason(T(as_of=old(100)), baseline=False) is None
    assert entry._watchlist_stale_price_reason(T(age_known=False, source="stale_last_good(x)"), baseline=False) is None


# ── no trade today ────────────────────────────────────────────────────────────────
def test_zero_volume_at_previous_close_is_held():
    r = entry._watchlist_stale_price_reason(T(volume=0, prev_close=100.0), baseline=False)
    assert r and "no trades today" in r


def test_zero_volume_with_unknown_prev_close_is_held():
    assert entry._watchlist_stale_price_reason(T(volume=0), baseline=False)


def test_zero_volume_but_price_moved_is_not_held():
    assert entry._watchlist_stale_price_reason(T(volume=0, prev_close=95.0), baseline=False) is None


def test_traded_volume_is_not_held():
    assert entry._watchlist_stale_price_reason(T(volume=1500, prev_close=100.0), baseline=False) is None


def test_unknown_volume_never_blocks():
    assert entry._watchlist_stale_price_reason(T(volume=None, prev_close=100.0), baseline=False) is None


def test_no_trade_hold_switch(monkeypatch):
    monkeypatch.setenv("WATCHLIST_NO_TRADE_HOLD", "0")
    assert entry._watchlist_stale_price_reason(T(volume=0), baseline=False) is None


def test_master_switch_off_restores_old_behaviour(monkeypatch):
    monkeypatch.setenv("WATCHLIST_STALE_PRICE_GATE", "0")
    t = T(volume=0, age_known=False, source="stale_last_good(x)", as_of=old(500))
    assert entry._watchlist_stale_price_reason(t, baseline=True) is None


def test_never_raises_on_a_broken_tick():
    assert entry._watchlist_stale_price_reason(NS(volume="abc", source=None), baseline=False) is None
    assert entry._watchlist_stale_price_reason(None, baseline=True) is None


# ── through the trigger pass (SQLite) ─────────────────────────────────────────────
def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _row(db, *, catalyst_price, tier=3):
    import models
    row = models.WatchlistEntry(
        mode="DEMO", symbol="GERMAN", catalyst_type="volume_shock", catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=3.0, entry_band_pct=0.05,
        source_tier=tier, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _run(db, monkeypatch, tick):
    from market_feed.feed import Tick

    async def _q(symbols, **_kw):
        return {"GERMAN": tick}
    monkeypatch.setattr(entry, "get_quotes", _q)
    monkeypatch.setattr(entry, "_watchlist_instrument_reason", lambda s, p: None)
    monkeypatch.setenv("WATCHLIST_REQUEUE_COOLDOWN_S", "0")
    return asyncio.run(entry.evaluate_watchlist_entries(db, "DEMO"))


def test_trigger_old_tick_does_not_store_baseline_or_queue(monkeypatch):
    from market_feed.feed import Tick
    db = _db()
    row = _row(db, catalyst_price=0.0)
    t = Tick(symbol="GERMAN", price=50.0, as_of=old(100), atr=1.0, source="dhan")
    tally = _run(db, monkeypatch, t)
    db.refresh(row)
    assert row.catalyst_price == 0.0 and tally["queued"] == 0 and tally["stale_price"] == 1


def test_trigger_no_trade_stock_is_not_queued(monkeypatch):
    from market_feed.feed import Tick
    db = _db()
    row = _row(db, catalyst_price=0.0)
    t = Tick(symbol="GERMAN", price=50.0, as_of=datetime.now(timezone.utc), atr=1.0, source="dhan", volume=0,
             prev_close=50.0)
    tally = _run(db, monkeypatch, t)
    db.refresh(row)
    assert row.catalyst_price == 0.0 and tally["queued"] == 0 and tally["stale_price"] == 1


def test_trigger_live_tick_still_sets_baseline_and_queues_tier3(monkeypatch):
    from market_feed.feed import Tick
    db = _db()
    row = _row(db, catalyst_price=0.0)
    t = Tick(symbol="GERMAN", price=50.0, as_of=datetime.now(timezone.utc), atr=1.0, source="dhan", volume=9000)
    tally = _run(db, monkeypatch, t)
    db.refresh(row)
    assert row.catalyst_price == 50.0 and tally["queued"] == 1 and "stale_price" not in tally
