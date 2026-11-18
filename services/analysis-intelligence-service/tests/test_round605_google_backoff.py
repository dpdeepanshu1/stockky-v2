"""Round 605: one back-off after a Google News 429, and a search that found nothing is not repeated for EVENT_GN_EMPTY_TTL_S.

Plain runner: `python tests/test_round605_google_backoff.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import main as em  # noqa: E402  (event/main.py)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, n):
        import time as _t
        return getattr(_t, n)


def _entry(title, hours_ago=2):
    t = (em._utcnow() - em.timedelta(hours=hours_ago)).timetuple()
    return types.SimpleNamespace(title=title, link="http://x/" + title, published_parsed=t, source=None)


def _reset(env=None):
    em._GN_EMPTY_CACHE.clear()
    em._GN_BACKOFF["until"] = 0.0
    for k in em._GN_STATS:
        em._GN_STATS[k] = 0
    for k in ("EVENT_GN_BACKOFF_S", "EVENT_GN_EMPTY_TTL_S"):
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    clock = _Clock()
    em.time = clock
    calls = []
    return clock, calls


def _fake_parse(calls, answers):
    def parse(url):
        calls.append(url)
        a = answers[min(len(calls) - 1, len(answers) - 1)]
        return a
    em.feedparser.parse = parse


def test_429_status_starts_a_backoff_and_later_searches_make_no_call():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False, status=429)])
    assert em._google_news_parse("A.NS", "Alpha", 8) == ([], False)
    assert len(calls) == 1 and em._GN_BACKOFF["until"] > clock.now
    assert em._google_news_parse("B.NS", "Beta", 8) == ([], False)        # other symbol, same window: no call
    assert len(calls) == 1 and em._GN_STATS["skipped_backoff"] == 1
    clock.now += 61                                                        # window over
    _fake_parse(calls, [types.SimpleNamespace(entries=[_entry("x")], bozo=False)])
    items, ok = em._google_news_parse("B.NS", "Beta", 8)
    assert ok and len(items) == 1 and len(calls) == 2


def test_429_in_an_exception_also_backs_off():
    clock, calls = _reset()

    def boom(url):
        calls.append(url)
        raise RuntimeError("HTTP Error 429: Too Many Requests")
    em.feedparser.parse = boom
    assert em._google_news_parse("A.NS", "Alpha", 8) == ([], False)
    assert em._GN_BACKOFF["until"] > clock.now
    em._google_news_parse("A.NS", "Alpha", 8)
    assert len(calls) == 1


def test_other_failures_do_not_back_off():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=True, status=503)])
    assert em._google_news_parse("A.NS", "Alpha", 8) == ([], False)
    assert em._GN_BACKOFF["until"] == 0.0
    em._google_news_parse("A.NS", "Alpha", 8)
    assert len(calls) == 2 and not em._GN_EMPTY_CACHE        # a failed search is never remembered


def test_backoff_can_be_switched_off():
    clock, calls = _reset({"EVENT_GN_BACKOFF_S": "0"})
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False, status=429)])
    em._google_news_parse("A.NS", "Alpha", 8)
    em._google_news_parse("B.NS", "Beta", 8)
    assert len(calls) == 2


def test_an_empty_answer_is_remembered_for_the_ttl_only():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False)])
    assert em._google_news_parse("CREATIVEYE.NS", "Creative Eye", 8) == ([], True)
    assert em._google_news_parse("CREATIVEYE.NS", "Creative Eye", 8) == ([], True)
    assert len(calls) == 1 and em._GN_STATS["skipped_empty_cached"] == 1
    em._google_news_parse("OTHER.NS", "Other Co", 8)                       # a different query is still asked
    assert len(calls) == 2
    clock.now += 1801
    em._google_news_parse("CREATIVEYE.NS", "Creative Eye", 8)
    assert len(calls) == 3


def test_stories_older_than_the_window_count_as_empty():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[_entry("old", hours_ago=24 * 30)], bozo=False)])
    assert em._google_news_parse("A.NS", "Alpha", 8) == ([], True)
    em._google_news_parse("A.NS", "Alpha", 8)
    assert len(calls) == 1


def test_a_search_with_stories_is_not_remembered():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[_entry("fresh")], bozo=False)])
    em._google_news_parse("A.NS", "Alpha", 8)
    em._google_news_parse("A.NS", "Alpha", 8)
    assert len(calls) == 2 and not em._GN_EMPTY_CACHE


def test_empty_memory_can_be_switched_off():
    clock, calls = _reset({"EVENT_GN_EMPTY_TTL_S": "0"})
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False)])
    em._google_news_parse("A.NS", "Alpha", 8)
    em._google_news_parse("A.NS", "Alpha", 8)
    assert len(calls) == 2


def test_widening_search_is_not_made_during_a_backoff_and_publisher_search_shares_it():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False, status=429)])
    saved = em._get_company_name
    em._get_company_name = lambda s: "Alpha Industries"                    # no yfinance lookup in a test
    try:
        assert em._fetch_google_news("A.NS", max_items=8) == []
        assert len(calls) == 1                                             # first search 429 -> ok False -> no second query
        assert em._fetch_publisher_search("A.NS", max_items=8) == []
        assert len(calls) == 1                                             # the publisher search waits for the same window
    finally:
        em._get_company_name = saved


def test_news_sources_route_reports_google_state():
    clock, calls = _reset()
    _fake_parse(calls, [types.SimpleNamespace(entries=[], bozo=False, status=429)])
    em._google_news_parse("A.NS", "Alpha", 8)
    g = em.news_sources()["google"]
    assert g["rate_limited"] == 1 and g["backoff_left_s"] > 0


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    orig_time, orig_parse = em.time, getattr(em.feedparser, "parse", None)
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            finally:
                em.time = orig_time
                if orig_parse is not None:
                    em.feedparser.parse = orig_parse
            n += 1
            print("ok", name)
    print(n, "passed")
