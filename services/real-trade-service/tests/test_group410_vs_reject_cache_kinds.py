"""group410: volume-shock delivery and absolute quality-gate rejections are remembered (16 Oct log: the same 3 symbols were re-fetched every cycle)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from candidate_engine import candidates as cd


def test_delivery_and_quality_kinds_have_a_ttl_and_are_cached():
    cd.clear_reject_cache()
    assert cd._reject_ttl_for("vs_delivery") == cd.REJECT_CACHE_VS_S > 0
    assert cd._reject_ttl_for("vs_quality") == cd.REJECT_CACHE_PRICE_S > 0
    for kind in ("vs_delivery", "vs_quality"):
        cd._reject_cache_put("vs:X", {"reject_reason": "r", "reject_kind": kind})
        got = cd._reject_cache_get("vs:X")
        assert got and got["from_reject_cache"] is True and got["reject_kind"] == kind
        cd.clear_reject_cache()


def test_a_reject_without_a_kind_is_still_not_cached():
    cd.clear_reject_cache()
    cd._reject_cache_put("vs:Y", {"reject_reason": "No live quote available"})
    assert cd._reject_cache_get("vs:Y") is None
