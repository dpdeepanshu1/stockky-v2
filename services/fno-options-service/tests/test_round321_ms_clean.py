"""E321: patterns and chart_context drop bars the engine would reject (zero price, inverted range, open / close outside [low, high])."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockky_ms import chart_context as cc  # noqa: E402
from stockky_ms import patterns  # noqa: E402

GOOD = (1.0, 100.0, 101.0, 99.0, 100.0, 10.0)
BAD = [(2.0, 100.0, 101.0, 99.0, 140.0, 10.0), (3.0, 60.0, 101.0, 100.0, 100.0, 10.0), (4.0, 0.0, 0.0, 0.0, 0.0, 10.0), (5.0, 100.0, 99.0, 101.0, 100.0, 10.0)]


def test_patterns_clean():
    assert patterns._clean([GOOD] + BAD) == [GOOD]


def test_chart_context_clean_keeps_dates_parallel():
    bars, ds = cc._clean([GOOD, BAD[0], GOOD], ["2026-01-01", "2026-01-02", "2026-01-03"], 10)
    assert len(bars) == 2 and ds == ["2026-01-01", "2026-01-03"]
