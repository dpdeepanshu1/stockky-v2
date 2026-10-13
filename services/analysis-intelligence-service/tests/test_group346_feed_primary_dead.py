"""
group346 (log review #17): a primary news feed that is retired (404/410), frozen or serving HTML is not re-downloaded on
every cache expiry once a fallback has served for it. Transient errors never mark a feed dead.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_group346_feed_primary_dead.py -q
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


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ff.clear_cache()
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 0)          # exercise the download path on every call
    monkeypatch.setattr(ff, "PRIMARY_DEAD_TTL_SEC", 1800)
    yield
    ff.clear_cache()


def _parse_ok(raw):
    return types.SimpleNamespace(entries=[{"title": "x - CNBC TV18", "published_parsed": time.gmtime()}], bozo=False)


def test_retired_primary_is_skipped_on_the_next_call(monkeypatch):
    hits = []

    def dl(url):
        hits.append(url)
        if url == PRIMARY:
            return None, {"status": 404, "error": "http_404"}
        return b"<rss/>", {"status": 200, "error": None}

    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", _parse_ok)
    ff.fetch_feed_ex(PRIMARY)
    parsed, _ = ff.fetch_feed_ex(PRIMARY)
    assert hits.count(PRIMARY) == 1 and len(parsed.entries) == 1


def test_timeout_never_marks_a_primary_dead(monkeypatch):
    hits = []

    def dl(url):
        hits.append(url)
        if url == PRIMARY:
            return None, {"status": None, "error": "ReadTimeout"}
        return b"<rss/>", {"status": 200, "error": None}

    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", _parse_ok)
    ff.fetch_feed_ex(PRIMARY)
    ff.fetch_feed_ex(PRIMARY)
    assert hits.count(PRIMARY) == 2


def test_dead_mark_expires(monkeypatch):
    hits = []

    def dl(url):
        hits.append(url)
        if url == PRIMARY:
            return None, {"status": 404, "error": "http_404"}
        return b"<rss/>", {"status": 200, "error": None}

    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", _parse_ok)
    ff.fetch_feed_ex(PRIMARY)
    ff._primary_dead_until[PRIMARY] = time.monotonic() - 1
    ff.fetch_feed_ex(PRIMARY)
    assert hits.count(PRIMARY) == 2


def test_frozen_primary_is_skipped_on_the_next_call(monkeypatch):
    hits = []
    old = time.gmtime(time.time() - 10 * 86400)

    def parse(raw):
        if raw == b"OLD":
            return types.SimpleNamespace(entries=[{"title": "o", "published_parsed": old}], bozo=False)
        return types.SimpleNamespace(entries=[{"title": "n", "published_parsed": time.gmtime()}], bozo=False)

    def dl(url):
        hits.append(url)
        return (b"OLD" if url == PRIMARY else b"NEW"), {"status": 200, "error": None}

    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", parse)
    ff.fetch_feed_ex(PRIMARY)
    ff.fetch_feed_ex(PRIMARY)
    assert hits.count(PRIMARY) == 1


def test_switch_off(monkeypatch):
    monkeypatch.setattr(ff, "PRIMARY_DEAD_TTL_SEC", 0)
    hits = []

    def dl(url):
        hits.append(url)
        if url == PRIMARY:
            return None, {"status": 404, "error": "http_404"}
        return b"<rss/>", {"status": 200, "error": None}

    monkeypatch.setattr(ff, "_download", dl)
    monkeypatch.setattr(ff.feedparser, "parse", _parse_ok)
    ff.fetch_feed_ex(PRIMARY)
    ff.fetch_feed_ex(PRIMARY)
    assert hits.count(PRIMARY) == 2
