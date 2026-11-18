"""Round 604: the per-symbol news fetch does not ask the dead Moneycontrol / CNBC TV18 feeds (EVENT_SKIP_NEWS_SOURCES), and the event feed list
no longer carries the frozen Moneycontrol latestnews.xml entry.

Plain runner: `python tests/test_round604_skip_dead_news_sources.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import main as em  # noqa: E402  (event/main.py)
import event_feed as ef  # noqa: E402

NAMES = ["_fetch_yf_news", "_fetch_google_news", "_fetch_moneycontrol_news", "_fetch_economic_times",
         "_fetch_cnbc_tv18", "_fetch_publisher_search", "_fetch_market_feeds"]


def _run(env):
    """Run the multi-source fetch with every source stubbed; return the names of the sources that were asked."""
    os.environ.pop("EVENT_SKIP_NEWS_SOURCES", None)
    os.environ["EVENT_NEWS_PARALLEL"] = "0"
    if env is not None:
        os.environ["EVENT_SKIP_NEWS_SOURCES"] = env
    asked = []
    saved = {n: getattr(em, n) for n in NAMES}
    try:
        for n in NAMES:
            def fn(s, *a, _n=n, **k):
                asked.append(_n)
                return []
            setattr(em, n, fn)
        em._fetch_news_from_multiple_sources("A.NS")
    finally:
        for n, f in saved.items():
            setattr(em, n, f)
        os.environ.pop("EVENT_SKIP_NEWS_SOURCES", None)
        os.environ.pop("EVENT_NEWS_PARALLEL", None)
    return asked


def test_default_skips_the_two_dead_feeds():
    asked = _run(None)
    assert "_fetch_moneycontrol_news" not in asked and "_fetch_cnbc_tv18" not in asked, asked
    assert set(asked) == set(NAMES) - {"_fetch_moneycontrol_news", "_fetch_cnbc_tv18"}, asked


def test_none_or_blank_asks_every_source():
    for v in ("none", "", "off", "0"):
        assert set(_run(v)) == set(NAMES), v


def test_custom_list_is_case_insensitive_and_exact():
    asked = _run("economic times , yahoo finance")
    assert "_fetch_economic_times" not in asked and "_fetch_yf_news" not in asked
    assert "_fetch_moneycontrol_news" in asked and "_fetch_cnbc_tv18" in asked        # a custom list replaces the default list


def test_a_skipped_source_is_not_counted_as_asked():
    em._NEWS_SRC_STATS.clear()
    _run(None)
    assert "Moneycontrol" not in em._NEWS_SRC_STATS and "CNBC TV18" not in em._NEWS_SRC_STATS, em._NEWS_SRC_STATS
    assert "Google News" in em._NEWS_SRC_STATS


def test_event_feed_list_has_no_frozen_latestnews_entry():
    urls = [u for _, u in ef.FEEDS]
    names = [n for n, _ in ef.FEEDS]
    assert not any(u.endswith("/rss/latestnews.xml") for u in urls), urls
    assert "Moneycontrol" not in names and "Moneycontrol-business" not in names and "Moneycontrol-google" in names   # round 616: business.xml was STALE on the VM
    assert len(names) == len(set(names)) and all(u.startswith("https://") for u in urls)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
