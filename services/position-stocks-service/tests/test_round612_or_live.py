"""Round 612: Position Stocks live OR rescue of WINDOW_NO_SETUP (orders/or_live.py). Plain runner: `python tests/test_round612_or_live.py` (also collected by pytest)."""
import datetime as dt
import json
import os
import sys
import tempfile

os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(tempfile.mkdtemp(), "or_live_budget.json")   # round 613: the count is a file; never the real /data/shared one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from orders import or_live as o  # noqa: E402

WIN = "WINDOW_NO_SETUP: only a setup that matched (S1 / S2 / S3) may enter; a plain shortlist entry waits"
T10 = dt.datetime(2026, 11, 18, 10, 0)


def _cfg(**kw):
    base = {"OR_LIVE_MODE": "live", "OR_LIVE_SINCE_IST": "09:45", "OR_LIVE_MAX_PER_DAY": 4, "OR_LIVE_MIN_CONFIRMS": 2}
    base.update(kw)
    for k, v in base.items():
        setattr(config, k, v)
    o.reset()


def test_vwap_is_a_must_have():
    _cfg()
    assert o.rule_b(WIN, 1, True, "top", 0)["confirms"] == ["vwap", "event", "sector_top"]
    assert o.rule_b(WIN, 1, False, "top", 1) is None and o.rule_b(WIN, 1, None, "top", 1) is None


def test_needs_min_confirms_of_the_other_three():
    _cfg()
    assert o.rule_b(WIN, 0, True, "top", 1)["confirms"] == ["vwap", "sector_top", "order_flow"]
    assert o.rule_b(WIN, 1, True, "mid", 0) is None            # one of three
    assert o.rule_b(WIN, -1, True, "mid", -1) is None          # a negative event / tape is not a confirmation
    assert o.rule_b("", 1, True, "top", 1) is None             # nothing to rescue
    _cfg(OR_LIVE_MIN_CONFIRMS=3)
    assert o.rule_b(WIN, 1, True, "top", 0) is None and o.rule_b(WIN, 1, True, "top", 1) is not None
    _cfg()


def test_time_mode_and_daily_budget():
    _cfg()
    hit = o.rule_b(WIN, 1, True, "top", 0)
    assert o.allow(hit, dt.datetime(2026, 11, 18, 9, 30)) is False and o.allow(hit, None) is False
    _cfg(OR_LIVE_MODE="off")
    assert o.allow(hit, T10) is False
    _cfg(OR_LIVE_MAX_PER_DAY=2)
    assert [o.allow(hit, T10) for _ in range(3)] == [True, True, False]
    assert o.allow(hit, T10 + dt.timedelta(days=1)) is True
    _cfg(OR_LIVE_MAX_PER_DAY=0)
    assert o.allow(hit, T10) is False
    _cfg()


def test_stamp():
    out = None
    for i in range(6):
        out = o.stamp(out, {"rule": "B", "n": i})
    assert [h["n"] for h in json.loads(out)["or_live"]] == [2, 3, 4, 5]
    assert o.stamp("not json", {"rule": "B"}) == "not json"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
