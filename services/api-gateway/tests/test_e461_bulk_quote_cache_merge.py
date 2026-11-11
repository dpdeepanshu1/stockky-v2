"""E461: set_bulk_quote_cache merges with per-quote timestamps; a partial run no longer drops the rest or makes the map look fully warm."""
from __future__ import annotations

import time

import data_feed as df
from test_data_feed import feed, kv, store  # noqa: F401


def _open(monkeypatch, open_now=True):
    monkeypatch.setattr(df, "_is_nse_session_open", lambda: open_now)


def test_partial_write_keeps_the_other_quotes(kv, monkeypatch):
    _open(monkeypatch)
    df.set_bulk_quote_cache({"A": {"price": 1}, "B": {"price": 2}})
    out = df.set_bulk_quote_cache({"C": {"price": 3}}, complete=False)
    assert set(out["quotes"]) == {"A", "B", "C"} and out["_meta"]["count"] == 3 and out["_meta"]["written"] == 1
    assert set(kv.store[df.BULK_QUOTE_CACHE_KEY]["quotes"]) == {"A", "B", "C"}


def test_new_quote_wins_and_gets_its_own_timestamp(kv, monkeypatch):
    _open(monkeypatch)
    first = df.set_bulk_quote_cache({"A": {"price": 1}, "B": {"price": 2}})
    t_b = first["_meta"]["quote_ts"]["B"]
    time.sleep(0.01)
    out = df.set_bulk_quote_cache({"A": {"price": 9}}, complete=False)
    assert out["quotes"]["A"] == {"price": 9} and out["_meta"]["quote_ts"]["A"] > t_b
    assert out["_meta"]["quote_ts"]["B"] == t_b


def test_full_run_ts_moves_only_on_a_complete_write(kv, monkeypatch):
    _open(monkeypatch)
    first = df.set_bulk_quote_cache({"A": {"price": 1}})
    full = first["_meta"]["full_run_ts"]
    time.sleep(0.01)
    part = df.set_bulk_quote_cache({"B": {"price": 2}}, complete=False)
    assert part["_meta"]["full_run_ts"] == full and part["_meta"]["timestamp"] > full
    time.sleep(0.01)
    whole = df.set_bulk_quote_cache({"A": {"price": 1}, "B": {"price": 2}})
    assert whole["_meta"]["full_run_ts"] > full


def test_quotes_older_than_the_ttl_are_dropped_on_merge(kv, monkeypatch):
    _open(monkeypatch)
    old = time.time() - df.BULK_QUOTE_OPEN_TTL - 5
    kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": time.time(), "quote_ts": {"OLD": old, "NEW": time.time()}, "full_run_ts": old},
                                         "quotes": {"OLD": {"price": 1}, "NEW": {"price": 2}}}
    out = df.set_bulk_quote_cache({"C": {"price": 3}}, complete=False)
    assert set(out["quotes"]) == {"NEW", "C"}


def test_pre_e461_map_counts_as_written_at_its_timestamp(kv, monkeypatch):
    _open(monkeypatch)
    kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": time.time() - 10}, "quotes": {"A": {"price": 1}}}
    out = df.set_bulk_quote_cache({"B": {"price": 2}}, complete=False)
    assert set(out["quotes"]) == {"A", "B"} and out["_meta"]["quote_ts"]["A"] < time.time() - 5
    assert out["_meta"]["full_run_ts"] is not None                      # a pre-E461 map was a whole-universe write


def test_pre_e461_map_past_the_ttl_is_dropped(kv, monkeypatch):
    _open(monkeypatch)
    kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": time.time() - 9999}, "quotes": {"A": {"price": 1}}}
    assert set(df.set_bulk_quote_cache({"B": {"price": 2}}, complete=False)["quotes"]) == {"B"}


def test_merge_false_is_the_old_replace(kv, monkeypatch):
    _open(monkeypatch)
    df.set_bulk_quote_cache({"A": {"price": 1}})
    assert set(df.set_bulk_quote_cache({"B": {"price": 2}}, merge=False)["quotes"]) == {"B"}


def test_merge_with_nothing_new_writes_nothing(kv, monkeypatch):
    _open(monkeypatch)
    first = df.set_bulk_quote_cache({"A": {"price": 1}})
    n = len(kv.sets)
    out = df.set_bulk_quote_cache({}, complete=False)
    assert len(kv.sets) == n and set(out["quotes"]) == {"A"} and out["_meta"]["timestamp"] == first["_meta"]["timestamp"]


def test_junk_timestamps_never_raise(kv, monkeypatch):
    _open(monkeypatch)
    kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": "x", "quote_ts": {"A": float("nan"), "B": None}}, "quotes": {"A": {"price": 1}, "B": {"price": 2}}}
    assert df.set_bulk_quote_cache({"C": {"price": 3}}, complete=False)["quotes"] == {"C": {"price": 3}}


def test_age_helpers():
    now = time.time()
    c = {"_meta": {"timestamp": now - 50, "quote_ts": {"A": now - 5}, "full_run_ts": now - 40}}
    assert 4 < df.bulk_quote_age_sec("a.ns", c) < 8
    assert 48 < df.bulk_quote_age_sec("Z", c) < 53               # no own time: the map's
    assert 38 < df.bulk_full_run_age_sec(c) < 42
    assert df.bulk_quote_age_sec("A", {}) is None and df.bulk_full_run_age_sec({}) is None
    assert 48 < df.bulk_full_run_age_sec({"_meta": {"timestamp": now - 50}}) < 53    # pre-E461: the timestamp


def test_warm_hit_skips_a_requested_quote_that_is_past_the_ttl(feed, monkeypatch):
    _open(monkeypatch)
    now = time.time()
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": now, "quote_ts": {"A": now, "B": now - df.BULK_QUOTE_OPEN_TTL - 30}, "full_run_ts": now},
                                              "quotes": {"A": {"price": 5}, "B": {"price": 6}}}
    r = df.run_bulk_yahoo_price_feed_cached(["A", "B"])
    assert r["source"] == "cache" and r["symbols"] == ["A"]


def test_partial_writes_do_not_make_a_whole_universe_request_a_hit(feed, monkeypatch):
    _open(monkeypatch)
    now = time.time()
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": now, "quote_ts": {"A": now}, "full_run_ts": now - df.BULK_QUOTE_OPEN_TTL - 30},
                                              "quotes": {"A": {"price": 5}}}
    feed.bhav = {}
    monkeypatch.setattr(df, "run_bulk_yahoo_price_feed", lambda *a, **k: {"status": "success", "symbols": [], "source": "live-probe"})
    assert df.run_bulk_yahoo_price_feed_cached()["source"] == "live-probe"


def test_whole_universe_request_hits_when_the_full_write_is_fresh(feed, monkeypatch):
    _open(monkeypatch)
    now = time.time()
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": now, "quote_ts": {"A": now, "B": now - df.BULK_QUOTE_OPEN_TTL - 30}, "full_run_ts": now},
                                              "quotes": {"A": {"price": 5}, "B": {"price": 6}}}
    r = df.run_bulk_yahoo_price_feed_cached()
    assert r["source"] == "cache" and r["symbols"] == ["A"]


def test_live_partial_run_merges_into_the_cache_and_keeps_full_run_ts(feed, monkeypatch):
    _open(monkeypatch)
    now = time.time()
    feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": now, "quote_ts": {"X": now}, "full_run_ts": now}, "quotes": {"X": {"price": 9}}}
    feed.bhav = {}
    monkeypatch.setattr(df, "run_bulk_yahoo_price_feed", lambda *a, **k: {"status": "success", "symbols": ["A"], "source": "given"})
    feed.store.put_symbol("A", {"price": 10.0, "last_price": 10.0})
    df.run_bulk_yahoo_price_feed_cached(["A"], force=True)
    cache = feed.kv.store[df.BULK_QUOTE_CACHE_KEY]
    assert set(cache["quotes"]) == {"X", "A"} and cache["_meta"]["full_run_ts"] == now
