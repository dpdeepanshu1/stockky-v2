"""
tests/test_news_main.py — coverage for news/main.py

No network: feedparser.parse, httpx.Client / httpx.post and yfinance are all
faked. Route functions are called directly (FastAPI's @app.get returns the
original function), so no TestClient is needed.

test_newsapi_key_redaction.py already covers the real-httpx log-redaction
regression; this file covers everything else in the module.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_news_main.py -v
"""
from __future__ import annotations

import logging
import os
import runpy
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "news"))

import pytest

import main as nm  # noqa: E402  (news/main.py)


@pytest.fixture(autouse=True)
def _no_network_feeds(monkeypatch):
    """2026-10-04 (item 9): feeds go through feed_fetch._download (httpx); these tests keep faking
    feedparser.parse, so stub the download (no network) and start each test with an empty per-URL cache."""
    import feed_fetch
    feed_fetch.clear_cache()
    # The body IS the URL (bytes): feed_fetch hands the downloaded body to feedparser.parse, so a faked
    # parse() sees which feed was requested via _url(raw). Real parse() of it yields zero entries.
    monkeypatch.setattr(feed_fetch, "_download", lambda url: (url.encode(), {"status": 200, "error": None}))
    yield
    feed_fetch.clear_cache()


@pytest.fixture(autouse=True)
def _no_company_name_lookup(monkeypatch):
    """2026-10-04: _company_query() now resolves unknown tickers' names via Yahoo (cached). Existing tests
    must never hit the network for that; the dedicated TestCompanyNameLookup class re-enables it with a fake."""
    monkeypatch.setattr(nm, "_NAME_LOOKUP_ENABLED", False)
    nm._NAME_CACHE.clear()
    nm._NAME_CACHE_TS.clear()
    yield
    nm._NAME_CACHE.clear()
    nm._NAME_CACHE_TS.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tt(dt):
    """datetime -> time-tuple-like sequence as feedparser's published_parsed."""
    return dt.timetuple()[:9]


def _entry(title="Infosys wins deal", desc="", days_ago=1, link="http://x/1", **kw):
    e = SimpleNamespace(title=title, description=desc, link=link)
    if days_ago is not None:
        e.published_parsed = _tt(_now() - timedelta(days=days_ago))
    for k, v in kw.items():
        setattr(e, k, v)
    return e


def _url(raw):
    """The feed URL a faked feedparser.parse() was asked for (see _no_network_feeds)."""
    return raw.decode() if isinstance(raw, bytes) else raw


def _parsed(*entries):
    return SimpleNamespace(entries=list(entries))


class _Resp:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._payload


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.urls.append(url)
        if self._exc:
            raise self._exc
        return self._resp


@pytest.fixture()
def rl(monkeypatch):
    """Stub rate_limit_report; records calls."""
    calls = {"hit": [], "reported": []}
    fake = types.ModuleType("rate_limit_report")
    fake.record_rate_limit_hit = lambda **kw: calls["hit"].append(kw)
    fake.report_if_rate_limited = lambda exc, **kw: calls["reported"].append((exc, kw))
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    return calls


# ── _redact_secrets / filter ──────────────────────────────────────────────────

class TestRedactSecrets:
    def test_redacts_api_key_param(self):
        out = nm._redact_secrets("GET https://x/y?q=1&apiKey=SECRET123 200")
        assert "SECRET123" not in out
        assert "apiKey=***" in out

    def test_case_insensitive_and_first_param(self):
        out = nm._redact_secrets("https://x/y?APIKEY=abc&z=1")
        assert "abc" not in out and "APIKEY=***" in out

    def test_no_secret_unchanged(self):
        assert nm._redact_secrets("plain text") == "plain text"

    def test_non_string_is_stringified(self):
        assert nm._redact_secrets(ValueError("boom")) == "boom"

    def test_never_raises_when_str_fails(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("no str")
        assert nm._redact_secrets(Bad()) == "<text withheld: redaction failed>"


class TestSecretFilter:
    def _record(self, msg, args=()):
        return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)

    def test_redacts_message_and_clears_args(self):
        rec = self._record("GET %s", ("https://n/x?apiKey=TOPSECRET",))
        assert nm._SecretRedactingFilter().filter(rec) is True
        assert "TOPSECRET" not in rec.getMessage()
        assert rec.args == ()

    def test_clean_message_untouched(self):
        rec = self._record("hello %s", ("world",))
        assert nm._SecretRedactingFilter().filter(rec) is True
        assert rec.msg == "hello %s" and rec.args == ("world",)

    def test_filter_never_breaks_logging(self):
        class BadRec:
            def getMessage(self):
                raise RuntimeError("boom")
        assert nm._SecretRedactingFilter().filter(BadRec()) is True

    def test_install_is_idempotent(self):
        nm._install_httpx_secret_filter()
        nm._install_httpx_secret_filter()
        filters = logging.getLogger("httpx").filters
        assert sum(isinstance(f, nm._SecretRedactingFilter) for f in filters) == 1


# ── symbol / keyword helpers ──────────────────────────────────────────────────

class TestSymbolHelpers:
    def test_base_symbol_strips_ns_suffix(self):
        assert nm._base_symbol("TCS.NS") == "TCS"

    def test_base_symbol_lowercase_suffix_is_stripped(self):
        # The suffix is matched after upper-casing, so ".ns" / ".bo" are stripped
        # exactly like ".NS" / ".BO".
        assert nm._base_symbol("tcs.ns") == "TCS"
        assert nm._base_symbol("infy.bo") == "INFY"
        assert nm._base_symbol("  Tcs.Ns ") == "TCS"

    def test_base_symbol_only_strips_a_trailing_suffix(self):
        assert nm._base_symbol("A.NSB") == "A.NSB"
        assert nm._base_symbol("TCS") == "TCS"

    def test_company_query_lowercase_suffix_finds_hint(self):
        assert nm._company_query("tcs.ns") == "Tata Consultancy Services"

    def test_base_symbol_bo(self):
        assert nm._base_symbol("INFY.BO") == "INFY"

    def test_base_symbol_strips_whitespace(self):
        assert nm._base_symbol("  wipro ") == "WIPRO"

    def test_company_query_known(self):
        assert nm._company_query("TCS.NS") == "Tata Consultancy Services"

    def test_company_query_unknown_falls_back_to_base(self):
        assert nm._company_query("zzzco") == "ZZZCO"


class TestMatchKeywords:
    def test_known_symbol_has_name_parts_and_alias(self):
        keys = nm._match_keywords("TCS.NS")
        assert "tcs" in keys
        assert "tata consultancy services" in keys
        assert "tata" in keys and "consultancy" in keys
        assert "tata consultancy" in keys          # from ALIASES
        assert "tataconsultancyservices" in keys   # compact form

    def test_short_symbol_not_added_as_keyword(self):
        keys = nm._match_keywords("PW")
        assert "pw" not in keys
        assert "physics wallah" in keys

    def test_two_char_name_parts_dropped(self):
        keys = nm._match_keywords("LT")
        assert "lt" not in keys
        assert "larsen & toubro" in keys
        assert "l&t" in keys
        assert "larsen" in keys and "toubro" in keys

    def test_unknown_symbol_uses_itself(self):
        keys = nm._match_keywords("NEWCO.NS")
        assert "newco" in keys

    def test_short_aliases_dropped(self):
        # ALIASES entries shorter than 3 chars after strip must not appear
        for k in nm._match_keywords("SBIN"):
            assert len(k) >= 3 or k == "sbi"

    def test_returns_list(self):
        assert isinstance(nm._match_keywords("INFY"), list)


class TestIsRelevant:
    def test_empty_text_false(self):
        assert nm._is_relevant("", "", ["infosys"]) is False
        assert nm._is_relevant(None, None, ["infosys"]) is False

    def test_strong_keyword_substring(self):
        assert nm._is_relevant("Infosys bags deal", "", ["infosys"]) is True

    def test_multiword_keyword_is_strong(self):
        assert nm._is_relevant("The pw skills unit", "", ["pw skills"]) is True

    def test_weak_keyword_needs_word_boundary(self):
        assert nm._is_relevant("tcs results out", "", ["tcs"]) is True
        assert nm._is_relevant("tcsxyz results", "", ["tcs"]) is False

    def test_description_is_searched(self):
        assert nm._is_relevant("Market wrap", "Wipro gains today", ["wipro"]) is True

    def test_no_match(self):
        assert nm._is_relevant("Unrelated", "text", ["infosys", "tcs"]) is False

    def test_short_keywords_ignored(self):
        assert nm._is_relevant("go to it", "", ["it"]) is False


# ── _parse_feed_items ─────────────────────────────────────────────────────────

class TestParseFeedItems:
    KW = ["infosys"]

    def test_basic_item_shape(self):
        items = nm._parse_feed_items(_parsed(_entry(desc="short")), "Pub", self.KW, 5)
        assert len(items) == 1
        it = items[0]
        assert it["title"] == "Infosys wins deal"
        assert it["publisher"] == "Pub"
        assert it["url"] == "http://x/1"
        assert it["snippet"] == "short"
        assert it["published"] is not None

    def test_irrelevant_skipped(self):
        assert nm._parse_feed_items(_parsed(_entry(title="Other co")), "P", self.KW, 5) == []

    def test_old_entry_excluded(self):
        e = _entry(days_ago=30)
        assert nm._parse_feed_items(_parsed(e), "P", self.KW, 5, days=10) == []

    def test_days_param_controls_cutoff(self):
        e = _entry(days_ago=12)
        assert nm._parse_feed_items(_parsed(e), "P", self.KW, 5, days=14) != []

    def test_no_published_date_kept_with_none(self):
        items = nm._parse_feed_items(_parsed(_entry(days_ago=None)), "P", self.KW, 5)
        assert items[0]["published"] is None

    def test_bad_published_parsed_is_ignored(self):
        e = _entry(days_ago=None, published_parsed=("x", "y"))
        items = nm._parse_feed_items(_parsed(e), "P", self.KW, 5)
        assert len(items) == 1 and items[0]["published"] is None

    def test_max_items_respected(self):
        es = [_entry(title=f"Infosys news {i}", link=f"http://x/{i}") for i in range(6)]
        assert len(nm._parse_feed_items(_parsed(*es), "P", self.KW, 3)) == 3

    def test_long_description_truncated_with_ellipsis(self):
        items = nm._parse_feed_items(_parsed(_entry(desc="x" * 300)), "P", self.KW, 5)
        assert items[0]["snippet"].endswith("…")
        assert len(items[0]["snippet"]) == 221

    def test_summary_used_when_no_description(self):
        e = SimpleNamespace(title="Infosys up", summary="from summary", link="l")
        items = nm._parse_feed_items(_parsed(e), "P", self.KW, 5)
        assert items[0]["snippet"] == "from summary"

    def test_missing_entries_attribute(self):
        assert nm._parse_feed_items(SimpleNamespace(), "P", self.KW, 5) == []

    def test_missing_link_becomes_empty(self):
        e = SimpleNamespace(title="Infosys up", description="")
        items = nm._parse_feed_items(_parsed(e), "P", self.KW, 5)
        assert items[0]["url"] == ""

    def test_only_first_50_entries_scanned(self):
        es = [_entry(title="Unrelated")] * 50 + [_entry(title="Infosys late")]
        assert nm._parse_feed_items(_parsed(*es), "P", self.KW, 5) == []


# ── _fetch_yahoo_news ─────────────────────────────────────────────────────────

def _fake_yf(news=None, raises=False):
    mod = types.ModuleType("yfinance")
    seen = []

    class Ticker:
        def __init__(self, sym):
            seen.append(sym)
            if raises:
                raise RuntimeError("yahoo down")
            self.news = news

    mod.Ticker = Ticker
    mod._seen = seen
    return mod


@pytest.fixture(autouse=True)
def _reset_yahoo_backoff(monkeypatch):
    """The Yahoo backoff keeps per-process state; start every test with it closed and the defaults."""
    nm._yahoo_state["misses"] = 0
    nm._yahoo_state["skip_until"] = 0.0
    monkeypatch.setattr(nm, "_YAHOO_BACKOFF_AFTER", 10)
    monkeypatch.setattr(nm, "_YAHOO_BACKOFF_SECONDS", 600.0)
    yield
    nm._yahoo_state["misses"] = 0
    nm._yahoo_state["skip_until"] = 0.0


class TestFetchYahooNews:
    def test_flat_shape(self, monkeypatch):
        yf = _fake_yf([{"title": "T1", "link": "L1", "publisher": "Pub", "providerPublishTime": 123}])
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        out = nm._fetch_yahoo_news("INFY.NS")
        assert out == [{"title": "T1", "link": "L1", "publisher": "Pub", "published": "123", "source": "yahoo"}]
        assert yf._seen == ["INFY.NS"]

    def test_nested_content_shape(self, monkeypatch):
        n = {"content": {
            "title": "CT", "clickThroughUrl": {"url": "U1"},
            "provider": {"displayName": "Prov"}, "pubDate": "2026-09-01",
        }}
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf([n]))
        out = nm._fetch_yahoo_news("TCS")
        assert out[0]["title"] == "CT"
        assert out[0]["link"] == "U1"
        assert out[0]["publisher"] == "Prov"
        assert out[0]["published"] == "2026-09-01"

    def test_canonical_url_fallback(self, monkeypatch):
        n = {"content": {"title": "CT", "clickThroughUrl": None, "canonicalUrl": {"url": "CU"}}}
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf([n]))
        assert nm._fetch_yahoo_news("TCS")[0]["link"] == "CU"

    def test_defaults_publisher_yahoo_and_empty_link(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf([{"title": "Only title"}]))
        out = nm._fetch_yahoo_news("TCS")
        assert out[0]["publisher"] == "Yahoo"
        assert out[0]["link"] == ""
        assert out[0]["published"] == ""

    def test_skips_non_dict_and_missing_title(self, monkeypatch):
        raw = ["str", None, {"link": "x"}, {"title": "ok"}]
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(raw))
        out = nm._fetch_yahoo_news("TCS")
        assert [o["title"] for o in out] == ["ok"]

    def test_max_items_slices_raw(self, monkeypatch):
        raw = [{"title": f"t{i}"} for i in range(10)]
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(raw))
        assert len(nm._fetch_yahoo_news("TCS", max_items=3)) == 3

    def test_none_news_returns_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(None))
        assert nm._fetch_yahoo_news("TCS") == []

    def test_exception_returns_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(raises=True))
        assert nm._fetch_yahoo_news("TCS") == []


# ── _fetch_google_news ────────────────────────────────────────────────────────

class TestFetchGoogleNews:
    def test_queries_and_publisher(self, monkeypatch):
        urls = []

        def fake_parse(url):
            urls.append(_url(url))
            return _parsed(_entry(title=f"Tata Consultancy Services item {len(urls)}", link=f"l{len(urls)}"))

        monkeypatch.setattr(nm.feedparser, "parse", fake_parse)
        out = nm._fetch_google_news("TCS.NS", max_items=15)
        assert len(urls) == 3                       # name query, base query, 1 alias query (capped at 3)
        assert all(u.startswith("https://news.google.com/rss/search?q=") for u in urls)
        assert all(o["publisher"] == "Google News" for o in out)
        assert len(out) == 3

    def test_unknown_symbol_only_two_queries(self, monkeypatch):
        urls = []
        monkeypatch.setattr(nm.feedparser, "parse", lambda u: (urls.append(_url(u)), _parsed())[1])
        nm._fetch_google_news("NEWCO.NS")
        assert len(urls) == 2

    def test_max_items_truncates(self, monkeypatch):
        monkeypatch.setattr(
            nm.feedparser, "parse",
            lambda u: _parsed(*[_entry(title=f"Infosys {i}") for i in range(5)]),
        )
        assert len(nm._fetch_google_news("INFY", max_items=4)) == 4

    def test_one_query_failure_does_not_stop_others(self, monkeypatch):
        calls = {"n": 0}

        def fake_parse(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _parsed(_entry(title="Infosys ok"))

        monkeypatch.setattr(nm.feedparser, "parse", fake_parse)
        out = nm._fetch_google_news("INFY")
        assert calls["n"] == 3
        assert len(out) >= 1


# ── RSS-style fetchers ────────────────────────────────────────────────────────

RSS_FETCHERS = [
    ("_fetch_moneycontrol", "Moneycontrol", "moneycontrol.com"),
    ("_fetch_economic_times", "Economic Times", "economictimes"),
    ("_fetch_business_standard", "Business Standard", "business-standard"),
    ("_fetch_ndtv_profit", "NDTV Profit", "bloombergquint"),
    ("_fetch_livemint", "LiveMint", "livemint.com"),
    ("_fetch_financial_express", "Financial Express", "financialexpress"),
    ("_fetch_reuters_india", "Reuters", "news.google.com"),
]


class TestRssFetchers:
    @pytest.mark.parametrize("fname,publisher,host", RSS_FETCHERS)
    def test_success_tags_publisher_and_hits_right_feed(self, monkeypatch, fname, publisher, host):
        urls = []

        def fake_parse(url):
            urls.append(_url(url))
            return _parsed(_entry(title="Infosys results beat"), _entry(title="Something else"))

        monkeypatch.setattr(nm.feedparser, "parse", fake_parse)
        out = getattr(nm, fname)("INFY.NS")
        assert host in urls[0]
        assert len(out) == 1
        assert out[0]["publisher"] == publisher

    @pytest.mark.parametrize("fname,publisher,host", RSS_FETCHERS)
    def test_exception_returns_empty(self, monkeypatch, fname, publisher, host):
        def boom(url):
            raise RuntimeError("feed down")

        monkeypatch.setattr(nm.feedparser, "parse", boom)
        assert getattr(nm, fname)("INFY") == []

    def test_reuters_query_uses_company_name(self, monkeypatch):
        urls = []
        monkeypatch.setattr(nm.feedparser, "parse", lambda u: (urls.append(_url(u)), _parsed())[1])
        nm._fetch_reuters_india("TCS.NS")
        assert "Tata%20Consultancy%20Services" in urls[0]
        assert "reuters.com" in urls[0]


# ── _fetch_newsapi ────────────────────────────────────────────────────────────

def _article(title="Infosys wins", desc="", published=None, source="Src", url="http://a"):
    a = {"title": title, "description": desc, "url": url, "source": {"name": source}}
    if published is not None:
        a["publishedAt"] = published
    return a


def _iso_z(days_ago):
    return (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestFetchNewsApi:
    def _client(self, monkeypatch, resp=None, exc=None):
        fc = _FakeClient(resp=resp, exc=exc)
        monkeypatch.setattr(nm.httpx, "Client", lambda **kw: fc)
        return fc

    def test_no_key_returns_empty_without_http(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        fc = self._client(monkeypatch, _Resp(200, {"articles": []}))
        assert nm._fetch_newsapi("INFY") == []
        assert fc.urls == []

    def test_success_filters_and_shapes(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        arts = [
            _article("Infosys wins order", "d" * 300, _iso_z(1), "Reuters", "http://r"),
            _article("Unrelated headline", "", _iso_z(1)),
            _article("Infosys old news", "", _iso_z(40)),
            _article("Infosys bad date", "", "not-a-date"),
            _article("Infosys no date", ""),
        ]
        fc = self._client(monkeypatch, _Resp(200, {"articles": arts}))
        out = nm._fetch_newsapi("INFY.NS", max_items=7)
        titles = [o["title"] for o in out]
        assert titles == ["Infosys wins order", "Infosys bad date", "Infosys no date"]
        first = out[0]
        assert first["publisher"] == "Reuters"
        assert first["url"] == "http://r"
        assert first["snippet"].endswith("…")
        assert out[1]["published"] is None and out[2]["published"] is None
        assert "pageSize=7" in fc.urls[0] and "apiKey=K" in fc.urls[0]

    def test_source_defaults_to_newsapi(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        art = {"title": "Infosys up", "description": None}
        self._client(monkeypatch, _Resp(200, {"articles": [art]}))
        out = nm._fetch_newsapi("INFY")
        assert out[0]["publisher"] == "NewsAPI" and out[0]["url"] == ""

    def test_empty_articles(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        self._client(monkeypatch, _Resp(200, {}))
        assert nm._fetch_newsapi("INFY") == []

    @pytest.mark.parametrize("status", [429, 403, 503])
    def test_rate_limited_status_is_recorded(self, monkeypatch, rl, status):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        self._client(monkeypatch, _Resp(status))
        assert nm._fetch_newsapi("INFY") == []
        assert rl["hit"] == [{"provider": "analysis", "status": status, "path": "news/newsapi"}]

    def test_other_error_status_not_recorded(self, monkeypatch, rl):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        self._client(monkeypatch, _Resp(500))
        assert nm._fetch_newsapi("INFY") == []
        assert rl["hit"] == []

    def test_recording_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        fake = types.ModuleType("rate_limit_report")

        def boom(**kw):
            raise RuntimeError("kv down")

        fake.record_rate_limit_hit = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        self._client(monkeypatch, _Resp(429))
        assert nm._fetch_newsapi("INFY") == []

    def test_exception_returns_empty_and_redacts_key(self, monkeypatch, caplog):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        exc = RuntimeError("failed https://newsapi.org/x?apiKey=LEAKEDKEY123&y=1")
        self._client(monkeypatch, exc=exc)
        with caplog.at_level(logging.WARNING):
            assert nm._fetch_newsapi("INFY") == []
        assert "LEAKEDKEY123" not in caplog.text
        assert "apiKey=***" in caplog.text


# ── _fetch_headlines ──────────────────────────────────────────────────────────

_SOURCE_NAMES = [
    "_fetch_yahoo_news", "_fetch_google_news", "_fetch_moneycontrol",
    "_fetch_economic_times", "_fetch_business_standard", "_fetch_ndtv_profit",
    "_fetch_livemint", "_fetch_financial_express", "_fetch_reuters_india",
]


def _patch_sources(monkeypatch, mapping=None, newsapi=None):
    """Replace all sources with stubs returning [] unless in mapping."""
    mapping = mapping or {}
    called = []
    for name in _SOURCE_NAMES + ["_fetch_newsapi"]:
        def make(n):
            def f(symbol, max_items=10):
                called.append(n)
                v = mapping.get(n, [])
                if isinstance(v, Exception):
                    raise v
                return list(v)
            f.__name__ = n
            return f
        monkeypatch.setattr(nm, name, make(name))
    return called


# 2026-10-04: Moneycontrol and Financial Express return HTTP 403 from the VM and are no longer queried.
_RETIRED = ("_fetch_moneycontrol", "_fetch_financial_express")
_ACTIVE_SOURCE_NAMES = [n for n in _SOURCE_NAMES if n not in _RETIRED]


class TestFetchHeadlines:
    def test_calls_all_seven_sources_without_newsapi_key(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        called = _patch_sources(monkeypatch)
        assert nm._fetch_headlines("INFY") == []
        assert called == _ACTIVE_SOURCE_NAMES and len(called) == 7
        assert not set(called) & set(_RETIRED)

    def test_one_info_summary_line_per_symbol_not_one_per_source(self, monkeypatch, caplog):
        # group129: was 7+ INFO lines per symbol ("Fetched N items from _fetch_x")
        import logging
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        _patch_sources(monkeypatch, {
            "_fetch_google_news": [{"title": "a", "published": "2026-01-01"}],
            "_fetch_livemint": RuntimeError("boom"),
        })
        caplog.set_level(logging.INFO)
        nm._fetch_headlines("INFY")
        info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert not any(m.startswith("Fetched ") for m in info)
        summary = [m for m in info if m.startswith("news sources for INFY:")]
        assert len(summary) == 1
        assert "yahoo_news=0" in summary[0] and "google_news=1" in summary[0]
        assert "livemint=failed" in summary[0] and summary[0].endswith("(total 1)")

    def test_newsapi_added_when_key_present(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", "K")
        called = _patch_sources(monkeypatch)
        nm._fetch_headlines("INFY")
        assert called[-1] == "_fetch_newsapi" and len(called) == 8

    def test_dedupes_by_title_case_insensitive_and_drops_blank(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        _patch_sources(monkeypatch, {
            "_fetch_yahoo_news": [{"title": "Same Title", "published": "2026-01-01"}, {"title": ""}, {"title": None}],
            "_fetch_google_news": [{"title": "same title ", "published": "2026-01-02"}],
        })
        out = nm._fetch_headlines("INFY")
        assert len(out) == 1

    def test_sorted_newest_first_none_last(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        _patch_sources(monkeypatch, {
            "_fetch_yahoo_news": [
                {"title": "old", "published": "2026-01-01"},
                {"title": "none", "published": None},
                {"title": "new", "published": "2026-06-01"},
            ],
        })
        assert [h["title"] for h in nm._fetch_headlines("INFY")] == ["new", "old", "none"]

    def test_max_items_limit(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        _patch_sources(monkeypatch, {
            "_fetch_yahoo_news": [{"title": f"t{i}", "published": f"2026-01-{i + 1:02d}"} for i in range(9)],
        })
        assert len(nm._fetch_headlines("INFY", max_items=4)) == 4

    def test_failing_source_is_skipped(self, monkeypatch):
        monkeypatch.setattr(nm, "NEWSAPI_KEY", None)
        _patch_sources(monkeypatch, {
            "_fetch_yahoo_news": RuntimeError("boom"),
            "_fetch_google_news": [{"title": "survivor", "published": "2026-01-01"}],
        })
        assert [h["title"] for h in nm._fetch_headlines("INFY")] == ["survivor"]


# ── _summarize_headlines ──────────────────────────────────────────────────────

class TestSummarizeHeadlines:
    def test_empty(self):
        assert nm._summarize_headlines([], "TCS.NS") == "No recent relevant news found for Tata Consultancy Services."

    def test_basic_format(self):
        hs = [{"title": "Board approves buyback", "publisher": "ET"}]
        out = nm._summarize_headlines(hs, "INFY")
        assert out.startswith("Infosys: ")
        assert "1 relevant item(s)." in out
        assert "• [ET] Board approves buyback" in out

    def test_theme_detection(self):
        hs = [
            {"title": "Q2 profit jumps", "publisher": "A"},
            {"title": "Company bagged large order", "publisher": "B"},
            {"title": "Promoter stake sale", "publisher": "C"},
            {"title": "Raises guidance for FY27", "publisher": "D"},
        ]
        out = nm._summarize_headlines(hs, "INFY")
        assert "Themes: Results/earnings, Deal/order, Management/stake, Guidance/outlook." in out

    def test_no_theme_when_nothing_matches(self):
        out = nm._summarize_headlines([{"title": "Plain headline", "publisher": "A"}], "INFY")
        assert "Themes:" not in out

    def test_only_top_four_listed_but_count_is_total(self):
        hs = [{"title": f"Plain {i}", "publisher": "P"} for i in range(6)]
        out = nm._summarize_headlines(hs, "INFY")
        assert "6 relevant item(s)." in out
        assert out.count("• [P]") == 4

    def test_long_title_truncated(self):
        out = nm._summarize_headlines([{"title": "x" * 200, "publisher": "P"}], "INFY")
        assert "x" * 107 + "…" in out
        assert "x" * 108 not in out

    def test_missing_publisher_defaults_to_source(self):
        out = nm._summarize_headlines([{"title": "Plain"}], "INFY")
        assert "• [Source] Plain" in out

    def test_blank_titles_produce_no_bullets(self):
        out = nm._summarize_headlines([{"title": "  ", "publisher": "P"}, {"title": None}], "INFY")
        assert "•" not in out


# ── _score_headline ───────────────────────────────────────────────────────────

class TestScoreHeadline:
    @pytest.fixture(autouse=True)
    def _reset_hf_backoff(self, monkeypatch):
        monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", 0.0)

    def test_default_endpoint_is_the_router_not_the_retired_host(self):
        assert "api-inference.huggingface.co" not in nm.HF_API_URL
        assert nm.HF_API_URL.startswith("https://router.huggingface.co/")

    def test_network_failure_backs_off_then_skips_calls(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise nm.httpx.ConnectError("No address associated with hostname")

        monkeypatch.setattr(nm.httpx, "post", boom)
        assert nm._score_headline("h1") == 0.0
        assert nm._score_headline("h2") == 0.0
        assert nm._score_headline("h3") == 0.0
        assert len(calls) == 1  # backed off after the first transport failure

    def test_backoff_expires(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", nm.time.monotonic() - 1)
        monkeypatch.setattr(nm.httpx, "post",
                            lambda *a, **k: _Resp(200, {"choices": [{"message": {"content": "positive"}}]}))
        assert nm._score_headline("h") == 0.8

    def test_non_network_error_does_not_back_off(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(200, {"unexpected": True}))
        assert nm._score_headline("h") == 0.0
        assert nm._HF_BACKOFF_UNTIL == 0.0

    def test_no_key_returns_zero(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", None)
        assert nm._score_headline("anything") == 0.0

    @pytest.mark.parametrize("text,expected", [
        ("Positive", 0.8), ("this is NEGATIVE", -0.8), ("neutral", 0.0), ("unclear", 0.0),
    ])
    def test_classification(self, monkeypatch, text, expected):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return _Resp(200, {"choices": [{"message": {"content": f"  {text} "}}]})

        monkeypatch.setattr(nm.httpx, "post", fake_post)
        assert nm._score_headline("Some headline") == expected
        assert captured["headers"]["Authorization"] == "Bearer K"
        assert "Some headline" in captured["json"]["messages"][0]["content"]
        assert captured["json"]["model"] == nm.HF_MODEL
        assert captured["url"] == nm.HF_API_URL

    @pytest.mark.parametrize("status", [429, 503])
    def test_rate_limit_status_recorded(self, monkeypatch, rl, status):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(status))
        assert nm._score_headline("h") == 0.0
        assert rl["hit"] == [{"provider": "analysis", "status": status, "path": "news/huggingface"}]

    def test_other_error_not_recorded(self, monkeypatch, rl):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(500))
        assert nm._score_headline("h") == 0.0
        assert rl["hit"] == []

    def test_record_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        fake = types.ModuleType("rate_limit_report")

        def boom(**kw):
            raise RuntimeError("x")

        fake.record_rate_limit_hit = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(429))
        assert nm._score_headline("h") == 0.0

    def test_exception_reports_and_returns_zero(self, monkeypatch, rl):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        err = RuntimeError("timeout")

        def boom(*a, **k):
            raise err

        monkeypatch.setattr(nm.httpx, "post", boom)
        assert nm._score_headline("h") == 0.0
        assert rl["reported"][0][0] is err
        assert rl["reported"][0][1] == {"provider": "analysis", "path": "news/huggingface"}

    def test_exception_report_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        fake = types.ModuleType("rate_limit_report")

        def boom(*a, **k):
            raise RuntimeError("x")

        fake.report_if_rate_limited = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        monkeypatch.setattr(nm.httpx, "post", boom)
        assert nm._score_headline("h") == 0.0

    def test_malformed_payload_returns_zero(self, monkeypatch, rl):
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(200, {"unexpected": True}))
        assert nm._score_headline("h") == 0.0


class TestScoreHeadlineConfigErrorPause:
    """group 176: 400/401/403/404/410 from the HF router pause the call and log one explained warning."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", 0.0)
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.delenv("HF_ERROR_BACKOFF_SEC", raising=False)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_config_error_pauses_after_one_call(self, monkeypatch, status):
        calls = []

        def post(*a, **k):
            calls.append(1)
            return _Resp(status)

        monkeypatch.setattr(nm.httpx, "post", post)
        assert [nm._score_headline(f"h{i}") for i in range(4)] == [0.0] * 4
        assert len(calls) == 1
        assert nm._HF_BACKOFF_UNTIL > nm.time.monotonic() + 1700

    def test_one_warning_carries_hf_message_and_model(self, monkeypatch, caplog):
        resp = _Resp(400)
        resp.text = '{"error": "The requested model  is not supported by any provider you have enabled."}'
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: resp)
        with caplog.at_level(logging.WARNING):
            for i in range(3):
                nm._score_headline(f"h{i}")
        msgs = [r.getMessage() for r in caplog.records if "HF API error" in r.getMessage()]
        assert len(msgs) == 1
        assert "400" in msgs[0] and "not supported by any provider" in msgs[0] and nm.HF_MODEL in msgs[0]
        assert "1800" in msgs[0]

    def test_pause_expires(self, monkeypatch):
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(400))
        nm._score_headline("h")
        monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", nm.time.monotonic() - 1)
        monkeypatch.setattr(nm.httpx, "post",
                            lambda *a, **k: _Resp(200, {"choices": [{"message": {"content": "negative"}}]}))
        assert nm._score_headline("h") == -0.8

    def test_zero_means_never_pause(self, monkeypatch):
        monkeypatch.setenv("HF_ERROR_BACKOFF_SEC", "0")
        calls = []
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: calls.append(1) or _Resp(400))
        for i in range(3):
            nm._score_headline(f"h{i}")
        assert len(calls) == 3 and nm._HF_BACKOFF_UNTIL == 0.0

    @pytest.mark.parametrize("raw", ["", "  ", "x", "-3"])
    def test_blank_or_invalid_uses_default(self, monkeypatch, raw):
        monkeypatch.setenv("HF_ERROR_BACKOFF_SEC", raw)
        assert nm._hf_error_backoff_sec() == 1800.0

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_other_statuses_do_not_pause(self, monkeypatch, status):
        monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _Resp(status))
        nm._score_headline("h")
        assert nm._HF_BACKOFF_UNTIL == 0.0


# ── routes ────────────────────────────────────────────────────────────────────

class TestRoutes:
    def test_root(self):
        r = nm.root()
        assert r["service"] == "Stockky News Intelligence Service"
        assert r["status"] == "running"
        assert "Google News" in r["sources"] and "NewsAPI (optional)" in r["sources"]

    def test_health(self):
        assert nm.health() == {"status": "ok", "service": "news-intelligence-service"}


class TestAnalyze:
    def _h(self, n, publisher="Pub"):
        return [{"title": f"Headline number {i}", "publisher": publisher, "published": f"2026-01-{i + 1:02d}"} for i in range(n)]

    def test_uses_news_quality_payload_when_it_has_content(self, monkeypatch):
        payload = {"headline_count": 3, "headlines": [1, 2, 3], "news_score": 71}
        seen = {}

        def fake_build(symbol, company_name=None, llm_summarizer=None):
            seen.update(symbol=symbol, company_name=company_name, llm=llm_summarizer)
            return payload

        monkeypatch.setattr(nm, "build_news_response", fake_build)
        assert nm.analyze("TCS.NS", company_name="Tata") is payload
        assert seen == {"symbol": "TCS.NS", "company_name": "Tata", "llm": None}

    def test_known_symbol_gets_company_name_hint(self, monkeypatch):
        seen = {}

        def fake_build(symbol, company_name=None, llm_summarizer=None):
            seen["cn"] = company_name
            return {"headline_count": 1, "headlines": [1]}

        monkeypatch.setattr(nm, "build_news_response", fake_build)
        nm.analyze("TCS.NS")
        assert seen["cn"] == "Tata Consultancy Services"
        nm.analyze("NEWCO")
        assert seen["cn"] is None

    def test_empty_news_payload_marks_quality_none(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: [])
        out = nm.analyze("NEWCO")
        assert out["data_quality"]["level"] == "none" and out["headline_count"] == 0

    def test_ndtv_feed_is_the_live_atom_url(self, monkeypatch):
        urls = []
        monkeypatch.setattr(nm.feedparser, "parse", lambda u: (urls.append(1), _parsed())[1])
        import feed_fetch
        seen = []
        monkeypatch.setattr(feed_fetch, "_download", lambda url: (seen.append(url), (b"x", {"status": 200}))[1])
        nm._fetch_ndtv_profit("INFY")
        assert "ndtv.com/business/rss" not in seen[0] and "bloombergquint" in seen[0]

    def test_falls_through_when_payload_empty(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", lambda *a, **k: {"headline_count": 0, "headlines": []})
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: [])
        out = nm.analyze("infy.ns")
        assert out["headline_count"] == 0 and out["news_score"] == 50

    def test_falls_through_when_payload_not_dict(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", lambda *a, **k: None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: [])
        assert nm.analyze("INFY")["news_score"] == 50

    def test_falls_back_on_exception(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("nq broke")

        monkeypatch.setattr(nm, "build_news_response", boom)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: [])
        with caplog.at_level(logging.WARNING):
            out = nm.analyze("INFY")
        assert out["news_score"] == 50
        assert "using legacy" in caplog.text

    def test_no_headlines_neutral_payload(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: [])
        out = nm.analyze("tcs.bo")
        assert out["symbol"] == "TCS"
        assert out["news_score"] == 50
        assert out["headline_count"] == 0
        assert out["headlines"] == []
        assert out["reasons"] == ["No recent relevant news found — treating as neutral"]
        assert "No recent relevant news" in out["summary"]

    def test_positive_scoring(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "HF_API_KEY", "K")
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: self._h(4))
        monkeypatch.setattr(nm, "_score_headline", lambda t: 0.8)
        out = nm.analyze("INFY.NS")
        assert out["symbol"] == "INFY"
        assert out["news_score"] == 90
        assert out["headline_count"] == 4
        assert any(r.startswith("Notably positive") for r in out["reasons"])
        assert not any(r.startswith("Notably negative") for r in out["reasons"])
        assert out["reasons"][-1] == "4 relevant headlines, average sentiment positive"
        assert out["data_quality"]["level"] == "high"
        assert out["data_quality"]["note"] == "Multiple corroborating headlines"
        assert out["data_quality"]["hf_sentiment"] is True
        assert out["data_quality"]["sources_used"] == ["Pub"]

    def test_negative_scoring(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "HF_API_KEY", None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: self._h(2))
        monkeypatch.setattr(nm, "_score_headline", lambda t: -0.8)
        out = nm.analyze("INFY")
        assert out["news_score"] == 10
        assert any(r.startswith("Notably negative") for r in out["reasons"])
        assert out["reasons"][-1].endswith("average sentiment negative")
        assert out["data_quality"]["level"] == "medium"
        assert out["data_quality"]["note"] == "Adequate free-source coverage"
        assert out["data_quality"]["hf_sentiment"] is False

    def test_neutral_scoring_single_headline_is_low_quality(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: self._h(1))
        monkeypatch.setattr(nm, "_score_headline", lambda t: 0.0)
        out = nm.analyze("INFY")
        assert out["news_score"] == 50
        assert out["reasons"] == ["1 relevant headlines, average sentiment neutral"]
        assert out["data_quality"]["level"] == "low"
        assert out["data_quality"]["note"] == "Limited free-source coverage — treat score as soft"

    def test_mixed_scores_surface_both_extremes(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        hs = self._h(2)
        hs[0]["title"] = "Great news"
        hs[1]["title"] = "Terrible news"
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: hs)
        monkeypatch.setattr(nm, "_score_headline", lambda t: 0.8 if t == "Great news" else -0.8)
        out = nm.analyze("INFY")
        assert out["news_score"] == 50
        assert out["reasons"][0].startswith('Notably negative: "Terrible news')
        assert out["reasons"][1].startswith('Notably positive: "Great news')

    def test_headlines_capped_at_eight(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: self._h(12))
        monkeypatch.setattr(nm, "_score_headline", lambda t: 0.0)
        assert len(nm.analyze("INFY")["headlines"]) == 8

    def test_unknown_publisher_labelled(self, monkeypatch):
        monkeypatch.setattr(nm, "build_news_response", None)
        hs = [{"title": "Headline one", "published": "2026-01-01"}]
        monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: hs)
        monkeypatch.setattr(nm, "_score_headline", lambda t: 0.0)
        assert nm.analyze("INFY")["data_quality"]["sources_used"] == ["unknown"]


# ── import fallback, __main__ block, _utcnow ──────────────────────────────────

_NEWS_MAIN = os.path.join(os.path.dirname(_HERE), "news", "main.py")


class TestModuleLevel:
    def test_news_quality_missing_disables_build_news_response(self, monkeypatch):
        # `from news_quality import ...` failing must leave build_news_response = None
        # instead of breaking service start-up (lines 11-12).
        monkeypatch.setitem(sys.modules, "news_quality", None)
        ns = runpy.run_path(_NEWS_MAIN, run_name="news_main_no_quality")
        assert ns["build_news_response"] is None

    def _run_main(self, monkeypatch):
        started = []
        fake = types.ModuleType("uvicorn")
        fake.run = lambda *a, **k: started.append((a, k))
        monkeypatch.setitem(sys.modules, "uvicorn", fake)
        runpy.run_path(_NEWS_MAIN, run_name="__main__")
        return started

    def test_main_block_port_from_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "9124")
        assert self._run_main(monkeypatch) == [
            (("main:app",), {"host": "0.0.0.0", "port": 9124, "reload": True})]

    def test_main_block_port_default(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        assert self._run_main(monkeypatch)[0][1]["port"] == 8005

    def test_utcnow_naive_current_no_deprecation(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            t = nm._utcnow()
        assert t.tzinfo is None
        assert abs((_now() - t).total_seconds()) < 5


# ── Yahoo news backoff (group108, item 9) ─────────────────────────────────────

_real_yahoo = nm._fetch_yahoo_news


class TestYahooNewsBackoff:
    def _clock(self, monkeypatch, start=1000.0):
        now = {"t": start}
        monkeypatch.setattr(nm, "_mono", lambda: now["t"])
        return now

    def test_skips_yahoo_after_consecutive_empty_results_then_resumes(self, monkeypatch):
        now = self._clock(monkeypatch)
        monkeypatch.setattr(nm, "_YAHOO_BACKOFF_AFTER", 3)
        yf = _fake_yf([])
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        for _ in range(3):
            assert nm._fetch_yahoo_news("TCS") == []
        assert len(yf._seen) == 3
        assert nm._fetch_yahoo_news("TCS") == [] and len(yf._seen) == 3   # skipped, no yfinance call
        now["t"] += 599
        nm._fetch_yahoo_news("TCS")
        assert len(yf._seen) == 3                                          # still inside the window
        now["t"] += 2
        nm._fetch_yahoo_news("TCS")
        assert len(yf._seen) == 4                                          # window over, Yahoo tried again

    def test_exceptions_count_as_misses(self, monkeypatch):
        self._clock(monkeypatch)
        monkeypatch.setattr(nm, "_YAHOO_BACKOFF_AFTER", 2)
        yf = _fake_yf(raises=True)
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        nm._fetch_yahoo_news("A")
        nm._fetch_yahoo_news("B")
        nm._fetch_yahoo_news("C")
        assert len(yf._seen) == 2

    def test_a_result_with_items_resets_the_count(self, monkeypatch):
        self._clock(monkeypatch)
        monkeypatch.setattr(nm, "_YAHOO_BACKOFF_AFTER", 3)
        empty, good = _fake_yf([]), _fake_yf([{"title": "ok"}])
        monkeypatch.setitem(sys.modules, "yfinance", empty)
        nm._fetch_yahoo_news("A")
        nm._fetch_yahoo_news("B")
        monkeypatch.setitem(sys.modules, "yfinance", good)
        assert nm._fetch_yahoo_news("C")[0]["title"] == "ok"
        monkeypatch.setitem(sys.modules, "yfinance", empty)
        nm._fetch_yahoo_news("D")
        nm._fetch_yahoo_news("E")
        assert nm._yahoo_backoff_active() is False                         # 2 misses since the reset, below 3
        nm._fetch_yahoo_news("F")
        assert nm._yahoo_backoff_active() is True

    def test_zero_disables_the_backoff(self, monkeypatch):
        self._clock(monkeypatch)
        monkeypatch.setattr(nm, "_YAHOO_BACKOFF_AFTER", 0)
        yf = _fake_yf([])
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        for _ in range(25):
            nm._fetch_yahoo_news("TCS")
        assert len(yf._seen) == 25

    def test_other_sources_still_run_while_yahoo_is_skipped(self, monkeypatch):
        self._clock(monkeypatch)
        nm._yahoo_state["skip_until"] = 10 ** 9
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf([{"title": "never"}]))
        called = _patch_sources(monkeypatch, {"_fetch_google_news": [{"title": "G", "published": "2026-10-01"}]})
        # _patch_sources replaced _fetch_yahoo_news with a stub; restore the real one to exercise the skip
        monkeypatch.setattr(nm, "_fetch_yahoo_news", _real_yahoo)
        out = nm._fetch_headlines("TCS")
        assert [h["title"] for h in out] == ["G"]
        assert "_fetch_google_news" in called

    def test_blank_env_values_mean_the_defaults(self, monkeypatch):
        # Run the file fresh (not importlib.reload: several test files import a module named `main`).
        monkeypatch.setenv("YAHOO_NEWS_BACKOFF_AFTER", "")
        monkeypatch.setenv("YAHOO_NEWS_BACKOFF_SECONDS", " ")
        g = runpy.run_path(nm.__file__, run_name="news_main_fresh")
        assert g["_YAHOO_BACKOFF_AFTER"] == 10 and g["_YAHOO_BACKOFF_SECONDS"] == 600.0



# ── company-name lookup for tickers outside NAME_HINTS (2026-10-04 startup-log audit, issue 3) ──

def _fake_yf_info(info=None, raises=False):
    mod = types.ModuleType("yfinance")
    seen = []

    class Ticker:
        def __init__(self, sym):
            seen.append(sym)
            if raises:
                raise RuntimeError("yahoo down")
            self.info = info

    mod.Ticker = Ticker
    mod._seen = seen
    return mod


class TestCompanyNameLookup:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setattr(nm, "_NAME_LOOKUP_ENABLED", True)

    def test_unknown_ticker_uses_yahoo_long_name_without_ltd_suffix(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Shree Tirupati Balajee Agro Trading Co. Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        assert nm._company_query("shreetnb.ns") == "Shree Tirupati Balajee Agro Trading Co"
        assert yf._seen == ["SHREETNB.NS"]

    def test_falls_back_to_short_name_when_no_long_name(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf_info({"shortName": "DEVIT LTD"}))
        assert nm._company_query("DEVIT") == "DEVIT"      # cleaned name == ticker -> no information, ticker used

    def test_name_hint_wins_and_never_calls_yahoo(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Should Not Be Used"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        assert nm._company_query("TCS") == "Tata Consultancy Services"
        assert yf._seen == []

    def test_result_is_cached_so_yahoo_is_called_once(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Delta Magnets Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        assert nm._company_query("DELTAMAGNT") == "Delta Magnets"
        assert nm._company_query("DELTAMAGNT.NS") == "Delta Magnets"
        assert len(yf._seen) == 1

    def test_failed_lookup_falls_back_to_ticker_and_is_cached_for_the_miss_ttl(self, monkeypatch):
        yf = _fake_yf_info(raises=True)
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        assert nm._company_query("ZZZCO") == "ZZZCO"
        assert nm._company_query("ZZZCO") == "ZZZCO"
        assert len(yf._seen) == 1                         # miss cached - no retry storm

    def test_miss_is_retried_after_the_miss_ttl(self, monkeypatch):
        yf = _fake_yf_info(raises=True)
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        monkeypatch.setattr(nm, "_NAME_MISS_TTL_S", 0.0)
        nm._company_query("ZZZCO")
        nm._company_query("ZZZCO")
        assert len(yf._seen) == 2

    def test_hit_is_refreshed_after_the_hit_ttl(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Acme Widgets Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        monkeypatch.setattr(nm, "_NAME_HIT_TTL_S", 0.0)
        nm._company_query("ACMEW")
        nm._company_query("ACMEW")
        assert len(yf._seen) == 2

    def test_yahoo_backoff_window_skips_lookup_and_does_not_cache_a_false_miss(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Acme Widgets Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        nm._yahoo_state["skip_until"] = nm._mono() + 600
        assert nm._company_query("ACMEW") == "ACMEW"
        assert yf._seen == [] and "ACMEW" not in nm._NAME_CACHE
        nm._yahoo_state["skip_until"] = 0.0
        assert nm._company_query("ACMEW") == "Acme Widgets"      # recovered once the window closes

    def test_lookup_can_be_switched_off(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Acme Widgets Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        monkeypatch.setattr(nm, "_NAME_LOOKUP_ENABLED", False)
        assert nm._company_query("ACMEW") == "ACMEW" and yf._seen == []

    def test_clean_company_name_edge_cases(self):
        assert nm._clean_company_name(None, "X") == ""
        assert nm._clean_company_name("   ", "X") == ""
        assert nm._clean_company_name("AB", "X") == ""                       # too short to be a useful query
        assert nm._clean_company_name("Infosys Ltd.", "INFY") == "Infosys"
        assert nm._clean_company_name("Acme Corporation", "ACMEX") == "Acme"

    def test_match_keywords_include_the_resolved_name_parts(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf_info({"longName": "Delta Magnets Limited"}))
        keys = nm._match_keywords("DELTAMAGNT")
        assert "delta magnets" in keys and "magnets" in keys and "deltamagnt" in keys

    def test_analyze_passes_the_resolved_name_to_the_quality_path(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf_info({"longName": "Delta Magnets Limited"}))
        seen = {}

        def fake_build(symbol, company_name=None, llm_summarizer=None):
            seen["name"] = company_name
            return {"headline_count": 1, "headlines": [{"title": "x"}]}

        monkeypatch.setattr(nm, "build_news_response", fake_build)
        nm.analyze("DELTAMAGNT")
        assert seen["name"] == "Delta Magnets"

    def test_analyze_explicit_company_name_is_not_overridden(self, monkeypatch):
        yf = _fake_yf_info({"longName": "Delta Magnets Limited"})
        monkeypatch.setitem(sys.modules, "yfinance", yf)
        seen = {}

        def fake_build(symbol, company_name=None, llm_summarizer=None):
            seen["name"] = company_name
            return {"headline_count": 1, "headlines": [{"title": "x"}]}

        monkeypatch.setattr(nm, "build_news_response", fake_build)
        nm.analyze("DELTAMAGNT", company_name="Custom Name")
        assert seen["name"] == "Custom Name" and yf._seen == []


class TestNameLookupEnvIsBlankSafe:
    """A stray `NEWS_NAME_*=` (set but empty) in a .env must keep the defaults - a blank TTL used to be able
    to crash the import with float('') and a blank flag must not switch the lookup off."""

    def test_blank_env_values_keep_the_defaults(self, monkeypatch):
        for k in ("NEWS_NAME_LOOKUP", "NEWS_NAME_CACHE_TTL_S", "NEWS_NAME_MISS_TTL_S"):
            monkeypatch.setenv(k, "   ")
        g = runpy.run_path(nm.__file__, run_name="news_main_blank_env")
        assert g["_NAME_LOOKUP_ENABLED"] is True
        assert g["_NAME_HIT_TTL_S"] == 604800.0 and g["_NAME_MISS_TTL_S"] == 3600.0

    def test_explicit_values_are_honoured(self, monkeypatch):
        monkeypatch.setenv("NEWS_NAME_LOOKUP", "off")
        monkeypatch.setenv("NEWS_NAME_CACHE_TTL_S", "60")
        monkeypatch.setenv("NEWS_NAME_MISS_TTL_S", "5")
        g = runpy.run_path(nm.__file__, run_name="news_main_explicit_env")
        assert g["_NAME_LOOKUP_ENABLED"] is False
        assert g["_NAME_HIT_TTL_S"] == 60.0 and g["_NAME_MISS_TTL_S"] == 5.0
