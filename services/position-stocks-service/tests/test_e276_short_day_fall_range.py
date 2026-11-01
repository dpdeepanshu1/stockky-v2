"""E276 round 67: the short day-fall limit follows the stock's own daily range. Plain runner: python3 tests/test_e276_short_day_fall_range.py (service folder)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from orders import short_gates  # noqa: E402


def _set(**kw):
    for k, v in kw.items():
        setattr(config, k, v)


def test_limit_math():
    _set(SHORT_DAY_FALL_RANGE_MULT=1.5, SHORT_DAY_FALL_HARD_CEILING_PCT=8.0)
    assert short_gates.day_fall_limit(5.0, 1.5) == 5.0          # quiet stock: never tighter than the fixed number
    assert abs(short_gates.day_fall_limit(5.0, 4.0) - 6.0) < 1e-9
    assert short_gates.day_fall_limit(5.0, 9.0) == 8.0          # hard ceiling
    assert short_gates.day_fall_limit(5.0, None) == 5.0         # unknown range
    assert short_gates.day_fall_limit(0.0, 4.0) == 0.0          # off stays off
    _set(SHORT_DAY_FALL_RANGE_MULT=0.0)
    assert short_gates.day_fall_limit(5.0, 4.0) == 5.0          # switch off = old behaviour


def test_gate_uses_own_range():
    _set(SHORT_MAX_DAY_FALL_PCT=5.0, SHORT_DAY_FALL_RANGE_MULT=1.5, SHORT_DAY_FALL_HARD_CEILING_PCT=8.0, ENTRY_MAX_SLIPPAGE_PCT=0.0)
    base = {"live": 94.0, "ltp": 94.0, "signal_ltp": 94.0, "prev_close": 100.0, "tick_age_s": 1.0}      # down 6 %
    r1 = short_gates.price_guard(dict(base, avg_range_pct=1.5), None)
    r2 = short_gates.price_guard(dict(base, avg_range_pct=4.5), None)
    assert r1 and r1[0] == "SHORT_DAY_FALL", r1                 # quiet stock, 6 % fall: refused
    assert r2 is None, r2                                       # volatile stock (limit 6.75 %): allowed


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print("ok", name)
    print(n, "passed")
