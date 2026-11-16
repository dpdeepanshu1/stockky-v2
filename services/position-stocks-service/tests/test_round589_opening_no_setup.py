"""round 589: before 09:30 a LONG that matched no setup is refused (16 Nov: both 09:20 'shortlist (no setup)' longs were stopped out within minutes, 0 wins of 5).
Plain runner: python3 tests/test_round589_opening_no_setup.py"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders import setup_live as sl

D = (2026, 11, 16)
NO = {"id": None, "skip": "NO_SETUP", "why": "none"}
S1 = {"id": "S1_CATALYST_UP", "size_mult": 0.25}


def r(res, h, m, **kw):
    return sl.opening_no_setup_refusal(res, now=datetime(*D, h, m), exempt=kw.pop("exempt", False), **kw)


def test_no_setup_before_0930_is_refused():
    assert "OPENING_NO_SETUP" in r(NO, 9, 20)
    assert "OPENING_NO_SETUP" in r(NO, 9, 15)
    assert "OPENING_NO_SETUP" in r(NO, 9, 29)


def test_from_0930_it_is_allowed():
    assert r(NO, 9, 30) is None and r(NO, 10, 0) is None


def test_a_matched_setup_is_never_refused():
    assert r(S1, 9, 20) is None


def test_exempt_error_switch_and_clock_fail_open():
    assert r(NO, 9, 20, exempt=True) is None
    assert r({"id": None, "why": "error"}, 9, 20) is None
    assert r(None, 9, 20) is None
    assert r(NO, 9, 20, enabled=False) is None
    assert sl.opening_no_setup_refusal(NO, now=None, exempt=False) is None
    assert r(NO, 9, 0) is None            # before the bell: not this guard's window


def test_until_is_configurable():
    assert r(NO, 9, 40, until="09:45") is not None
    assert r(NO, 9, 40, until="garbage") is None


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 5")
