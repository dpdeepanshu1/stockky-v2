"""round 582: records_report.exit_slippage_pct reads every stop spelling, not stop_hit only. Plain asserts or pytest."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import records_report as rr  # noqa: E402


def _r(reason, xp):
    return {"exit_reason": reason, "entry_price": 100.0, "stop_pct": 2.0, "exit_price": xp}


def test_broker_stop_and_gap_exits_are_measured():
    s = rr.exit_slippage_pct([_r("stop_hit", 98.0), _r("broker_stop", 97.0), _r("emergency_gap_down", 94.0)])
    assert s["n"] == 3 and s["best"] == 0.0 and s["worst"] == round((94.0 - 98.0) / 98.0 * 100, 3)


def test_non_stop_exits_still_left_out():
    assert rr.exit_slippage_pct([_r("trail_stop_approx", 103.0), _r("time_stop", 97.0), _r("eod_squareoff", 50.0), _r(None, 90.0)]) is None


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 2")
