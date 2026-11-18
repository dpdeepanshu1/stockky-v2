"""Round 614: a live OR rescue whose entry never became an order is given back to the day's budget. Plain runner: `python tests/test_round615_or_refund.py` (also collected by pytest)."""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(tempfile.mkdtemp(), "or_live_budget.json")
os.environ.pop("OR_LIVE_SHARED_BUDGET", None)
os.environ.pop("OR_LIVE_REFUND", None)
import config  # noqa: E402
from entry_engine import shared_or_budget as sb  # noqa: E402
from entry_engine import or_live as o  # noqa: E402

DAY = "2026-11-18"
T10 = dt.datetime(2026, 11, 18, 10, 0)
HIT = {"rule": "B", "confirms": ["vwap", "event", "sector_top"], "refused": "x"}


def _fresh(cap=4):
    os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(tempfile.mkdtemp(), "or_live_budget.json")
    os.environ["OR_LIVE_SHARED_BUDGET"] = "1"
    os.environ.pop("OR_LIVE_REFUND", None)
    config.OR_LIVE_MODE, config.OR_LIVE_SINCE_IST, config.OR_LIVE_MAX_PER_DAY = "live", "09:45", cap
    o.reset()


def test_give_back_lowers_the_shared_count_and_never_goes_below_zero():
    _fresh()
    sb.take(DAY, 4, "rt")
    sb.take(DAY, 4, "ps")
    assert sb.give_back(DAY, "rt", "file") is True
    assert json.load(open(sb.path())) == {"day": DAY, "n": 1, "by": {"rt": 0, "ps": 1}}
    assert sb.give_back(DAY, "ps", "file") is True
    assert sb.give_back(DAY, "ps", "file") is False and sb.used(DAY) == 0     # nothing left to give back
    assert sb.give_back("2026-11-19", "rt", "file") is False                  # another day: untouched


def test_give_back_for_a_process_count():
    _fresh()
    os.environ["OR_LIVE_SHARED_BUDGET"] = "0"
    sb.reset_process()
    sb.take(DAY, 2, "rt")
    assert sb.give_back(DAY, "rt", "process") is True
    assert sb.take(DAY, 2, "rt")[1] == 1


def test_allow_with_key_then_give_back_restores_the_slot_once():
    _fresh(cap=1)
    assert o.allow(HIT, T10, key="K") is True and o.used_today() == 1
    assert o.allow(HIT, T10, key="K2") is False                      # budget spent
    assert o.give_back("K") is True and o.used_today() == 0
    assert o.give_back("K") is False                                 # only once
    assert o.allow(HIT, T10, key="K3") is True                       # the slot can be used again


def test_keep_means_it_stays_spent():
    _fresh(cap=1)
    assert o.allow(HIT, T10, key="K") is True
    o.keep("K")
    assert o.give_back("K") is False and sb.used(DAY) == 1


def test_refund_switch_off_keeps_the_rescue_spent():
    _fresh(cap=1)
    os.environ["OR_LIVE_REFUND"] = "0"
    try:
        assert o.allow(HIT, T10, key="K") is True
        assert o.give_back("K") is False and sb.used(DAY) == 1
    finally:
        os.environ.pop("OR_LIVE_REFUND", None)


def test_allow_without_key_is_as_before_and_never_refundable():
    _fresh(cap=1)
    assert o.allow(HIT, T10) is True and o.pending_keys() == []


# ---- Real Trade: the settle step after a cycle -------------------------------------------------------------------------------------------------
def _entry():
    from entry_engine import entry as e
    return e


def test_settle_gives_back_only_rescues_without_an_enter_row():
    e = _entry()
    _fresh(cap=4)
    for sym in ("AAA", "BBB", "CCC"):
        assert o.allow(HIT, T10, key=("DEMO", sym)) is True
    assert o.allow(HIT, T10, key=("REAL", "ZZZ")) is True
    assert sb.used(DAY) == 4
    real_unc = e._uncertain_buy_active
    e._uncertain_buy_active = lambda mode, sym: sym == "CCC"          # CCC: a BUY of unknown status may be live
    try:
        e._or_live_settle("DEMO", {"entry_details": [{"symbol": "AAA", "action": "ENTER"}, {"symbol": "BBB", "action": "WAIT"}, {"symbol": "CCC", "action": "WAIT"}]})
    finally:
        e._uncertain_buy_active = real_unc
    assert sb.used(DAY) == 3                                          # only BBB given back
    assert o.pending_keys() == [("REAL", "ZZZ")]                      # the other mode is untouched


def test_settle_with_no_result_or_odd_result_gives_nothing_back():
    e = _entry()
    _fresh(cap=4)
    o.allow(HIT, T10, key=("DEMO", "AAA"))
    e._or_live_settle("DEMO", None)
    assert sb.used(DAY) == 1 and o.pending_keys() == []               # cycle raised: stays spent, pending cleared
    o.allow(HIT, T10, key=("DEMO", "BBB"))
    e._or_live_settle("DEMO", {"entry_details": "nope"})
    assert sb.used(DAY) == 2


def test_evaluate_mode_decorator_settles_after_the_cycle_and_after_an_error():
    e = _entry()
    _fresh(cap=4)

    async def ok_cycle(db, mode, armed):
        o.allow(HIT, T10, key=(mode, "AAA"))
        o.allow(HIT, T10, key=(mode, "BBB"))
        return {"entry_details": [{"symbol": "AAA", "action": "ENTER"}, {"symbol": "BBB", "action": "WAIT"}]}

    async def bad_cycle(db, mode, armed):
        o.allow(HIT, T10, key=(mode, "CCC"))
        raise RuntimeError("boom")

    res = asyncio.run(e._or_live_settled(ok_cycle)(None, "DEMO", True))
    assert res["entry_details"][0]["symbol"] == "AAA" and sb.used(DAY) == 1              # BBB given back
    try:
        asyncio.run(e._or_live_settled(bad_cycle)(None, "DEMO", True))
    except RuntimeError:
        pass
    assert sb.used(DAY) == 2 and o.pending_keys() == []                                  # CCC stays spent after an error


def test_evaluate_mode_keeps_its_name_and_source():
    import inspect
    e = _entry()
    assert e.evaluate_mode.__name__ == "evaluate_mode"
    assert "async def evaluate_mode" in inspect.getsource(e.evaluate_mode)
    assert asyncio.iscoroutinefunction(e.evaluate_mode)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
