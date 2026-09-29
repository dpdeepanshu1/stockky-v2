"""
tests/test_shared_adaptive.py — technical/shared_adaptive.py
Pure stdlib — no network, no DB.
"""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "technical"))

import pytest
import shared_adaptive as sa


def run(coro): return asyncio.run(coro)


class TestPercentileRank:
    def test_empty_window_returns_50(self):
        assert sa.percentile_rank(10.0, []) == 50.0

    def test_value_above_all_is_100(self):
        assert sa.percentile_rank(100.0, [1, 2, 3]) == 100.0

    def test_value_below_all_returns_low(self):
        assert sa.percentile_rank(0.0, [1, 2, 3]) == 0.0

    def test_median(self):
        result = sa.percentile_rank(5.0, [1, 3, 5, 7, 9])
        assert 50.0 <= result <= 70.0

    def test_returns_native_float(self):
        result = sa.percentile_rank(5.0, [1, 5, 10])
        assert isinstance(result, float)


class TestAdaptiveGate:
    def test_thin_sample_uses_guardrail_min(self):
        passes, thr, note = sa.adaptive_gate(5.0, [1.0, 2.0], 70, 3.0, 90.0, min_sample=8)
        assert "thin sample" in note
        assert thr == 3.0

    def test_thin_sample_passes_when_above_guardrail(self):
        passes, _, _ = sa.adaptive_gate(5.0, [1.0], 70, 3.0, 90.0, min_sample=8)
        assert passes is True

    def test_thin_sample_fails_when_below_guardrail(self):
        passes, _, _ = sa.adaptive_gate(2.0, [1.0], 70, 3.0, 90.0, min_sample=8)
        assert passes is False

    def test_full_sample_uses_percentile(self):
        window = list(range(1, 21))  # 20 elements
        passes, thr, note = sa.adaptive_gate(18.0, window, 70, 3.0, 90.0, min_sample=8)
        assert "adaptive" in note
        assert isinstance(passes, bool)

    def test_threshold_clamped_to_guardrail_max(self):
        window = list(range(1, 21))
        # base_pctl=95 > guardrail_max=90 → threshold = 90
        _, thr, _ = sa.adaptive_gate(18.0, window, 95, 3.0, 90.0)
        assert thr == 90.0

    def test_threshold_clamped_to_guardrail_min(self):
        window = list(range(1, 21))
        # base_pctl=20 < guardrail_min=30 → threshold = 30
        _, thr, _ = sa.adaptive_gate(18.0, window, 20, 30.0, 90.0)
        assert thr == 30.0

    def test_returns_native_bool(self):
        passes, _, _ = sa.adaptive_gate(5.0, list(range(10)), 70, 3.0, 90.0)
        assert type(passes) is bool


class TestHybridGate:
    def test_thin_sample_always_false(self):
        assert sa.hybrid_gate(10.0, [1.0], 70, 3.0, 90.0, abs_floor=3.0, min_sample=8) is False

    def test_passes_pctl_and_floor(self):
        window = list(range(1, 21))
        result = sa.hybrid_gate(19.0, window, 70, 3.0, 90.0, abs_floor=3.0)
        assert result is True

    def test_fails_when_below_abs_floor(self):
        window = list(range(1, 21))
        # value=1.0 is below abs_floor=3.0 → False regardless of percentile
        result = sa.hybrid_gate(1.0, window, 10, 3.0, 90.0, abs_floor=3.0)
        assert result is False

    def test_fails_when_low_percentile(self):
        window = list(range(1, 21))
        # value=2 is at 10th pctile; base_pctl=70 → fails pctl check
        result = sa.hybrid_gate(2.0, window, 70, 3.0, 90.0, abs_floor=1.0)
        assert result is False

    def test_returns_native_bool(self):
        result = sa.hybrid_gate(5.0, list(range(10)), 70, 3.0, 90.0, abs_floor=3.0)
        assert type(result) is bool


async def _coro(v):
    return v


class TestRelativeStrengthVsSector:
    def test_no_data_returns_false(self):
        async def _go():
            return await sa.relative_strength_vs_sector(
                "X", "IT",
                get_return_fn=lambda s, d: _coro(None),
                get_peers_fn=lambda sec: _coro([]),
            )
        result = run(_go())
        assert result["passes"] is False
        assert result["stock_return_10d"] is None

    def test_thin_peers_passes_false(self):
        async def _get_ret(sym, days):
            return 5.0
        async def _get_peers(sec):
            return ["A"]
        async def _go():
            return await sa.relative_strength_vs_sector(
                "X", "IT", get_return_fn=_get_ret, get_peers_fn=_get_peers
            )
        result = run(_go())
        assert result["passes"] is False
        assert "thin sample" in result["note"]

    def test_good_return_vs_many_peers_may_pass(self):
        async def _get_ret(sym, days):
            return 15.0 if sym == "X" else 2.0
        async def _get_peers(sec):
            return [f"P{i}" for i in range(12)]
        async def _go():
            return await sa.relative_strength_vs_sector(
                "X", "IT", get_return_fn=_get_ret, get_peers_fn=_get_peers
            )
        result = run(_go())
        assert result["stock_return_10d"] == 15.0
        assert result["sector_percentile"] > 50.0

    def test_peer_with_none_return_excluded_from_window(self):
        async def _get_ret(sym, days):
            return 10.0 if sym == "X" else None
        async def _get_peers(sec):
            return ["P1", "P2"]
        async def _go():
            return await sa.relative_strength_vs_sector(
                "X", "IT", get_return_fn=_get_ret, get_peers_fn=_get_peers
            )
        result = run(_go())
        assert result["peers_in_window"] == 0
        assert result["sector_percentile"] == 50.0

    def test_self_excluded_from_peer_window(self):
        async def _get_ret(sym, days):
            return 10.0
        async def _get_peers(sec):
            return ["X", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
        async def _go():
            return await sa.relative_strength_vs_sector(
                "X", "IT", get_return_fn=_get_ret, get_peers_fn=_get_peers
            )
        result = run(_go())
        # X is excluded from the peer window — 9 peers remain
        assert result["peers_in_window"] == 9
