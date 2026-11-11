"""E462: a warm-cache hit does not push an older cached quote over a newer stored price (patch_feed_price in between)."""
from __future__ import annotations

import time
from datetime import datetime

import pytest

import data_feed as df
from test_data_feed import feed, kv, store  # noqa: F401


def _iso(epoch):
    return datetime.fromtimestamp(epoch, df.IST).isoformat()


def _cache(feed, qt):
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {
        "_meta": {"timestamp": time.time(), "quote_ts": {"A": qt}, "full_run_ts": time.time()},
        "quotes": {"A": {"symbol": "A", "price": 105.5, "cmp": 105.5, "previous_close": 99.0}},
    }


def test_iso_epoch_reads_aware_naive_and_z():
    assert df._iso_epoch("2026-11-11T10:00:00+05:30") == df._iso_epoch("2026-11-11T04:30:00Z")
    assert df._iso_epoch("2026-11-11T10:00:00") == df._iso_epoch("2026-11-11T10:00:00+05:30")


@pytest.mark.parametrize("bad", [None, "", "  ", "x", 5, [], "2026-13-45T00:00:00"])
def test_iso_epoch_unreadable_is_none(bad):
    assert df._iso_epoch(bad) is None


def test_newer_row_price_detection():
    now = time.time()
    row = {"last_price": 101.0, "price_refreshed_at": _iso(now)}
    assert df._row_price_is_newer_than_quote(row, now - 60) is True
    assert df._row_price_is_newer_than_quote(row, now - 0.5) is False            # inside the one-second tolerance
    assert df._row_price_is_newer_than_quote(row, now + 60) is False             # the quote is the newer one
    assert df._row_price_is_newer_than_quote(row, None) is False                 # unknown quote time: push, as before
    assert df._row_price_is_newer_than_quote({"price_refreshed_at": _iso(now)}, now - 60) is False   # no usable stored price
    assert df._row_price_is_newer_than_quote({"last_price": 1.0}, now - 60) is False                 # no stamp on the row
    assert df._row_price_is_newer_than_quote(None, now - 60) is False


def test_older_cached_quote_does_not_overwrite_a_newer_patched_price(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    now = time.time()
    feed.store.put_symbol("A", {"symbol": "A", "close": 100.0, "last_price": 101.0, "price_refreshed_at": _iso(now)})
    _cache(feed, now - 90)
    r = df.run_bulk_yahoo_price_feed_cached(["A"])
    assert r["source"] == "cache" and r["symbols"] == ["A"] and r["kept_newer_feed_price"] == 1
    after = feed.store.get_symbol("A")
    assert after["last_price"] == 101.0 and after["prev_close"] == 100.0


def test_newer_cached_quote_still_refreshes_an_older_row_and_stamps_its_own_time(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    now = time.time()
    feed.store.put_symbol("A", {"symbol": "A", "close": 100.0, "last_price": 101.0, "price_refreshed_at": _iso(now - 300)})
    _cache(feed, now - 20)
    r = df.run_bulk_yahoo_price_feed_cached(["A"])
    assert r["kept_newer_feed_price"] == 0
    after = feed.store.get_symbol("A")
    assert after["last_price"] == 105.5 and after["prev_close"] == 100.0
    stamp = df._iso_epoch(after["price_refreshed_at"])
    assert abs(stamp - (now - 20)) < 3                       # the quote's time, not "now"


def test_row_without_a_stamp_is_refreshed_as_before(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    feed.store.put_symbol("A", {"symbol": "A", "close": 100.0, "last_price": 101.0})
    _cache(feed, time.time() - 10)
    df.run_bulk_yahoo_price_feed_cached(["A"])
    assert feed.store.get_symbol("A")["last_price"] == 105.5


def test_pre_e461_cache_without_quote_times_uses_the_map_timestamp(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    now = time.time()
    feed.store.put_symbol("A", {"symbol": "A", "close": 100.0, "last_price": 101.0, "price_refreshed_at": _iso(now)})
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": now - 60}, "quotes": {"A": {"symbol": "A", "price": 105.5}}}
    r = df.run_bulk_yahoo_price_feed_cached(["A"])
    assert r["kept_newer_feed_price"] == 1 and feed.store.get_symbol("A")["last_price"] == 101.0


def test_a_failing_row_read_is_swallowed_and_the_symbol_is_still_served(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    _cache(feed, time.time() - 10)
    monkeypatch.setattr(feed.store, "get_symbol", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert df.run_bulk_yahoo_price_feed_cached(["A"])["symbols"] == ["A"]
