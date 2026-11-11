"""E458 (round 437): a warm bulk-cache hit pushes the cached quote into the feed WITHOUT turning its price into `prev_close`."""
from __future__ import annotations

import time

import data_feed as df
from test_data_feed import feed, kv, store  # noqa: F401


def test_cached_quote_row_drops_the_close_aliases_and_keeps_the_rest():
    q = {"symbol": "A", "price": 105.5, "cmp": 105.5, "close": 105.5, "previous_close": 100, "day_change_pct": 5.5,
         "day_high": 106, "day_low": 99, "volume": 10, "source": "yahoo_bulk"}
    row = df._cached_quote_feed_row(q)
    assert not ({"price", "cmp", "close", "prev_close"} & set(row))
    assert row["last_price"] == 105.5 and row["current_price"] == 105.5
    assert row["previous_close"] == 100 and row["day_high"] == 106 and row["volume"] == 10 and row["source"] == "yahoo_bulk"


def test_cached_quote_row_without_a_usable_price_sends_no_price_keys():
    assert df._cached_quote_feed_row({"price": 0, "volume": 3}) == {"volume": 3}
    assert df._cached_quote_feed_row({"price": float("inf")}) == {}
    assert df._cached_quote_feed_row(None) == {}


def test_the_input_quote_is_not_mutated():
    q = {"symbol": "A", "price": 5}
    df._cached_quote_feed_row(q)
    assert q == {"symbol": "A", "price": 5}


def test_warm_cache_hit_keeps_the_stored_close_and_refreshes_last_price(feed, monkeypatch):
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
    feed.store.put_symbol("A", {"symbol": "A", "close": 100.0, "last_price": 101.0, "pe_ratio": 20.0})
    before = feed.store.get_symbol("A")
    assert before["prev_close"] == 100.0
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {
        "_meta": {"timestamp": time.time()},
        "quotes": {"A": {"symbol": "A", "price": 105.5, "cmp": 105.5, "previous_close": 99.0}},
    }
    r = df.run_bulk_yahoo_price_feed_cached(["A"])
    assert r["source"] == "cache" and r["symbols"] == ["A"]
    after = feed.store.get_symbol("A")
    assert after["prev_close"] == 100.0                      # the stored close is not replaced by the cached price
    assert after["last_price"] == 105.5 and after["current_price"] == 105.5
    assert after["pe_ratio"] == 20.0                         # fundamentals still protected by the merge
