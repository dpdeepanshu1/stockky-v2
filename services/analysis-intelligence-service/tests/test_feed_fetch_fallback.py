"""
tests/test_feed_fetch_fallback.py - news/feed_fetch.py, group 239 (Moneycontrol HTTP 403 in the news pillar).

  * a bot-gate status retries the same URL once with the alternate header profile;
  * a feed with registered fallbacks (Moneycontrol) falls back to them when the primary download fails,
    and Google News " - Moneycontrol" title suffixes are stripped;
  * a fully blocked feed is remembered for NEWS_FEED_BLOCKED_TTL_SEC (not 60 s);
  * NEWS_FEED_FALLBACKS=0 turns the fallbacks off.

No network: httpx.Client and feedparser.parse are faked.
Run from services/analysis-intelligence-service:  python3 -m pytest tests/test_feed_fetch_fallback.py -v
"""
from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "news"))

import pytest

import feed_fetch as ff

MC = "https://www.moneycontrol.com/rss/latestnews.xml"
FB1 = "https://www.moneycontrol.com/rss/business.xml"
FB2 = "https://news.google.com/rss/search?q=site:moneycontrol.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en"


class _Resp:
    def __init__(self, status=200, body=b"<rss/>", ctype="application/rss+xml"):
        self.status_code, self.content, self.headers = status, body, {"content-type": ctype}


def _client_factory(table, calls):
    """table: url -> response or list of responses (consumed in order, last one repeats)."""
    seqs = {k: (list(v) if isinstance(v, list) else [v]) for k, v in table.items()}

    class C:
        def __init__(self, **kw):
            self.headers = kw.get("headers") or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls.append((url, self.headers.get("User-Agent", "")))
            seq = seqs[url]
            return seq.pop(0) if len(seq) > 1 else seq[0]
    return C


def _parsed_from(raw):
    # the "body" doubles as a '|'-separated list of titles for the fake parser; a title starting "OLD:" gets a
    # 2020 published_parsed, "NEW:" gets now (the prefix is stripped), anything else has no date at all.
    import time as _t
    entries = []
    for t in [t for t in raw.decode().split("|") if t]:
        e = {"title": t}
        if t.startswith("OLD:"):
            e["title"], e["published_parsed"] = t[4:], _t.gmtime(1577836800)
        elif t.startswith("NEW:"):
            e["title"], e["published_parsed"] = t[4:], _t.gmtime()
        entries.append(e)
    return types.SimpleNamespace(entries=entries)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ff.clear_cache()
    monkeypatch.setattr(ff, "CACHE_TTL_SEC", 300)
    monkeypatch.setattr(ff, "_FALLBACKS_ENABLED", True)
    monkeypatch.setattr(ff.feedparser, "parse", _parsed_from)
    yield
    ff.clear_cache()


def _use(monkeypatch, table):
    calls = []
    monkeypatch.setattr(ff.httpx, "Client", _client_factory(table, calls))
    return calls


class TestHeaderRetry:
    def test_403_then_ok_on_alternate_profile(self, monkeypatch):
        calls = _use(monkeypatch, {"https://x.test/rss": [_Resp(403, b"no"), _Resp(200, b"A|B")]})
        body, info = ff._download("https://x.test/rss")
        assert body == b"A|B" and info["error"] is None
        assert len(calls) == 2 and calls[0][1] != calls[1][1]

    def test_403_on_both_profiles_fails(self, monkeypatch):
        calls = _use(monkeypatch, {"https://x.test/rss": _Resp(403, b"no")})
        body, info = ff._download("https://x.test/rss")
        assert body is None and info["error"] == "http_403" and len(calls) == 2

    def test_non_block_status_does_not_retry(self, monkeypatch):
        calls = _use(monkeypatch, {"https://x.test/rss": _Resp(503, b"x")})
        body, info = ff._download("https://x.test/rss")
        assert body is None and info["error"] == "http_503" and len(calls) == 1


class TestFallbacks:
    def test_primary_ok_no_fallback_requests(self, monkeypatch):
        calls = _use(monkeypatch, {MC: _Resp(200, b"Reliance order win")})
        p, info = ff.fetch_feed_ex(MC, "news")
        assert [e["title"] for e in p.entries] == ["Reliance order win"] and "via" not in info
        assert [c[0] for c in calls] == [MC]

    def test_blocked_primary_served_by_second_moneycontrol_path(self, monkeypatch):
        calls = _use(monkeypatch, {MC: _Resp(403, b"no"), FB1: _Resp(200, b"TCS wins deal")})
        p, info = ff.fetch_feed_ex(MC, "news")
        assert [e["title"] for e in p.entries] == ["TCS wins deal"]
        assert info["via"] == FB1 and info["entries"] == 1 and info["error"] is None
        assert [c[0] for c in calls] == [MC, MC, FB1]

    def test_google_fallback_strips_publisher_suffix(self, monkeypatch):
        _use(monkeypatch, {MC: _Resp(403, b"no"), FB1: _Resp(403, b"no"),
                           FB2: _Resp(200, b"Infosys bags deal - Moneycontrol|Plain headline")})
        p, info = ff.fetch_feed_ex(MC, "news")
        assert [e["title"] for e in p.entries] == ["Infosys bags deal", "Plain headline"]
        assert info["via"] == FB2

    def test_fallback_result_is_cached_under_the_primary_url(self, monkeypatch):
        calls = _use(monkeypatch, {MC: _Resp(403, b"no"), FB1: _Resp(200, b"A")})
        ff.fetch_feed_ex(MC, "news")
        n = len(calls)
        p, info = ff.fetch_feed_ex(MC, "news")
        assert info["cached"] is True and len(calls) == n and len(p.entries) == 1

    def test_all_blocked_is_cached_for_the_blocked_ttl_not_60s(self, monkeypatch):
        calls = _use(monkeypatch, {MC: _Resp(403, b"no"), FB1: _Resp(403, b"no"), FB2: _Resp(403, b"no")})
        p, info = ff.fetch_feed_ex(MC, "news")
        assert p.entries == [] and info["error"] == "http_403" or info["status"] == 403
        n = len(calls)
        ff.fetch_feed_ex(MC, "news")
        assert len(calls) == n                              # second call served from the negative cache
        remaining = ff._cache[MC][0] - __import__("time").monotonic()
        assert remaining > ff._FAIL_TTL_SEC                  # remembered well past the old 60 s

    def test_non_block_failure_keeps_the_short_cache(self, monkeypatch):
        _use(monkeypatch, {"https://x.test/f": _Resp(503, b"x")})
        ff.fetch_feed_ex("https://x.test/f", "news")
        import time
        assert ff._cache["https://x.test/f"][0] - time.monotonic() <= ff._FAIL_TTL_SEC

    def test_feed_without_fallbacks_is_unchanged(self, monkeypatch):
        calls = _use(monkeypatch, {"https://x.test/g": _Resp(403, b"no")})
        p, info = ff.fetch_feed_ex("https://x.test/g", "news")
        assert p.entries == [] and len(calls) == 2           # primary x two header profiles, nothing else

    def test_env_switch_disables_fallbacks(self, monkeypatch):
        monkeypatch.setattr(ff, "_FALLBACKS_ENABLED", False)
        calls = _use(monkeypatch, {MC: _Resp(403, b"no")})
        p, _ = ff.fetch_feed_ex(MC, "news")
        assert p.entries == [] and [c[0] for c in calls] == [MC, MC]

    def test_empty_fallback_feed_is_not_accepted(self, monkeypatch):
        _use(monkeypatch, {MC: _Resp(403, b"no"), FB1: _Resp(200, b""), FB2: _Resp(403, b"no")})
        p, info = ff.fetch_feed_ex(MC, "news")
        assert p.entries == [] and "via" not in info

    def test_strip_publisher_only_strips_trailing_publisher(self):
        p = types.SimpleNamespace(entries=[{"title": "Moneycontrol picks - Mint"}, {"title": "Deal | moneycontrol"}])
        ff._strip_publisher(p, "Moneycontrol")
        assert [e["title"] for e in p.entries] == ["Moneycontrol picks - Mint", "Deal"]

    def test_registry_lists_moneycontrol_with_https_fallbacks(self):
        pub, urls = ff._FALLBACKS[MC]
        assert pub == "Moneycontrol" and urls and all(u.startswith("https://") for u in urls)


NDTV = "https://prod-qt-images.s3.amazonaws.com/production/bloombergquint/feed.xml"
CNBC = "https://www.cnbctv18.com/feed/"
GN_NDTV = ff._FALLBACKS[NDTV][1][0]
GN_CNBC = ff._FALLBACKS[CNBC][1][0]


class TestStaleAndNewFallbacks:
    def test_frozen_primary_uses_fallback_and_strips_publisher(self, monkeypatch):
        calls = _use(monkeypatch, {NDTV: _Resp(200, b"OLD:Ancient story|OLD:Another old one"),
                                   GN_NDTV: _Resp(200, b"NEW:Sensex jumps - NDTV Profit")})
        p, info = ff.fetch_feed_ex(NDTV, "news")
        assert [e["title"] for e in p.entries] == ["Sensex jumps"] and info["via"] == GN_NDTV
        assert [c[0] for c in calls] == [NDTV, GN_NDTV]

    def test_frozen_primary_with_no_better_fallback_keeps_primary_entries(self, monkeypatch):
        _use(monkeypatch, {NDTV: _Resp(200, b"OLD:Ancient story"), GN_NDTV: _Resp(403, b"no")})
        p, info = ff.fetch_feed_ex(NDTV, "news")
        assert [e["title"] for e in p.entries] == ["Ancient story"] and info["error"] == "all_stale"

    def test_one_fresh_entry_is_not_frozen(self, monkeypatch):
        calls = _use(monkeypatch, {NDTV: _Resp(200, b"OLD:Ancient story|NEW:Today")})
        p, _ = ff.fetch_feed_ex(NDTV, "news")
        assert len(p.entries) == 2 and [c[0] for c in calls] == [NDTV]

    def test_undated_entries_are_never_frozen(self, monkeypatch):
        calls = _use(monkeypatch, {NDTV: _Resp(200, b"No date here")})
        ff.fetch_feed_ex(NDTV, "news")
        assert [c[0] for c in calls] == [NDTV]

    def test_stale_check_only_applies_to_feeds_with_fallbacks(self, monkeypatch):
        calls = _use(monkeypatch, {"https://x.test/old": _Resp(200, b"OLD:Ancient story")})
        p, _ = ff.fetch_feed_ex("https://x.test/old", "news")
        assert len(p.entries) == 1 and len(calls) == 1

    def test_stale_days_zero_disables_the_check(self, monkeypatch):
        monkeypatch.setattr(ff, "STALE_DAYS", 0)
        calls = _use(monkeypatch, {NDTV: _Resp(200, b"OLD:Ancient story")})
        ff.fetch_feed_ex(NDTV, "news")
        assert [c[0] for c in calls] == [NDTV]

    def test_cnbc_404_falls_back_to_google_news_site_search(self, monkeypatch):
        calls = _use(monkeypatch, {CNBC: _Resp(404, b"gone"), GN_CNBC: _Resp(200, b"NEW:Nifty ends higher - CNBC TV18")})
        p, info = ff.fetch_feed_ex(CNBC, "event")
        assert [e["title"] for e in p.entries] == ["Nifty ends higher"] and info["via"] == GN_CNBC
        n = len(calls)
        ff.fetch_feed_ex(CNBC, "event")
        assert len(calls) == n                              # result cached under the primary: the 404 is not re-hit

    def test_helpers(self):
        import time as _t
        old = types.SimpleNamespace(entries=[{"published_parsed": _t.gmtime(1577836800)}])
        assert ff._all_entries_stale(old, 3) is True and ff._all_entries_stale(old, 0) is False
        assert ff._all_entries_stale(types.SimpleNamespace(entries=[]), 3) is False
        assert ff._all_entries_stale(types.SimpleNamespace(entries=[object()]), 3) is False
