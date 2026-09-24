"""
tests/test_afterhours_scan_pure_helpers.py

SESSION91 (2026-09-24) — round 1 of closing watchlist_engine/afterhours_scan.py's
coverage gap (13%, 962 lines). Only _extract_symbol had direct tests before this
(tests/test_afterhours_extract_symbol.py); this file covers the other five
self-contained, DB-free, network-free helpers, same "pure helpers first" order
this repo's other coverage rounds used (candidates.py round 1, auto_pilot.py
round 1):

  - _has_uncontextualized_negative  (cost-context sentiment veto exception)
  - _score_headline                 (0-100 priority score)
  - _parse_item_datetime            (RFC-822 / ISO-8601 / plain-date parser)
  - _is_within_max_age              (recency filter)
  - _parse_feed_items               (RSS 2.0 vs Atom XML parsing)

NOT covered here (deferred — need httpx mocking and/or a db fixture):
  _fetch_rss_items, _fetch_bulk_deal_hits, _validate_symbols,
  run_afterhours_scan, finalize_nextday_watchlist.

CAVEAT (same as sessions 76/77/82c/86): this sandbox has no network access,
so these tests were written and traced by hand against the actual source
(py_compile + manual step-through of every branch touched), NOT run through
a live pytest. Run for real on the VM before trusting the pass/fail result:

    cd services/real-trade-service
    python -m pytest tests/test_afterhours_scan_pure_helpers.py -v

and fix anything that doesn't match — the trace-by-hand approach has already
been shown (session87) to occasionally get a fixture's own expected value
wrong even when the production logic itself is fine.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from watchlist_engine.afterhours_scan import (
    _has_uncontextualized_negative,
    _is_within_max_age,
    _parse_feed_items,
    _parse_item_datetime,
    _score_headline,
)


# ── _has_uncontextualized_negative ──────────────────────────────────────────

class TestHasUncontextualizedNegative:
    def test_genuine_negative_is_flagged(self):
        h = "company profit falls amid weak demand"
        assert _has_uncontextualized_negative(h) is True

    def test_cost_context_single_connector_word_is_not_flagged(self):
        # The module's own worked example: "fall" immediately followed by
        # the connector "in" then the cost noun "input" (costs).
        h = "fall in input costs boosts profit margin"
        assert _has_uncontextualized_negative(h) is False

    def test_cost_context_multiword_noun_via_connector_is_not_flagged(self):
        # window[1] alone ("raw") isn't in _COST_CONTEXT_WORDS, but the
        # two-word phrase "raw material" (window_str minus the connector) is
        # — exercises the window_str[...] branch specifically.
        h = "drop in raw material costs boosts margins"
        assert _has_uncontextualized_negative(h) is False

    def test_cost_word_directly_after_keyword_no_connector_is_not_flagged(self):
        h = "decline cost pressure eases for makers"
        assert _has_uncontextualized_negative(h) is False

    def test_no_negative_keyword_present_returns_false(self):
        h = "record profit growth beats estimates"
        assert _has_uncontextualized_negative(h) is False

    def test_negative_keyword_with_no_cost_word_nearby_is_flagged(self):
        h = "shares tumble after weak guidance"
        assert _has_uncontextualized_negative(h) is True

    def test_negative_keyword_at_end_of_headline_short_window_is_flagged(self):
        # Keyword is the last word — window is empty, so `if window and ...`
        # is falsy and the elif's len(window) >= 2 is also falsy: not a
        # cost context, must be flagged.
        h = "quarterly results miss"
        assert _has_uncontextualized_negative(h) is True


# ── _score_headline ──────────────────────────────────────────────────────────

class TestScoreHeadline:
    def test_negative_headline_scores_zero(self):
        h = "Company profit falls amid weak demand"
        assert _score_headline(h, ["results"], 10.0) == 0.0

    def test_no_positive_keyword_scores_zero(self):
        h = "Nifty ends flat in a quiet session"
        assert _score_headline(h, ["news"], 10.0) == 0.0

    def test_results_headline_with_bonus_keywords(self):
        h = "record profit growth beat estimates"
        # base=40 (results) + source_bonus=10 + kw_bonus: "record"(5) +
        # "beat"(5) + "profit growth"(5) = 15 (already at the 15 cap).
        assert _score_headline(h, ["results"], 10.0) == 65.0

    def test_empty_catalyst_types_defaults_to_news_base_score(self):
        h = "stock gains on strong buy interest"
        # base=15 (news default) + source_bonus=8 + kw_bonus=0 (no
        # _BONUS_KEYWORDS phrase present) = 23
        assert _score_headline(h, [], 8.0) == 23.0

    def test_score_is_capped_at_100(self):
        h = (
            "record profit growth revenue growth beat highest upgrade "
            "acquisition strong results q4 results order win fund raise"
        )
        assert _score_headline(h, ["results"], 100.0) == 100.0

    def test_uses_highest_base_score_among_multiple_catalyst_types(self):
        h = "strong buy rating after board approval"
        # "board"=25 vs "news"=15 -> should use 25, not 15.
        score_multi = _score_headline(h, ["news", "board"], 0.0)
        score_news_only = _score_headline(h, ["news"], 0.0)
        assert score_multi > score_news_only


# ── _parse_item_datetime ─────────────────────────────────────────────────────

class TestParseItemDatetime:
    def test_none_returns_none(self):
        assert _parse_item_datetime(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_item_datetime("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_item_datetime("   ") is None

    def test_unparseable_garbage_returns_none(self):
        assert _parse_item_datetime("not a date at all") is None

    def test_rfc822_pubdate_parses(self):
        dt = _parse_item_datetime("Wed, 16 Sep 2026 21:32:36 +0530")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 9, 16)
        assert dt.tzinfo is not None

    def test_iso8601_with_trailing_z_parses(self):
        dt = _parse_item_datetime("2026-01-14T12:23:24.829Z")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 1, 14)
        assert dt.tzinfo is not None

    def test_iso8601_with_explicit_offset_parses(self):
        dt = _parse_item_datetime("2026-01-14T12:23:24+05:30")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 1, 14)
        assert dt.tzinfo is not None

    def test_plain_yyyy_mm_dd_parses(self):
        dt = _parse_item_datetime("2026-09-16")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 9, 16)
        assert dt.tzinfo is not None

    def test_naive_result_gets_utc_attached(self):
        # Whichever branch handles it, the function's contract is "always
        # return an aware datetime or None" — never a naive one.
        dt = _parse_item_datetime("2026-09-16")
        assert dt.utcoffset() is not None


# ── _is_within_max_age ───────────────────────────────────────────────────────

class TestIsWithinMaxAge:
    def test_none_datetime_is_treated_as_within_age(self):
        assert _is_within_max_age(None) is True

    def test_recent_datetime_is_within_age(self):
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
        recent = now - timedelta(days=config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS - 1)
        assert _is_within_max_age(recent, now) is True

    def test_old_datetime_is_not_within_age(self):
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
        old = now - timedelta(days=config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS + 1)
        assert _is_within_max_age(old, now) is False

    def test_exactly_at_boundary_is_within_age(self):
        # `<= max_age`, so the boundary itself still counts as within.
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
        boundary = now - timedelta(days=config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS)
        assert _is_within_max_age(boundary, now) is True

    def test_default_now_uses_current_time(self):
        # No `now` passed -> falls back to datetime.now(timezone.utc);
        # a timestamp from a minute ago must still read as within age.
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert _is_within_max_age(recent) is True


# ── _parse_feed_items ─────────────────────────────────────────────────────────

_RSS_XML = """<rss version="2.0"><channel>
<item>
  <title>Reliance jumps 5% on strong Q4 results</title>
  <link>https://example.com/1</link>
  <pubDate>Wed, 16 Sep 2026 21:32:36 +0530</pubDate>
</item>
<item>
  <title></title>
  <link>https://example.com/2</link>
  <pubDate>Wed, 16 Sep 2026 21:00:00 +0530</pubDate>
</item>
</channel></rss>"""

_ATOM_XML = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>TCS wins large export order</title>
  <link rel="self" href="https://example.com/self"/>
  <link href="https://example.com/alt"/>
  <published>2026-01-14T12:23:24.829Z</published>
</entry>
<entry>
  <title></title>
  <link href="https://example.com/empty-title"/>
</entry>
<entry>
  <title>Fallback to updated when published is missing</title>
  <link href="https://example.com/updated-only"/>
  <updated>2026-01-01T00:00:00+00:00</updated>
</entry>
</feed>"""

_EMPTY_XML = """<rss version="2.0"><channel></channel></rss>"""


class TestParseFeedItems:
    def test_rss_items_parsed_and_empty_title_skipped(self):
        root = ET.fromstring(_RSS_XML)
        items = _parse_feed_items(root)
        assert len(items) == 1
        assert items[0]["title"] == "Reliance jumps 5% on strong Q4 results"
        assert items[0]["link"] == "https://example.com/1"
        assert items[0]["pubDate"] == "Wed, 16 Sep 2026 21:32:36 +0530"

    def test_atom_entries_parsed_preferring_alternate_link(self):
        root = ET.fromstring(_ATOM_XML)
        items = _parse_feed_items(root)
        # Second entry (empty title) is skipped; 2 remain.
        assert len(items) == 2
        first = items[0]
        assert first["title"] == "TCS wins large export order"
        # rel="self" link must be skipped in favor of the untagged
        # (implicitly "alternate") link.
        assert first["link"] == "https://example.com/alt"
        assert first["pubDate"] == "2026-01-14T12:23:24.829Z"

    def test_atom_entry_falls_back_to_updated_when_published_missing(self):
        root = ET.fromstring(_ATOM_XML)
        items = _parse_feed_items(root)
        third = items[1]
        assert third["title"] == "Fallback to updated when published is missing"
        assert third["pubDate"] == "2026-01-01T00:00:00+00:00"

    def test_neither_rss_items_nor_atom_entries_returns_empty_list(self):
        root = ET.fromstring(_EMPTY_XML)
        assert _parse_feed_items(root) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
