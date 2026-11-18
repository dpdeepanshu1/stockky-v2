"""round 616: a fallback feed whose every item is old is passed over for the next fallback (VM 18 Nov: business.xml answered 200 with items 938 days old and,
being the first fallback of the frozen latestnews.xml, hid the Google News search behind it).
Same style as test_feed_fetch_fallback.py: the module is imported as `feed_fetch`, the parser and the download are replaced for each test."""
from __future__ import annotations

import os
import sys
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "news"))

import pytest

import feed_fetch as ff

PRIMARY = "https://www.moneycontrol.com/rss/latestnews.xml"
BUSINESS = "https://www.moneycontrol.com/rss/business.xml"
GOOGLE = "https://news.google.com/rss/search?q=site:moneycontrol.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en"


def _parse(raw):
    # body = '|' separated titles; "OLD:" = a 2020 date, "NEW:" = now
    entries = []
    for t in [t for t in raw.decode().split("|") if t]:
        e = {"title": t}
        if t.startswith("OLD:"):
            e["title"], e["published_parsed"] = t[4:], time.gmtime(1577836800)
        elif t.startswith("NEW:"):
            e["title"], e["published_parsed"] = t[4:], time.gmtime()
        entries.append(e)
    return types.SimpleNamespace(entries=entries)


@pytest.fixture()
def run(monkeypatch):
    def _run(answers, stale_days=3):
        calls = []

        def fake_download(u):
            calls.append(u)
            a = answers.get(u)
            return (a, {"status": 200, "error": None}) if a is not None else (None, {"error": "http_404"})
        monkeypatch.setattr(ff, "_download", fake_download)
        monkeypatch.setattr(ff.feedparser, "parse", _parse)
        monkeypatch.setattr(ff, "_FALLBACKS", {PRIMARY: ("Moneycontrol", [BUSINESS, GOOGLE])})
        monkeypatch.setattr(ff, "_FALLBACKS_ENABLED", True)
        monkeypatch.setattr(ff, "STALE_DAYS", stale_days)
        info = {"status": 200}
        parsed = ff._try_fallbacks(PRIMARY, info)
        return parsed, info, calls
    return _run


def test_a_stale_first_fallback_is_passed_over_for_a_fresh_second(run):
    parsed, info, calls = run({BUSINESS: b"OLD:old story", GOOGLE: b"NEW:fresh story"})
    assert calls == [BUSINESS, GOOGLE] and info["via"] == GOOGLE and parsed.entries[0]["title"] == "fresh story"


def test_a_fresh_first_fallback_still_wins_and_the_second_is_not_asked(run):
    parsed, info, calls = run({BUSINESS: b"NEW:fresh story", GOOGLE: b"NEW:other"})
    assert calls == [BUSINESS] and info["via"] == BUSINESS


def test_when_every_fallback_is_stale_the_first_stale_one_is_still_returned(run):
    parsed, info, calls = run({BUSINESS: b"OLD:old one", GOOGLE: b"OLD:old two"})
    assert calls == [BUSINESS, GOOGLE] and info["via"] == BUSINESS and parsed is not None     # as before round 616: the news window drops old items later


def test_an_undated_fallback_is_never_called_stale(run):
    parsed, info, calls = run({BUSINESS: b"no date here", GOOGLE: b"NEW:x"})
    assert calls == [BUSINESS] and info["via"] == BUSINESS


def test_stale_check_off_keeps_the_old_order(run):
    parsed, info, calls = run({BUSINESS: b"OLD:old", GOOGLE: b"NEW:fresh"}, stale_days=0)
    assert calls == [BUSINESS] and info["via"] == BUSINESS
