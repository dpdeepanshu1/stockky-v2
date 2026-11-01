"""E273 round 31: Position Stocks - low relative volume is a SIZE PENALTY down to LIQUIDITY_RVOL_HARD_FRAC x ENTRY_MIN_RVOL, refused under it.
Plain runner: python3 tests/test_e273_rvol_penalty.py (service folder). Stdlib only; sqlalchemy / httpx stubbed when missing."""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

import config  # noqa: E402
from orders import soft_gates as sg  # noqa: E402


def test_no_penalty_at_or_above_the_floor_or_when_unreadable():
    assert sg.rvol_penalty(0.35, 0.35) is None and sg.rvol_penalty(1.2, 0.35) is None
    assert sg.rvol_penalty(None, 0.35) is None and sg.rvol_penalty("x", 0.35) is None and sg.rvol_penalty(0.2, 0) is None


def test_penalty_grows_from_one_to_the_cap():
    just = sg.rvol_penalty(0.349, 0.35)
    mid = sg.rvol_penalty(0.26, 0.35)
    low = sg.rvol_penalty(0.176, 0.35)
    assert 1.0 < just < mid < low <= sg.cap() and abs(just - 1.0) < 0.02 and low > 2.9


def test_under_half_the_floor_has_no_penalty_so_the_refusal_stands():
    assert sg.rvol_penalty(0.17, 0.35, 0.5) is None and sg.rvol_penalty(0.01, 0.35, 0.5) is None
    assert sg.rvol_penalty(0.17, 0.35, 0.4) is not None            # a lower hard fraction widens the penalty band


def test_default_and_wiring():
    assert config.LIQUIDITY_RVOL_HARD_FRAC == 0.5
    src = open(os.path.join(ROOT, "orders", "entry.py")).read()
    assert 'liquidity_gate.rvol_ratio(' in src and '_penalties["rvol"]' in src
    from orders import liquidity_gate
    assert liquidity_gate.rvol_ratio("NOSUCH", 100.0) is None      # never raises


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", n, repr(ex))
    sys.exit(1 if fails else 0)
