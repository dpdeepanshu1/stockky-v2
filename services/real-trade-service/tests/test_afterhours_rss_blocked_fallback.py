"""
tests/test_afterhours_rss_blocked_fallback.py

Group 238 - Moneycontrol RSS answered HTTP 403 from the deploy VM, so the after-hours scan got 0 items from
its highest-weighted feed. watchlist_engine/afterhours_scan.py::_fetch_rss_items now:

  * retries a bot-gate status (401/403/406/429/451) once with an alternate browser header profile,
  * falls back through feed["fallback_urls"] in order (only when the primary fails),
  * strips the " - <Source>" suffix Google News adds to titles on fallback items,
  * skips a fully-blocked feed for AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS instead of retrying every tick,
  * leaves a feed WITHOUT fallbacks behaving as before (any failure -> []).

Run: cd services/real-trade-service && python -m pytest tests/test_afterhours_rss_blocked_fallback.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import watchlist_engine.afterhours_scan as ahs

PRIMARY = "https://www.moneycontrol.com/rss/latestnews.xml"
FB1 = "https://www.moneycontrol.com/rss/business.xml"
FB2 = "https://news.google.com/rss/search?q=site:moneycontrol.com"

_RSS = (
    "<rss version=\"2.0\"><channel>"
    "<item><title>{title}</title><link>https://example.com/1</link>"
    "<pubDate>{date}</pubDate></item></channel></rss>"
)
_FEED = {"source": "Moneycontrol", "url": PRIMARY, "source_bonus": 10, "fallback_urls": [FB1, FB2]}


def _now_rfc822():
    import email.utils
    return email.utils.formatdate(usegmt=True)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_state():
    ahs._reset_feed_block_state()
    yield
    ahs._reset_feed_block_state()


def _ok(title="Reliance wins big order"):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.text = _RSS.format(title=title, date=_now_rfc822())
    return r


def _status(code, url=PRIMARY):
    r = MagicMock()
    r.status_code = code
    r.text = "denied"
    r.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        f"HTTP {code}", request=httpx.Request("GET", url), response=httpx.Response(code)))
    return r


class _Router:
    """Patches httpx.AsyncClient: each client.get(url) is answered from `table[url]`, a response or a list of
    responses consumed one per call. Records every (url, user-agent) call."""

    def __init__(self, table):
        self.table = {k: (list(v) if isinstance(v, list) else [v]) for k, v in table.items()}
        self.calls = []

    def __call__(self, *a, headers=None, **kw):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        async def _get(url):
            self.calls.append((url, (headers or {}).get("User-Agent", "")))
            seq = self.table[url]
            return seq.pop(0) if len(seq) > 1 else seq[0]

        client.get = _get
        return client


def _fetch(router, feed=_FEED):
    with patch("httpx.AsyncClient", side_effect=router):
        return run(ahs._fetch_rss_items(feed))


class TestBlockedFeedFallback:
    def test_primary_ok_uses_no_fallback(self):
        r = _Router({PRIMARY: _ok()})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["Reliance wins big order"]
        assert [c[0] for c in r.calls] == [PRIMARY]

    def test_403_retries_alternate_header_profile_then_succeeds(self):
        r = _Router({PRIMARY: [_status(403), _ok()]})
        items = _fetch(r)
        assert len(items) == 1
        assert [c[0] for c in r.calls] == [PRIMARY, PRIMARY]
        assert r.calls[0][1] != r.calls[1][1]          # different User-Agent on the retry
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_primary_blocked_falls_to_next_url(self):
        r = _Router({PRIMARY: _status(403), FB1: _ok("Tata Motors bags order")})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["Tata Motors bags order"]
        assert [c[0] for c in r.calls] == [PRIMARY, PRIMARY, FB1]
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_google_fallback_strips_publisher_suffix(self):
        r = _Router({PRIMARY: _status(403), FB1: _status(403, FB1),
                     FB2: _ok("Infosys bags large deal - Moneycontrol")})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["Infosys bags large deal"]

    def test_suffix_is_only_stripped_when_it_is_the_publisher_at_the_end(self):
        assert ahs._strip_publisher_suffix("Moneycontrol picks stocks - Mint", "Moneycontrol") \
            == "Moneycontrol picks stocks - Mint"
        assert ahs._strip_publisher_suffix("Deal wins | moneycontrol", "Moneycontrol") == "Deal wins"
        assert ahs._strip_publisher_suffix("", "Moneycontrol") == ""

    def test_all_urls_blocked_returns_empty_and_sets_cooldown(self):
        r = _Router({PRIMARY: _status(403), FB1: _status(403, FB1), FB2: _status(429, FB2)})
        assert _fetch(r) == []
        assert "Moneycontrol" in ahs._FEED_BLOCKED_UNTIL

    def test_blocked_feed_is_skipped_during_cooldown_without_any_request(self):
        r = _Router({PRIMARY: _status(403), FB1: _status(403, FB1), FB2: _status(403, FB2)})
        _fetch(r)
        n = len(r.calls)
        assert _fetch(r) == []
        assert len(r.calls) == n                        # nothing fetched the second time

    def test_feed_is_retried_after_cooldown_expires(self):
        r = _Router({PRIMARY: [_status(403), _status(403), _ok()], FB1: _status(403, FB1),
                     FB2: _status(403, FB2)})
        _fetch(r)
        ahs._FEED_BLOCKED_UNTIL["Moneycontrol"] = 0.0   # cool-down over
        items = _fetch(r)
        assert len(items) == 1
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_success_clears_a_previous_block(self):
        ahs._FEED_BLOCKED_UNTIL["Moneycontrol"] = 0.0
        r = _Router({PRIMARY: _ok()})
        assert len(_fetch(r)) == 1
        assert "Moneycontrol" not in ahs._FEED_BLOCKED_UNTIL

    def test_non_block_http_error_does_not_start_cooldown(self):
        r = _Router({PRIMARY: _status(503), FB1: _status(500, FB1), FB2: _status(502, FB2)})
        assert _fetch(r) == []
        assert ahs._FEED_BLOCKED_UNTIL == {}
        assert [c[0] for c in r.calls] == [PRIMARY, FB1, FB2]   # 5xx: no header retry, still tries fallbacks

    def test_non_xml_primary_falls_back(self):
        html = MagicMock()
        html.raise_for_status = MagicMock()
        html.status_code = 200
        html.text = "<html><body>Access denied</body>"
        r = _Router({PRIMARY: html, FB1: _ok("Wipro order")})
        assert [i["title"] for i in _fetch(r)] == ["Wipro order"]

    def test_feed_without_fallbacks_behaves_as_before(self):
        feed = {"source": "LiveMint", "url": "https://example.com/rss", "source_bonus": 8}
        r = _Router({"https://example.com/rss": _status(503, "https://example.com/rss")})
        assert _fetch(r, feed) == []
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_transport_error_on_every_url_returns_empty(self):
        def boom(*a, **kw):
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.get = AsyncMock(side_effect=ConnectionError("refused"))
            return client
        with patch("httpx.AsyncClient", side_effect=boom):
            assert run(ahs._fetch_rss_items(_FEED)) == []
        assert ahs._FEED_BLOCKED_UNTIL == {}


class TestConfigAndFeedList:
    def test_moneycontrol_entry_declares_fallbacks_after_primary(self):
        mc = next(f for f in ahs._RSS_FEEDS if f["source"] == "Moneycontrol")
        assert mc["url"] == PRIMARY
        assert mc["fallback_urls"] and all(u.startswith("https://") for u in mc["fallback_urls"])
        assert any("news.google.com" in u for u in mc["fallback_urls"])

    def test_cooldown_has_a_floor(self):
        with patch.object(ahs.config, "AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS", 5):
            assert ahs._blocked_cooldown_seconds() == 60
        with patch.object(ahs.config, "AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS", "junk"):
            assert ahs._blocked_cooldown_seconds() == 1800


# ── group 241: a feed that answers 200 but is all-stale counts as unavailable ───────────────────────────────

_OLD_RSS = (
    "<rss version=\"2.0\"><channel>"
    "<item><title>{title}</title><link>https://example.com/1</link>"
    "<pubDate>Wed, 01 Jan 2020 10:00:00 +0530</pubDate></item></channel></rss>"
)
_NO_DATE_RSS = "<rss version=\"2.0\"><channel><item><title>{title}</title><link>https://e.com/1</link></item></channel></rss>"
_FRESH_RSS = (
    "<rss version=\"2.0\"><channel><item><title>{title}</title><link>https://e.com/1</link>"
    "<pubDate>{date}</pubDate></item></channel></rss>"
)


def _body(xml):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.text = xml
    return r


class TestStaleFeedFallback:
    def test_all_stale_primary_falls_to_fresh_fallback(self):
        r = _Router({PRIMARY: _body(_OLD_RSS.format(title="Old news")),
                     FB1: _body(_OLD_RSS.format(title="Also old")),
                     FB2: _body(_FRESH_RSS.format(title="Fresh deal - Moneycontrol",
                                                  date=__import__("email.utils").utils.formatdate(usegmt=True)))})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["Fresh deal"]
        assert [c[0] for c in r.calls] == [PRIMARY, FB1, FB2]
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_all_stale_everywhere_returns_the_stale_primary_items(self):
        r = _Router({PRIMARY: _body(_OLD_RSS.format(title="Old news")),
                     FB1: _body(_OLD_RSS.format(title="Also old")),
                     FB2: _body(_OLD_RSS.format(title="Old again"))})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["Old news"]          # funnel still sees 'N items, N stale'
        assert ahs._FEED_BLOCKED_UNTIL == {}

    def test_stale_primary_and_failing_fallbacks_returns_stale_primary(self):
        r = _Router({PRIMARY: _body(_OLD_RSS.format(title="Old news")),
                     FB1: _status(500, FB1), FB2: _status(503, FB2)})
        assert [i["title"] for i in _fetch(r)] == ["Old news"]

    def test_undated_items_are_never_called_stale(self):
        r = _Router({PRIMARY: _body(_NO_DATE_RSS.format(title="No date"))})
        items = _fetch(r)
        assert [i["title"] for i in items] == ["No date"]
        assert [c[0] for c in r.calls] == [PRIMARY]

    def test_fresh_primary_makes_no_fallback_request(self):
        r = _Router({PRIMARY: _body(_FRESH_RSS.format(
            title="Today", date=__import__("email.utils").utils.formatdate(usegmt=True)))})
        assert len(_fetch(r)) == 1
        assert [c[0] for c in r.calls] == [PRIMARY]

    def test_stale_feed_without_fallbacks_is_returned_as_before(self):
        feed = {"source": "LiveMint", "url": "https://example.com/rss", "source_bonus": 8}
        r = _Router({"https://example.com/rss": _body(_OLD_RSS.format(title="Old"))})
        assert [i["title"] for i in _fetch(r, feed)] == ["Old"]

    def test_all_items_stale_helper(self):
        assert ahs._all_items_stale([]) is False
        assert ahs._all_items_stale([{"title": "x", "pubDate": "Wed, 01 Jan 2020 10:00:00 +0530"}]) is True
        assert ahs._all_items_stale([{"title": "x", "pubDate": "Wed, 01 Jan 2020 10:00:00 +0530"},
                                     {"title": "y", "pubDate": ""}]) is False

    def test_publisher_key_drives_the_suffix_strip(self):
        feed = {"source": "NDTVProfit", "url": PRIMARY, "source_bonus": 9, "publisher": "NDTV Profit",
                "fallback_urls": [FB2]}
        r = _Router({PRIMARY: _body(_OLD_RSS.format(title="Frozen")),
                     FB2: _body(_FRESH_RSS.format(title="Sensex jumps - NDTV Profit",
                                                  date=__import__("email.utils").utils.formatdate(usegmt=True)))})
        assert [i["title"] for i in _fetch(r, feed)] == ["Sensex jumps"]

    def test_ndtv_entry_has_a_google_fallback_and_publisher(self):
        nd = next(f for f in ahs._RSS_FEEDS if f["source"] == "NDTVProfit")
        assert nd["publisher"] == "NDTV Profit"
        assert any("news.google.com" in u and "ndtvprofit.com" in u for u in nd["fallback_urls"])
