"""E442 (round 423): Repair and Purge drop the memoised Health Audit (AUDIT_TTL_SEC), so the audit read that follows counts the new state."""
import asyncio
import os

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


@pytest.fixture
def memo(monkeypatch):
    d = {"feed_audit:500": (1.0, {"a": 1}), "feed_audit:7": (1.0, {"a": 2}), "surprise_audit": (1.0, {"s": 1})}
    monkeypatch.setattr(gw, "_AUDIT_MEMO", d)
    return d


def test_clear_drops_only_the_prefixed_keys(memo):
    assert gw._audit_cache_clear("feed_audit:") == 2
    assert list(memo) == ["surprise_audit"]
    assert gw._audit_cache_clear("nothing") == 0 and gw._audit_cache_clear() == 1 and memo == {}


def test_repair_clears_the_feed_audit_memo_after_a_normal_run(memo, monkeypatch):
    async def impl(symbol, client):
        return {"symbol": symbol, "complete": True}
    monkeypatch.setattr(gw, "_patch_single_stock_feed_impl", impl)
    assert asyncio.run(gw._patch_single_stock_feed("A", None)) == {"symbol": "A", "complete": True}
    assert list(memo) == ["surprise_audit"]


def test_repair_clears_the_memo_even_when_the_worker_raises(memo, monkeypatch):
    async def impl(symbol, client):
        raise RuntimeError("boom")
    monkeypatch.setattr(gw, "_patch_single_stock_feed_impl", impl)
    with pytest.raises(RuntimeError):
        asyncio.run(gw._patch_single_stock_feed("A", None))
    assert list(memo) == ["surprise_audit"]


def test_purge_over_cap_route_clears_the_memo_when_it_purged_something():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_support", "bound_routes_f.py"),
               encoding="utf-8").read()
    i = src.index("async def purge_over_cap_feed_symbols")
    body = src[i:i + 3500]
    assert 'if purged:' in body and 'ref._audit_cache_clear("feed_audit:")' in body
