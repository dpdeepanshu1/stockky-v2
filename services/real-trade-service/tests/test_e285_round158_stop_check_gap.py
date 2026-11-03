"""E285 round 158 (plan item 16): a late same-day stop check is detected (gap between completed checks) and alerted."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from execution import stop_check_gap as g  # noqa: E402


def _fresh():
    g._state.clear()
    g._last_warn.clear()
    os.environ.pop("EXIT_CHECK_GAP_WARN_S", None)


def test_normal_cadence_is_quiet_and_a_long_gap_with_positions_is_reported_once():
    _fresh()
    assert g.note_done("REAL", 2, 100.0) is None          # first check: nothing to compare
    assert g.note_done("REAL", 2, 108.0) is None          # 8 s: normal
    gap = g.note_done("REAL", 2, 170.0)                   # 62 s since the last completed check, positions were open
    assert gap is not None and abs(gap - 62.0) < 1e-9
    assert g.note_done("REAL", 2, 240.0) is None          # 70 s again but inside the 120 s warning throttle
    assert g.note_done("REAL", 2, 330.0) is not None      # 90 s, throttle over


def test_no_positions_before_the_gap_means_no_alert_and_modes_are_separate():
    _fresh()
    g.note_done("REAL", 0, 100.0)
    assert g.note_done("REAL", 0, 400.0) is None          # nothing was open: a quiet book is not a late check
    g.note_done("DEMO", 3, 100.0)
    assert g.note_done("REAL", 1, 500.0) is None          # REAL's previous check had 0 open
    assert g.note_done("DEMO", 3, 200.0) is not None


def test_reset_at_the_close_and_switch_off_and_junk_env():
    _fresh()
    g.note_done("REAL", 2, 100.0)
    g.reset("REAL")
    assert g.note_done("REAL", 2, 9999.0) is None         # first check of a new session
    os.environ["EXIT_CHECK_GAP_WARN_S"] = "0"
    g.note_done("REAL", 2, 10000.0)
    assert g.note_done("REAL", 2, 20000.0) is None and g.warn_s() == 0
    os.environ["EXIT_CHECK_GAP_WARN_S"] = "junk"
    assert g.warn_s() == 45.0
    os.environ["EXIT_CHECK_GAP_WARN_S"] = "-5"
    assert g.warn_s() == 45.0
    _fresh()


def test_never_raises_on_bad_input_and_is_wired_into_both_exit_paths():
    _fresh()
    assert g.note_done("REAL", None, None) is None and g.note_done("REAL", "x", 1.0) is None
    ap = open(os.path.join(ROOT, "execution", "auto_pilot.py"), encoding="utf-8").read()
    assert "stop_check_gap.note_done(mode" in ap and "Stop check late" in ap and "stop_check_gap.reset(mode)" in ap
    cr = open(os.path.join(ROOT, "cycle_runner.py"), encoding="utf-8").read()
    assert "_scg.note_done(mode" in cr


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                f += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if f else 0)
