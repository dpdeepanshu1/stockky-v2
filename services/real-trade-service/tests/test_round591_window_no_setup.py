"""round 591: no Real Trade long before 09:20, then (to 15:30) only a matched setup (16 Nov: 7 failed-entry exits, all "BUY NOW (no setup)").
Plain runner: python3 tests/test_round591_window_no_setup.py"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from entry_engine import setup_live as sl

D = (2026, 11, 16)
NO = {"id": None, "skip": "NO_SETUP"}
S2 = {"id": "S2_RS_BREAKOUT", "size_mult": 0.5}


def r(res, h, m, **kw):
    return sl.window_no_setup_refusal(res, now=datetime(*D, h, m), exempt=kw.pop("exempt", False), **kw)


def test_everything_waits_before_0920():
    assert "WINDOW_WAIT" in r(NO, 9, 15) and "WINDOW_WAIT" in r(S2, 9, 17) and "WINDOW_WAIT" in r(None, 9, 19)
    assert r(NO, 9, 0) is None                     # before the bell: not this guard's window


def test_from_0920_only_a_matched_setup_enters_all_day():
    for h, m in ((9, 20), (9, 30), (9, 45), (11, 0), (15, 29)):
        assert "WINDOW_NO_SETUP" in r(NO, h, m), (h, m)
        assert r(S2, h, m) is None, (h, m)
    assert r(NO, 15, 30) is None and r(NO, 16, 0) is None


def test_other_verdicts_exempt_error_switch_and_clock_fail_open():
    assert r({"id": None, "skip": "PENALTY_TOO_HIGH"}, 9, 35) is None
    assert r({"id": None, "skip": "NO_SETUP", "why": "error"}, 9, 35) is None
    assert r(None, 9, 35) is None
    assert r(NO, 9, 35, exempt=True) is None
    assert r(NO, 9, 35, enabled=False) is None and r(NO, 9, 16, enabled=False) is None
    assert sl.window_no_setup_refusal(NO, now=None, exempt=False) is None


def test_since_and_until_are_configurable_and_garbage_fails_open():
    assert r(NO, 9, 25, since="09:30") is not None and "WINDOW_WAIT" in r(S2, 9, 25, since="09:30")
    assert r(NO, 10, 30, until="10:00") is None
    assert r(NO, 9, 35, until="garbage") is None
    assert r(NO, 9, 35, since="garbage") is None


def test_config_catalog_and_wiring_present():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = open(os.path.join(here, "config.py"), encoding="utf-8").read()
    assert "OPENING_NO_SETUP_REFUSE" in cfg and "OPENING_NO_SETUP_UNTIL_IST" in cfg and "OPENING_NO_SETUP_SINCE_IST" in cfg
    cat = open(os.path.join(here, "control", "catalog.py"), encoding="utf-8").read()
    assert '"OPENING_NO_SETUP_REFUSE"' in cat and '"OPENING_NO_SETUP_UNTIL_IST"' in cat and '"OPENING_NO_SETUP_SINCE_IST"' in cat
    ent = open(os.path.join(here, "entry_engine", "entry.py"), encoding="utf-8").read()
    assert "window_no_setup_refusal" in ent and "no_setup_window" in ent
    auto = open(os.path.join(here, "..", "calibration-runner-service", "autocalib.py"), encoding="utf-8").read()
    assert '"OPENING_NO_SETUP_REFUSE"' in auto and '"OPENING_NO_SETUP_UNTIL_IST"' in auto and '"OPENING_NO_SETUP_SINCE_IST"' in auto


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 6")
