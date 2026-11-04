"""Round 198: a "Market feeds" news source reads nine live Business Standard / BusinessLine / ET / Mint feeds.

4 Nov feed test on the VM: every Moneycontrol feed answers 200 with entries ~22,000 h old (dead); these nine answered 200 with entries 0.1-0.7 h old.
Keyword filtered like the other site readers, shared cache + frozen warning via _parse_site_feed, a dead feed never loses the others."""
from __future__ import annotations

import os
import sys
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import pytest

import main as em  # noqa: E402  (event/main.py)


def _entry(title, desc="", hours_ago=1.0, link="https://x/y"):
    t = time.gmtime(time.time() - hours_ago * 3600)
    return types.SimpleNamespace(title=title, description=desc, published_parsed=t, link=link)


def _feeds(monkeypatch, by_url, boom=()):
    def fake_parse(url):
        if url in boom:
            raise RuntimeError("feed down")
        return types.SimpleNamespace(entries=by_url.get(url, []))
    monkeypatch.setattr(em, "_parse_site_feed", fake_parse)
    monkeypatch.setattr(em, "_get_keywords", lambda sym: ["pokarna"])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EVENT_MARKET_FEEDS", raising=False)


def test_default_list_is_the_nine_live_feeds():
    urls = em._market_feed_urls()
    assert len(urls) == 9 and len(set(urls)) == 9
    assert not any("moneycontrol" in u for u in urls)
    assert all(u.startswith("https://") for u in urls)


def test_switch_off_and_custom_list(monkeypatch):
    monkeypatch.setenv("EVENT_MARKET_FEEDS", "off")
    assert em._market_feed_urls() == [] and em._fetch_market_feeds("POKARNA.NS") == []
    monkeypatch.setenv("EVENT_MARKET_FEEDS", " https://a/x.rss , https://b/y.rss ")
    assert em._market_feed_urls() == ["https://a/x.rss", "https://b/y.rss"]


def test_publisher_names_come_from_the_feed_domain():
    assert em._market_feed_publisher("https://www.business-standard.com/rss/markets-106.rss") == "Business Standard"
    assert em._market_feed_publisher("https://www.thehindubusinessline.com/markets/feeder/default.rss") == "Business Line"
    assert em._market_feed_publisher("https://www.livemint.com/rss/markets") == "Mint"
    assert em._market_feed_publisher("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms") == "Economic Times"
    assert em._market_feed_publisher("https://unknown.example/rss") == "Market feed"


def test_matches_by_keyword_dedupes_and_sorts_newest_first(monkeypatch):
    u = em._market_feed_urls()
    _feeds(monkeypatch, {
        u[0]: [_entry("Pokarna wins export order", hours_ago=3), _entry("Sensex ends flat", hours_ago=1)],
        u[2]: [_entry("POKARNA wins export order", hours_ago=2), _entry("Pokarna Q2 profit up", "quartz maker", hours_ago=0.5)],
    })
    out = em._fetch_market_feeds("POKARNA.NS")
    assert [i["title"] for i in out] == ["Pokarna Q2 profit up", "Pokarna wins export order"]     # dup dropped (first feed wins), newest first
    assert out[0]["publisher"] == "Business Line" and out[1]["publisher"] == "Business Standard"
    assert out[0]["url"] == "https://x/y" and out[0]["published"]


def test_old_entries_and_missing_titles_are_skipped(monkeypatch):
    u = em._market_feed_urls()
    _feeds(monkeypatch, {u[0]: [_entry("Pokarna old story", hours_ago=24 * 40), _entry("", hours_ago=1), _entry("Pokarna fresh", hours_ago=1)]})
    assert [i["title"] for i in em._fetch_market_feeds("POKARNA.NS")] == ["Pokarna fresh"]


def test_one_dead_feed_does_not_lose_the_others(monkeypatch):
    u = em._market_feed_urls()
    _feeds(monkeypatch, {u[1]: [_entry("Pokarna lands order")]}, boom=(u[0], u[3]))
    assert [i["title"] for i in em._fetch_market_feeds("POKARNA.NS")] == ["Pokarna lands order"]


def test_max_items_caps_the_result(monkeypatch):
    u = em._market_feed_urls()
    _feeds(monkeypatch, {u[0]: [_entry("Pokarna story %d" % i, hours_ago=i + 1) for i in range(12)]})
    assert len(em._fetch_market_feeds("POKARNA.NS", max_items=4)) == 4


def test_it_is_the_seventh_source_and_is_tallied(monkeypatch):
    for n in ("_fetch_yf_news", "_fetch_google_news", "_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18", "_fetch_publisher_search"):
        monkeypatch.setattr(em, n, lambda s, *a, **k: [])
    monkeypatch.setattr(em, "_fetch_market_feeds", lambda s, *a, **k: [{"title": "From the feeds", "publisher": "Mint", "published": None}])
    out = em._fetch_news_from_multiple_sources("A.NS")
    assert [n["title"] for n in out] == ["From the feeds"]
    assert em._NEWS_SRC_STATS["Market feeds"]["items"] == 1
