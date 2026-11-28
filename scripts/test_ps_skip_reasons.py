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
    ent, ent_syms, skipped, fams = p.summarize(rows)
    assert (ent, ent_syms, skipped) == (1, 1, 4)
    assert fams[0]["family"] == "MAX_POSITIONS" and fams[0]["rows"] == 3 and len(fams[0]["syms"]) == 2
    assert fams[0]["first"] == datetime(2026, 11, 28, 3, 50)
    assert len(fams[0]["samples"]) == 2


def test_format_has_ist_time_share_and_the_empty_day_message():
    txt = p.format_day("2026-11-28", [("A", "SKIPPED", "COST_GATE:x", T)])
    assert "COST_GATE" in txt and "09:30" in txt and "100.0%" in txt
    assert "no scalp_candidate_log rows" in p.format_day("2026-11-28", [])


def test_window_is_the_ist_session_in_utc():
    lo, hi = p.window_utc("2026-11-28")
    assert (lo.hour, lo.minute, hi.hour) == (3, 45, 10)
