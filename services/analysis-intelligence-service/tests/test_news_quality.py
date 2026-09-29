"""
tests/test_news_quality.py — coverage for news/news_quality.py
No real HTTP — feedparser.parse is monkeypatched.
"""
from __future__ import annotations
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news"))

import pytest
import news_quality as nq
from datetime import datetime


def _entry(title="", summary="", published_parsed=None, link=""):
    e = types.SimpleNamespace(
        title=title, summary=summary, description="",
        published_parsed=published_parsed, link=link,
    )
    return e


def _parsed(*entries):
    return types.SimpleNamespace(entries=list(entries))


# ── expand_keywords ──────────────────────────────────────────────────────────

class TestExpandKeywords:
    def test_base_symbol_included(self):
        assert "reliance" in nq.expand_keywords("RELIANCE")

    def test_strips_ns_and_bo(self):
        kw = nq.expand_keywords("TCS.NS")
        assert "tcs" in kw
        assert ".ns" not in " ".join(kw)

    def test_company_name_words_added(self):
        kw = nq.expand_keywords("INFY", "Infosys Limited")
        assert "infosys" in kw

    def test_limited_suffix_stripped(self):
        kw = nq.expand_keywords("INFY", "Infosys Limited")
        assert "infosys" in kw

    def test_short_words_excluded(self):
        kw = nq.expand_keywords("X", "A B Co")
        assert "a" not in kw
        assert "b" not in kw

    def test_extra_aliases_pwl(self):
        kw = nq.expand_keywords("PWL")
        assert "physics wallah" in kw

    def test_extra_aliases_lgeindia(self):
        kw = nq.expand_keywords("LGEINDIA")
        assert "lg india" in kw

    def test_deduplicates(self):
        kw = nq.expand_keywords("RELIANCE", "Reliance")
        assert kw.count("reliance") == 1

    def test_empty_symbol_returns_list(self):
        kw = nq.expand_keywords("")
        assert isinstance(kw, list)

    def test_none_symbol_returns_list(self):
        kw = nq.expand_keywords(None)
        assert isinstance(kw, list)


# ── _is_relevant ─────────────────────────────────────────────────────────────

class TestIsRelevant:
    def test_keyword_in_title_is_relevant(self):
        assert nq._is_relevant("Reliance Q3 results", "", ["reliance"]) is True

    def test_keyword_in_desc_is_relevant(self):
        assert nq._is_relevant("Market update", "Reliance sees growth", ["reliance"]) is True

    def test_no_match_is_not_relevant(self):
        assert nq._is_relevant("Gold price rises", "USD up", ["reliance"]) is False

    def test_single_char_keywords_skipped(self):
        assert nq._is_relevant("A title", "", ["a"]) is False

    def test_case_insensitive(self):
        assert nq._is_relevant("RELIANCE Q3", "", ["reliance"]) is True


# ── _parse_entries ────────────────────────────────────────────────────────────

class TestParseEntries:
    def test_relevant_entry_included(self):
        entry = _entry("Reliance posts profit", "Strong Q3", link="http://x.com")
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"], max_items=5)
        assert len(result) == 1
        assert result[0]["title"] == "Reliance posts profit"

    def test_irrelevant_entry_excluded(self):
        entry = _entry("Gold hits all time high", "Bullion rally")
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"])
        assert result == []

    def test_old_entry_excluded(self):
        from datetime import timedelta
        old = nq._utcnow() - timedelta(days=20)
        entry = _entry("Reliance old news", "",
                       published_parsed=old.timetuple()[:6])
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"], days=14)
        assert result == []

    def test_recent_entry_included(self):
        from datetime import timedelta
        recent = nq._utcnow() - timedelta(days=2)
        entry = _entry("Reliance strong", "",
                       published_parsed=recent.timetuple()[:6])
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"])
        assert len(result) == 1
        assert result[0]["published_at"] is not None

    def test_max_items_respected(self):
        entries = [_entry(f"Reliance news {i}") for i in range(10)]
        result = nq._parse_entries(_parsed(*entries), "TestSource", ["reliance"], max_items=3)
        assert len(result) == 3

    def test_bad_date_not_crash(self):
        entry = _entry("Reliance news", "", published_parsed=(9999, 99, 99, 0, 0, 0))
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"])
        # Either included (bad date → published=None, no cutoff applied) or excluded
        assert isinstance(result, list)

    def test_html_tags_stripped_from_desc(self):
        entry = _entry("Reliance Q3", "<b>Revenue</b> up 10%")
        result = nq._parse_entries(_parsed(entry), "TestSource", ["reliance"])
        assert "<b>" not in result[0]["description"]

    def test_empty_feed_returns_empty(self):
        result = nq._parse_entries(_parsed(), "TestSource", ["reliance"])
        assert result == []


# ── fetch_multi_source ────────────────────────────────────────────────────────

class TestFetchMultiSource:
    def test_returns_list(self, monkeypatch):
        from datetime import timedelta
        recent = nq._utcnow() - timedelta(days=1)
        entry = _entry("Reliance surges", "Strong result",
                       published_parsed=recent.timetuple()[:6], link="http://x.com")
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: _parsed(entry))
        result = nq.fetch_multi_source("RELIANCE")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_deduplicates_by_title(self, monkeypatch):
        from datetime import timedelta
        recent = nq._utcnow() - timedelta(days=1)
        entry = _entry("Reliance Q3 profit", "",
                       published_parsed=recent.timetuple()[:6])
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: _parsed(entry))
        result = nq.fetch_multi_source("RELIANCE")
        titles = [r["title"] for r in result]
        assert len(titles) == len(set(titles))

    def test_max_25_returned(self, monkeypatch):
        from datetime import timedelta
        recent = nq._utcnow() - timedelta(days=1)
        entries = [_entry(f"Reliance news unique {i}", "",
                          published_parsed=recent.timetuple()[:6])
                   for i in range(50)]
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: _parsed(*entries))
        result = nq.fetch_multi_source("RELIANCE")
        assert len(result) <= 25

    def test_source_exception_swallowed(self, monkeypatch):
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: (_ for _ in ()).throw(RuntimeError("timeout")))
        result = nq.fetch_multi_source("RELIANCE")
        assert result == []

    def test_sorted_newest_first(self, monkeypatch):
        from datetime import timedelta
        older = (nq._utcnow() - timedelta(days=5)).timetuple()[:6]
        newer = (nq._utcnow() - timedelta(days=1)).timetuple()[:6]
        entries = [
            _entry("Reliance old", "", published_parsed=older),
            _entry("Reliance new", "", published_parsed=newer),
        ]
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: _parsed(*entries))
        result = nq.fetch_multi_source("RELIANCE")
        if len(result) >= 2:
            assert result[0]["published_at"] >= result[1]["published_at"]


    def test_sort_key_failure_is_tolerated(self, monkeypatch):
        # An item whose .get("published_at") raises must sort as "" instead of
        # crashing the whole fetch (lines 158-159).
        class _Item(dict):
            def get(self, key, default=None):
                if key == "published_at":
                    raise RuntimeError("boom")
                return super().get(key, default)

        monkeypatch.setattr(nq, "_parse_entries",
                            lambda parsed, publisher, kw, n=8, days=14:
                            [_Item(title=f"Reliance {publisher} story")])
        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url: _parsed())
        result = nq.fetch_multi_source("RELIANCE")
        assert len(result) >= 1


class TestUtcNow:
    def test_naive_current_and_no_deprecation(self):
        import warnings
        from datetime import timezone
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            t = nq._utcnow()
        assert t.tzinfo is None
        assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - t).total_seconds()) < 5


# ── summarize_headlines ───────────────────────────────────────────────────────

class TestSummarizeHeadlines:
    def test_empty_returns_no_news_msg(self):
        result = nq.summarize_headlines([], "RELIANCE")
        assert "No recent" in result

    def test_extractive_fallback(self):
        items = [{"title": "Reliance Q3 profit up"}, {"title": "Reliance signs deal"}]
        result = nq.summarize_headlines(items, "RELIANCE")
        assert "Reliance" in result
        assert len(result) > 10

    def test_llm_summarizer_used_when_provided(self):
        items = [{"title": "TCS wins deal"}]
        llm = lambda sys, usr: "TCS secures a large contract boosting revenue."
        result = nq.summarize_headlines(items, "TCS", llm_summarizer=llm)
        assert "TCS secures" in result

    def test_llm_failure_falls_back_to_extractive(self):
        items = [{"title": "INFY misses estimate"}]
        def _boom(sys, usr): raise RuntimeError("LLM down")
        result = nq.summarize_headlines(items, "INFY", llm_summarizer=_boom)
        assert "INFY" in result

    def test_llm_empty_return_falls_back(self):
        items = [{"title": "WIPRO quarterly update"}]
        result = nq.summarize_headlines(items, "WIPRO", llm_summarizer=lambda s, u: "")
        assert "WIPRO" in result

    def test_max_bullets_respected(self):
        items = [{"title": f"Reliance news {i}"} for i in range(10)]
        result = nq.summarize_headlines(items, "RELIANCE", max_bullets=3)
        # At most 3 bullets in extractive path
        assert result.count("Reliance news") <= 3


# ── build_news_response ───────────────────────────────────────────────────────

class TestBuildNewsResponse:
    def test_structure(self, monkeypatch):
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: [
            {"title": "Reliance profit up", "description": "",
             "url": "http://x.com", "publisher": "Test", "published_at": None}
        ])
        result = nq.build_news_response("RELIANCE", "Reliance Industries")
        assert result["symbol"] == "RELIANCE"
        assert "news_score" in result
        assert "summary" in result
        assert "headlines" in result
        assert result["headline_count"] == 1
        assert result["sources_checked"] == 8

    def test_empty_headlines_score_50(self, monkeypatch):
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: [])
        result = nq.build_news_response("X")
        assert result["news_score"] == 50.0

    def test_positive_sentiment_raises_score(self, monkeypatch):
        items = [{"title": "profit surge rally beat growth",
                  "description": "", "url": "", "publisher": "", "published_at": None}
                 for _ in range(5)]
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: items)
        result = nq.build_news_response("X")
        assert result["news_score"] > 50.0

    def test_negative_sentiment_lowers_score(self, monkeypatch):
        items = [{"title": "loss fraud ban probe delay",
                  "description": "", "url": "", "publisher": "", "published_at": None}
                 for _ in range(5)]
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: items)
        result = nq.build_news_response("X")
        assert result["news_score"] < 50.0

    def test_score_clamped_0_100(self, monkeypatch):
        items = [{"title": "profit " * 20,
                  "description": "", "url": "", "publisher": "", "published_at": None}
                 for _ in range(50)]
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: items)
        result = nq.build_news_response("X")
        assert 0.0 <= result["news_score"] <= 100.0

    def test_keywords_included(self, monkeypatch):
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: [])
        result = nq.build_news_response("PWL")
        assert "physics wallah" in result["keywords_used"]

    def test_headlines_capped_at_12(self, monkeypatch):
        items = [{"title": f"News {i}", "description": "",
                  "url": "", "publisher": "", "published_at": None}
                 for i in range(20)]
        monkeypatch.setattr(nq, "fetch_multi_source", lambda s, cn=None: items)
        result = nq.build_news_response("X")
        assert len(result["headlines"]) <= 12
