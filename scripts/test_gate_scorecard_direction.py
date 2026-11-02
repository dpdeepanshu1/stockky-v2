"""E272 round 9: gate_scorecard.py --direction. The script needs the DB, so side_sign and forward are lifted out of its source and run on fixed bars.
Run: python3 scripts/test_gate_scorecard_direction.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "position-stocks-service"))
from orders.scorecard import forward, side_sign      # E285 batch 6: the logic moved out of the script into orders/scorecard.py (the script is a thin wrapper)
T0 = 1_793_000_000.0 - (1_793_000_000 % 60)


def bars(closes, wick=0.0):
    return [[T0 + 60 * i, c, c + wick, c - wick, c, 100] for i, c in enumerate(closes)]


def test_side_sign():
    assert side_sign("SHORT_VWAP", "long") == 1 and side_sign("SHORT_VWAP", "short") == -1
    assert side_sign("SHORT_VWAP", "auto") == -1 and side_sign(" short_x", "auto") == -1
    assert side_sign("RANGE_GATE:NEAR_DAY_HIGH", "auto") == 1 and side_sign(None, "auto") == 1 and side_sign("RANGE_GATE", "short") == -1


def test_a_falling_stock_is_a_win_for_a_short_and_a_loss_for_a_long():
    b = bars([100.0] + [98.0] * 70)
    long_ = forward(b, T0, 1)
    short = forward(b, T0, -1)
    assert abs(long_[1] + 2.0) < 1e-9 and abs(short[1] - 2.0) < 1e-9 and long_[3] == "stop" and short[3] == "target"


def test_default_sign_is_the_old_long_reading():
    b = bars([100.0] + [102.0] * 70)
    assert abs(forward(b, T0)[1] - 2.0) < 1e-9 and forward(b, T0)[3] == "target"


def test_both_touched_in_one_bar_is_still_the_worse_case_for_a_short():
    b = bars([100.0, 100.0, 100.0] + [100.0] * 70, wick=2.0)       # every bar spans +-2%
    assert forward(b, T0, -1)[3] == "stop" and forward(b, T0, 1)[3] == "stop"


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n} passed")
