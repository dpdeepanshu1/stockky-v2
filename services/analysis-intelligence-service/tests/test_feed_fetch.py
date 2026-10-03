"""
tests/test_feed_fetch.py - news/feed_fetch.py (2026-10-04, item 9: News pillar returned 0 items from every source).

No network: httpx.Client and feedparser.parse are faked.
Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_feed_fetch.py -v
"""
from __future__ import annotations

import logging
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "news"))

import pytest

import feed_fetch as ff


class _Resp:
    def __init__(self, status=200, body=b"<rss><channel/></rss>", ctype="application/rss+xml"):
        self.status_code, self.content, self.headers = status, body, {"content-type": ctype}


def _client(resp=None, exc=None, seen=None):
    class C:
        def __init__(self, **kw):
            if seen is not None:
                seen["kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            if seen is not None:
                seen["url"] = url
            if exc:
                raise exc
            return resp
    return C


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ff.clear_cache()
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 300)
    yield
    ff.clear_cache()


def _parsed(n):
    return types.SimpleNamespace(entries=[object()] * n)


class TestDownload:
    def test_sends_browser_user_agent_and_timeout(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(ff.httpx, "Client", _client(_Resp(), seen=seen))
        body, info = ff._download("https://x.test/rss")
        assert body and info["status"] == 200
        assert "Mozilla/5.0" in seen["kw"]["headers"]["User-Agent"]
        assert seen["kw"]["timeout"] == ff.FEED_TIMEOUT_SEC and seen["kw"]["follow_redirects"] is True

    def test_non_200_is_none_and_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(ff.httpx, "Client", _client(_Resp(status=403, body=b"no")))
        with caplog.at_level(logging.WARNING):
            body, info = ff._download("https://x.test/rss")
        assert body is None and info["error"] == "http_403" and "HTTP 403" in caplog.text

    def test_html_body_is_not_a_feed(self, monkeypatch, caplog):
        monkeypatch.setattr(ff.httpx, "Client", _client(_Resp(body=b"<!DOCTYPE html><html>", ctype="text/html; charset=utf-8")))
        with caplog.at_level(logging.WARNING):
            body, info = ff._download("https://x.test/rss")
        assert body is None and info["error"] == "html_instead_of_feed" and "HTML" in caplog.text

    def test_transport_error_never_raises(self, monkeypatch):
        monkeypatch.setattr(ff.httpx, "Client", _client(exc=TimeoutError("slow")))
        body, info = ff._download("https://x.test/rss")
        assert body is None and info["error"] == "TimeoutError"


class TestFetchFeed:
    def test_success_parses_and_caches(self, monkeypatch):
        calls = {"dl": 0, "parse": 0}

        def dl(url):
            calls["dl"] += 1
            return b"raw", {"status": 200, "error": None}

        def parse(raw):
            calls["parse"] += 1
            return _parsed(3)

        monkeypatch.setattr(ff, "_download", dl)
        monkeypatch.setattr(ff.feedparser, "parse", parse)
        p1, i1 = ff.fetch_feed_ex("https://x.test/a", "A")
        p2, i2 = ff.fetch_feed_ex("https://x.test/a", "A")
        assert len(p1.entries) == 3 and i1["entries"] == 3 and i1["cached"] is False
        assert p2 is p1 and i2["cached"] is True
        assert calls == {"dl": 1, "parse": 1}

    def test_zero_entries_is_flagged_and_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(ff, "_download", lambda url: (b"raw", {"status": 200, "error": None}))
        monkeypatch.setattr(ff.feedparser, "parse", lambda raw: _parsed(0))
        with caplog.at_level(logging.WARNING):
            _, info = ff.fetch_feed_ex("https://x.test/b", "B")
        assert info["error"] == "zero_entries" and "0 entries" in caplog.text

    def test_failed_download_gives_empty_entries_and_is_cached_briefly(self, monkeypatch):
        n = {"dl": 0}

        def dl(url):
            n["dl"] += 1
            return None, {"status": 403, "error": "http_403"}

        monkeypatch.setattr(ff, "_download", dl)
        p, info = ff.fetch_feed_ex("https://x.test/c")
        assert p.entries == [] and info["error"] == "http_403"
        ff.fetch_feed_ex("https://x.test/c")
        assert n["dl"] == 1                                    # blocked source not hammered

    def test_parse_exception_propagates_and_is_not_cached(self, monkeypatch):
        monkeypatch.setattr(ff, "_download", lambda url: (b"raw", {"status": 200, "error": None}))

        def boom(raw):
            raise RuntimeError("bad xml")

        monkeypatch.setattr(ff.feedparser, "parse", boom)
        with pytest.raises(RuntimeError):
            ff.fetch_feed("https://x.test/d")
        monkeypatch.setattr(ff.feedparser, "parse", lambda raw: _parsed(1))
        assert len(ff.fetch_feed("https://x.test/d").entries) == 1

    def test_ttl_zero_disables_cache(self, monkeypatch):
        monkeypatch.setattr(ff, "CACHE_TTL_SEC", 0)
        n = {"dl": 0}

        def dl(url):
            n["dl"] += 1
            return b"raw", {"status": 200, "error": None}

        monkeypatch.setattr(ff, "_download", dl)
        monkeypatch.setattr(ff.feedparser, "parse", lambda raw: _parsed(1))
        ff.fetch_feed("https://x.test/e")
        ff.fetch_feed("https://x.test/e")
        assert n["dl"] == 2

    def test_google_search_ttl_is_capped(self):
        assert ff._ttl_for("https://news.google.com/rss/search?q=x") == 120
        assert ff._ttl_for("https://www.moneycontrol.com/rss/latestnews.xml") == 300

    def test_cache_is_bounded(self, monkeypatch):
        monkeypatch.setattr(ff, "_download", lambda url: (b"raw", {"status": 200, "error": None}))
        monkeypatch.setattr(ff.feedparser, "parse", lambda raw: _parsed(1))
        for i in range(450):
            ff.fetch_feed(f"https://x.test/{i}")
        assert len(ff._cache) <= 400


class TestEnv:
    @pytest.mark.parametrize("raw,expected", [("", 300), ("  ", 300), ("abc", 300), ("99999", 300), ("120", 120), ("0", 0)])
    def test_env_int_blank_safe(self, monkeypatch, raw, expected):
        monkeypatch.setenv("NEWS_FEED_CACHE_TTL_SEC", raw)
        assert ff._env_int("NEWS_FEED_CACHE_TTL_SEC", 300, 0, 3600) == expected
