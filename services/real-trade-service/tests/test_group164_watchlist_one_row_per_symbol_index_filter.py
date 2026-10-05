"""group164 (item 5, first part): index names are not watchlist stocks, and one active row per symbol is
decided per cycle (no QUEUED/SKIPPED flapping between two rows of the same stock).
Run: python3 -m pytest tests/test_group164_watchlist_one_row_per_symbol_index_filter.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from entry_engine import entry
from market_feed.feed import Tick
from watchlist_engine import symbol_filter as sf
from watchlist_engine import watchlist


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
    for k in ("WATCHLIST_INDEX_FILTER", "WATCHLIST_ONE_ROW_PER_SYMBOL", "WATCHLIST_INDEX_SYMBOLS",
              "WATCHLIST_ADVERSE_GUARD", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT",
              "WATCHLIST_EXPIRE_DROP_PCT", "CANDIDATE_MIN_STOCK_PRICE"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()


def make_row(db, *, symbol="PACEDIGITK", catalyst_price=100.0, tier=1, ctype="results", ts=None, status="active"):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type=ctype, catalyst_price=catalyst_price,
        catalyst_ts=ts or datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=12.0,
        entry_band_pct=0.07, source_tier=tier, conviction_score=70.0, status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def patch_quotes(monkeypatch, prices, seen=None):
    async def fake(symbols, **kw):
        if seen is not None:
            seen.append(sorted(symbols))
        return {s: Tick(symbol=s, price=p, as_of=datetime.now(timezone.utc), atr=1.5, source="test")
                for s, p in prices.items() if s in symbols}
    monkeypatch.setattr(entry, "get_quotes", fake)


# ── symbol_filter ───────────────────────────────────────────────────────────

class TestIsIndexSymbol:
    @pytest.mark.parametrize("sym", ["NIFTY", "nifty", "NIFTY 50", "NIFTY50.NS", "BANKNIFTY", "^NSEI", "SENSEX",
                                     "SENSEX.BO", "INDIA-VIX", "FINNIFTY", "CNXIT"])
    def test_index_names(self, sym):
        assert sf.is_index_symbol(sym) is True

    @pytest.mark.parametrize("sym", ["TCS", "RELIANCE", "NIFTYBEES", "PACEDIGITK", "", None, "M&M"])
    def test_stocks_and_etfs_are_not_indices(self, sym):
        assert sf.is_index_symbol(sym) is False

    def test_extra_symbols_from_env(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_INDEX_SYMBOLS", "foo, NIFTY-FOO ,")
        assert sf.is_index_symbol("FOO") is True
        assert sf.is_index_symbol("NIFTYFOO.NS") is True
        assert sf.is_index_symbol("BAR") is False

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(sf, "normalize", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        assert sf.is_index_symbol("NIFTY") is False


class TestFlags:
    @pytest.mark.parametrize("val,expected", [("", True), ("  ", True), ("1", True), ("garbage", True),
                                              ("0", False), ("false", False), ("NO", False), (" off ", False)])
    def test_flags(self, monkeypatch, val, expected):
        monkeypatch.setenv("WATCHLIST_INDEX_FILTER", val)
        monkeypatch.setenv("WATCHLIST_ONE_ROW_PER_SYMBOL", val)
        assert sf.index_filter_on() is expected
        assert sf.one_row_per_symbol_on() is expected


def _r(id, symbol, tier=1, ts=None):
    return SimpleNamespace(id=id, symbol=symbol, source_tier=tier, catalyst_ts=ts)


class TestSplitPrimaryRows:
    def test_one_row_per_symbol_untouched(self):
        rows = [_r(1, "A"), _r(2, "B")]
        assert sf.split_primary_rows(rows) == (rows, [])

    def test_best_tier_wins(self):
        t3, t1 = _r(1, "A", tier=3), _r(2, "A", tier=1)
        assert sf.split_primary_rows([t3, t1]) == ([t1], [t3])

    def test_same_tier_newest_catalyst_wins(self):
        now = datetime.now(timezone.utc)
        old, new = _r(1, "A", ts=now - timedelta(days=3)), _r(2, "A", ts=now)
        assert sf.split_primary_rows([old, new]) == ([new], [old])

    def test_naive_timestamps_are_treated_as_utc(self):
        naive = _r(1, "A", ts=datetime(2026, 10, 1))
        aware = _r(2, "A", ts=datetime(2026, 10, 2, tzinfo=timezone.utc))
        assert sf.split_primary_rows([naive, aware])[0] == [aware]

    def test_tie_goes_to_highest_id(self):
        a, b = _r(1, "A"), _r(2, "A")
        assert sf.split_primary_rows([a, b]) == ([b], [a])

    def test_suffix_spellings_are_one_stock(self):
        a, b = _r(1, "KOTAKBANK"), _r(2, "KOTAKBANK.NS", tier=3)
        assert sf.split_primary_rows([b, a]) == ([a], [b])

    def test_primaries_keep_original_order(self):
        a1, b1, a2 = _r(1, "A", tier=3), _r(2, "B"), _r(3, "A", tier=1)
        assert sf.split_primary_rows([a1, b1, a2]) == ([b1, a2], [a1])

    def test_missing_fields_do_not_raise(self):
        a, b = SimpleNamespace(symbol="A"), SimpleNamespace(symbol="A")
        prim, dups = sf.split_primary_rows([a, b])
        assert len(prim) == 1 and len(dups) == 1

    def test_blank_symbols_do_not_merge_with_each_other_wrongly(self):
        a, b = SimpleNamespace(id=1, symbol="", source_tier=1), SimpleNamespace(id=2, symbol="", source_tier=1)
        prim, dups = sf.split_primary_rows([a, b])
        assert len(prim) == 1 and len(dups) == 1

    def test_empty(self):
        assert sf.split_primary_rows([]) == ([], [])


# ── ingest ──────────────────────────────────────────────────────────────────

class TestIngestSkipsIndexNames:
    def _refresh(self, db, candidates):
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates", new=AsyncMock(return_value=candidates)):
            return run(watchlist.refresh_watchlist(db, "DEMO"))

    def test_index_items_are_not_inserted_and_stocks_are(self, db, caplog):
        cands = [{"symbol": "NIFTY", "catalyst_type": "board", "catalyst_price": 24500.0},
                 {"symbol": "BANKNIFTY.NS", "catalyst_type": "board", "catalyst_price": 52000.0},
                 {"symbol": "TCS", "catalyst_type": "board", "catalyst_price": 3900.0}]
        with caplog.at_level("INFO", logger="real-trade-watchlist"):
            added = self._refresh(db, cands)
        assert added == 1
        assert [r.symbol for r in db.query(models.WatchlistEntry).all()] == ["TCS"]
        assert any("2 index-name item(s) ignored" in r.getMessage() for r in caplog.records)

    def test_only_index_items_means_nothing_added_and_no_commit_needed(self, db):
        assert self._refresh(db, [{"symbol": "SENSEX", "catalyst_type": "board", "catalyst_price": 80000.0}]) == 0
        assert db.query(models.WatchlistEntry).count() == 0

    def test_switch_off_inserts_index_rows_as_before(self, db, monkeypatch):
        monkeypatch.setenv("WATCHLIST_INDEX_FILTER", "0")
        assert self._refresh(db, [{"symbol": "NIFTY", "catalyst_type": "board", "catalyst_price": 24500.0}]) == 1


# ── trigger pass ────────────────────────────────────────────────────────────

class TestTriggerPass:
    def test_index_rows_already_in_the_table_are_retired_without_a_price_lookup(self, db, monkeypatch):
        idx = make_row(db, symbol="NIFTY", catalyst_price=24500.0)
        stock = make_row(db, symbol="TCS", catalyst_price=100.0)
        seen = []
        patch_quotes(monkeypatch, {"TCS": 101.0, "NIFTY": 24600.0}, seen)
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        db.refresh(idx)
        db.refresh(stock)
        assert idx.status == "expired" and idx.missed_reason.startswith("index:")
        assert stock.status == "active"
        assert seen == [["TCS"]]
        assert tally["index_expired"] == 1 and tally["queued"] == 1

    def test_only_index_rows_returns_an_empty_tally_without_fetching_quotes(self, db, monkeypatch):
        make_row(db, symbol="SENSEX", catalyst_price=80000.0)
        seen = []
        patch_quotes(monkeypatch, {}, seen)
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally == {"watchlist_checked": 0, "band_ok": 0, "missed": 0, "queued": 0, "adverse": 0,
                         "index_expired": 1}
        assert seen == []

    def test_index_switch_off_leaves_index_rows_alone(self, db, monkeypatch):
        monkeypatch.setenv("WATCHLIST_INDEX_FILTER", "0")
        idx = make_row(db, symbol="NIFTY", catalyst_price=24500.0)
        patch_quotes(monkeypatch, {"NIFTY": 24600.0})
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        db.refresh(idx)
        assert idx.status == "active" and "index_expired" not in tally

    def test_two_rows_one_decision_the_better_catalyst_row_queues(self, db, monkeypatch):
        # PACEDIGITK: Tier-1 row in band, Tier-3 row (own baseline) that the day-change guard would skip.
        good = make_row(db, catalyst_price=100.0, tier=1, ctype="results")
        shock = make_row(db, catalyst_price=104.0, tier=3, ctype="volume_shock")
        patch_quotes(monkeypatch, {"PACEDIGITK": 102.0})
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        cands = db.query(models.TradeCandidate).all()
        assert len(cands) == 1 and cands[0].watchlist_entry_id == good.id
        assert tally["queued"] == 1 and tally["duplicates"] == 1 and tally["watchlist_checked"] == 1
        db.refresh(shock)
        assert shock.status == "active"       # left untouched, not expired

    def test_no_flapping_across_cycles(self, db, monkeypatch):
        make_row(db, catalyst_price=100.0, tier=1, ctype="results")
        make_row(db, catalyst_price=104.0, tier=3, ctype="volume_shock")
        patch_quotes(monkeypatch, {"PACEDIGITK": 102.0})
        first = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        second = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert first["queued"] == 1 and second["queued"] == 0
        assert second["adverse"] == 0 and second["band_ok"] == 1

    def test_next_row_takes_over_when_the_primary_is_missed(self, db, monkeypatch):
        primary = make_row(db, catalyst_price=100.0, tier=1, ctype="results")
        backup = make_row(db, catalyst_price=118.0, tier=3, ctype="volume_shock")
        patch_quotes(monkeypatch, {"PACEDIGITK": 120.0})
        run(entry.evaluate_watchlist_entries(db, "DEMO"))      # primary ran away from its catalyst
        db.refresh(primary)
        assert primary.status == "missed"
        run(entry.evaluate_watchlist_entries(db, "DEMO"))      # the backup row is now the only active row
        db.refresh(backup)
        assert db.query(models.TradeCandidate).filter_by(watchlist_entry_id=backup.id).count() == 1

    def test_different_symbols_are_all_evaluated(self, db, monkeypatch):
        make_row(db, symbol="AAA")
        make_row(db, symbol="BBB")
        patch_quotes(monkeypatch, {"AAA": 101.0, "BBB": 101.0})
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally["queued"] == 2 and "duplicates" not in tally

    def test_one_row_switch_off_evaluates_every_row(self, db, monkeypatch):
        monkeypatch.setenv("WATCHLIST_ONE_ROW_PER_SYMBOL", "0")
        make_row(db, catalyst_price=100.0, tier=1)
        make_row(db, catalyst_price=104.0, tier=3, ctype="volume_shock")
        patch_quotes(monkeypatch, {"PACEDIGITK": 102.0})
        tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
        assert tally["watchlist_checked"] == 2 and "duplicates" not in tally
