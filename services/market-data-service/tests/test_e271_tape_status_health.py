"""E271: order_flow_tape.status() reports how many of today's symbols have a reliable tape (what the calibration card and the live check read)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import order_flow_tape as t  # noqa: E402
from test_e268_order_flow_tape import _feed, _row, T0  # noqa: E402


def setup_function(_f=None):
    t._reset_for_tests()


def test_status_is_unreliable_when_nothing_is_tracked():
    s = t.status(T0)
    assert s["symbols_today"] == 0 and s["reliable_symbols"] == 0 and s["reliable"] is False and s["avg_coverage_pct"] is None and s["enabled"] in (True, False)


def test_status_counts_a_reliable_symbol_and_the_reasons_for_the_others():
    end = _feed(300, step_s=2, dv=1000, sym="GOOD")
    t.on_rows([_row(100.0, 100_000, 100.0, 99.95, 100.05, None, None, "NEW")], now=end)      # one snapshot only: too few intervals
    s = t.status(end)
    assert s["symbols_today"] == 2 and s["reliable_symbols"] == 1 and s["reliable"] is True
    assert s["unreliable_reasons"] == {"too_few_intervals": 1} and s["avg_coverage_pct"] is not None


def test_status_keeps_the_old_counters_and_ignores_yesterdays_symbols():
    end = _feed(300, step_s=2, dv=1000, sym="GOOD")
    s = t.status(end)
    assert {"rows", "intervals", "errors", "symbols", "skipped_reorder"} <= set(s)
    tomorrow = end + timedelta(days=1)
    s2 = t.status(tomorrow)
    assert s2["symbols"] == 1 and s2["symbols_today"] == 0 and s2["reliable"] is False
