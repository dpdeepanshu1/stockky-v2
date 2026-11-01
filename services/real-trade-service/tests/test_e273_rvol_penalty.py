"""E273 round 29: the relative-volume test of the Real Trade liquidity gate is a SIZE PENALTY down to LIQUIDITY_RVOL_HARD_FRAC x its floor; the turnover tests still refuse.
Plain runner: python3 tests/test_e273_rvol_penalty.py (service folder). Stdlib only; the gate and the ratio are stubbed."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import liquidity_gate as lg  # noqa: E402
from entry_engine import soft_gates as sg  # noqa: E402

RV = "LIQUIDITY_RVOL:volume is 0.25x its normal pace for this time (< 0.35x) - the name is not trading today"


class Stub:
    def __init__(self, why, ratio):
        self.o = (lg.reject_reason_tick, lg.rvol_ratio)
        lg.reject_reason_tick, lg.rvol_ratio = (lambda t, now=None: why), (lambda t, now=None: ratio)

    def done(self):
        lg.reject_reason_tick, lg.rvol_ratio = self.o


def run(why, ratio):
    s = Stub(why, ratio)
    try:
        return sg.liquidity(object())
    finally:
        s.done()


def test_no_reason_no_penalty():
    assert run(None, 1.0) == (None, 1.0)


def test_low_rvol_between_floor_and_half_floor_is_a_penalty():
    why, m = run(RV, 0.30)
    assert why is None and 0.40 < m < 0.85
    why2, m2 = run(RV, 0.20)
    assert why2 is None and 0.40 <= m2 < m


def test_under_half_the_floor_is_still_refused():
    assert run(RV, 0.17) == (RV, 1.0) and run(RV, 0.05) == (RV, 1.0)


def test_turnover_refusals_are_never_softened():
    t = "LIQUIDITY_AVG_TURNOVER:normal daily turnover Rs 1.00 cr < floor Rs 3.00 cr - too thin to scalp"
    d = "LIQUIDITY_DAY_TURNOVER:traded only Rs 0.50 cr today < floor Rs 1.00 cr - no depth to exit into"
    assert run(t, 0.30) == (t, 1.0) and run(d, 0.30) == (d, 1.0)


def test_veto_mode_and_unreadable_ratio_keep_the_refusal():
    old = config.SOFT_GATES_MODE
    config.SOFT_GATES_MODE = "veto"
    try:
        assert run(RV, 0.30) == (RV, 1.0)
    finally:
        config.SOFT_GATES_MODE = old
    assert run(RV, None) == (RV, 1.0)


def test_rvol_ratio_never_raises_and_wiring():
    assert lg.rvol_ratio(None) is None
    assert config.LIQUIDITY_RVOL_HARD_FRAC == 0.5
    assert '_soft("rvol"' in (HERE / "entry_engine" / "entry.py").read_text()


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", n, repr(ex))
    sys.exit(1 if fails else 0)
