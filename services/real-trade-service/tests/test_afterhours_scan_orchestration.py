"""
tests/test_afterhours_scan_orchestration.py

SESSION91 round 3 (final round) for watchlist_engine/afterhours_scan.py —
covers the two DB-writing orchestrators that rounds 1 (pure helpers) and 2
(network fetchers) deliberately deferred:

  - run_afterhours_scan        — the main per-tick scan: fetch RSS + bulk
                                  hits, score, dedupe, upsert into
                                  NextDayWatchlistEntry
  - finalize_nextday_watchlist — the ~08:45 trim pass: keep top-N by
                                  priority_score, mark the rest consumed

Same mocking shape as test_candidates_orchestration.py's own orchestration
round: rather than re-simulating every chained call these functions make
(already covered directly by round 1/2's tests of the functions they call),
each orchestrator's own lower-level dependencies (_fetch_rss_items,
_fetch_bulk_deal_hits, _validate_symbols, symbol_master.get_all_symbols,
classify_text, notifier.notify_async) are monkeypatched directly, so these
tests exercise the orchestration's OWN control flow: staleness filtering,
symbol-extraction skip, zero-score skip, the known-symbols vs. degraded-
fallback validation branch, bulk-hit merge/dedup, insert-vs-update-vs-skip
upsert logic, the consumed=False upsert-lookup filter, and best-effort
Telegram-notification failure isolation.

`db` fixture (real in-memory SQLite via SQLAlchemy) copied from
test_candidates_orchestration.py's own fixture, same convention this repo
already uses for orchestration-level tests.

CAVEAT — same as rounds 1 and 2: no network in this sandbox, so these were
NOT run through live pytest. Written and hand-traced against the real
source. Run for real before trusting the result:

    cd services/real-trade-service
    python -m pytest tests/test_afterhours_scan_orchestration.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import watchlist_engine.afterhours_scan as ahs

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _rss_mock(per_source: dict):
    """AsyncMock for _fetch_rss_items keyed by feed['source']; feeds not
    given an entry return []."""
    async def _fake(feed):
        return per_source.get(feed["source"], [])
    return AsyncMock(side_effect=_fake)


def _item(title, pub_date="2026-09-24"):
    return {"title": title, "link": "https://example.com/x", "pubDate": pub_date}


# Common patch targets bundled so each test only lists what it overrides.
def _patched(
    known_symbols=frozenset({"RELIANCE"}),
    rss_items=None,
    classify=("results",),
    validate_symbols=None,
    bulk_hits=None,
    notify_ok=True,
):
    rss_items = rss_items or {}
    validate_symbols = AsyncMock(return_value=set()) if validate_symbols is None else validate_symbols
    bulk_hits = AsyncMock(return_value={}) if bulk_hits is None else bulk_hits
    notify = AsyncMock(return_value=True) if notify_ok else AsyncMock(side_effect=RuntimeError("telegram down"))
    return [
        patch("symbol_master.get_all_symbols", AsyncMock(return_value=set(known_symbols))),
        patch("watchlist_engine.afterhours_scan._fetch_rss_items", _rss_mock(rss_items)),
        patch("watchlist_engine.afterhours_scan.classify_text", MagicMock(return_value=list(classify))),
        patch("watchlist_engine.afterhours_scan._validate_symbols", validate_symbols),
        patch("watchlist_engine.afterhours_scan._fetch_bulk_deal_hits", bulk_hits),
        patch("notifier.notify_async", notify),
    ]


class _Patches:
    """Small context-manager helper to apply a list of patch() objects."""
    def __init__(self, patchers):
        self.patchers = patchers

    def __enter__(self):
        for p in self.patchers:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self.patchers):
            p.stop()


# ── run_afterhours_scan ──────────────────────────────────────────────────────

class TestRunAfterhoursScan:
    def test_new_scored_item_is_inserted(self, db):
        headline = "record profit growth beat estimates"  # score 65 w/ +10 bonus, see round 1
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline)]},
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))

        assert written == 1
        row = db.query(models.NextDayWatchlistEntry).filter_by(mode="DEMO", symbol="RELIANCE").first()
        assert row is not None
        assert row.priority_score == 65.0
        assert row.catalyst_type == "results"
        assert row.market_date == "2026-09-25"
        assert row.consumed is False

    def test_stale_item_is_dropped(self, db):
        headline = "record profit growth beat estimates"
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline, pub_date="2020-01-01")]},
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0

    def test_item_with_no_extractable_symbol_is_skipped(self, db):
        headline = "The economy overall stayed resilient this quarter"
        with _Patches(_patched(
            known_symbols={"RELIANCE"},  # headline contains no token in this set
            rss_items={"Moneycontrol": [_item(headline)]},
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0

    def test_zero_score_item_is_skipped(self, db):
        # Same headline round 1's TestScoreHeadline proved scores 0.0
        # (negative-outcome veto). "COMPANY" stands in as the known symbol
        # purely so the extraction step succeeds and the score check is
        # what's actually being isolated here.
        headline = "Company profit falls amid weak demand"
        with _Patches(_patched(
            known_symbols={"COMPANY"},
            rss_items={"Moneycontrol": [_item(headline)]},
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0

    def test_known_symbols_available_skips_validate_symbols(self, db):
        headline = "record profit growth beat estimates"
        validate_spy = AsyncMock(return_value=set())
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline)]},
            validate_symbols=validate_spy,
        )):
            run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        validate_spy.assert_not_called()

    def test_degraded_path_confirmed_symbol_is_kept(self, db):
        headline = "Pidilitind shares gain on strong order demand"
        with _Patches(_patched(
            known_symbols=set(),  # symbol master unavailable -> degraded fallback
            rss_items={"Moneycontrol": [_item(headline)]},
            validate_symbols=AsyncMock(return_value={"PIDILITIND"}),
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 1
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="PIDILITIND").first()
        assert row is not None

    def test_degraded_path_unconfirmed_symbol_is_dropped(self, db):
        headline = "Pidilitind shares gain on strong order demand"
        with _Patches(_patched(
            known_symbols=set(),
            rss_items={"Moneycontrol": [_item(headline)]},
            validate_symbols=AsyncMock(return_value=set()),  # nothing confirmed
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0

    def test_bulk_hit_with_higher_score_overrides_rss_hit(self, db):
        headline = "record profit growth beat estimates"  # RSS score 65
        bulk = AsyncMock(return_value={
            "RELIANCE": {"score": 90.0, "headline": "Bulk deal flagged", "catalyst_type": "bulk_block", "source": "NSE-bulk-deals"},
        })
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline)]},
            bulk_hits=bulk,
        )):
            run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="RELIANCE").first()
        assert row.priority_score == 90.0
        assert row.catalyst_type == "bulk_block"

    def test_bulk_hit_with_lower_score_does_not_override_rss_hit(self, db):
        headline = "record profit growth beat estimates"  # RSS score 65
        bulk = AsyncMock(return_value={
            "RELIANCE": {"score": 20.0, "headline": "Minor bulk note", "catalyst_type": "bulk_block", "source": "NSE-bulk-deals"},
        })
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline)]},
            bulk_hits=bulk,
        )):
            run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="RELIANCE").first()
        assert row.priority_score == 65.0
        assert row.catalyst_type == "results"

    def test_bulk_only_symbol_with_no_rss_hit_is_inserted(self, db):
        bulk = AsyncMock(return_value={
            "TCS": {"score": 55.0, "headline": "TCS bulk deal", "catalyst_type": "bulk_block", "source": "NSE-bulk-deals"},
        })
        with _Patches(_patched(known_symbols={"RELIANCE"}, bulk_hits=bulk)):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 1
        assert db.query(models.NextDayWatchlistEntry).filter_by(symbol="TCS").first() is not None

    def test_existing_lower_score_row_is_updated(self, db):
        db.add(models.NextDayWatchlistEntry(
            mode="DEMO", symbol="RELIANCE", catalyst_type="news", catalyst_source="Old",
            headline="old headline", priority_score=10.0, market_date="2026-09-25",
            collected_at=datetime.now(timezone.utc), consumed=False,
        ))
        db.commit()
        headline = "record profit growth beat estimates"  # score 65
        with _Patches(_patched(known_symbols={"RELIANCE"}, rss_items={"Moneycontrol": [_item(headline)]})):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 1
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="RELIANCE").first()
        assert row.priority_score == 65.0
        assert row.catalyst_type == "results"

    def test_existing_higher_or_equal_score_row_is_not_updated(self, db):
        db.add(models.NextDayWatchlistEntry(
            mode="DEMO", symbol="RELIANCE", catalyst_type="news", catalyst_source="Old",
            headline="old headline", priority_score=99.0, market_date="2026-09-25",
            collected_at=datetime.now(timezone.utc), consumed=False,
        ))
        db.commit()
        headline = "record profit growth beat estimates"  # score 65 < 99
        with _Patches(_patched(known_symbols={"RELIANCE"}, rss_items={"Moneycontrol": [_item(headline)]})):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0
        row = db.query(models.NextDayWatchlistEntry).filter_by(symbol="RELIANCE").first()
        assert row.priority_score == 99.0
        assert row.headline == "old headline"

    def test_consumed_existing_row_is_ignored_by_upsert_lookup(self, db):
        db.add(models.NextDayWatchlistEntry(
            mode="DEMO", symbol="RELIANCE", catalyst_type="news", catalyst_source="Old",
            headline="old consumed headline", priority_score=99.0, market_date="2026-09-25",
            collected_at=datetime.now(timezone.utc), consumed=True,
        ))
        db.commit()
        headline = "record profit growth beat estimates"
        with _Patches(_patched(known_symbols={"RELIANCE"}, rss_items={"Moneycontrol": [_item(headline)]})):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        # upsert lookup only matches consumed=False, so this is a fresh insert,
        # not an update -- two rows now exist for the same symbol/date.
        assert written == 1
        rows = db.query(models.NextDayWatchlistEntry).filter_by(symbol="RELIANCE").all()
        assert len(rows) == 2

    def test_no_scored_items_returns_zero_and_writes_nothing(self, db):
        with _Patches(_patched(known_symbols={"RELIANCE"})):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 0
        assert db.query(models.NextDayWatchlistEntry).count() == 0

    def test_notify_failure_does_not_crash_or_change_written_count(self, db):
        headline = "record profit growth beat estimates"
        with _Patches(_patched(
            known_symbols={"RELIANCE"},
            rss_items={"Moneycontrol": [_item(headline)]},
            notify_ok=False,
        )):
            written = run(ahs.run_afterhours_scan(db, "DEMO", "2026-09-25"))
        assert written == 1


# ── finalize_nextday_watchlist ───────────────────────────────────────────────

def _row(symbol, score, mode="DEMO", market_date="2026-09-25", consumed=False):
    return models.NextDayWatchlistEntry(
        mode=mode, symbol=symbol, catalyst_type="results", catalyst_source="Moneycontrol",
        headline=f"{symbol} headline", priority_score=score, market_date=market_date,
        collected_at=datetime.now(timezone.utc), consumed=consumed,
    )


class TestFinalizeNextdayWatchlist:
    def test_no_active_rows_returns_empty_list(self, db):
        with patch("notifier.notify_async", AsyncMock(return_value=True)):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert result == []

    def test_rows_within_limit_are_all_kept_sorted_by_score_desc(self, db):
        db.add_all([_row("A", 10), _row("B", 30), _row("C", 20)])
        db.commit()
        with patch("notifier.notify_async", AsyncMock(return_value=True)):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert result == ["B", "C", "A"]
        assert db.query(models.NextDayWatchlistEntry).filter_by(consumed=True).count() == 0

    def test_rows_over_limit_are_trimmed_and_discarded_marked_consumed(self, db):
        max_picks = config.AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES
        rows = [_row(f"SYM{i}", score=float(i)) for i in range(max_picks + 3)]
        db.add_all(rows)
        db.commit()
        with patch("notifier.notify_async", AsyncMock(return_value=True)):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert len(result) == max_picks
        discarded = db.query(models.NextDayWatchlistEntry).filter_by(consumed=True).all()
        assert len(discarded) == 3
        for r in discarded:
            assert r.consumed_at is not None

    def test_already_consumed_rows_are_excluded_and_untouched(self, db):
        db.add_all([_row("ACTIVE", 50), _row("OLD", 999, consumed=True)])
        db.commit()
        with patch("notifier.notify_async", AsyncMock(return_value=True)):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert result == ["ACTIVE"]
        old = db.query(models.NextDayWatchlistEntry).filter_by(symbol="OLD").first()
        assert old.consumed_at is None  # untouched, not re-processed

    def test_mode_isolation(self, db):
        db.add_all([_row("DEMOSYM", 50, mode="DEMO"), _row("REALSYM", 90, mode="REAL")])
        db.commit()
        with patch("notifier.notify_async", AsyncMock(return_value=True)):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert result == ["DEMOSYM"]

    def test_notify_failure_does_not_crash_or_change_result(self, db):
        db.add_all([_row("A", 10), _row("B", 30)])
        db.commit()
        with patch("notifier.notify_async", AsyncMock(side_effect=RuntimeError("telegram down"))):
            result = run(ahs.finalize_nextday_watchlist(db, "DEMO", "2026-09-25"))
        assert result == ["B", "A"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
