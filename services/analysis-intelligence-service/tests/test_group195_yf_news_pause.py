"""
group195: the Yahoo Finance news source is skipped for a while after it has answered nothing many times in a row.

2026-10-06: 'Yahoo Finance for <SYM>: 0 items' for 40+ consecutive symbols (ABB, BSE, CGPOWER ...), each still costing a
Yahoo request. Google News and the shared RSS feeds are untouched.

    cd services/analysis-intelligence-service
    python -m pytest tests/test_group195_yf_news_pause.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event"))

import pytest

import main as ev


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for k in ("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "EVENT_YF_NEWS_PAUSE_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(ev._YF_NEWS_STATE, "empty", 0)
    monkeypatch.setitem(ev._YF_NEWS_STATE, "pause_until", 0.0)


@pytest.fixture()
def feed(monkeypatch):
    calls = []
    answer = {"data": []}

    def fake(symbol):
        calls.append(symbol)
        return answer["data"]

    monkeypatch.setattr(ev, "_get_news", fake)
    return calls, answer


_ITEM = {"title": "Headline", "publisher": "P", "link": "https://x.test", "providerPublishTime": 1}


def test_cfg_defaults_and_bad_values(monkeypatch):
    assert ev._yf_news_cfg("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", 30) == 30
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "5")
    assert ev._yf_news_cfg("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", 30) == 5
    for bad in ("abc", "-3"):
        monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", bad)
        assert ev._yf_news_cfg("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", 30) == 30


def test_pauses_after_the_configured_number_of_empty_answers(feed, monkeypatch, caplog):
    calls, _ = feed
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "3")
    with caplog.at_level(logging.WARNING):
        for s in ("A", "B", "C"):
            assert ev._fetch_yf_news(s) == []
    assert calls == ["A", "B", "C"] and ev._yf_news_paused()
    assert "skipping it for 1800s" in caplog.text
    assert ev._fetch_yf_news("D") == [] and calls == ["A", "B", "C"]      # no request while paused


def test_an_item_resets_the_streak(feed, monkeypatch):
    calls, answer = feed
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "3")
    ev._fetch_yf_news("A"); ev._fetch_yf_news("B")
    answer["data"] = [_ITEM]
    out = ev._fetch_yf_news("C")
    assert out and out[0]["title"] == "Headline"
    answer["data"] = []
    ev._fetch_yf_news("D"); ev._fetch_yf_news("E")
    assert not ev._yf_news_paused()                                       # streak restarted at C


def test_probe_after_the_pause_repauses_on_one_more_empty_answer(feed, monkeypatch):
    calls, _ = feed
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "2")
    ev._fetch_yf_news("A"); ev._fetch_yf_news("B")
    assert ev._yf_news_paused()
    ev._YF_NEWS_STATE["pause_until"] = 0.0                                # pause over
    assert not ev._yf_news_paused()
    ev._fetch_yf_news("PROBE")
    assert calls[-1] == "PROBE" and ev._yf_news_paused()


def test_probe_that_finds_news_resumes_normal_use(feed, monkeypatch):
    calls, answer = feed
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "2")
    ev._fetch_yf_news("A"); ev._fetch_yf_news("B")
    ev._YF_NEWS_STATE["pause_until"] = 0.0
    answer["data"] = [_ITEM]
    ev._fetch_yf_news("PROBE")
    answer["data"] = []
    ev._fetch_yf_news("X")
    assert not ev._yf_news_paused()


def test_zero_disables_the_pause(feed, monkeypatch):
    calls, _ = feed
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "0")
    for i in range(60):
        ev._fetch_yf_news(f"S{i}")
    assert len(calls) == 60 and not ev._yf_news_paused()


def test_pause_length_is_configurable(feed, monkeypatch):
    monkeypatch.setenv("EVENT_YF_NEWS_EMPTY_PAUSE_AFTER", "1")
    monkeypatch.setenv("EVENT_YF_NEWS_PAUSE_SECONDS", "60")
    before = ev.time.time()
    ev._fetch_yf_news("A")
    assert 55 <= ev._YF_NEWS_STATE["pause_until"] - before <= 65
