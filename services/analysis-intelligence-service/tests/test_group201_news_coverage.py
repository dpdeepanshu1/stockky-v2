"""
group201: thinner-than-necessary news coverage in the event tracker.

Two causes, both in event/main.py:
  1. a bare-ticker "company name" (yfinance rate-limited / no name) was cached for the whole process life, so Google
     News kept being searched by ticker after yfinance recovered;
  2. Google News was asked ONE question; a symbol with fewer than EVENT_GN_THIN_BELOW (default 3) results now gets a
     second, differently worded search, merged and de-duplicated.

    cd services/analysis-intelligence-service
    python -m pytest tests/test_group201_news_coverage.py -v
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event"))

import pytest

import main as ev


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for d in (ev._company_name_cache, ev._company_name_fallback_until):
        d.clear()
    monkeypatch.delenv("EVENT_COMPANY_NAME_FALLBACK_TTL_S", raising=False)
    monkeypatch.delenv("EVENT_GN_THIN_BELOW", raising=False)   # the real default (3) unless a test sets it
    monkeypatch.setattr(ev, "_yf_rate_limited_until", 0.0)
    yield
    for d in (ev._company_name_cache, ev._company_name_fallback_until):
        d.clear()


def _entry(title, days_ago=1):
    t = (datetime.utcnow() - timedelta(days=days_ago)).timetuple()
    return SimpleNamespace(title=title, link=f"http://x/{title}", published_parsed=t)


def _feed(titles, bozo=False):
    return SimpleNamespace(entries=[_entry(t) for t in titles], bozo=bozo)


class _Ticker:
    def __init__(self, info):
        self.info = info


# ── 1. company-name fallback expiry ───────────────────────────────────────────

class TestCompanyNameFallbackExpiry:
    def test_real_name_cached_with_no_expiry(self, monkeypatch):
        monkeypatch.setattr(ev, "_get_ticker", lambda s: _Ticker({"longName": "ABB India Limited"}))
        assert ev._get_company_name("ABB.NS") == "ABB India Limited"
        assert "ABB.NS" not in ev._company_name_fallback_until
        monkeypatch.setattr(ev, "_get_ticker", lambda s: pytest.fail("a real name must stay cached"))
        assert ev._get_company_name("ABB.NS") == "ABB India Limited"

    def test_rate_limited_fallback_gets_an_expiry(self):
        ev._yf_rate_limited_until = time.time() + 1000
        before = time.time()
        assert ev._get_company_name("ABB.NS") == "ABB"
        until = ev._company_name_fallback_until["ABB.NS"]
        assert before + 590 <= until <= time.time() + 610

    def test_fallback_served_inside_window_then_refetched(self, monkeypatch):
        ev._yf_rate_limited_until = time.time() + 1000
        assert ev._get_company_name("ABB.NS") == "ABB"
        ev._yf_rate_limited_until = 0.0
        calls = []
        monkeypatch.setattr(ev, "_get_ticker", lambda s: calls.append(s) or _Ticker({"longName": "ABB India Limited"}))
        assert ev._get_company_name("ABB.NS") == "ABB" and calls == []
        ev._company_name_fallback_until["ABB.NS"] = time.time() - 0.01
        assert ev._get_company_name("ABB.NS") == "ABB India Limited" and calls == ["ABB.NS"]
        assert "ABB.NS" not in ev._company_name_fallback_until

    def test_expired_fallback_that_fails_again_gets_a_new_window(self, monkeypatch):
        monkeypatch.setattr(ev, "_get_ticker", lambda s: _Ticker({}))     # yfinance answers, but with no name
        assert ev._get_company_name("ZZZ.NS") == "ZZZ"
        first = ev._company_name_fallback_until["ZZZ.NS"]
        ev._company_name_fallback_until["ZZZ.NS"] = time.time() - 1
        assert ev._get_company_name("ZZZ.NS") == "ZZZ"
        assert ev._company_name_fallback_until["ZZZ.NS"] > time.time() + 500 and first > 0

    def test_exception_path_gets_an_expiry(self, monkeypatch):
        class Boom:
            @property
            def info(self):
                raise RuntimeError("boom")
        monkeypatch.setattr(ev, "_get_ticker", lambda s: Boom())
        assert ev._get_company_name("QQQ.NS") == "QQQ"
        assert "QQQ.NS" in ev._company_name_fallback_until

    def test_ttl_zero_restores_the_old_permanent_cache(self, monkeypatch):
        monkeypatch.setenv("EVENT_COMPANY_NAME_FALLBACK_TTL_S", "0")
        ev._yf_rate_limited_until = time.time() + 1000
        assert ev._get_company_name("ABB.NS") == "ABB"
        assert "ABB.NS" not in ev._company_name_fallback_until
        ev._yf_rate_limited_until = 0.0
        monkeypatch.setattr(ev, "_get_ticker", lambda s: pytest.fail("old behaviour: never asked again"))
        assert ev._get_company_name("ABB.NS") == "ABB"

    @pytest.mark.parametrize("raw,expected", [("", 600.0), ("abc", 600.0), ("-5", 600.0), ("90", 90.0), ("0", 0.0)])
    def test_ttl_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("EVENT_COMPANY_NAME_FALLBACK_TTL_S", raw)
        assert ev._company_name_fallback_ttl() == expected


# ── 2. widened Google News search ─────────────────────────────────────────────

class TestExtraQuery:
    def test_legal_suffix_dropped(self):
        assert ev._google_news_extra_query("ABB.NS", "ABB India Limited") == "ABB India share price NSE"

    def test_private_limited_both_dropped(self):
        assert ev._google_news_extra_query("X.NS", "Foo Bar Private Limited") == "Foo Bar share price NSE"

    def test_ticker_only_name_uses_the_ticker_query(self):
        assert ev._google_news_extra_query("ABB.NS", "ABB") == "ABB NSE share price"
        assert ev._google_news_extra_query("XYZ.BO", "xyz") == "XYZ NSE share price"

    def test_empty_company_uses_ticker_query(self):
        assert ev._google_news_extra_query("ABB.NS", "") == "ABB NSE share price"


class TestWidenedGoogleNews:
    @pytest.fixture()
    def parse(self, monkeypatch):
        urls, by_query = [], {}

        def fake(url):
            urls.append(url)
            q = unquote(url.split("q=")[1].split("&hl=")[0])
            r = by_query.get(q, _feed([]))
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(ev.feedparser, "parse", fake)
        monkeypatch.setattr(ev, "_get_company_name", lambda s: "Zenith Widgets Limited")
        return SimpleNamespace(urls=urls, q=by_query)

    def test_thin_first_search_triggers_second_and_merges(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A"])
        parse.q["Zenith Widgets share price NSE"] = _feed(["B", "C"])
        out = ev._fetch_google_news("ZWID.NS", max_items=8)
        assert [i["title"] for i in out] == ["A", "B", "C"]
        assert len(parse.urls) == 2

    def test_duplicate_titles_across_searches_are_dropped(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A", "B"])
        parse.q["Zenith Widgets share price NSE"] = _feed(["  b  ", "C"])
        assert [i["title"] for i in ev._fetch_google_news("ZWID.NS")] == ["A", "B", "C"]

    def test_three_items_is_not_thin(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A", "B", "C"])
        assert len(ev._fetch_google_news("ZWID.NS")) == 3
        assert len(parse.urls) == 1

    def test_empty_first_search_triggers_second(self, parse):
        parse.q["Zenith Widgets share price NSE"] = _feed(["B"])
        assert [i["title"] for i in ev._fetch_google_news("ZWID.NS")] == ["B"]
        assert len(parse.urls) == 2

    def test_failed_first_search_is_not_asked_twice(self, parse):
        parse.q["Zenith Widgets Limited"] = RuntimeError("dns")
        assert ev._fetch_google_news("ZWID.NS") == []
        assert len(parse.urls) == 1

    def test_bozo_empty_first_search_is_not_asked_twice(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed([], bozo=True)
        assert ev._fetch_google_news("ZWID.NS") == []
        assert len(parse.urls) == 1

    def test_failed_second_search_keeps_the_first_result(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A"])
        parse.q["Zenith Widgets share price NSE"] = RuntimeError("dns")
        assert [i["title"] for i in ev._fetch_google_news("ZWID.NS")] == ["A"]

    def test_threshold_zero_never_widens(self, parse, monkeypatch):
        monkeypatch.setenv("EVENT_GN_THIN_BELOW", "0")
        parse.q["Zenith Widgets Limited"] = _feed(["A"])
        assert len(ev._fetch_google_news("ZWID.NS")) == 1 and len(parse.urls) == 1

    def test_threshold_is_configurable(self, parse, monkeypatch):
        monkeypatch.setenv("EVENT_GN_THIN_BELOW", "5")
        parse.q["Zenith Widgets Limited"] = _feed(["A", "B", "C", "D"])
        parse.q["Zenith Widgets share price NSE"] = _feed(["E"])
        assert len(ev._fetch_google_news("ZWID.NS")) == 5

    @pytest.mark.parametrize("raw,expected", [("", 3), ("x", 3), ("-1", 3), ("2", 2), ("0", 0)])
    def test_threshold_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("EVENT_GN_THIN_BELOW", raw)
        assert ev._gn_thin_below() == expected

    def test_old_items_in_second_search_still_dropped(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A"])
        old = SimpleNamespace(entries=[_entry("old", days_ago=60), _entry("new")], bozo=False)
        parse.q["Zenith Widgets share price NSE"] = old
        assert [i["title"] for i in ev._fetch_google_news("ZWID.NS")] == ["A", "new"]

    def test_first_query_url_unchanged(self, parse):
        parse.q["Zenith Widgets Limited"] = _feed(["A", "B", "C"])
        ev._fetch_google_news("ZWID.NS")
        assert parse.urls[0] == ("https://news.google.com/rss/search?q=Zenith%20Widgets%20Limited"
                                 "&hl=en-IN&gl=IN&ceid=IN:en")
