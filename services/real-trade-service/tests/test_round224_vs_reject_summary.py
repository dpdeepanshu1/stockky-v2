"""tests/test_round224_vs_reject_summary.py - round 224: one INFO summary line per cycle for the volume-shock rejections."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from candidate_engine import candidates as c


def test_numbers_are_replaced_so_the_same_rule_groups_together():
    a = c._vs_reject_category("Today's return -80.6% < 2.5% volume-shock breakout threshold (quote pre-check, history not fetched).")
    b = c._vs_reject_category("Today's return 0.4% < 2.5% volume-shock breakout threshold (quote pre-check, history not fetched).")
    assert a == b and "#" in a and "80.6" not in a


def test_price_floor_reason_groups_across_symbols():
    a = c._vs_reject_category("Price ₹2.70 is below ₹10.0 minimum. Sub-₹10 stocks have operator activity risk.")
    b = c._vs_reject_category("Price ₹8.28 is below ₹10.0 minimum. Sub-₹10 stocks have operator activity risk.")
    assert a == b


def test_long_reasons_are_cut_visibly():
    out = c._vs_reject_category("x " * 200)
    assert out.endswith("...") and len(out) <= 95


def test_summary_line_counts_groups_and_examples():
    rej = [("KMEW", "Today's return -80.6% < 2.5% threshold"), ("ABC", "Today's return 0.4% < 2.5% threshold"),
           ("PREMIER", "Price ₹2.70 is below ₹10.0 minimum"), ("SHAH", "Price ₹2.81 is below ₹10.0 minimum"),
           ("AGOL", "Price ₹6.01 is below ₹10.0 minimum")]
    line = c._vs_reject_summary_line("REAL", rej, 40)
    assert line.startswith("VOLUME_SHOCK rejected 5 of 40 symbol(s) this cycle (mode=REAL)")
    assert "[3] Price" in line and "[2] Today's return" in line
    assert "PREMIER" in line and "KMEW" in line
    assert "DEBUG" in line


def test_summary_is_capped_to_top_groups_and_says_how_many_more():
    rej = [(f"S{i}", f"reason {'word' * i} number {i}") for i in range(1, 12)]
    line = c._vs_reject_summary_line("REAL", rej, 11, top=3)
    assert line.count("[1]") == 3 and "+8 more reason(s)" in line


def test_no_rejections_no_line_and_junk_never_raises():
    assert c._vs_reject_summary_line("REAL", [], 10) is None
    assert c._vs_reject_summary_line("REAL", [("A", None)], 1) is not None
    assert c._vs_reject_summary_line("REAL", None, 1) is None


def test_mode_switch(monkeypatch):
    monkeypatch.delenv("VOLUME_SHOCK_REJECT_LOG", raising=False)
    assert c._vs_reject_log_mode() == "summary"
    for v in ("each", "all", "OFF", "0"):
        monkeypatch.setenv("VOLUME_SHOCK_REJECT_LOG", v)
        assert c._vs_reject_log_mode() == "each"
    monkeypatch.setenv("VOLUME_SHOCK_REJECT_LOG", "typo")
    assert c._vs_reject_log_mode() == "summary"
