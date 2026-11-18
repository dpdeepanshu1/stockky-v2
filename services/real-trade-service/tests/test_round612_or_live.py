"""Round 612: live OR rescue (entry_engine/or_live.py). Plain runner: `python tests/test_round612_or_live.py` (also collected by pytest)."""
import datetime as dt
import os
import sys
import tempfile

os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(tempfile.mkdtemp(), "or_live_budget.json")   # round 613: the count is a file; never the real /data/shared one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from entry_engine import or_live as o  # noqa: E402

EV = {"polarity": 1, "type": "results", "strength": 0.8}
WIN = "WINDOW_NO_SETUP: only a setup that matched may enter"
STACK = "3 soft size penalties stacked (a, b, c)"
T10 = dt.datetime(2026, 11, 18, 10, 0)
T0930 = dt.datetime(2026, 11, 18, 9, 30)


def _cfg(**kw):
    base = {"OR_LIVE_MODE": "live", "OR_LIVE_SINCE_IST": "09:45", "OR_LIVE_MAX_PER_DAY": 4, "OR_LIVE_MIN_CONFIRMS": 2, "OR_LIVE_MIN_IMBALANCE": 0.1}
    base.update(kw)
    for k, v in base.items():
        setattr(config, k, v)
    o.reset()


def test_rule_a_needs_a_matched_setup():
    assert o.rule_a(STACK, {"id": "S1"})["rule"] == "A"
    assert o.rule_a(STACK, {"id": None}) is None and o.rule_a("", {"id": "S1"}) is None and o.rule_a(STACK, None) is None


def test_rule_b_vwap_is_a_must_have():
    _cfg()
    assert o.rule_b(WIN, EV, True, "top", None)["confirms"] == ["vwap", "event", "sector_top"]
    assert o.rule_b(WIN, EV, False, "top", 0.5) is None and o.rule_b(WIN, EV, None, "top", 0.5) is None


def test_rule_b_needs_min_confirms_of_the_other_three():
    _cfg()
    assert o.rule_b(WIN, None, True, "top", 0.3)["confirms"] == ["vwap", "sector_top", "order_flow"]
    assert o.rule_b(WIN, None, True, "top", None) is None                       # only 1 of 3
    assert o.rule_b(WIN, EV, True, "mid", 0.05) is None                         # flow under the minimum, sector not top
    assert o.rule_b(WIN, {**EV, "polarity": -1}, True, "mid", 0.3) is None      # a negative event is not a confirmation
    assert o.rule_b(WIN, {**EV, "strength": 0.3}, True, "mid", 0.3) is None     # weak event
    assert o.rule_b(WIN, EV, True, "mid", 0.3, priced_in=2.0) is None           # priced in
    assert o.rule_b("", EV, True, "top", 0.3) is None                           # no refusal to rescue
    _cfg(OR_LIVE_MIN_CONFIRMS=3)
    assert o.rule_b(WIN, EV, True, "top", None) is None and o.rule_b(WIN, EV, True, "top", 0.3) is not None
    _cfg()


def test_time_mode_and_budget():
    _cfg()
    hit = o.rule_a(STACK, {"id": "S1"})
    assert o.allow(hit, T0930) is False                       # before 09:45
    assert o.allow(hit, None) is False                        # unknown time fails closed
    _cfg(OR_LIVE_MODE="off")
    assert o.allow(hit, T10) is False
    _cfg(OR_LIVE_MAX_PER_DAY=2)
    assert [o.allow(hit, T10) for _ in range(3)] == [True, True, False] and o.used_today() == 2
    assert o.allow(hit, T10 + dt.timedelta(days=1)) is True   # a new day resets the count
    _cfg(OR_LIVE_MAX_PER_DAY=0)
    assert o.allow(hit, T10) is False
    _cfg()


def test_stamp_keeps_json_and_last_four():
    out = None
    for i in range(6):
        out = o.stamp(out, {"rule": "A", "n": i})
    import json
    assert [h["n"] for h in json.loads(out)["or_live"]] == [2, 3, 4, 5]
    assert o.stamp("not json", {"rule": "A"}) == "not json"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
