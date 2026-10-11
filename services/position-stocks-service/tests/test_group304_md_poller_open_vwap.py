"""group304: the market-data poller passes the quote row's open and average price (exchange VWAP) through."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import feed.ws_client as wsc
from feed import md_poller as mp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for d in (wsc._tick_buffers, wsc._last_volume, wsc._last_quote, wsc._day_stats, wsc._exchange_vwap):
        d.clear()
    wsc._on_tick_callbacks.clear()
    mp.reset()
    monkeypatch.setattr(config, "POSITION_BULK_MAX_AGE_S", 30.0, raising=False)
    yield
    wsc._on_tick_callbacks.clear()
    mp.reset()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()


def _row(sym="SBIN", price=100.0, at=None, **kw):
    r = {"symbol": sym, "price": price, "fetched_at": _iso(at if at is not None else time.time())}
    r.update(kw)
    return r


def test_open_is_stored_in_day_stats():
    mp.ingest_rows([_row(open=99.5, day_high=101, day_low=99, previous_close=98)], time.time())
    assert wsc.get_day_stats("SBIN") == (99.5, 101.0, 99.0, 98.0)


def test_open_alone_is_enough_to_store_day_stats():
    mp.ingest_rows([_row(open=100.0)], time.time())
    assert wsc.get_day_stats("SBIN") == (100.0, None, None, None)


def test_zero_or_bad_open_is_none_not_zero():
    now = time.time()
    mp.ingest_rows([_row(open=0, day_high=101, day_low=99)], now)
    assert wsc.get_day_stats("SBIN")[0] is None
    mp.reset(); wsc._day_stats.clear()
    mp.ingest_rows([_row(open="x", day_high=101, day_low=99)], now)
    assert wsc.get_day_stats("SBIN")[0] is None


def test_avg_price_becomes_exchange_vwap():
    mp.ingest_rows([_row(avg_price=99.8)], time.time())
    assert wsc.get_exchange_vwap("SBIN") == 99.8


def test_implausible_vwap_is_ignored():
    now = time.time()
    mp.ingest_rows([_row(avg_price=300.0)], now)
    assert wsc.get_exchange_vwap("SBIN") is None
    mp.reset()
    mp.ingest_rows([_row(avg_price=40.0)], now)
    assert wsc.get_exchange_vwap("SBIN") is None


def test_missing_avg_price_leaves_vwap_unset():
    mp.ingest_rows([_row()], time.time())
    assert wsc.get_exchange_vwap("SBIN") is None


def test_implausible_open_rejects_whole_day_stats_like_before():
    mp.ingest_rows([_row(open=900.0, day_high=101, day_low=99)], time.time())
    assert wsc.get_day_stats("SBIN") is None


def test_opening_gate_sees_a_day_open_on_this_feed():
    mp.ingest_rows([_row(open=99.0, day_high=101, day_low=98.5, previous_close=98)], time.time())
    ds = wsc.get_day_stats("SBIN")
    assert ds is not None and ds[0] == 99.0 and ds[3] == 98.0
