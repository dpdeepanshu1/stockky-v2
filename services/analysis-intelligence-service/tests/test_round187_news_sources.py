"""Round 187 (issue #3): per-symbol publisher search, paused-source logging, source tally, frozen-feed warning (event/main.py)."""
from __future__ import annotations

import logging
import os
import sys
import time
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import pytest

import main as em  # noqa: E402  (event/main.py)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    em._NEWS_SRC_STATS.clear()
    em._FEED_AGE.clear()
    em._YF_NEWS_STATE.update({"empty": 0, "pause_until": 0.0})
    em._FEED_CACHE.clear()
    fake = types.ModuleType("rate_limit_report")
    fake.report_if_rate_limited = lambda exc, **kw: None
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    for k in ("EVENT_PUBLISHER_SITES", "EVENT_PUBLISHER_SEARCH_DAYS", "EVENT_FEED_STALE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── publisher search ─────────────────────────────────────────────────────────

def test_query_names_company_and_all_default_sites():
    q = em._publisher_search_query("POKARNA.NS", "Pokarna Quartz Limited")
    assert q.startswith('"Pokarna Quartz" (') and q.endswith(") when:7d")
    for site in ("moneycontrol.com", "economictimes.indiatimes.com", "cnbctv18.com", "business-standard.com", "livemint.com", "ndtvprofit.com"):
        assert f"site:{site}" in q
    assert " OR " in q


def test_query_falls_back_to_ticker_when_name_unknown():
    assert em._publisher_search_query("HMAAGRO.NS", "HMAAGRO").startswith('"HMAAGRO" NSE (')
    assert em._publisher_search_query("HMAAGRO.NS", "").startswith('"HMAAGRO" NSE (')


def test_sites_and_days_are_configurable(monkeypatch):
    monkeypatch.setenv("EVENT_PUBLISHER_SITES", "a.com, B.org")
    monkeypatch.setenv("EVENT_PUBLISHER_SEARCH_DAYS", "3")
    q = em._publisher_search_query("X.NS", "Xyz Ltd")
    assert "site:a.com OR site:b.org" in q and q.endswith("when:3d")


@pytest.mark.parametrize("val", ["off", "0", "none", "FALSE"])
def test_switch_off_makes_no_request(monkeypatch, val):
    monkeypatch.setenv("EVENT_PUBLISHER_SITES", val)
    monkeypatch.setattr(em, "_google_news_parse", lambda *a, **k: pytest.fail("must not search when off"))
    assert em._fetch_publisher_search("X.NS") == []


def test_fetch_uses_google_parse_with_the_query(monkeypatch):
    seen = {}
    monkeypatch.setattr(em, "_get_company_name", lambda s: "Pokarna Quartz Limited")
    def fake(symbol, query, max_items):
        seen.update(symbol=symbol, query=query, n=max_items)
        return [{"title": "Pokarna wins order", "publisher": "Moneycontrol"}], True
    monkeypatch.setattr(em, "_google_news_parse", fake)
    out = em._fetch_publisher_search("POKARNA.NS", max_items=8)
    assert out[0]["publisher"] == "Moneycontrol" and seen["n"] == 8 and '"Pokarna Quartz"' in seen["query"]


def test_aggregator_includes_publisher_items_and_dedupes_against_google(monkeypatch):
    for n in ("_fetch_yf_news", "_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18", "_fetch_market_feeds"):
        monkeypatch.setattr(em, n, lambda s, *a, **k: [])
    monkeypatch.setattr(em, "_fetch_google_news", lambda s, *a, **k: [{"title": "Same story", "publisher": "G", "published": "2026-11-04T05:00:00"}])
    monkeypatch.setattr(em, "_fetch_publisher_search", lambda s, *a, **k: [
        {"title": "same story", "publisher": "P", "published": "2026-11-04T05:00:00"},
        {"title": "Only on the publisher", "publisher": "Moneycontrol", "published": "2026-11-04T06:00:00"}])
    out = em._fetch_news_from_multiple_sources("A.NS")
    assert [n["title"] for n in out] == ["Only on the publisher", "Same story"]


# ── paused / tally ───────────────────────────────────────────────────────────

def _quiet_sources(monkeypatch, **by_name):
    for n in ("_fetch_yf_news", "_fetch_google_news", "_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18", "_fetch_publisher_search", "_fetch_market_feeds"):
        data = by_name.get(n, [])
        monkeypatch.setattr(em, n, lambda s, *a, _d=data, **k: list(_d))


def test_paused_yahoo_is_logged_as_paused_not_zero_items(monkeypatch, caplog):
    _quiet_sources(monkeypatch)
    em._YF_NEWS_STATE["pause_until"] = time.time() + 900
    with caplog.at_level(logging.INFO):
        em._fetch_news_from_multiple_sources("A.NS")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Yahoo Finance for A.NS: PAUSED" in text and "Yahoo Finance for A.NS: 0 items" not in text
    assert "Google News for A.NS: 0 items" in text


def test_tally_counts_asked_items_empty_and_paused(monkeypatch):
    _quiet_sources(monkeypatch, _fetch_google_news=[{"title": "x"}])
    em._fetch_news_from_multiple_sources("A.NS")
    em._YF_NEWS_STATE["pause_until"] = time.time() + 900
    em._fetch_news_from_multiple_sources("B.NS")
    st = em._NEWS_SRC_STATS
    assert st["Google News"] == {"asked": 2, "items": 2, "empty": 0, "paused": 0}
    assert st["Yahoo Finance"] == {"asked": 1, "items": 0, "empty": 1, "paused": 1}
    assert st["Publisher search"]["empty"] == 2


def test_news_sources_route_reports_state():
    em._note_news_source("Google News", 3, False)
    em._YF_NEWS_STATE["pause_until"] = time.time() + 600
    out = em.news_sources()
    assert out["sources"]["Google News"]["items"] == 1
    assert out["yahoo"]["paused"] is True and out["yahoo"]["seconds_left"] > 500
    assert "moneycontrol.com" in out["publisher_sites"]


# ── frozen feed ──────────────────────────────────────────────────────────────

def _feed(days_old, n=3):
    t = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(days=days_old)).timetuple()
    return SimpleNamespace(entries=[SimpleNamespace(published_parsed=t) for _ in range(n)])


def test_old_feed_is_flagged_frozen_and_warned_once(caplog):
    with caplog.at_level(logging.WARNING):
        em._note_feed_age("http://x/feed", _feed(10))
        em._note_feed_age("http://x/feed", _feed(10))
    assert em._FEED_AGE["http://x/feed"]["frozen"] is True
    assert sum("FROZEN" in r.getMessage() for r in caplog.records) == 1


def test_fresh_feed_is_not_frozen(caplog):
    with caplog.at_level(logging.WARNING):
        em._note_feed_age("http://x/feed", _feed(0))
    assert em._FEED_AGE["http://x/feed"]["frozen"] is False and not caplog.records


def test_stale_limit_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EVENT_FEED_STALE_HOURS", "0")
    em._note_feed_age("http://x/feed", _feed(30))
    assert em._FEED_AGE["http://x/feed"]["frozen"] is False


def test_empty_or_undated_feed_never_raises():
    em._note_feed_age("http://x/e", SimpleNamespace(entries=[]))
    em._note_feed_age("http://x/u", SimpleNamespace(entries=[SimpleNamespace()]))
    em._note_feed_age("http://x/n", None)
    assert em._FEED_AGE["http://x/e"]["frozen"] is False


def test_parse_site_feed_runs_the_age_check(monkeypatch):
    monkeypatch.setattr(em, "_site_feed_parse", lambda url: _feed(20))
    em._parse_site_feed("http://x/frozen")
    assert em._FEED_AGE["http://x/frozen"]["frozen"] is True
