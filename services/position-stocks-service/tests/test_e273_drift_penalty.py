"""E273 round 33: Position Stocks - the entry-slippage chase (live price above the signal) is a SIZE PENALTY up to DRIFT_HARD_MULT x ENTRY_MAX_SLIPPAGE_PCT, refused beyond it.
Plain runner: python3 tests/test_e273_drift_penalty.py (service folder). Stdlib only; sqlalchemy / httpx stubbed when missing."""
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


def _cfg(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def test_no_penalty_at_or_under_the_limit_or_when_unusable():
    assert sg.drift_penalty(0.25, 0.25) is None and sg.drift_penalty(0.1, 0.25) is None and sg.drift_penalty(-1.0, 0.25) is None
    assert sg.drift_penalty(None, 0.25) is None and sg.drift_penalty("x", 0.25) is None and sg.drift_penalty(0.3, 0) is None
    assert sg.drift_penalty(float("nan"), 0.25) is None


def test_penalty_grows_from_one_to_the_cap_and_ceiling_refuses():
    old = _cfg(SOFT_GATES_MODE="penalty", DRIFT_HARD_MULT=1.75, SOFT_GATE_MAX_PENALTY=3.0)
    try:
        lo, mid, hi = sg.drift_penalty(0.26, 0.25), sg.drift_penalty(0.34, 0.25), sg.drift_penalty(0.43, 0.25)      # ceiling 0.4375
        assert 1.0 <= lo < mid < hi <= 3.0 and lo < 1.15 and hi > 2.8
        assert sg.drift_penalty(0.4375, 0.25) is None and sg.drift_penalty(2.0, 0.25) is None
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def test_veto_mode_gives_no_penalty():
    old = _cfg(SOFT_GATES_MODE="veto")
    try:
        assert sg.drift_penalty(0.3, 0.25) is None
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def test_default_and_wiring():
    assert config.DRIFT_HARD_MULT == 1.75
    src = open(os.path.join(ROOT, "orders", "entry.py"), encoding="utf-8").read()
    a = src.index("_price_guard_reject(candidate.symbol, candidate.current_ltp, soft=")
    b = src.index("for _dk, (_dv, _dwhy) in _drift_soft.items()", a)
    assert a < b < src.index("_pen_early = dict(_penalties)") < src.index("setup_live.classify_long(")       # the penalty is in before the setup engine sizes
    assert "soft_gates.drift_penalty(" in src and "NO_LIVE_TICK" in src and "_stale_tick_message(age, max_age)" in src     # stale / no tick stay hard


def test_round34_day_gain_penalty_ramp_ceiling_and_wiring():
    old = _cfg(SOFT_GATES_MODE="penalty", DAY_GAIN_HARD_MULT=1.75, SOFT_GATE_MAX_PENALTY=3.0)
    try:
        assert sg.day_gain_penalty(7.0, 7.0) is None and sg.day_gain_penalty(5.0, 7.0) is None and sg.day_gain_penalty("x", 7.0) is None and sg.day_gain_penalty(8.0, 0) is None
        lo, hi = sg.day_gain_penalty(7.1, 7.0), sg.day_gain_penalty(12.0, 7.0)             # ceiling 12.25
        assert 1.0 <= lo < hi <= 3.0 and hi > 2.8 and sg.day_gain_penalty(12.25, 7.0) is None and sg.day_gain_penalty(20.0, 7.0) is None
        config.SOFT_GATES_MODE = "veto"
        assert sg.day_gain_penalty(8.0, 7.0) is None
    finally:
        for k, v in old.items():
            setattr(config, k, v)
    assert config.DAY_GAIN_HARD_MULT == 1.75
    src = open(os.path.join(ROOT, "orders", "entry.py"), encoding="utf-8").read()
    assert 'soft["day_gain"]' in src and "for _dk, (_dv, _dwhy) in _drift_soft.items()" in src
    assert src.index("for _dk, (_dv, _dwhy) in _drift_soft.items()") < src.index("_pen_early = dict(_penalties)")


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1; print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
