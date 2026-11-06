"""tests/test_round223_qty_trace.py - round 223: the one-line explanation when the risk engine cuts an entry's quantity."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entry_engine import qty_trace


def test_a_cut_is_described_with_reason_and_conviction():
    line = qty_trace.cut_line("REAL", "AMBUJACEM", 18, 7, "sized_down", "Per-trade risk cap", 0.52)
    assert line.startswith("entry_engine: REAL AMBUJACEM risk engine cut qty 18 -> 7 (sized_down, conviction size x0.52)")
    assert "Per-trade risk cap" in line


def test_no_cut_no_line():
    assert qty_trace.cut_line("REAL", "X", 10, 10, "all_checks_passed", "ok") is None
    assert qty_trace.cut_line("REAL", "X", 10, None, "all_checks_passed", "ok") is None
    assert qty_trace.cut_line("REAL", "X", 0, 0, "x", "y") is None


def test_missing_reason_and_junk_never_raise():
    assert "no reason given" in qty_trace.cut_line("REAL", "X", 10, 4, None, "")
    assert qty_trace.cut_line("REAL", "X", "abc", 4, "a", "b") is None
