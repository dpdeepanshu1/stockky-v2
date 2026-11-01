"""E273 round 2: setup_live feeds setup_engine for LONG entries (S1). Plain runner: python3 tests/test_e273_setup_live.py"""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders import setup_live as sl, setup_engine as se

T0925 = datetime(2026, 11, 2, 9, 25)
T1015 = datetime(2026, 11, 2, 10, 15)
ORB = {"orb": {"range_high": 100.0, "range_low": 98.0, "close_trigger": True, "trigger_close": 100.6}}
QUOTE = {"previous_close": 100.0, "avg_price": 100.2}
TAPE = {"reliable": True, "buy_pct_10m": 62, "delta_agrees": True}


def run(now=T0925, pol=1, fj=ORB, quote=QUOTE, gap=0.5, tape=TAPE, price=100.8):
    return sl.classify_long(symbol="ABC", price=price, now=now, event_polarity=pol, flow_json=json.dumps(fj) if fj is not None else None,
                            quote=quote, gap_pct=gap, tape=tape)


def test_s1_matches_with_tape_and_vwap_at_quarter_size_in_the_0920_slot():
    r = run()
    assert r["id"] == se.S1 and r["size_mult"] == 0.25 and "tape_buying" in r["confirmations"] and "above_vwap" in r["confirmations"]
    assert r["stamp"]["slot"] == "0920"


def test_later_slot_is_half_size():
    assert run(now=T1015)["size_mult"] == 0.5


def test_missing_or_wrong_facts_never_match():
    assert run(pol=0)["id"] is None and run(pol=-1)["id"] is None            # unknown / negative event
    assert run(fj={})["id"] is None                                          # no ORB record: no closed bar above the range
    assert run(fj={"orb": {"range_high": 100.0, "close_trigger": False}})["id"] is None
    assert run(now=datetime(2026, 11, 2, 9, 18))["id"] is None               # before the first closed bar after the range
    assert run(gap=None)["id"] is None and run(quote=None)["id"] is None     # no pre-open price: cannot say price holds it


def test_pre_open_price_must_be_held():
    assert run(gap=0.5, price=100.8)["id"] == se.S1                          # pre-open 100.5, price above it
    assert run(gap=3.0, price=100.8)["id"] is None                           # pre-open 103: it gave the gap back


def test_two_confirmations_needed():
    assert run(tape=None)["id"] is None                                      # vwap only = 1 of 3
    assert run(tape=None, quote={"previous_close": 100.0, "avg_price": 101.5})["id"] is None
    assert run(tape={"reliable": False, "buy_pct_10m": 80})["id"] is None    # an unreliable tape counts for nothing
    assert run(quote={"previous_close": 100.0, "avg_price": 101.5})["id"] is None   # tape only = 1 of 3
    assert run(tape={"reliable": True, "buy_pct_10m": 62, "delta_agrees": False})["id"] is None


def test_tape_sign():
    assert sl.tape_sign(TAPE) == 1 and sl.tape_sign({"reliable": True, "buy_pct_10m": 40}) == -1
    assert sl.tape_sign({"reliable": True, "buy_pct_10m": 50}) == 0 and sl.tape_sign(None) == 0 and sl.tape_sign({"buy_pct_10m": 90}) == 0


def test_extra_factor_takes_the_lower_multiplier_not_the_product():
    assert sl.extra_factor(0.25, 0.3) == 0.25 / 0.3 and sl.extra_factor(0.5, 0.3) == 1.0 and sl.extra_factor(0.25, 1.0) == 0.25
    assert sl.extra_factor(None, 0.3) == 1.0 and sl.extra_factor(0, 0.3) == 1.0


def test_wiring_by_source_text():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "orders", "entry.py")).read()
    assert "setup_live.classify_long" in src and "setup_live.extra_factor" in src
    assert src.index("setup_live.classify_long") < src.index("tm_gate.entry_check") < src.index("setup_live.extra_factor")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print("ok", f.__name__)
    print(len(fns), "passed")
