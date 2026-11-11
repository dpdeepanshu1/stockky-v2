"""E361: the breakeven raise after a partial exit replaces a missing / NaN / <= 0 stop instead of keeping it.

`max(current_stop or 0, entry)` returns a NaN stop unchanged (nan is truthy and max keeps its first argument when the comparison is False)."""
import math
import pathlib

from portfolio.portfolio import raised_to_breakeven

SRC = pathlib.Path(__file__).resolve().parents[1]


def test_the_old_expression_kept_a_nan_stop():
    assert math.isnan(max(float("nan") or 0, 100.0))          # the defect the helper removes


def test_a_normal_stop_is_raised_never_lowered():
    assert raised_to_breakeven(90.0, 100.0) == 100.0
    assert raised_to_breakeven(110.0, 100.0) == 110.0


def test_a_bad_stop_becomes_the_entry_price():
    for bad in (None, float("nan"), float("inf"), float("-inf"), 0, -5.0, "x"):
        assert raised_to_breakeven(bad, 100.0) == 100.0


def test_an_unusable_entry_price_leaves_the_stop_alone():
    for bad in (None, float("nan"), float("inf"), 0, -1.0, "x"):
        assert raised_to_breakeven(95.0, bad) == 95.0


def test_all_three_partial_exit_sites_use_the_helper():
    for rel in ("portfolio/portfolio.py", "exit_engine/exit.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "max(position.current_stop or 0" not in text and "position.current_stop or 0, position.avg_entry_price" not in text, rel
    assert (SRC / "exit_engine/exit.py").read_text(encoding="utf-8").count("raised_to_breakeven(position.current_stop") == 2
    assert (SRC / "portfolio/portfolio.py").read_text(encoding="utf-8").count("raised_to_breakeven(position.current_stop") == 1
