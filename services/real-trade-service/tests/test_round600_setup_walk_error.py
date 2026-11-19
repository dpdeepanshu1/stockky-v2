"""round 600: a setup walk that RAISES no longer lets a plain shortlist entry through inside the 09:20-15:30 window.

Before: entry.py logged the exception at DEBUG and left the walk result None; window_no_setup_refusal reads "no walk result" as a pass, so a broken walk turned
every candidate into a plain entry (the 7 failed-entry exits of 12-16 Nov were all plain entries). Now the failure is counted, warned about (throttled per symbol)
and marked why="walk_error", which waits inside the window.
Plain runner: python3 tests/test_round600_setup_walk_error.py"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from entry_engine import setup_live as sl

D = (2026, 11, 18)
WALK_ERR = {"id": None, "skip": "NO_SETUP", "why": "walk_error"}


def r(res, h, m, **kw):
    return sl.window_no_setup_refusal(res, now=datetime(*D, h, m), exempt=kw.pop("exempt", False), **kw)


def test_walk_error_waits_inside_the_window():
    for h, m in ((9, 20), (9, 45), (11, 0), (15, 29)):
        msg = r(WALK_ERR, h, m)
        assert msg and msg.startswith("WINDOW_NO_SETUP") and "failed" in msg, (h, m, msg)


def test_walk_error_outside_the_window_and_exempt_and_switched_off_still_pass():
    assert r(WALK_ERR, 15, 30) is None and r(WALK_ERR, 9, 0) is None
    assert r(WALK_ERR, 10, 0, exempt=True) is None            # a pullback re-arm entry carries its own trigger
    assert r(WALK_ERR, 10, 0, enabled=False) is None
    assert r(WALK_ERR, 10, 0, until="garbage") is None        # bad config still fails open


def test_walk_error_before_0920_waits_like_everything_else():
    assert "WINDOW_WAIT" in r(WALK_ERR, 9, 16)


def test_the_old_verdicts_are_unchanged():
    no = {"id": None, "skip": "NO_SETUP"}
    assert "WINDOW_NO_SETUP" in r(no, 10, 0) and "failed" not in r(no, 10, 0)
    assert r({"id": "S2_RS_BREAKOUT"}, 10, 0) is None
    assert r({"id": None, "skip": "NO_SETUP", "why": "error"}, 10, 0) is None     # the classifier's own error: unchanged
    assert r({"id": None, "skip": "PENALTY_TOO_HIGH"}, 10, 0) is None
    assert r(None, 10, 0) is None                                                  # the walk did not run at all (e.g. no tick): unchanged


def test_no_setup_mode_live_treats_a_walk_error_as_not_a_verdict_when_all_ran_is_false():
    assert sl.no_setup_refusal(WALK_ERR, mode="live", exempt=False, all_ran=False) is None


def test_entry_py_marks_counts_and_warns():
    src = open(os.path.join(HERE, "entry_engine", "entry.py"), encoding="utf-8").read()
    assert '"why": "walk_error"' in src and "_walk_error_note(cand.symbol, _walk_exc)" in src
    assert 'setup walk failed for %s (legacy stamp, full size)' not in src       # the DEBUG-only handler is gone
    m = re.search(r"def _walk_error_note.*?\n\n\n", src, re.S)
    assert m and "logger.warning" in m.group(0) and "SETUP_WALK_ERRORS" in m.group(0)


def test_walk_error_note_counts_every_failure_but_warns_once_per_symbol(caplog=None):
    import types
    # load only the helper out of entry.py (importing the whole module needs the service's dependencies)
    src = open(os.path.join(HERE, "entry_engine", "entry.py"), encoding="utf-8").read()
    m = re.search(r"SETUP_WALK_ERRORS = .*?\n\n\n(?=# ──)", src, re.S)
    assert m
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec)
    # Round 643: a private logger with the handler attached and the level set, instead of the shared "real-trade-entry"
    # logger (the old code left a handler on it for every later test and could be silenced by logging state left by an
    # earlier test in a full run).
    lg = logging.getLogger("round600-walk-error-test")
    lg.handlers[:] = [H()]
    lg.setLevel(logging.DEBUG)
    lg.disabled = False
    lg.propagate = False
    saved_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    ns = {"logger": lg, "_time_r600": __import__("time")}
    exec(m.group(0), ns)
    for _ in range(3):
        ns["_walk_error_note"]("ABC", ValueError("boom"))
    ns["_walk_error_note"]("XYZ", KeyError("k"))
    assert ns["SETUP_WALK_ERRORS"]["count"] == 4
    logging.disable(saved_disable)
    warns = [x for x in records if x.levelno == logging.WARNING]
    assert len(warns) == 2 and "ABC" in warns[0].getMessage() and "ValueError" in warns[0].getMessage()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("pass", k)
    print(n, "passed")
