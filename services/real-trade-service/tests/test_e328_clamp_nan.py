"""
E328 - return_sanity.clamp_for_atr / clamp_series must drop NaN and inf.

`abs(nan) > 30` is False, so a NaN daily return / ATR % passed the corporate-action clamp unchanged and flowed into
exit.py's trail (`trail_candidate` NaN -> the stop silently stops ratcheting), entry sizing and portfolio ATR %.
Pure stdlib: runs without sqlalchemy.

Run from services/real-trade-service:
    python -m pytest tests/test_e328_clamp_nan.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import return_sanity as rs

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_clamp_for_atr_excludes_non_finite(bad):
    assert rs.clamp_for_atr(bad) is None


@pytest.mark.parametrize("ok", [0.0, 2.5, -2.5, 30.0])
def test_clamp_for_atr_keeps_normal_values(ok):
    assert rs.clamp_for_atr(ok) == ok


def test_atr_input_series_drops_non_finite():
    assert rs.atr_input_series([1.0, NAN, 2.0, INF, -INF, 99.0]) == [1.0, 2.0]


def test_clamp_series_drops_non_finite():
    assert rs.clamp_series([1.0, NAN, 2.0, INF, None, 50.0]) == [1.0, 2.0]


def test_shared_and_service_copies_are_identical():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shared = os.path.join(here, "..", "..", "shared", "return_sanity.py")
    if os.path.exists(shared):
        assert open(shared, "rb").read() == open(os.path.join(here, "return_sanity.py"), "rb").read()
