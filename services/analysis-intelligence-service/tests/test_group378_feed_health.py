"""group378: per-feed health registry in news/feed_fetch.py (GET /news/feed-health). Evidence only - the news score never reads it.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_group378_feed_health.py -q
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from news import feed_fetch as ff

PRIMARY = "https://www.cnbctv18.com/feed/"
OTHER = "https://example.com/plain-feed.xml"      # not in _FALLBACKS


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ff.clear_cache()
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 0)
    yield
    ff.clear_cache()


def _entries(age_h, n=2):
    ts = time.gmtime(time.time() - age_h * 3600)
    return [{"title": f"t{i}", "published_parsed": ts} for i in range(n)]


def _wire(monkeypatch, dl, parse):
    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", parse)


def _row(url):
    (r,) = [r for r in ff.health_snapshot()["feeds"] if r["url"] == ff._short(url)]
    return r


def test_healthy_feed_is_ok(monkeypatch):
    _wire(monkeypatch, lambda u: (b"<rss/>", {"status": 200, "error": None}),
          lambda raw: types.SimpleNamespace(entries=_entries(1), bozo=False))
    ff.fetch_feed_ex(OTHER, "Plain")
    r = _row(OTHER)
    assert r["state"] == "ok" and r["served_by"] == "primary" and r["entries"] == 2 and r["newest_item_age_h"] < 2 and r["failures"] == 0


def test_dead_primary_served_by_fallback_is_reported_as_fallback(monkeypatch):
    def dl(u):
        return (None, {"status": 404, "error": "http_404"}) if u == PRIMARY else (b"<rss/>", {"status": 200, "error": None})
    _wire(monkeypatch, dl, lambda raw: types.SimpleNamespace(entries=_entries(1), bozo=False))
    ff.fetch_feed_ex(PRIMARY, "CNBC")
    r = _row(PRIMARY)
    assert r["state"] == "fallback" and "news.google.com" in r["served_by"] and r["entries"] == 2


def test_blocked_with_no_fallback_result_is_blocked_and_counts_a_failure(monkeypatch):
    _wire(monkeypatch, lambda u: (None, {"status": 403, "error": "http_403"}), lambda raw: types.SimpleNamespace(entries=[], bozo=False))
    ff.fetch_feed_ex(OTHER, "Gated")
    r = _row(OTHER)
    assert r["state"] == "blocked" and r["http_status"] == 403 and r["entries"] == 0 and r["failures"] == 1 and r["checks"] == 1
    ff.fetch_feed_ex(OTHER, "Gated")
    assert _row(OTHER)["checks"] == 2 and _row(OTHER)["failures"] == 2


def test_old_items_on_a_feed_without_fallback_are_reported_frozen(monkeypatch):
    _wire(monkeypatch, lambda u: (b"<rss/>", {"status": 200, "error": None}),
          lambda raw: types.SimpleNamespace(entries=_entries(24 * 10), bozo=False))
    ff.fetch_feed_ex(OTHER, "Old")
    assert _row(OTHER)["state"] == "frozen"


def test_cache_hits_do_not_inflate_the_counters(monkeypatch):
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 300)
    _wire(monkeypatch, lambda u: (b"<rss/>", {"status": 200, "error": None}),
          lambda raw: types.SimpleNamespace(entries=_entries(1), bozo=False))
    ff.fetch_feed_ex(OTHER, "Plain")
    ff.fetch_feed_ex(OTHER, "Plain")
    assert _row(OTHER)["checks"] == 1


def test_snapshot_lists_unhealthy_first_and_tallies(monkeypatch):
    def dl(u):
        return (None, {"status": 403, "error": "http_403"}) if u == OTHER else (b"<rss/>", {"status": 200, "error": None})
    _wire(monkeypatch, dl, lambda raw: types.SimpleNamespace(entries=_entries(1) if raw else [], bozo=False))
    ff.fetch_feed_ex("https://example.com/good.xml", "Good")
    ff.fetch_feed_ex(OTHER, "Bad")
    snap = ff.health_snapshot()
    assert snap["feeds"][0]["state"] == "blocked" and snap["tally"] == {"blocked": 1, "ok": 1}


def test_endpoint_is_wired_in_news_main():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news", "main.py"), encoding="utf-8").read()
    assert '@app.get("/feed-health")' in src and "health_snapshot" in src
