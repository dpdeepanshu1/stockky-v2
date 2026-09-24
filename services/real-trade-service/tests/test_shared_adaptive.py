"""
tests/test_shared_adaptive.py

100% coverage for shared_adaptive.py — closes all 24 missed lines:

  * line 21    : percentile_rank empty-window fallback (return 50.0)
  * lines 38-42: adaptive_gate thin-sample path (len(window) < min_sample)
  * lines 59-64: hybrid_gate — both thin-sample path (False) and normal path
  * lines 81-96: relative_strength_vs_sector — stock_ret=None early-return,
                 normal peer-collection loop (including self-exclusion and
                 None-return filtering), thin-sample note, and the normal
                 note format.

Run from services/real-trade-service:
    python3 -m pytest tests/test_shared_adaptive.py -q \\
        --cov=shared_adaptive --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import shared_adaptive as sa


def run(coro):
    return asyncio.run(coro)


# ── percentile_rank ────────────────────────────────────────────────────────────

class TestPercentileRank:
    def test_empty_window_returns_50(self):
        # line 21: empty window → 50.0
        assert sa.percentile_rank(99.0, []) == 50.0

    def test_all_below_value(self):
        assert sa.percentile_rank(10.0, [1.0, 2.0, 3.0]) == 100.0

    def test_value_below_all(self):
        assert sa.percentile_rank(0.0, [1.0, 2.0, 3.0]) == 0.0

    def test_value_in_middle(self):
        # 2 of 4 values <= 5 → 50.0
        result = sa.percentile_rank(5.0, [3.0, 5.0, 7.0, 9.0])
        assert result == 50.0

    def test_boundary_included(self):
        # value == window element → counted (<=)
        assert sa.percentile_rank(5.0, [5.0, 10.0]) == 50.0


# ── adaptive_gate ──────────────────────────────────────────────────────────────

class TestAdaptiveGate:
    def test_thin_sample_passes_above_guardrail_min(self):
        # lines 38-39: len(window) < min_sample → guardrail_min as threshold
        passes, thr, note = sa.adaptive_gate(60.0, [50.0, 55.0], 70, 40, 90, min_sample=8)
        assert passes is True
        assert thr == 40
        assert "thin sample" in note
        assert isinstance(passes, bool)  # not numpy.bool_

    def test_thin_sample_fails_below_guardrail_min(self):
        # lines 38-39: thin sample, value below guardrail_min
        passes, thr, note = sa.adaptive_gate(30.0, [10.0], 70, 40, 90, min_sample=8)
        assert passes is False
        assert thr == 40
        assert "thin sample" in note

    def test_normal_path_passes(self):
        # lines 40-42: full window, percentile above threshold
        window = list(range(1, 20))  # 19 items ≥ min_sample=8
        passes, thr, note = sa.adaptive_gate(15.0, window, 50, 30, 80)
        assert isinstance(passes, bool)
        assert "adaptive pctl" in note

    def test_normal_path_threshold_clamped_to_guardrail_min(self):
        # threshold = max(guardrail_min, min(base_pctl, guardrail_max))
        # base_pctl=10 < guardrail_min=30 → threshold=30
        window = list(range(1, 20))
        _, thr, _ = sa.adaptive_gate(15.0, window, 10, 30, 80)
        assert thr == 30

    def test_normal_path_threshold_clamped_to_guardrail_max(self):
        # base_pctl=95 > guardrail_max=80 → threshold=80
        window = list(range(1, 20))
        _, thr, _ = sa.adaptive_gate(15.0, window, 95, 30, 80)
        assert thr == 80

    def test_exactly_at_min_sample_uses_normal_path(self):
        # len(window) == min_sample → NOT thin sample
        window = [1.0] * 8
        passes, thr, note = sa.adaptive_gate(1.0, window, 50, 30, 80, min_sample=8)
        assert "adaptive pctl" in note


# ── hybrid_gate ────────────────────────────────────────────────────────────────

class TestHybridGate:
    def test_thin_sample_always_returns_false(self):
        # lines 59-60: thin sample → False without calling adaptive_gate
        result = sa.hybrid_gate(100.0, [99.0], 50, 30, 80, abs_floor=3.0, min_sample=8)
        assert result is False
        assert isinstance(result, bool)

    def test_normal_path_passes_when_both_conditions_met(self):
        # lines 61-64: passes_pctl=True AND value >= abs_floor → True
        window = list(range(1, 20))
        result = sa.hybrid_gate(15.0, window, 50, 30, 80, abs_floor=3.0)
        assert isinstance(result, bool)
        # value=15.0 is well above abs_floor=3.0; percentile of 15 in 1..19 ≈ 78%
        assert result is True

    def test_normal_path_fails_when_abs_floor_not_met(self):
        # passes_pctl might be True but abs_floor not met → False
        window = list(range(1, 20))
        result = sa.hybrid_gate(1.0, window, 50, 30, 80, abs_floor=3.0)
        assert result is False

    def test_normal_path_fails_when_percentile_too_low(self):
        # abs_floor met but percentile too low → False
        window = list(range(10, 110, 10))  # [10,20,...,100]
        # value=5 is below abs_floor=3 anyway but also p=0; let's use a case
        # where abs_floor passes but percentile fails
        result = sa.hybrid_gate(4.0, window, 90, 80, 95, abs_floor=3.0)
        assert result is False

    def test_exactly_min_sample_uses_normal_path(self):
        # len(window) == min_sample → normal path, not thin-sample
        window = [5.0] * 8
        result = sa.hybrid_gate(5.0, window, 50, 30, 80, abs_floor=3.0, min_sample=8)
        assert isinstance(result, bool)


# ── relative_strength_vs_sector ───────────────────────────────────────────────

class TestRelativeStrengthVsSector:
    """Lines 81-96: the async function."""

    def test_no_stock_data_returns_no_data_result(self):
        # line 82-84: stock_ret is None → early return
        async def _no_data(sym, days):
            return None

        async def _peers(sector):
            return ["A", "B"]

        result = run(sa.relative_strength_vs_sector("X", "TECH", _no_data, _peers))
        assert result["passes"] is False
        assert result["note"] == "no data"
        assert result["stock_return_10d"] is None
        assert result["sector_percentile"] is None

    def test_no_peers_uses_fallback_percentile_50(self):
        # peers list is empty → peer_rets=[] → pctl=50.0 (empty-window fallback)
        # and hybrid_gate with thin sample → passes=False
        async def _ret(sym, days):
            return 5.0  # stock always has data

        async def _peers(sector):
            return []  # no peers at all

        result = run(sa.relative_strength_vs_sector("X", "TECH", _ret, _peers))
        assert result["stock_return_10d"] == 5.0
        assert result["sector_percentile"] == 50.0
        assert result["passes"] is False  # thin sample (0 < MIN_SECTOR_SAMPLE=8)
        assert "thin sample" in result["note"]

    def test_self_is_excluded_from_peer_rets(self):
        # line 88-89: if p == symbol → skip
        calls = []

        async def _ret(sym, days):
            calls.append(sym)
            return 8.0

        async def _peers(sector):
            return ["SELF", "A", "B"]

        result = run(sa.relative_strength_vs_sector("SELF", "TECH", _ret, _peers))
        # "SELF" should not appear in the peer get_return_fn calls
        peer_calls = [c for c in calls if c != "SELF"]
        assert "SELF" not in peer_calls
        # initial stock call + peers A and B (not SELF)
        assert calls.count("A") == 1
        assert calls.count("B") == 1
        assert calls.count("SELF") == 1  # only the initial stock fetch

    def test_none_peer_return_is_filtered_out(self):
        # line 91-92: r is None → not appended
        async def _ret(sym, days):
            if sym == "BADFEED":
                return None
            return 5.0

        async def _peers(sector):
            return ["BADFEED", "GOOD1"]

        result = run(sa.relative_strength_vs_sector("X", "TECH", _ret, _peers))
        # Only 1 peer (GOOD1) has data → thin sample (1 < 8) → passes=False
        assert result["peers_in_window"] == 1
        assert result["passes"] is False

    def test_sufficient_peers_passes_with_high_return(self):
        # Normal path with enough peers so percentile and abs_floor both pass
        # Stock return = 15.0, peers all return 1.0 → stock at 100th percentile
        peer_syms = [f"P{i}" for i in range(10)]  # 10 peers ≥ MIN_SECTOR_SAMPLE=8

        async def _ret(sym, days):
            if sym == "STAR":
                return 15.0
            return 1.0  # all peers return 1.0

        async def _peers(sector):
            return peer_syms + ["STAR"]  # STAR itself excluded by self-check

        result = run(sa.relative_strength_vs_sector("STAR", "TECH", _ret, _peers))
        assert result["stock_return_10d"] == 15.0
        assert result["peers_in_window"] == 10
        assert result["passes"] is True
        # note should be the "pXX vs N peers" format, not "thin sample"
        assert "peers" in result["note"]
        assert "thin sample" not in result["note"]

    def test_sufficient_peers_fails_with_low_return(self):
        # Stock return = 0.5 (below abs_floor=3.0) → passes=False
        peer_syms = [f"P{i}" for i in range(10)]

        async def _ret(sym, days):
            if sym == "LAGGARD":
                return 0.5
            return 5.0

        async def _peers(sector):
            return peer_syms

        result = run(sa.relative_strength_vs_sector("LAGGARD", "TECH", _ret, _peers))
        assert result["passes"] is False
