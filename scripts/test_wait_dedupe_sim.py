"""round 681: scripts/wait_dedupe_sim.py - the replay of a day's WAIT rows under the current dedupe rule and two stricter ones. No database."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import wait_dedupe_sim as w  # noqa: E402

WIN = 900.0


def _rows(sym, tags, step=180):
    return [(sym, t, "reason", "v", "vr", i * step) for i, t in enumerate(tags)]


def test_signature_ignores_numbers_and_spacing_like_the_service_rule():
    a = w.signature("structure", "dip 0.83%  below", "REJECTED", "x 12")
    b = w.signature("structure", "dip 0.85% below", "REJECTED", "x 99")
    assert a == b and w.signature("liquidity", "dip 0.83% below", "REJECTED", "x 12") != a


def test_a_steady_name_keeps_one_row_per_window_under_every_rule():
    r = w.simulate(_rows("S", ["A"] * 20), WIN)               # 20 rows over 57 minutes
    assert r["rows"] == 20 and r["current"] == r["any_sig"] == r["sym_gate"] == 4


def test_a_name_flipping_between_two_tags_is_the_case_the_current_rule_misses():
    r = w.simulate(_rows("F", ["A", "B"] * 10), WIN)
    assert r["current"] == 20 and r["any_sig"] == 8 and r["sym_gate"] == 8
    assert r["per_symbol"]["F"]["flips_back"] == 18 and r["per_symbol"]["F"]["distinct"] == 2


def test_rows_arrive_in_any_order_and_a_longer_gap_keeps_the_row_again():
    rows = list(reversed(_rows("G", ["A"] * 3, step=1000)))     # 1000 s apart: each is past the 900 s window
    r = w.simulate(rows, WIN)
    assert r["current"] == r["any_sig"] == 3
