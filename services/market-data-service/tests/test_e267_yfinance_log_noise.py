"""E267: yfinance's live-socket drop is logged once a minute as one short WARNING, not a traceback every few seconds.

Run from services/market-data-service:  python3 -m pytest tests/test_e267_yfinance_log_noise.py -q
"""
from __future__ import annotations

import logging

import pytest

import yahoo_ws_feed as feed


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def cap(monkeypatch):
    lg = logging.getLogger("yfinance")
    lg.filters[:] = [f for f in lg.filters if not isinstance(f, feed._YfLiveNoiseFilter)]
    flt = feed._YfLiveNoiseFilter()
    lg.addFilter(flt)
    h = _Cap()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    clock = {"t": 1000.0}
    monkeypatch.setattr(feed.time, "monotonic", lambda: clock["t"])
    yield h, clock
    lg.removeHandler(h)
    lg.removeFilter(flt)
    lg.setLevel(old)


def _drop(lg):
    try:
        raise ConnectionError("no close frame received or sent")
    except ConnectionError:
        lg.error("Error while listening to messages: no close frame received or sent", exc_info=True)
    lg.info("Attempting to reconnect...")


def test_a_burst_of_drops_is_one_short_warning_without_a_traceback(cap):
    h, clock = cap
    lg = logging.getLogger("yfinance")
    for _ in range(5):
        _drop(lg)
        clock["t"] += 2.0
    listen = [r for r in h.records if "dropped" in r.getMessage()]
    assert len(listen) == 1
    r = listen[0]
    assert r.levelno == logging.WARNING and r.exc_info is None and "reconnects by itself" in r.getMessage()
    assert len([x for x in h.records if x.getMessage().startswith("Attempting to reconnect")]) == 1


def test_the_next_window_says_how_many_were_hidden(cap):
    h, clock = cap
    lg = logging.getLogger("yfinance")
    for _ in range(4):
        _drop(lg)
        clock["t"] += 5.0
    clock["t"] += 100.0
    _drop(lg)
    msgs = [r.getMessage() for r in h.records if "dropped" in r.getMessage()]
    assert len(msgs) == 2 and "3 more in the last minute not shown" in msgs[1]


def test_other_yfinance_messages_are_untouched(cap):
    h, _ = cap
    lg = logging.getLogger("yfinance")
    for _ in range(3):
        lg.warning("Subscribed to symbols: ['A.NS']")
    lg.error("something else broke", exc_info=False)
    assert len(h.records) == 4 and h.records[-1].levelno == logging.ERROR


def test_the_filter_is_installed_once_at_import():
    feed._install_log_filter()
    feed._install_log_filter()
    assert sum(isinstance(f, feed._YfLiveNoiseFilter) for f in logging.getLogger("yfinance").filters) == 1
