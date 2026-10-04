"""
tests/test_peer_fundamentals_canonical_key.py -- fundamental/peer_multi_quarter.py

2026-10-04 (log-audit item 4): peer fundamentals were fetched twice per analysis,
as "INFY" and "INFY.NS". The in-process cache was keyed by whatever spelling the
caller used, and the market-data URL used that spelling too. Now the cache key and
the URL always use the canonical ".NS"/".BO" form, concurrent asks for the same
symbol make one call, and a batch containing both spellings fetches once.

httpx.get is monkeypatched. No network.
Run from services/analysis-intelligence-service:
    python -m pytest tests/test_peer_fundamentals_canonical_key.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest
import peer_multi_quarter as pmq


@pytest.fixture(autouse=True)
def _clean_cache():
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()
        pmq._FUND_INFLIGHT.clear()
    yield
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()
        pmq._FUND_INFLIGHT.clear()


class _R:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _patch_get(monkeypatch, calls, delay=0.0):
    import httpx

    def _get(url, timeout=None, **k):
        calls.append(url)
        if delay:
            time.sleep(delay)
        return _R({"pe": 20})

    monkeypatch.setattr(httpx, "get", _get)


def test_bare_and_dot_ns_share_one_cache_entry(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls)
    a = pmq.fetch_fundamentals("http://mds", "INFY")
    b = pmq.fetch_fundamentals("http://mds", "INFY.NS")
    assert a == b == {"pe": 20}
    assert calls == ["http://mds/fundamentals/INFY.NS"]      # one call, canonical URL


def test_bare_symbol_is_requested_in_canonical_form(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls)
    pmq.fetch_fundamentals("http://mds/", "tcs")
    assert calls == ["http://mds/fundamentals/TCS.NS"]


def test_bo_suffix_is_kept_and_not_merged_with_ns(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls)
    pmq.fetch_fundamentals("http://mds", "ABC.BO")
    pmq.fetch_fundamentals("http://mds", "ABC.NS")
    assert calls == ["http://mds/fundamentals/ABC.BO", "http://mds/fundamentals/ABC.NS"]


def test_cache_get_set_accept_either_spelling():
    pmq._fund_cache_set("INFY", {"pe": 1})
    assert pmq._fund_cache_get("INFY.NS") == {"pe": 1}
    assert pmq._fund_cache_get("infy") == {"pe": 1}
    assert "INFY.NS" in pmq._FUND_CACHE and "INFY" not in pmq._FUND_CACHE


def test_concurrent_same_symbol_makes_one_call(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls, delay=0.3)
    out = []

    def _w(sym):
        out.append(pmq.fetch_fundamentals("http://mds", sym))

    ts = [threading.Thread(target=_w, args=(s,)) for s in ("INFY", "INFY.NS", "infy", "INFY.NS")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1
    assert out == [{"pe": 20}] * 4


def test_failed_fetch_is_not_cached_and_does_not_block_retry(monkeypatch):
    import httpx
    n = {"c": 0}

    def _get(url, timeout=None, **k):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("boom")
        return _R({"pe": 5})

    monkeypatch.setattr(httpx, "get", _get)
    assert pmq.fetch_fundamentals("http://mds", "XYZ") == {}
    assert pmq.fetch_fundamentals("http://mds", "XYZ") == {"pe": 5}


def test_batch_with_both_spellings_fetches_once_and_returns_both_keys(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls)
    out = pmq.fetch_fundamentals_batch("http://mds", ["INFY", "INFY.NS", "TCS.NS"])
    assert set(out) == {"INFY", "INFY.NS", "TCS.NS"}
    assert out["INFY"] == out["INFY.NS"] == {"pe": 20}
    assert sorted(calls) == ["http://mds/fundamentals/INFY.NS", "http://mds/fundamentals/TCS.NS"]


def test_batch_fully_cached_with_other_spelling_makes_no_call(monkeypatch):
    calls = []
    _patch_get(monkeypatch, calls)
    pmq._fund_cache_set("INFY.NS", {"pe": 9})
    out = pmq.fetch_fundamentals_batch("http://mds", ["INFY", "INFY.NS"])
    assert out == {"INFY": {"pe": 9}, "INFY.NS": {"pe": 9}}
    assert calls == []


def test_batch_duplicate_of_failed_fetch_maps_to_empty(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = pmq.fetch_fundamentals_batch("http://mds", ["INFY", "INFY.NS"])
    assert out == {"INFY": {}, "INFY.NS": {}}
