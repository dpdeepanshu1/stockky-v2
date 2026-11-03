"""E285 round 156 (plan item 11): the breakeven stop covers round-trip charges plus a small buffer."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import cost_model as ex  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_function(_fn=None):
    config.BREAKEVEN_COVER_CHARGES = True          # conftest turns it off for older exit tests; these tests want the real default


def test_level_is_above_entry_by_charges_plus_buffer_and_below_the_price():
    lv = ex.breakeven_level(500.0, 10, 510.0)
    assert 500.0 < lv < 510.0
    assert lv >= round(500.0 + 5.36 / 10 + 500.0 * 0.05 / 100.0, 2) - 0.02      # charges ~Rs5.4 on a Rs5000 round trip + 0.05 % buffer


def test_never_at_or_above_the_market_falls_back_to_entry():
    assert ex.breakeven_level(500.0, 10, 500.2) == 500.0                          # covered level would be ~500.79 > 500.2


def test_off_or_bad_inputs_give_the_plain_entry():
    old = config.BREAKEVEN_COVER_CHARGES
    try:
        config.BREAKEVEN_COVER_CHARGES = False
        assert ex.breakeven_level(500.0, 10, 520.0) == 500.0
    finally:
        config.BREAKEVEN_COVER_CHARGES = old
    assert ex.breakeven_level(500.0, 0, 520.0) == 500.0 and ex.breakeven_level(0.0, 5, 520.0) == 0.0 and ex.breakeven_level(500.0, None, 520.0) == 500.0


def test_carried_position_pays_more_than_a_same_day_one():
    assert ex.breakeven_level(500.0, 10, 530.0, carried=True) > ex.breakeven_level(500.0, 10, 530.0, carried=False)


def test_wired_into_the_breakeven_block_config_and_catalog():
    src = open(os.path.join(ROOT, "exit_engine", "exit.py"), encoding="utf-8").read()
    assert "be_level = cost_model.breakeven_level(position.avg_entry_price" in src
    cfg = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert "BREAKEVEN_COVER_CHARGES" in cfg and "BREAKEVEN_BUFFER_PCT" in cfg
    cat = open(os.path.join(ROOT, "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("BREAKEVEN_COVER_CHARGES"' in cat and 'Flag("BREAKEVEN_BUFFER_PCT"' in cat


if __name__ == "__main__":
    f = 0
    setup_function()
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                f += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if f else 0)
