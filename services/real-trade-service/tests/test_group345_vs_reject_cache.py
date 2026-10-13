"""group345: volume-shock rejects carry a kind and are remembered; the price floor is judged on the quote, before any history call."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from candidate_engine import candidates as ce


def test_price_floor_is_judged_before_history(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("history must not be fetched for a sub-floor price")
    monkeypatch.setattr(ce, "_fetch_history", boom)
    res = asyncio.run(ce._volume_shock_analysis(None, "HATHWAY", {"price": 10.6}))
    assert res["reject_kind"] == "price_floor" and "minimum" in res["reject_reason"]


def test_vs_rejects_are_cached_with_their_own_ttl():
    ce.clear_reject_cache()
    r = {"reject_reason": "x", "atr_pct": None, "reject_kind": "vs_return"}
    ce._reject_cache_put("vs:AAA", r)
    assert ce._reject_cache_get("vs:AAA")["from_reject_cache"] is True
    assert ce._reject_cache_get("AAA") is None            # the standard track's key is separate
    assert ce._reject_ttl_for("vs_volume") == ce.REJECT_CACHE_VS_S
    ce.clear_reject_cache()
