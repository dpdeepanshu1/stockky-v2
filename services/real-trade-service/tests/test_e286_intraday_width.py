"""E286 round 214: stop / target width cap for same-day shortlist entries (entry_engine/intraday_width.py). Plain runner: python3 tests/test_e286_intraday_width.py."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from entry_engine.intraday_width import cap_for_intraday as cap


def test_coforge_like_swing_width_is_capped_with_same_ratio():
    s, t, note = cap(4.5, 9.0, 3.0)                 # 1.5 x 3 % ATR = 4.5 % stop, 2:1 target
    assert s == 1.8 and t == 3.6 and note and "4.50%" in note


def test_stop_already_inside_the_cap_is_untouched():
    assert cap(1.5, 3.0, 3.0) == (1.5, 3.0, None)


def test_floor_keeps_a_quiet_stock_from_a_tiny_stop():
    s, t, _ = cap(3.0, 6.0, 1.0)                    # 0.6 x 1 % = 0.6 % < floor 1.2 %
    assert s == 1.2 and t == 2.4


def test_ratio_is_kept_for_a_non_2_to_1_target():
    s, t, _ = cap(5.0, 12.5, 3.0)
    assert s == 1.8 and abs(t / s - 2.5) < 0.02


def test_no_atr_or_bad_data_is_no_change_and_never_raises():
    for args in ((3.2, 6.5, None), (3.2, 6.5, 0), (3.2, 6.5, "x"), (None, 6.5, 3.0), (3.2, float("nan"), 3.0)):
        assert cap(*args)[2] is None and cap(*args)[:2] == args[:2]


def test_config_defaults():
    import config
    assert config.INTRADAY_WIDTH_MODE == "live" and config.INTRADAY_WIDTH_ATR_FRAC == 0.6 and config.INTRADAY_WIDTH_FLOOR_PCT == 1.2
    assert config.INTRADAY_WIDTH_LABELS == frozenset({"BUY NOW", "PREPARE TO BUY"})


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"{n} passed")
