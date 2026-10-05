"""group155: watchlist trigger no longer queues stocks that fell away from their catalyst, nor Tier-3
volume-shock rows that are not up on the day. Run: python3 -m pytest tests/test_group155_watchlist_adverse_guard.py -q"""
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
from market_feed import feed
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
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()


def make_row(db, *, symbol="TESTCO", catalyst_price=100.0, source_tier=1, band=0.07):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type="results", catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=3.0,
        entry_band_pct=band, source_tier=source_tier, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def tick(price, symbol="TESTCO", prev_close=None):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=1.5, source="test",
                prev_close=prev_close)


def patch_quotes(monkeypatch, t):
    async def fake(symbols, **kw):
        return {t.symbol: t}
    monkeypatch.setattr(entry, "get_quotes", fake)


def queued(db):
    return db.query(models.TradeCandidate).count()


def test_big_drop_not_queued_but_row_stays_active(db, monkeypatch):
    row = make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(90.0))  # -10%, inside the old (one-sided) 7% band logic
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1 and tally["queued"] == 0 and tally["band_ok"] == 0
    assert queued(db) == 0
    db.refresh(row)
    assert row.status == "active"


def test_small_drop_inside_limit_still_queued(db, monkeypatch):
    make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(98.0))  # -2%
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["queued"] == 1 and tally["adverse"] == 0


def test_drop_limit_env_override(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_DROP_PCT", "0.10")
    make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(92.0))  # -8% < 10% limit
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_guard_off_switch_restores_old_behaviour(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(90.0))
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_bad_env_value_falls_back_to_default(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_DROP_PCT", "abc")
    make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(90.0))
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["adverse"] == 1


def test_upward_overrun_still_missed(db, monkeypatch):
    row = make_row(db, catalyst_price=100.0, band=0.06)
    patch_quotes(monkeypatch, tick(107.0))
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["missed"] == 1 and tally["adverse"] == 0
    db.refresh(row)
    assert row.status == "missed"


def test_tier3_down_on_the_day_not_queued(db, monkeypatch):
    make_row(db, source_tier=3, catalyst_price=0.0)
    patch_quotes(monkeypatch, tick(98.0, prev_close=100.0))  # -2% on the day, first-touch baseline
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1 and tally["queued"] == 0


def test_tier3_up_on_the_day_queued(db, monkeypatch):
    make_row(db, source_tier=3, catalyst_price=0.0)
    patch_quotes(monkeypatch, tick(103.0, prev_close=100.0))  # +3%
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_tier3_without_prev_close_fails_open(db, monkeypatch):
    make_row(db, source_tier=3, catalyst_price=0.0)
    patch_quotes(monkeypatch, tick(98.0, prev_close=None))
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_tier1_ignores_day_change_rule(db, monkeypatch):
    make_row(db, source_tier=1, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(100.5, prev_close=105.0))  # down on day but flat vs catalyst
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_guard_exception_fails_open(db, monkeypatch):
    monkeypatch.setattr(entry, "_tick_day_change_pct", lambda t: (_ for _ in ()).throw(RuntimeError("x")))
    make_row(db, source_tier=3, catalyst_price=0.0)
    patch_quotes(monkeypatch, tick(98.0, prev_close=100.0))
    assert run(entry.evaluate_watchlist_entries(db, "DEMO"))["queued"] == 1


def test_adverse_log_is_throttled(db, monkeypatch, caplog):
    make_row(db, catalyst_price=100.0)
    patch_quotes(monkeypatch, tick(90.0))
    with caplog.at_level("INFO"):
        run(entry.evaluate_watchlist_entries(db, "DEMO"))
        run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert sum("SKIPPED" in r.message for r in caplog.records) == 1


def test_safe_prev_close_and_tick_parsing():
    assert feed._safe_prev_close("12.5") == 12.5
    for bad in (None, "", "x", 0, -1, float("nan")):
        assert feed._safe_prev_close(bad) is None
    t = feed._tick_from_bulk_item({"symbol": "ABC", "price": 10, "previous_close": 9.5,
                                   "fetched_at": datetime.now(timezone.utc).isoformat()})
    assert t is not None and t.prev_close == 9.5
    assert tick(10.0).prev_close is None
