"""Round 600: the 3-day news rule (NEWS_MAX_AGE_DAYS). News older than 72 h is not recorded, not cached, not scored and not served.

Covers: the shared helper (parsing, boundaries, env clamp, counters), the event tracker (per-source filter, ISO normalisation, cached and fallback
reads, the 3-day fallback TTL, the publisher-search window, /events/raw-feed, /news-sources), the news service (headline filter, source caps),
the event score (nothing older than the window moves it) and the byte-for-byte copy of news_age.py in event/ and news/."""
from __future__ import annotations

import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import pytest

import news_age as na  # noqa: E402  (event/news_age.py)
import main as em  # noqa: E402  (event/main.py)
import event_depth as ed  # noqa: E402

NOW = datetime(2026, 11, 18, 10, 0, 0)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _live_ago(hours: float) -> str:
    return (em._utcnow() - timedelta(hours=hours)).isoformat()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("NEWS_MAX_AGE_DAYS", raising=False)
    monkeypatch.delenv("EVENT_PUBLISHER_SEARCH_DAYS", raising=False)
    na._STATS.clear()
    na._LAST_LOG[0] = 0.0
    em._NEWS_SRC_STATS.clear()
    fake = types.ModuleType("rate_limit_report")
    fake.report_if_rate_limited = lambda exc, **kw: None
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    yield


# ── the setting ──────────────────────────────────────────────────────────────

def test_default_is_three_days():
    assert na.news_max_age_days() == 3.0 and na.news_max_age_seconds() == 3 * 86400.0


@pytest.mark.parametrize("raw,expected", [("2", 2.0), ("1", 1.0), ("1.5", 1.5), ("3", 3.0), (" 2 ", 2.0)])
def test_it_can_only_lower_the_window(monkeypatch, raw, expected):
    monkeypatch.setenv("NEWS_MAX_AGE_DAYS", raw)
    assert na.news_max_age_days() == expected


@pytest.mark.parametrize("raw", ["", "  ", "abc", "nan", "inf", "0", "0.5", "-1", "4", "7", "30"])
def test_anything_else_falls_back_to_three(monkeypatch, raw):
    """A typo, a blank, a value under 1 or any value above 3 gives 3 - nobody can raise the window past three days."""
    monkeypatch.setenv("NEWS_MAX_AGE_DAYS", raw)
    assert na.news_max_age_days() == 3.0


# ── reading a publish time ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "2026-11-17T10:00:00", "2026-11-17T10:00:00Z", "2026-11-17T15:30:00+05:30", "Tue, 17 Nov 2026 10:00:00 GMT",
    datetime(2026, 11, 17, 10, 0, 0), datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc),
])
def test_parse_reads_every_format_the_sources_send(value):
    assert na.parse_news_time(value) == datetime(2026, 11, 17, 10, 0, 0)


def test_parse_epoch_seconds_and_milliseconds_and_strings():
    ts = int(datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc).timestamp())
    for v in (ts, float(ts), str(ts), ts * 1000, str(ts * 1000)):
        assert na.parse_news_time(v) == datetime(2026, 11, 17, 10, 0, 0)


@pytest.mark.parametrize("value", [None, "", "  ", "None", "null", "nan", "not a date", True, 0, -5, 12345, [], {}])
def test_parse_returns_none_for_what_it_cannot_read(value):
    assert na.parse_news_time(value) is None


# ── freshness boundaries ─────────────────────────────────────────────────────

def test_71_hours_is_kept_and_73_hours_is_dropped():
    assert na.is_fresh_news(_ago(71), NOW) is True
    assert na.is_fresh_news(_ago(72), NOW) is True          # the boundary itself is still inside
    assert na.is_fresh_news(_ago(73), NOW) is False


def test_missing_or_unreadable_date_is_not_fresh():
    for v in (None, "", "junk", "1700000000"):
        assert na.is_fresh_news(v, NOW) is False


def test_future_stamp_allowed_only_within_clock_skew():
    assert na.is_fresh_news((NOW + timedelta(hours=1)).isoformat(), NOW) is True
    assert na.is_fresh_news((NOW + timedelta(hours=12)).isoformat(), NOW) is False


def test_window_follows_the_setting(monkeypatch):
    monkeypatch.setenv("NEWS_MAX_AGE_DAYS", "1")
    assert na.is_fresh_news(_ago(23), NOW) is True and na.is_fresh_news(_ago(25), NOW) is False
    assert na.news_cutoff(NOW) == NOW - timedelta(days=1)


def test_explicit_window_argument():
    assert na.is_fresh_news(_ago(30), NOW, max_age_days=1) is False
    assert na.is_fresh_news(_ago(30), NOW, max_age_days=2) is True


# ── filter + counters ────────────────────────────────────────────────────────

def test_filter_keeps_order_drops_old_undated_and_non_dicts_and_counts():
    items = [{"t": "a", "published": _ago(5)}, {"t": "old", "published": _ago(100)}, {"t": "none", "published": None},
             "junk", {"t": "b", "published": _ago(60)}]
    out = na.filter_fresh_news(items, "published", NOW, "Google News")
    assert [i["t"] for i in out] == ["a", "b"]
    assert na.age_drop_stats()["sources"]["Google News"] == {"kept": 2, "dropped_old": 2, "dropped_undated": 1}
    assert na.age_drop_stats()["max_age_days"] == 3.0


def test_filter_handles_none_and_empty():
    assert na.filter_fresh_news(None) == [] and na.filter_fresh_news([]) == []
    assert na.age_drop_stats()["sources"] == {}


def test_hourly_log_line_once(caplog):
    na.filter_fresh_news([{"published": _ago(100)}], "published", NOW, "Yahoo Finance")
    lg = logging.getLogger("t600")
    with caplog.at_level(logging.INFO, logger="t600"):
        na.maybe_log_age_drops(lg)
        na.maybe_log_age_drops(lg)
    lines = [r.getMessage() for r in caplog.records if "news age rule" in r.getMessage()]
    assert len(lines) == 1 and "Yahoo Finance: kept 0, old 1, undated 0" in lines[0]


# ── the two copies of the helper stay identical ──────────────────────────────

def test_event_and_news_copies_are_identical():
    a = open(os.path.join(_SERVICE, "event", "news_age.py"), "rb").read()
    b = open(os.path.join(_SERVICE, "news", "news_age.py"), "rb").read()
    assert a == b


# ── event tracker ────────────────────────────────────────────────────────────

def test_fresh_news_items_drops_old_undated_and_rewrites_published_as_iso():
    epoch_fresh = str(int((em._utcnow() - timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp()))
    items = [
        {"title": "yahoo epoch", "published": epoch_fresh},
        {"title": "rss iso", "published": _live_ago(10)},
        {"title": "too old", "published": _live_ago(80)},
        {"title": "undated", "published": None},
        {"title": "yahoo empty string", "published": ""},
    ]
    out = em._fresh_news_items(items, "Yahoo Finance")
    assert [i["title"] for i in out] == ["yahoo epoch", "rss iso"]
    for i in out:
        datetime.fromisoformat(i["published"])           # every kept item now has an ISO stamp
        assert "T" in i["published"]


_ALL_BUT = ("_fetch_google_news", "_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18", "_fetch_publisher_search",
            "_fetch_market_feeds")


def test_old_yahoo_item_no_longer_gets_through(monkeypatch):
    """Before round 600 the Yahoo items were never age-checked at all."""
    for n in _ALL_BUT:
        monkeypatch.setattr(em, n, lambda s, *a, **k: [])
    monkeypatch.setattr(em, "_fetch_yf_news", lambda s: [{"title": "From 2023", "published": "1700000000"}])
    assert em._fetch_news_from_multiple_sources("A.NS") == []
    assert em._NEWS_SRC_STATS["Yahoo Finance"]["empty"] == 1     # a source that only returns old items shows as empty (dead-feed signal)


def test_mixed_formats_now_sort_newest_first(monkeypatch):
    for n in _ALL_BUT:
        monkeypatch.setattr(em, n, lambda s, *a, **k: [])
    newest_epoch = str(int((em._utcnow() - timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()))
    monkeypatch.setattr(em, "_fetch_yf_news", lambda s: [{"title": "older", "published": _live_ago(20)}, {"title": "newest", "published": newest_epoch}])
    assert [n["title"] for n in em._fetch_news_from_multiple_sources("A.NS")] == ["newest", "older"]


def test_all_old_source_is_logged(monkeypatch, caplog):
    for n in ("_fetch_yf_news", "_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18", "_fetch_publisher_search", "_fetch_market_feeds"):
        monkeypatch.setattr(em, n, lambda s, *a, **k: [])
    monkeypatch.setattr(em, "_fetch_google_news", lambda s, *a, **k: [{"title": "x", "published": _live_ago(200)}])
    with caplog.at_level(logging.INFO):
        em._fetch_news_from_multiple_sources("A.NS")
    assert any("Google News for A.NS: 1 item(s), all older than 3 days or undated - dropped" in r.getMessage() for r in caplog.records)


def test_per_source_rss_cutoff_is_three_days_not_thirty(monkeypatch):
    def entry(days_ago):
        t = (em._utcnow() - timedelta(days=days_ago)).timetuple()
        return types.SimpleNamespace(title="Zenith Widgets posts profit", link="http://x", published_parsed=t, source=None)
    parsed = types.SimpleNamespace(entries=[entry(2), entry(5), entry(20)], bozo=False)
    monkeypatch.setattr(em.feedparser, "parse", lambda url: parsed)
    items, ok = em._google_news_parse("ZWID.NS", "Zenith Widgets", 10)
    assert ok and len(items) == 1


# ── fallback + cache reads ───────────────────────────────────────────────────

def test_fallback_copy_lives_three_days_not_thirty():
    assert em.EVENT_FALLBACK_TTL == 3 * 24 * 3600


def _record(*news):
    return {"symbol": "ZWID.NS", "recent_news": list(news), "classified_events": [dict(n) for n in news], "bulk_deals": [],
            "last_dividend": None, "next_earnings_date": None, "summary": "s"}


def test_apply_news_age_returns_the_same_object_when_nothing_aged():
    rec = _record({"title": "fresh", "published": _live_ago(5)})
    assert em._apply_news_age(rec) is rec


def test_apply_news_age_drops_aged_items_and_rebuilds_the_summary():
    rec = _record({"title": "fresh", "published": _live_ago(5)}, {"title": "stale", "published": _live_ago(90)}, {"title": "undated"})
    out = em._apply_news_age(rec)
    assert [n["title"] for n in out["recent_news"]] == ["fresh"]
    assert [n["title"] for n in out["classified_events"]] == ["fresh"]
    assert len(rec["recent_news"]) == 3                      # the stored record is not mutated
    assert "event_score" in out                              # the score was rebuilt from what is left


def test_apply_news_age_passes_non_dicts_and_survives_errors(monkeypatch):
    assert em._apply_news_age(None) is None
    bad = {"recent_news": [{"title": "x", "published": _live_ago(90)}]}
    monkeypatch.setattr(em, "_summarize_events", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    assert em._apply_news_age(bad) is bad                    # an error returns the record as stored


def test_cached_record_with_aged_news_is_filtered_on_read(monkeypatch):
    rec = _record({"title": "fresh", "published": _live_ago(5)}, {"title": "aged since cached", "published": _live_ago(80)})
    em._redis_set(f"{em.EVENT_CACHE_PREFIX}ZWID.NS", rec, ttl=100)
    monkeypatch.setattr(em, "_get_earnings_dates", lambda *a, **k: pytest.fail("cache should short-circuit"))
    out = em._fetch_events("ZWID.NS")
    assert [n["title"] for n in out["recent_news"]] == ["fresh"]


def test_fallback_record_is_filtered_before_it_is_served(monkeypatch):
    rec = _record({"title": "month old", "published": _live_ago(24 * 29)})
    rec["last_dividend"] = None
    em._redis_set(f"{em.EVENT_FALLBACK_PREFIX}ZWID.NS", rec, ttl=100)
    for name in ("_get_earnings_dates", "_get_dividends", "_get_splits", "_get_insider_transactions", "_get_upgrades_downgrades",
                 "_get_institutional_holders", "_get_earnings_history"):
        monkeypatch.setattr(em, name, lambda *a, **k: None)
    monkeypatch.setattr(em, "_fetch_news_from_multiple_sources", lambda *a, **k: [])
    out = em._fetch_events("ZWID.NS", force=True)
    assert out.get("stale") is True and out["recent_news"] == [] and out["classified_events"] == []


# ── publisher search window ──────────────────────────────────────────────────

def test_publisher_query_defaults_to_three_days():
    assert em._publisher_search_query("POKARNA.NS", "Pokarna Quartz Limited").endswith("when:3d")


@pytest.mark.parametrize("raw,expected", [("7", "3"), ("30", "3"), ("2", "2"), ("1", "1"), ("0", "1"), ("junk", "3"), ("", "3")])
def test_publisher_search_days_never_exceed_the_window(monkeypatch, raw, expected):
    monkeypatch.setenv("EVENT_PUBLISHER_SEARCH_DAYS", raw)
    assert em._publisher_search_query("POKARNA.NS", "Pokarna Quartz Limited").endswith(f"when:{expected}d")


def test_publisher_search_follows_a_lower_news_window(monkeypatch):
    monkeypatch.setenv("NEWS_MAX_AGE_DAYS", "2")
    assert em._publisher_search_query("POKARNA.NS", "Pokarna Quartz Limited").endswith("when:2d")


# ── routes ───────────────────────────────────────────────────────────────────

def test_news_sources_route_reports_the_age_rule():
    na.filter_fresh_news([{"published": _ago(100)}], "published", NOW, "Google News")
    out = em.news_sources()
    assert out["age_rule"]["max_age_days"] == 3.0 and out["age_rule"]["sources"]["Google News"]["dropped_old"] == 1


# ── event score: nothing older than the window moves it ──────────────────────

_SCORE_NOW = datetime(2026, 9, 28, 10, 0, 0)


@pytest.mark.parametrize("events", [
    {"last_dividend": {"amount": 5.0, "date": "2026-09-20"}},
    {"recent_analyst_actions": [{"action": "upgrade", "date": "2026-09-10"}]},
    {"recent_insider_transactions": [{"transaction": "buy", "shares": 5000, "date": "2026-09-01"}]},
    {"earnings_surprise": {"surprise_pct": 12.0, "date": "2026-08-01"}},
    {"corporate_actions": [{"type": "buyback", "date": "2026-09-05"}]},
    {"regulatory_actions": [{"description": "SEBI probe", "date": "2026-09-10"}]},
    {"bulk_deals": [{"side": "buy", "published": "2026-09-15T10:00:00"}]},
])
def test_event_older_than_three_days_has_no_weight(events):
    out = ed.compute_event_score(events, _SCORE_NOW)
    assert out["event_score"] == 50.0 and out["event_score_breakdown"] == [] and out["event_score_ignored_old"]


@pytest.mark.parametrize("events,label", [
    ({"last_dividend": {"amount": 5.0, "date": "2026-09-26"}}, "dividend_announced"),
    ({"recent_analyst_actions": [{"action": "upgrade", "date": "2026-09-27"}]}, "analyst_upgrade"),
    ({"earnings_surprise": {"surprise_pct": 12.0, "date": "2026-09-25"}}, "earnings_strong_beat"),      # exactly 3 days: still inside
    ({"bulk_deals": [{"side": "buy", "published": "2026-09-27T10:00:00"}]}, "bulk_block_buy"),
])
def test_event_inside_the_window_still_counts(events, label):
    out = ed.compute_event_score(events, _SCORE_NOW)
    assert [b["type"] for b in out["event_score_breakdown"]] == [label] and out["event_score_ignored_old"] == []


def test_undated_snapshots_keep_their_weight():
    out = ed.compute_event_score({"fii_dii_net_flow": {"net": 100.0}}, _SCORE_NOW)
    assert [b["type"] for b in out["event_score_breakdown"]] == ["fii_dii_inflow"]


def test_upcoming_dates_are_not_old_news():
    """A result date a few days ahead is a calendar event, not old news (6 days out = pre-results momentum)."""
    out = ed.compute_event_score({"next_earnings_date": "2026-10-05"}, _SCORE_NOW)
    assert [b["type"] for b in out["event_score_breakdown"]] == ["pre_results_momentum"]
    assert out["event_score_ignored_old"] == []


def test_summary_does_not_describe_old_earnings_insider_or_bulk_items():
    text = ed.summarize_event_block({
        "earnings_surprise": {"surprise_pct": 9.0, "date": "2020-01-01"},
        "recent_insider_transactions": [{"transaction": "buy", "date": "2020-01-01"}],
        "bulk_deals": [{"side": "buy", "published": "2020-01-01T00:00:00"}],
    }, "ZWID")
    assert "earnings" not in text.lower().replace("no major results, bulk, or insider", "") and "No major results" in text


def test_enrich_events_carries_the_ignored_list():
    out = ed.enrich_events({"last_dividend": {"amount": 5.0, "date": "2020-01-01"}}, "ZWID")
    assert out["event_score"] == 50.0 and out["event_score_ignored_old"] == ["dividend_announced"]


def test_event_depth_still_works_without_the_helper_module(monkeypatch):
    """If news_age cannot be imported the rule still holds with the built-in 3 days."""
    import importlib.util
    monkeypatch.setitem(sys.modules, "news_age", None)
    spec = importlib.util.spec_from_file_location("event_depth_noimport", os.path.join(_SERVICE, "event", "event_depth.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._too_old(4) is True and mod._too_old(3) is False and mod._too_old(None) is False


# ── event feed (typed events) is capped at 72 h ──────────────────────────────

def test_event_feed_age_is_capped_at_72_hours():
    import event_feed as ef
    assert ef.FeedCollector(symbols_fn=lambda: [], max_age_h=500.0).max_age_h == 72.0
    assert ef.FeedCollector(symbols_fn=lambda: [], max_age_h=12.0).max_age_h == 12.0
    now = datetime.now(timezone.utc)
    evs = [{"symbol": "A", "type": "results", "polarity": 1, "published_ts": (now - timedelta(hours=100)).timestamp()},
           {"symbol": "B", "type": "results", "polarity": 1, "published_ts": (now - timedelta(hours=10)).timestamp()}]
    out = ef.build_response(evs, {}, None, now, max_age_h=500.0)
    assert [e["symbol"] for e in out["events"]] == ["B"]
