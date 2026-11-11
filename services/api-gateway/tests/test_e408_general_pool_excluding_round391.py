"""E408: the movers fallback's general pool is built with one exclusion set (was one set per symbol); result unchanged."""
from __future__ import annotations

import os

for _k in ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
    os.environ.pop(_k, None)

import main as gw


def test_pool_is_sorted_and_excludes_the_index_symbols():
    assert gw._general_pool_excluding(["ZEE", "ABB", "TCS", "ABB", "INFY"], ["TCS", "INFY"]) == ["ABB", "ABB", "ZEE"]


def test_empty_and_none_inputs():
    assert gw._general_pool_excluding([], ["TCS"]) == [] and gw._general_pool_excluding(None, None) == []
    assert gw._general_pool_excluding(["B", "A"], None) == ["A", "B"]


def test_matches_the_old_inline_expression():
    all_sec = [f"S{i:04d}" for i in range(0, 2000, 3)] + ["TCS"]
    idx = [f"S{i:04d}" for i in range(0, 600, 6)]
    assert gw._general_pool_excluding(all_sec, idx) == sorted(s for s in all_sec if s not in set(idx))


def test_the_fallback_uses_the_helper():
    import inspect
    src = inspect.getsource(gw._compute_momentum_movers)
    assert "_general_pool_excluding(all_sec, nifty_idx_full)" in src and "not in set(nifty_idx_full)" not in src
