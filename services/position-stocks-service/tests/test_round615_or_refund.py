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
from orders import shared_or_budget as sb  # noqa: E402
from orders import or_live as o  # noqa: E402

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


# ---- Position Stocks: attempt_entry settles the rescue --------------------------------------------------------------------------------------------
def _entry():
    from orders import entry as e
    return e


def _run(e, impl, sym="AAA"):
    real = e._attempt_entry_impl
    e._attempt_entry_impl = impl
    try:
        return e.attempt_entry(None, types.SimpleNamespace(symbol=sym))
    finally:
        e._attempt_entry_impl = real


def test_clean_skip_before_any_order_gives_the_rescue_back():
    e = _entry()
    _fresh(cap=2)

    def impl(db, cand, quality=None, rearm=None):
        assert o.allow(HIT, T10, key=cand.symbol)
        return None                                                  # e.g. SHARED_ORDER_BUDGET_EXHAUSTED / price guard
    assert _run(e, impl) is None
    assert sb.used(DAY) == 0 and o.pending_keys() == []


def test_entered_or_order_attempted_or_error_keeps_it_spent():
    e = _entry()
    _fresh(cap=4)

    def entered(db, cand, quality=None, rearm=None):
        o.allow(HIT, T10, key=cand.symbol)
        return object()
    assert _run(e, entered, "A1") is not None and sb.used(DAY) == 1

    def attempted_then_none(db, cand, quality=None, rearm=None):
        o.allow(HIT, T10, key=cand.symbol)
        o.keep(cand.symbol)                                          # what _hold_sent's neighbour does right before the Dhan call
        return None                                                  # BUY failed / status unknown: may be live
    assert _run(e, attempted_then_none, "A2") is None and sb.used(DAY) == 2

    def boom(db, cand, quality=None, rearm=None):
        o.allow(HIT, T10, key=cand.symbol)
        raise RuntimeError("x")
    try:
        _run(e, boom, "A3")
    except RuntimeError:
        pass
    assert sb.used(DAY) == 3 and o.pending_keys() == []


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
