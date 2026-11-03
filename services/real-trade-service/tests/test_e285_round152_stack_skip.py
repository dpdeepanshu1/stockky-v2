"""E285 round 152: entry_engine/size_floor.py stacked_refusal (plan item 7) and overshoot_pct (plan item 9), plus the entry.py wiring. Pure functions; no DB."""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from entry_engine import size_floor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _set(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def _restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def test_three_stacked_soft_penalties_that_hit_the_floor_are_skipped():
    old = _set(SIZE_STACK_SKIP_MIN_PENALTIES=3)
    try:
        why = size_floor.stacked_refusal({"vwap": 0.7, "spike_chase": 0.8, "gain_room": 0.9, "rvol": 1.0}, True)
        assert "3 soft size penalties" in why and "gain_room" in why and "vwap" in why and "rvol" not in why
    finally:
        _restore(old)


def test_two_penalties_or_no_lift_or_switch_off_never_skip():
    old = _set(SIZE_STACK_SKIP_MIN_PENALTIES=3)
    try:
        assert size_floor.stacked_refusal({"vwap": 0.7, "spike_chase": 0.8}, True) == ""
        assert size_floor.stacked_refusal({"a": .9, "b": .9, "c": .9}, False) == ""      # the floor did not lift anything
        assert size_floor.stacked_refusal({}, True) == "" and size_floor.stacked_refusal(None, True) == ""
        config.SIZE_STACK_SKIP_MIN_PENALTIES = 0
        assert size_floor.stacked_refusal({"a": .9, "b": .9, "c": .9}, True) == ""
        config.SIZE_STACK_SKIP_MIN_PENALTIES = 2
        assert size_floor.stacked_refusal({"a": .9, "b": .9}, True) != ""
        assert size_floor.stacked_refusal({"a": "x"}, True) == ""                        # junk never raises
    finally:
        _restore(old)


def test_penalty_of_exactly_one_does_not_count():
    old = _set(SIZE_STACK_SKIP_MIN_PENALTIES=3)
    try:
        assert size_floor.stacked_refusal({"a": 1.0, "b": 1.0, "c": 0.5}, True) == ""
    finally:
        _restore(old)


def test_overshoot_allowance_same_day_vs_overnight():
    old = _set(OVERNIGHT_HOLD_ENABLED=False, STOP_OVERSHOOT_SAMEDAY_PCT=0.25, STOP_OVERSHOOT_ALLOWANCE_PCT=1.0)
    try:
        assert size_floor.overshoot_pct() == 0.25
        config.OVERNIGHT_HOLD_ENABLED = True
        assert size_floor.overshoot_pct() == 1.0
        config.OVERNIGHT_HOLD_ENABLED = False
        config.STOP_OVERSHOOT_SAMEDAY_PCT = 0.0
        assert size_floor.overshoot_pct() == 0.0
        config.STOP_OVERSHOOT_SAMEDAY_PCT = "junk"
        assert size_floor.overshoot_pct() == 0.0
    finally:
        _restore(old)


def test_defaults_in_config_source():
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert '_og_float("STOP_OVERSHOOT_SAMEDAY_PCT", 0.25)' in src and '_og_float("SIZE_STACK_SKIP_MIN_PENALTIES", 3)' in src


def test_entry_wiring_skips_before_sizing_and_uses_the_helper():
    src = open(os.path.join(ROOT, "entry_engine", "entry.py"), encoding="utf-8").read()
    i = src.index("size_floor.stacked_refusal(_soft_pen, _floor_lifted)")
    j = src.index("max_trade_risk = account_state.equity * (adj_risk_pct / 100.0)")
    assert i < j and 'gate_tag = "soft_stack"' in src[i:j] and "continue" in src[i:j]
    assert "_ovs_pct = size_floor.overshoot_pct()" in src


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                f += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if f else 0)
