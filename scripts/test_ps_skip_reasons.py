"""Round 693: scripts/ps_skip_reasons.py (pure parts; no database)."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps_skip_reasons as p

T = datetime(2026, 11, 28, 4, 0)


def test_family_takes_the_text_before_the_colon():
    assert p.family("RANGE_GATE:chop 1.2%") == "RANGE_GATE"
    assert p.family("MAX_POSITIONS:3") == "MAX_POSITIONS"
    assert p.family("SERVICE_NOT_ARMED") == "SERVICE_NOT_ARMED"
    assert p.family("") == "(no reason)" and p.family(None) == "(no reason)"
    assert p.family("something happened here") == "something"


def test_summary_counts_rows_symbols_and_entered_apart():
    rows = [("A", "SKIPPED", "MAX_POSITIONS:3", T), ("A", "SKIPPED", "MAX_POSITIONS:4", datetime(2026, 11, 28, 3, 50)), ("B", "SKIPPED", "MAX_POSITIONS:3", T),
            ("C", "SKIPPED", "COST_GATE:x", T), ("D", "ENTERED", "ok", T)]
    r = p.summarize(rows)
    assert (r["entered_rows"]["L"], r["entered_syms"]["L"], r["skipped_rows"]) == (1, 1, 4)
    f = r["families"][0]
    assert f["family"] == "MAX_POSITIONS" and f["rows"] == 3 and len(f["syms"]) == 2 and f["side"] == "L"
    assert f["first"] == datetime(2026, 11, 28, 3, 50)
    assert len(f["samples"]) == 2


def test_a_placed_short_is_an_entry_not_a_skip():
    """Round 696: the 25-27 Nov read showed ENTERED 0 while SELL ... SUPER_ORDER rows sat under SKIPPED, because those rows carry decision SHORT_ENTERED."""
    rows = [("A", "SHORT_ENTERED", "SELL 3 @ 209.93 stop 213.43 target 202.93 SUPER_ORDER=1", T), ("B", "SHORT_SKIP", "SHORT_DAY_FALL:already down 9%", T),
            ("C", "SKIPPED", "NEAR_DAY_LOW:x", T), ("D", "SOFT", "OPENING_GATE:BELOW_OPEN: (size penalty 2.4, SOFT)", T)]
    r = p.summarize(rows)
    assert r["entered_rows"] == {"L": 0, "S": 1} and r["entered_syms"] == {"L": 0, "S": 1}
    assert r["skipped_rows"] == 2 and r["soft_rows"] == 1
    assert {(f["side"], f["family"]) for f in r["families"]} == {("S", "SHORT_DAY_FALL"), ("L", "NEAR_DAY_LOW")}
    assert all(f["family"] != "SELL" for f in r["families"])


def test_a_long_and_a_short_reason_of_one_name_stay_apart():
    rows = [("A", "SKIPPED", "X:1", T), ("B", "SHORT_SKIP", "X:2", T)]
    assert len(p.summarize(rows)["families"]) == 2


def test_format_has_ist_time_share_side_and_the_empty_day_message():
    txt = p.format_day("2026-11-28", [("A", "SKIPPED", "COST_GATE:x", T)])
    assert "COST_GATE" in txt and "09:30" in txt and "100.0%" in txt and "ENTERED long 0" in txt
    assert "no scalp_candidate_log rows" in p.format_day("2026-11-28", [])


def test_window_is_the_ist_session_in_utc():
    lo, hi = p.window_utc("2026-11-28")
    assert (lo.hour, lo.minute, hi.hour) == (3, 45, 10)
