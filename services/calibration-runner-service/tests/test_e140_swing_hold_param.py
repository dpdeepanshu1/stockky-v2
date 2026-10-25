"""E140: swing_backtest takes a Hold parameter; every hold other than 5 gets its own checkpoint folder. Offline, stdlib only."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402

CTX = dict(python=sys.executable, scripts="/s", bars="/b", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
           decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t", run="/r", cal="/c", exp="/e")


def _argv(params=None):
    j = J.JOBS["swing_backtest"]
    return j["builder"](CTX, J.coerce_params(j, params or {}))[0]["argv"]


def _val(argv, flag):
    return argv[argv.index(flag) + 1]


def test_default_is_the_lane_limit_with_the_old_checkpoint():
    a = _argv()
    assert _val(a, "--hold") == "5" and _val(a, "--checkpoint").endswith("ckpt/swing_7")


def test_long_hold_passes_through_and_keeps_its_own_checkpoint():
    a = _argv({"hold": "20"})
    assert _val(a, "--hold") == "20" and _val(a, "--checkpoint").endswith("ckpt/swing_h20_7")


def test_hold_choices_are_offered():
    ps = {p["name"]: p for p in J.JOBS["swing_backtest"]["params"]}
    assert ps["hold"]["choices"] == ["5", "10", "20", "40", "60"] and ps["hold"]["default"] == "5"


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
