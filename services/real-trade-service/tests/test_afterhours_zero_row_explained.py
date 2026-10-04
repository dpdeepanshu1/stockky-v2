"""
tests/test_afterhours_zero_row_explained.py

2026-10-04 (item 17): an after-hours scan pass that wrote 0 rows used to end
with no explanation. run_afterhours_scan now logs a per-feed funnel plus a
plain-English reason. Covers _format_funnel, _explain_empty and the three
zero-row paths of run_afterhours_scan (nothing scored, all already stored,
upserts failed).

Run:  cd services/real-trade-service && python -m pytest tests/test_afterhours_zero_row_explained.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import watchlist_engine.afterhours_scan as ahs

_engine = create_engine("sqlite:///:memory:")
HEADLINE = "Reliance record profit growth beat estimates"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _item(title, pub_date=None):
    return {"title": title, "pubDate": pub_date or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}


def _scan(db, rss=None, known=("RELIANCE",), bulk=None):
    rss = rss or {}

    async def _fake(feed):
        return rss.get(feed["source"], [])

    patchers = [
        patch("symbol_master.get_all_symbols", AsyncMock(return_value=set(known))),
        patch("watchlist_engine.afterhours_scan._fetch_rss_items", AsyncMock(side_effect=_fake)),
        patch("watchlist_engine.afterhours_scan._fetch_bulk_deal_hits", AsyncMock(return_value=bulk or {})),
        patch("notifier.notify_async", AsyncMock(return_value=True)),
    ]
    for p in patchers:
        p.start()
    try:
        return run(ahs.run_afterhours_scan(db, "DEMO", "2026-10-05"))
    finally:
        for p in reversed(patchers):
            p.stop()


class TestHelpers:
    def test_format_funnel_lists_each_feed_and_bulk(self):
        out = ahs._format_funnel(
            [{"source": "A", "items": 5, "stale": 1, "no_symbol": 2, "score0": 1, "scored": 1}], 3)
        assert "A: 5 items, 1 stale, 2 no-symbol, 1 score<=0, 1 scored" in out
        assert out.endswith("bulk/block: 3 hit(s)")

    def test_explain_all_feeds_empty(self):
        f = [{"source": "A", "items": 0, "stale": 0, "no_symbol": 0, "score0": 0, "scored": 0}]
        assert "returned 0 items" in ahs._explain_empty(f, 0, True)

    def test_explain_counts_each_drop_reason(self):
        f = [{"source": "A", "items": 10, "stale": 4, "no_symbol": 5, "score0": 1, "scored": 0}]
        out = ahs._explain_empty(f, 0, True)
        assert "10 RSS item(s) fetched" in out
        assert "4 older than the max news age" in out
        assert "5 matched no NSE symbol" in out
        assert "1 scored <= 0" in out
        assert "whitelist fallback" not in out

    def test_explain_flags_missing_symbol_master(self):
        f = [{"source": "A", "items": 2, "stale": 0, "no_symbol": 2, "score0": 0, "scored": 0}]
        assert "whitelist fallback" in ahs._explain_empty(f, 0, False)

    def test_explain_items_but_bulk_only_nothing_matched(self):
        f = [{"source": "A", "items": 1, "stale": 0, "no_symbol": 0, "score0": 0, "scored": 0}]
        assert "none passed the filters" in ahs._explain_empty(f, 2, True)


class TestRunExplainsZeroRows:
    def test_nothing_scored_logs_reason_and_funnel(self, db, caplog):
        caplog.set_level(logging.INFO)
        out = _scan(db, rss={"Moneycontrol": [_item("The economy stayed resilient overall")]})
        assert out == 0
        text = caplog.text
        assert "0 rows written" in text
        assert "matched no NSE symbol" in text
        assert "funnel" in text and "bulk/block: 0 hit(s)" in text

    def test_all_empty_feeds_say_fetch_failed(self, db, caplog):
        caplog.set_level(logging.INFO)
        assert _scan(db) == 0
        assert "returned 0 items" in caplog.text

    def test_already_stored_logs_nothing_new(self, db, caplog):
        rss = {"Moneycontrol": [_item(HEADLINE)]}
        assert _scan(db, rss=rss) == 1
        caplog.clear()
        caplog.set_level(logging.INFO)
        assert _scan(db, rss=rss) == 0
        assert "already stored" in caplog.text
        assert "nothing new to write" in caplog.text
        assert "funnel" in caplog.text

    def test_upsert_failure_is_reported_not_silent(self, db, caplog):
        caplog.set_level(logging.INFO)
        bad = MagicMock()
        bad.query.side_effect = RuntimeError("db down")
        assert _scan(bad, rss={"Moneycontrol": [_item(HEADLINE)]}) == 0
        assert "FAILED" in caplog.text
        assert "failed to upsert" in caplog.text

    def test_failure_on_second_pass_is_reported(self, db, caplog):
        caplog.set_level(logging.INFO)
        rss = {"Moneycontrol": [_item(HEADLINE)]}
        assert _scan(db, rss=rss) == 1
        caplog.clear()
        real_query = db.query
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_query(*a, **k)

        db.query = flaky
        # RELIANCE fails; add a second known symbol that is already stored
        assert _scan(db, rss=rss, known=("RELIANCE",)) == 0
        assert "FAILED" in caplog.text


class TestRunReportsAlreadyStoredSplit:
    """Group 121: "10 symbol(s) scored -> 1 rows upserted" read as 9 lost symbols.
    The summary line now splits scored / new-or-updated / already stored / failed."""

    def test_summary_line_counts_already_stored(self, db, caplog):
        rss = {"Moneycontrol": [_item(HEADLINE)]}
        assert _scan(db, rss=rss, known=("RELIANCE", "TCS")) == 1
        caplog.clear()
        caplog.set_level(logging.INFO)
        # second pass: RELIANCE already stored, TCS arrives as a new bulk hit
        bulk = {"TCS": {"score": 60.0, "catalyst_type": "bulk_block", "source": "NSE",
                        "headline": "TCS bulk deal"}}
        assert _scan(db, rss=rss, known=("RELIANCE", "TCS"), bulk=bulk) == 1
        assert ("2 symbol(s) scored → 1 new/updated row(s), "
                "1 already stored at an equal or higher score (unchanged), 0 failed") in caplog.text

    def test_summary_line_counts_failures(self, db, caplog):
        caplog.set_level(logging.INFO)
        bad = MagicMock()
        bad.query.side_effect = RuntimeError("db down")
        assert _scan(bad, rss={"Moneycontrol": [_item(HEADLINE)]}) == 0
        assert "0 new/updated row(s), 0 already stored at an equal or higher score (unchanged), 1 failed" in caplog.text

    def test_telegram_header_mentions_already_stored(self, db):
        rss = {"Moneycontrol": [_item(HEADLINE)]}
        assert _scan(db, rss=rss, known=("RELIANCE", "TCS")) == 1
        sent = []

        async def _notify(text):
            sent.append(text)

        bulk = {"TCS": {"score": 60.0, "catalyst_type": "bulk_block", "source": "NSE",
                        "headline": "TCS bulk deal"}}

        async def _fake(feed):
            return rss.get(feed["source"], [])

        with patch("symbol_master.get_all_symbols", AsyncMock(return_value={"RELIANCE", "TCS"})), \
             patch("watchlist_engine.afterhours_scan._fetch_rss_items", AsyncMock(side_effect=_fake)), \
             patch("watchlist_engine.afterhours_scan._fetch_bulk_deal_hits", AsyncMock(return_value=bulk)), \
             patch("notifier.notify_async", _notify):
            assert run(ahs.run_afterhours_scan(db, "DEMO", "2026-10-05")) == 1
        assert "1 row(s) new/updated · 2 total scored this pass · 1 already stored" in sent[0]
