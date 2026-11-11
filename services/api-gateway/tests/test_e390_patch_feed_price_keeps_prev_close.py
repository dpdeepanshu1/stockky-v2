"""E390: patch_feed_price (intraday quote refresh) must not overwrite the row's prev_close with the live price."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import data_feed as df  # noqa: E402


def _store():
    kv = {}
    return df.DataFeedStore(redis_get=lambda k: kv.get(k), redis_set=lambda k, v, ttl=None: kv.__setitem__(k, v)), kv


def test_e390_live_patch_keeps_prev_close():
    store, _kv = _store()
    store.put_symbol("TCS", {"symbol": "TCS", "price": 1000.0, "sector": "IT"})   # bulk feeder style: close -> prev_close
    row = store.get_symbol("TCS")
    assert row["prev_close"] == 1000.0
    orig = df.get_data_feed_store
    df.get_data_feed_store = lambda: store
    try:
        assert df.patch_feed_price("TCS", 1050.0) is True
        assert df.patch_feed_price("TCS", 1062.5) is True      # a second patch must not drift prev_close either
    finally:
        df.get_data_feed_store = orig
    row = store.get_symbol("TCS")
    assert row["prev_close"] == 1000.0                          # the close is untouched
    assert row["last_price"] == 1062.5 and row["current_price"] == 1062.5
    assert row["sector"] == "IT"
    assert df._payload_price(row) == 1062.5                     # readers still resolve the live price


def test_e390_patch_does_not_mutate_the_cached_row():
    store, _kv = _store()
    store.put_symbol("INFY", {"symbol": "INFY", "price": 1500.0})
    cached = store.get_symbol("INFY")
    snapshot = dict(cached)
    orig = df.get_data_feed_store
    df.get_data_feed_store = lambda: store
    try:
        class Spy:
            def get_symbol(self, s): return cached
            def put_symbol(self, s, p, **k): self.last = p
        spy = Spy()
        df.get_data_feed_store = lambda: spy
        assert df.patch_feed_price("INFY", 1510.0) is True
    finally:
        df.get_data_feed_store = orig
    assert cached == snapshot
    assert spy.last["last_price"] == 1510.0 and "price" not in spy.last and "close" not in spy.last
