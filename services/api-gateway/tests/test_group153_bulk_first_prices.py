"""
tests/test_group153_bulk_first_prices.py

group153 (2026-10-05, log-list item 4, market-data overload): `_fetch_prices_bulk_async`
sent one GET /quote/{sym} per symbol, so a scan / hot-picks chunk of N symbols became N
market-data /quote waterfalls (and overlapping callers asked for the same symbols again).

Now: the list is de-duplicated, lists of GATEWAY_BULK_QUOTE_MIN (15) or more are priced with
chunked POST /quotes/bulk first (stale rows ignored, results cached ~8 s), and only the
symbols bulk could not price use the old per-symbol path.

No network: the async client is a recording fake.

Run from services/api-gateway:
    python3 -m pytest tests/test_group153_bulk_first_prices.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def run(coro):
    return asyncio.run(coro)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _now_iso(age_s=0.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()


class FakeClient:
    """GET /quote/{sym} -> single[sym]; POST /quotes/bulk -> bulk(symbols) (or `bulk_resp`)."""

    def __init__(self, single=None, bulk_prices=None, bulk_age=0.0):
        self.gets, self.posts = [], []
        self.single = single or {}
        self.bulk_prices = bulk_prices if bulk_prices is not None else {}
        self.bulk_age = bulk_age
        self.bulk_resp = None

    async def get(self, url, timeout=None, **k):
        sym = url.rsplit("/", 1)[1]
        self.gets.append(sym)
        px = self.single.get(sym)
        return Resp(200, {"price": px}) if px else Resp(404, {})

    async def post(self, url, json=None, timeout=None, **k):
        self.posts.append((url, list(json["symbols"]), timeout))
        if self.bulk_resp is not None:
            if isinstance(self.bulk_resp, Exception):
                raise self.bulk_resp
            return self.bulk_resp
        rows = [{"symbol": s, "price": self.bulk_prices[s], "fetched_at": _now_iso(self.bulk_age)}
                for s in json["symbols"] if s in self.bulk_prices]
        return Resp(200, {"quotes": rows})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gw._GW_BULK_PX_CACHE.clear()
    for k in ("GATEWAY_BULK_QUOTE_MIN", "GATEWAY_BULK_QUOTE_CACHE_S", "GATEWAY_BULK_QUOTE_MAX_AGE_S",
              "GATEWAY_BULK_QUOTE_CHUNK", "GATEWAY_BULK_QUOTE_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    yield
    gw._GW_BULK_PX_CACHE.clear()


def _syms(n):
    return [f"S{i}" for i in range(n)]


def test_duplicates_are_requested_once_on_the_per_symbol_path():
    c = FakeClient(single={"A": 1.0, "B": 2.0})
    out = run(gw._fetch_prices_bulk_async(["A", "A.NS", "a", "B"], c))
    assert out == {"A": 1.0, "B": 2.0}
    assert sorted(c.gets) == ["A", "B"] and c.posts == []      # small list: no bulk, no duplicate GET


def test_large_list_is_priced_with_one_bulk_post_per_chunk_and_no_single_quotes():
    syms = _syms(120)
    c = FakeClient(bulk_prices={s: 10.0 + i for i, s in enumerate(syms)})
    out = run(gw._fetch_prices_bulk_async(syms, c))
    assert len(out) == 120 and out["S5"] == 15.0
    assert [len(p[1]) for p in c.posts] == [50, 50, 20]        # 3 requests instead of 120
    assert c.posts[0][0].endswith("/quotes/bulk")
    assert c.gets == []


def test_symbols_bulk_cannot_price_fall_back_to_the_per_symbol_path_only():
    syms = _syms(20)
    c = FakeClient(single={"S3": 3.0, "S7": 7.0}, bulk_prices={s: 1.0 for s in syms if s not in ("S3", "S7", "S9")})
    out = run(gw._fetch_prices_bulk_async(syms, c))
    assert out["S3"] == 3.0 and out["S7"] == 7.0 and out["S0"] == 1.0
    assert "S9" not in out                                     # nobody could price it
    assert sorted(c.gets) == ["S3", "S7", "S9"]                # only the 3 misses went per-symbol


def test_stale_or_undated_bulk_rows_are_ignored():
    syms = _syms(16)
    c = FakeClient(single={s: 5.0 for s in syms}, bulk_prices={s: 1.0 for s in syms}, bulk_age=60.0)
    out = run(gw._fetch_prices_bulk_async(syms, c))
    assert set(out.values()) == {5.0}                          # the 60 s old bulk prices were not used
    assert len(c.gets) == 16
    gw._GW_BULK_PX_CACHE.clear()
    c2 = FakeClient(single={s: 5.0 for s in syms})
    c2.bulk_resp = Resp(200, {"quotes": [{"symbol": s, "price": 1.0} for s in syms]})   # no fetched_at
    assert set(run(gw._fetch_prices_bulk_async(syms, c2)).values()) == {5.0}
    c3 = FakeClient(single={s: 5.0 for s in syms})
    c3.bulk_resp = Resp(200, {"quotes": [{"symbol": s, "price": 1.0, "fetched_at": "garbage"} for s in syms]})
    assert set(run(gw._fetch_prices_bulk_async(syms, c3)).values()) == {5.0}


def test_bulk_failure_degrades_to_the_old_per_symbol_path():
    syms = _syms(18)
    for bad in (Resp(500, {}), Resp(200, ["not", "a", "dict"]), RuntimeError("down")):
        gw._GW_BULK_PX_CACHE.clear()
        c = FakeClient(single={s: 2.0 for s in syms})
        c.bulk_resp = bad
        out = run(gw._fetch_prices_bulk_async(syms, c))
        assert len(out) == 18 and len(c.gets) == 18


def test_rows_for_symbols_that_were_not_asked_for_are_ignored():
    syms = _syms(15)
    c = FakeClient()
    c.bulk_resp = Resp(200, {"quotes": [
        {"symbol": "OTHER", "price": 9.0, "fetched_at": _now_iso()},
        {"symbol": "S1.NS", "price": 4.0, "fetched_at": _now_iso()},
        "junk",
    ]})
    out = run(gw._fetch_prices_bulk_async(syms, c))
    assert out == {"S1": 4.0}                                   # .NS suffix normalised, OTHER dropped


def test_overlapping_callers_reuse_bulk_prices_within_the_cache_window():
    syms = _syms(30)
    c = FakeClient(bulk_prices={s: 3.0 for s in syms})
    run(gw._fetch_prices_bulk_async(syms, c))
    assert len(c.posts) == 1
    out = run(gw._fetch_prices_bulk_async(syms, c))             # second caller, moments later
    assert len(c.posts) == 1 and len(out) == 30                 # served from the short cache


def test_cache_expires_and_off_switch_restores_per_symbol(monkeypatch):
    syms = _syms(20)
    monkeypatch.setenv("GATEWAY_BULK_QUOTE_CACHE_S", "0")        # caching disabled
    c = FakeClient(bulk_prices={s: 3.0 for s in syms})
    run(gw._fetch_prices_bulk_async(syms, c))
    run(gw._fetch_prices_bulk_async(syms, c))
    assert len(c.posts) == 2
    monkeypatch.setenv("GATEWAY_BULK_QUOTE_MIN", "0")            # bulk path off
    gw._GW_BULK_PX_CACHE.clear()
    c2 = FakeClient(single={s: 8.0 for s in syms})
    out = run(gw._fetch_prices_bulk_async(syms, c2))
    assert c2.posts == [] and len(c2.gets) == 20 and set(out.values()) == {8.0}


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("GATEWAY_BULK_QUOTE_MIN", "oops")
    syms = _syms(16)
    c = FakeClient(bulk_prices={s: 1.0 for s in syms})
    assert len(run(gw._fetch_prices_bulk_async(syms, c))) == 16
    assert len(c.posts) == 1                                    # default threshold 15 applied


def test_cache_is_bounded_expired_entries_are_dropped_past_5000():
    import time as _t
    old = _t.monotonic() - 600
    for i in range(5001):
        gw._GW_BULK_PX_CACHE[f"OLD{i}"] = (old, 1.0)
    syms = _syms(15)
    c = FakeClient(bulk_prices={s: 2.0 for s in syms})
    run(gw._fetch_prices_bulk_async(syms, c))
    assert len(gw._GW_BULK_PX_CACHE) == 15                      # the 5001 expired rows were purged
    assert all(k.startswith("S") for k in gw._GW_BULK_PX_CACHE)


def test_unexpected_bulk_helper_error_still_prices_via_per_symbol(monkeypatch):
    async def boom(bases, client):
        raise RuntimeError("helper bug")
    monkeypatch.setattr(gw, "_fetch_prices_bulk_first", boom)
    syms = _syms(16)
    c = FakeClient(single={s: 6.0 for s in syms})
    out = run(gw._fetch_prices_bulk_async(syms, c))
    assert len(out) == 16 and len(c.gets) == 16
