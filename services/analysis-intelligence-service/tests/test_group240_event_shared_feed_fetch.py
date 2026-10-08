"""
tests/test_group240_event_shared_feed_fetch.py - event/main.py site feeds (Moneycontrol / ET / CNBC TV18) now download
through news/feed_fetch.py (group 240).

Before: feedparser.parse(url) - feedparser's own User-Agent, no timeout, and a bot-gated site (Moneycontrol 403) just
produced zero entries with nothing logged. Now: the shared httpx downloader (browser headers, second header profile on a
bot-gate status, Moneycontrol fallbacks, timeout). EVENT_FEED_SHARED_FETCH=0 restores the direct feedparser call; the
rest of the event tests run with it off (tests/conftest.py).

Run from services/analysis-intelligence-service:  python -m pytest tests/test_group240_event_shared_feed_fetch.py -v
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from event import main as em

MC = "https://www.moneycontrol.com/rss/latestnews.xml"


def _entry(title, link="http://x"):
    return types.SimpleNamespace(title=title, link=link)


@pytest.fixture(autouse=True)
def _shared_on(monkeypatch):
    monkeypatch.setenv("EVENT_FEED_SHARED_FETCH", "1")
    saved = dict(em._SHARED_FETCH)
    em._SHARED_FETCH.clear()
    em._FEED_CACHE.clear()
    em._KW_PATTERN_CACHE.clear()
    yield
    em._SHARED_FETCH.clear()
    em._SHARED_FETCH.update(saved)
    em._FEED_CACHE.clear()


def _install(monkeypatch, entries, seen):
    def fake(url, source="feed"):
        seen.append((url, source))
        return types.SimpleNamespace(entries=entries), {"status": 200}
    em._SHARED_FETCH["fn"] = fake

    def boom(url):
        raise AssertionError("feedparser.parse must not be called when the shared downloader is on")
    monkeypatch.setattr(em.feedparser, "parse", boom)


class TestSiteFeedParse:
    def test_uses_shared_downloader_not_feedparser(self, monkeypatch):
        seen = []
        _install(monkeypatch, [_entry("a")], seen)
        parsed = em._site_feed_parse(MC)
        assert len(parsed.entries) == 1 and seen == [(MC, "event")]

    def test_env_switch_off_uses_feedparser(self, monkeypatch):
        monkeypatch.setenv("EVENT_FEED_SHARED_FETCH", "0")
        called = []
        monkeypatch.setattr(em.feedparser, "parse", lambda u: called.append(u) or types.SimpleNamespace(entries=[]))
        em._site_feed_parse(MC)
        assert called == [MC]

    def test_unloadable_shared_module_falls_back_to_feedparser_and_is_remembered(self, monkeypatch):
        import importlib.util as ilu

        def boom(*a, **k):
            raise ImportError("nope")
        monkeypatch.setattr(ilu, "spec_from_file_location", boom)
        assert em._load_shared_fetch() is None and em._SHARED_FETCH == {"fn": None}
        called = []
        monkeypatch.setattr(em.feedparser, "parse", lambda u: called.append(u) or types.SimpleNamespace(entries=[]))
        em._site_feed_parse(MC)
        assert called == [MC]

    def test_real_loader_finds_news_feed_fetch(self):
        fn = em._load_shared_fetch()
        assert callable(fn) and fn.__name__ == "fetch_feed_ex"
        assert em._load_shared_fetch() is fn                       # loaded once

    def test_shared_downloader_is_a_noop_cache_layer_for_parse_site_feed(self, monkeypatch):
        seen = []
        _install(monkeypatch, [_entry("Zenith Widgets wins order")], seen)
        monkeypatch.setattr(em, "_get_keywords", lambda sym: ["zenith widgets"])
        a = em._fetch_moneycontrol_news("ZWID.NS")
        b = em._fetch_moneycontrol_news("ZWID.NS")
        assert len(a) == 1 and a == b
        assert len(seen) == 1                                      # event's own TTL cache still sits on top

    def test_blocked_site_yields_no_items_without_raising(self, monkeypatch):
        seen = []
        _install(monkeypatch, [], seen)
        monkeypatch.setattr(em, "_get_keywords", lambda sym: ["zenith widgets"])
        assert em._fetch_moneycontrol_news("ZWID.NS") == []

    def test_every_site_feed_goes_through_the_shared_downloader(self, monkeypatch):
        seen = []
        _install(monkeypatch, [], seen)
        monkeypatch.setattr(em, "_get_keywords", lambda sym: ["zenith widgets"])
        em._fetch_moneycontrol_news("ZWID.NS")
        em._fetch_economic_times("ZWID.NS")
        em._fetch_cnbc_tv18("ZWID.NS")
        assert [u for u, _ in seen] == [MC, "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
                                        "https://www.cnbctv18.com/feed/"]
