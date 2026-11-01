"""E273 round 30: Position Stocks - a price under the previous close is a SIZE PENALTY down to OPENING_GATE_HARD_BELOW_PREV_PCT, refused below it.
Plain runner: python3 tests/test_e273_below_prev_close_penalty.py (service folder). Reuses the stubs of test_e273_opening_parts_penalty.py."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_e273_opening_parts_penalty as base  # noqa: E402
from test_e273_opening_parts_penalty import NOW, ST, _setup, config, og  # noqa: E402

ROOT = base.ROOT


def test_above_the_previous_close_has_no_such_penalty():
    _setup()
    assert "below_prev_close" not in og.parts_penalties("A", 99.5, NOW)          # prev close 99.0


def test_under_the_previous_close_is_a_penalty_that_grows_with_depth():
    _setup()
    shallow = og.parts_penalties("A", 98.5, NOW)["below_prev_close"]["penalty"]      # -0.5 %
    deep = og.parts_penalties("A", 97.4, NOW)["below_prev_close"]["penalty"]         # -1.6 %
    assert 1.0 < shallow < deep < 3.0 + 1e-9
    assert og.reject_reason("A", 98.5, NOW) is None or not str(og.reject_reason("A", 98.5, NOW)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
    assert "-0.51%" in og.parts_penalties("A", 98.5, NOW)["below_prev_close"]["why"]


def test_past_the_depth_it_is_still_refused():
    _setup()
    assert str(og.reject_reason("A", 96.5, NOW)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")     # -2.5 %
    config.OPENING_GATE_HARD_BELOW_PREV_PCT = 4.0
    try:
        assert not str(og.reject_reason("A", 96.5, NOW)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
    finally:
        config.OPENING_GATE_HARD_BELOW_PREV_PCT = 2.0


def test_veto_mode_restores_the_old_refusal():
    _setup("veto")
    assert str(og.reject_reason("A", 98.9, NOW)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
    assert "below_prev_close" not in og.parts_penalties("A", 98.9, NOW)


def test_default_and_wiring():
    assert config.OPENING_GATE_HARD_BELOW_PREV_PCT == 2.0
    assert "opening_gate.parts_penalties(" in open(os.path.join(ROOT, "orders", "entry.py")).read()


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", n, repr(ex))
    sys.exit(1 if fails else 0)
