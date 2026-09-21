"""
tests/test_watchlist_trigger.py — offline tests for entry_engine.entry.evaluate_watchlist_entries.

Previously 0% covered (see AUDIT_REPORT.md / test_rt_entry_helpers.py's own docstring,
which explicitly calls out evaluate_mode() as untested — this function is its sibling
Stage-2 trigger pass and had no tests at all). Found and fixed one real bug while
writing these: a live tick with price <= 0 for a row whose catalyst_price was still
the 0.0 "unknown" sentinel raised ZeroDivisionError, which aborted the rest of the
per-row loop for every OTHER active watchlist row that cycle too (not just the bad
one) — see the BUG FIX comment at the top of the function body in entry_engine/entry.py.

Run from services/real-trade-service:
    python3 -m pytest tests/test_watchlist_trigger.py -q --cov=entry_engine.entry --cov-report=term-missing
"""
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


def make_row(db, *, symbol="TESTCO", catalyst_price=100.0, source_tier=1,
             entry_band_pct=0.05, status="active", mode="DEMO"):
    row = models.WatchlistEntry(
        mode=mode, symbol=symbol,
        catalyst_type="bulk_block", catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc),
        horizon_class="mid", decay_half_life_days=3.0,
        entry_band_pct=entry_band_pct,
        source_tier=source_tier, conviction_score=70.0,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def tick(price, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=1.5, source="test")


class TestEmptyAndNoTick:
    def test_no_active_rows_returns_zeroed_tally(self, db):
        assert run(entry.evaluate_watchlist_entries(db, "DEMO")) == {
            "watchlist_checked": 0, "band_ok": 0, "missed": 0, "queued": 0,
        }

    def test_row_with_no_tick_this_cycle_is_skipped_not_crashed(self, db, monkeypatch):
        make_row(db)

        async def _no_quotes(symbols):
            return {}
        monkeypatch.setattr(entry, "get_quotes", _no_quotes)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 0, "missed": 0, "queued": 0}


class TestZeroPriceGuard:
    """The bug found + fixed this session."""

    def test_zero_price_tick_does_not_crash_and_is_skipped(self, db, monkeypatch):
        row = make_row(db, catalyst_price=0.0)  # Tier-3-style unknown baseline

        async def _zero_quote(symbols):
            return {row.symbol: tick(0.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _zero_quote)

        # Must not raise ZeroDivisionError.
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 0, "missed": 0, "queued": 0}
        # catalyst_price must NOT have been stamped from a bad 0.0 tick.
        db.refresh(row)
        assert row.catalyst_price == 0.0
        assert row.status == "active"

    def test_negative_price_tick_is_also_skipped_not_crashed(self, db, monkeypatch):
        row = make_row(db, catalyst_price=0.0)

        async def _neg_quote(symbols):
            return {row.symbol: tick(-1.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _neg_quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 0, "missed": 0, "queued": 0}

    def test_a_bad_zero_price_row_does_not_block_other_rows_same_cycle(self, db, monkeypatch):
        # This is the actual blast-radius of the bug: without the fix, the
        # ZeroDivisionError on the first (alphabetically/insertion-order)
        # row aborted the whole function, so a perfectly healthy second row
        # never got evaluated at all this cycle.
        make_row(db, symbol="BADCO", catalyst_price=0.0)
        make_row(db, symbol="GOODCO", catalyst_price=100.0)

        async def _mixed_quotes(symbols):
            return {"BADCO": tick(0.0, "BADCO"), "GOODCO": tick(101.0, "GOODCO")}
        monkeypatch.setattr(entry, "get_quotes", _mixed_quotes)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally["watchlist_checked"] == 2
        assert tally["queued"] == 1  # GOODCO queued; BADCO safely skipped
        queued_symbols = {c.symbol for c in db.query(models.TradeCandidate).all()}
        assert queued_symbols == {"GOODCO"}


class TestCatalystPriceBackfill:
    def test_tier1_zero_catalyst_price_is_set_and_falls_through_same_cycle(self, db, monkeypatch):
        row = make_row(db, catalyst_price=0.0, source_tier=1, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(100.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        db.refresh(row)
        assert row.catalyst_price == 100.0
        assert row.catalyst_price_source == "live"
        assert tally["queued"] == 1  # falls through to band-check + queues same cycle

    def test_tier3_zero_catalyst_price_also_queues_same_cycle(self, db, monkeypatch):
        row = make_row(db, catalyst_price=0.0, source_tier=3, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(50.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        db.refresh(row)
        assert row.catalyst_price == 50.0
        assert tally["queued"] == 1


class TestBandCheck:
    def test_within_band_queues_a_buy_now_candidate(self, db, monkeypatch):
        row = make_row(db, catalyst_price=100.0, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(103.0, row.symbol)}  # +3%, within 5% band
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 1, "missed": 0, "queued": 1}
        cand = db.query(models.TradeCandidate).one()
        assert cand.symbol == row.symbol
        assert cand.decision_label == "BUY NOW"
        assert cand.source_tab == "watchlist"
        assert cand.watchlist_entry_id == row.id
        assert cand.consumed is False

    def test_price_beyond_band_marks_missed_and_does_not_queue(self, db, monkeypatch):
        row = make_row(db, catalyst_price=100.0, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(110.0, row.symbol)}  # +10%, beyond 5% band
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 0, "missed": 1, "queued": 0}
        db.refresh(row)
        assert row.status == "missed"
        assert row.missed_reason is not None
        assert db.query(models.TradeCandidate).count() == 0

    def test_price_at_exact_band_edge_is_not_missed(self, db, monkeypatch):
        row = make_row(db, catalyst_price=100.0, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(105.0, row.symbol)}  # exactly +5%
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally["missed"] == 0
        assert tally["queued"] == 1

    def test_price_below_catalyst_is_within_band_and_queues(self, db, monkeypatch):
        # A drop is not "chasing" the catalyst — only an upward run past the
        # band marks it missed. A cheaper-than-catalyst entry still queues.
        row = make_row(db, catalyst_price=100.0, entry_band_pct=0.05)

        async def _quote(symbols):
            return {row.symbol: tick(90.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 1, "band_ok": 1, "missed": 0, "queued": 1}


class TestAlreadyQueuedDedup:
    def test_symbol_with_existing_unconsumed_watchlist_candidate_is_not_requeued(self, db, monkeypatch):
        row = make_row(db, catalyst_price=100.0, entry_band_pct=0.05)
        db.add(models.TradeCandidate(
            mode="DEMO", symbol=row.symbol, source_tab="watchlist",
            decision_label="BUY NOW", conviction_score=70.0, signal_price=101.0,
            raw_payload=None, consumed=False, watchlist_entry_id=row.id,
        ))
        db.commit()

        async def _quote(symbols):
            return {row.symbol: tick(101.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally["band_ok"] == 1
        assert tally["queued"] == 0  # dedup — no second candidate inserted
        assert db.query(models.TradeCandidate).count() == 1

    def test_mode_isolation_real_vs_demo_candidates_do_not_dedup_across_modes(self, db, monkeypatch):
        row = make_row(db, mode="REAL", catalyst_price=100.0, entry_band_pct=0.05)
        db.add(models.TradeCandidate(
            mode="DEMO", symbol=row.symbol, source_tab="watchlist",
            decision_label="BUY NOW", conviction_score=70.0, signal_price=101.0,
            raw_payload=None, consumed=False, watchlist_entry_id=999,
        ))
        db.commit()

        async def _quote(symbols):
            return {row.symbol: tick(101.0, row.symbol)}
        monkeypatch.setattr(entry, "get_quotes", _quote)

        tally = run(entry.evaluate_watchlist_entries(db, "REAL"))
        assert tally["queued"] == 1  # REAL row queues despite an unrelated DEMO row existing
