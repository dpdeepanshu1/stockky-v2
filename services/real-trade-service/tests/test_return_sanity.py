"""
tests/test_return_sanity.py — direct unit tests for return_sanity.py.

Was 82% (missing lines 53 and 58 — ``atr_input_series`` and ``clamp_series``);
every existing test only exercised ``clamp_for_atr`` indirectly through
entry.py / exit.py / portfolio.py. Pure stdlib, no DB, no network.

Run from services/real-trade-service:
    python -m pytest tests/test_return_sanity.py -v
"""
from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import return_sanity as rs

THRESH = rs.CORPORATE_ACTION_JUMP_THRESHOLD


class TestClampForAtr:
    def test_normal_move_returned_unchanged(self):
        assert rs.clamp_for_atr(2.5) == 2.5
        assert rs.clamp_for_atr(-2.5) == -2.5
        assert rs.clamp_for_atr(0.0) == 0.0

    def test_move_exactly_at_threshold_is_kept(self):
        # strict '>' — a move of exactly the threshold is still real trading
        assert rs.clamp_for_atr(THRESH) == THRESH
        assert rs.clamp_for_atr(-THRESH) == -THRESH

    def test_move_just_over_threshold_is_excluded_both_directions(self):
        assert rs.clamp_for_atr(THRESH + 0.01) is None
        assert rs.clamp_for_atr(-(THRESH + 0.01)) is None


class TestAtrInputSeries:
    def test_drops_corporate_action_jumps_and_keeps_order(self):
        raw = [1.0, -2.0, 55.0, 3.0, -80.0, 0.0]
        assert rs.atr_input_series(raw) == [1.0, -2.0, 3.0, 0.0]

    def test_all_normal_returns_same_values(self):
        raw = [0.5, -0.5, 1.5]
        assert rs.atr_input_series(raw) == raw

    def test_all_jumps_returns_empty_not_error(self):
        assert rs.atr_input_series([THRESH + 1, -(THRESH + 1)]) == []

    def test_empty_input(self):
        assert rs.atr_input_series([]) == []

    def test_does_not_mutate_input(self):
        raw = [1.0, 99.0, 2.0]
        rs.atr_input_series(raw)
        assert raw == [1.0, 99.0, 2.0]

    def test_zero_is_kept_not_treated_as_missing(self):
        # `if r is not None` — a legitimate 0.0 return must survive the filter
        assert rs.atr_input_series([0.0, 0.0]) == [0.0, 0.0]


class TestClampSeries:
    def test_default_threshold_matches_module_constant(self):
        raw = [1.0, THRESH, THRESH + 0.5, -THRESH, -(THRESH + 0.5)]
        assert rs.clamp_series(raw) == [1.0, THRESH, -THRESH]

    def test_custom_threshold(self):
        assert rs.clamp_series([1, 5, 10, -10, 11, -11], threshold=10) == [1, 5, 10, -10]

    def test_none_values_are_dropped(self):
        assert rs.clamp_series([1.0, None, 2.0, None]) == [1.0, 2.0]

    def test_zero_is_kept_not_treated_as_missing(self):
        assert rs.clamp_series([0, 0.0, None]) == [0, 0.0]

    def test_empty_and_all_none(self):
        assert rs.clamp_series([]) == []
        assert rs.clamp_series([None, None]) == []

    def test_boundary_agrees_with_clamp_for_atr(self):
        # the two helpers must classify the same value identically at the edge
        for v in (THRESH, -THRESH, THRESH + 1e-9, -(THRESH + 1e-9)):
            kept_by_series = rs.clamp_series([v]) == [v]
            kept_by_scalar = rs.clamp_for_atr(v) is not None
            assert kept_by_series == kept_by_scalar, v


class TestThresholdEnvOverride:
    """CORPORATE_ACTION_JUMP_THRESHOLD is read once at import from the env."""

    def test_env_override_is_honoured_on_import(self, monkeypatch):
        monkeypatch.setenv("CORPORATE_ACTION_JUMP_THRESHOLD", "10")
        try:
            mod = importlib.reload(rs)
            assert mod.CORPORATE_ACTION_JUMP_THRESHOLD == 10.0
            assert mod.clamp_for_atr(10.0) == 10.0
            assert mod.clamp_for_atr(10.5) is None
            assert mod.clamp_series([9, 11]) == [9]
        finally:
            monkeypatch.delenv("CORPORATE_ACTION_JUMP_THRESHOLD", raising=False)
            importlib.reload(rs)   # restore the default for every later test

    def test_default_is_30_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("CORPORATE_ACTION_JUMP_THRESHOLD", raising=False)
        try:
            mod = importlib.reload(rs)
            assert mod.CORPORATE_ACTION_JUMP_THRESHOLD == 30.0
        finally:
            importlib.reload(rs)
