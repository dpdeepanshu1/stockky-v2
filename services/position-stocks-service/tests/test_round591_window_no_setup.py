"""round 591 (widened): Position Stocks - no long before 09:20, then (to 15:30) only a LONG that matched a setup.
Plain runner: python3 tests/test_round591_window_no_setup.py"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders import setup_live as sl

D = (2026, 11, 16)
NO = {"id": None, "skip": "NO_SETUP"}
S1 = {"id": "S1_CATALYST", "size_mult": 0.25}


def r(res, h, m, **kw):
    return sl.window_no_setup_refusal(res, now=datetime(*D, h, m), exempt=kw.pop("exempt", False), **kw)


def test_wait_before_0920_then_only_matched_setup():
    assert "WINDOW_WAIT" in r(S1, 9, 16) and "WINDOW_WAIT" in r(NO, 9, 19)
    for h, m in ((9, 20), (9, 45), (12, 0), (15, 29)):
        assert "WINDOW_NO_SETUP" in r(NO, h, m) and r(S1, h, m) is None
    assert r(NO, 15, 30) is None and r(NO, 9, 0) is None


def test_exempt_error_switch_clock_and_garbage_fail_open():
    assert r(NO, 10, 0, exempt=True) is None
    assert r({"id": None, "why": "error"}, 10, 0) is None and r(None, 10, 0) is None
    assert r(NO, 10, 0, enabled=False) is None and r(NO, 9, 16, enabled=False) is None
    assert sl.window_no_setup_refusal(NO, now=None, exempt=False) is None
    assert r(NO, 10, 0, since="x") is None and r(NO, 10, 0, until="x") is None


def test_config_and_wiring_present():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = open(os.path.join(here, "config.py"), encoding="utf-8").read()
    assert "OPENING_NO_SETUP_SINCE_IST" in cfg and "OPENING_NO_SETUP_UNTIL_IST" in cfg
    ent = open(os.path.join(here, "orders", "entry.py"), encoding="utf-8").read()
    assert "window_no_setup_refusal" in ent


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 3")
