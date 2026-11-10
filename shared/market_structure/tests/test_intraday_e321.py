"""E321: intraday._clean_minutes applies the engine's bar rule, so one corrupt 1-minute print cannot reach an N-minute bar."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_structure import intraday  # noqa: E402
from market_structure.prep import _valid  # noqa: E402

OPEN = intraday.session_open_ts(1_700_000_000.0)


def mk(i, o, h, l, c, v=10.0):
    return (OPEN + 60 * i, o, h, l, c, v)


def test_close_above_high_is_dropped():
    bars = [mk(i, 100, 101, 99, 100) for i in range(15)]
    bars[14] = mk(14, 100, 101, 99, 140)       # the last minute: its close would become the bucket close
    agg = intraday.aggregate_session(bars, 15)
    assert len(agg) == 1 and agg[0][2] == 101 and agg[0][3] == 99 and agg[0][4] == 100


def test_open_below_low_is_dropped():
    bars = [mk(i, 100, 101, 99, 100) for i in range(15)]
    bars[3] = mk(3, 60, 101, 99, 100)
    assert len(intraday._clean_minutes(bars)) == 14


def test_within_tolerance_kept():
    assert len(intraday._clean_minutes([mk(0, 100, 101, 99, 101.02)])) == 1


def test_agrees_with_prep_valid():
    cases = [(100, 101, 99, 100), (100, 101, 99, 140), (60, 101, 100, 100), (100, 101, 99, 101.02), (100, 99, 101, 100), (1, 1, 1, 1)]
    for o, h, l, c in cases:
        b = mk(0, o, h, l, c)
        assert (len(intraday._clean_minutes([b])) == 1) == _valid(b, intraday.BAR_TOLERANCE_PCT), (o, h, l, c)
