"""E285 round 162 (plan item 20): a feed with no fallback that answers 404 / 410 is not downloaded again every 60 s."""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news"))

import pytest

import feed_fetch as ff

URL = "https://dead.test/rss.xml"


class _Resp:
    def __init__(self, status, body=b"x"):
        self.status_code, self.content, self.headers = status, body, {"content-type": "application/rss+xml"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    ff.clear_cache()
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 300)
    monkeypatch.setattr(ff, "DEAD_TTL_SEC", 1800)
    monkeypatch.setattr(ff.feedparser, "parse", lambda raw: types.SimpleNamespace(entries=[]))
    yield
    ff.clear_cache()


def _serve(monkeypatch, status):
    calls = []

    class C:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            calls.append(url)
            return _Resp(status)

    monkeypatch.setattr(ff.httpx, "Client", C)
    return calls


@pytest.mark.parametrize("status", [404, 410])
def test_retired_feed_without_fallback_is_remembered_for_the_dead_ttl(monkeypatch, status):
    calls = _serve(monkeypatch, status)
    p, info = ff.fetch_feed_ex(URL, "news")
    assert p.entries == [] and info["status"] == status
    assert ff._cache[URL][0] - time.monotonic() > ff._FAIL_TTL_SEC + 600       # far past the old 60 s
    n = len(calls)
    _, again = ff.fetch_feed_ex(URL, "news")
    assert again["cached"] is True and len(calls) == n


def test_zero_keeps_the_old_short_memory(monkeypatch):
    monkeypatch.setattr(ff, "DEAD_TTL_SEC", 0)
    _serve(monkeypatch, 404)
    ff.fetch_feed_ex(URL, "news")
    assert ff._cache[URL][0] - time.monotonic() <= ff._FAIL_TTL_SEC


def test_other_failures_keep_their_own_rules(monkeypatch):
    _serve(monkeypatch, 503)
    ff.fetch_feed_ex(URL, "news")
    assert ff._cache[URL][0] - time.monotonic() <= ff._FAIL_TTL_SEC
    ff.clear_cache()
    _serve(monkeypatch, 403)
    ff.fetch_feed_ex(URL, "news")
    assert ff._cache[URL][0] - time.monotonic() > ff._FAIL_TTL_SEC


def test_feed_with_a_fallback_that_serves_is_not_marked_dead_by_this_rule(monkeypatch):
    ok = types.SimpleNamespace(entries=[{"title": "A - Pub"}])
    monkeypatch.setattr(ff, "_FALLBACKS", {URL: ("Pub", ["https://fb.test/x"])})
    monkeypatch.setattr(ff.feedparser, "parse", lambda raw: ok)

    class C:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            return _Resp(404 if url == URL else 200)

    monkeypatch.setattr(ff.httpx, "Client", C)
    p, info = ff.fetch_feed_ex(URL, "news")
    assert len(p.entries) == 1 and info.get("via")
