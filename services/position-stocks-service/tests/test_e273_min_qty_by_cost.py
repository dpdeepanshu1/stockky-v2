"""E273 round 6: ENTRY_MIN_QTY by cost gate. Plain runner: python3 tests/test_e273_min_qty_by_cost.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from orders import cost_gate as cg


def setcfg(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def test_cost_mode_lets_a_small_trade_through_when_its_edge_pays():
    o = setcfg(ENTRY_MIN_QTY_MODE="cost", SCALP_COST_GATE_ENABLED=True)
    try:
        assert cg.effective_min_qty(1500.0, 2.5, 3) == 1                 # a Rs 1,500 share at a 2.5 % target: one share already pays for its costs
    finally:
        restore(o)


def test_cost_mode_keeps_the_floor_when_nothing_below_it_passes():
    o = setcfg(ENTRY_MIN_QTY_MODE="cost", SCALP_COST_GATE_ENABLED=True, SCALP_MIN_EDGE_TO_COST_RATIO=1000.0)
    try:
        assert cg.effective_min_qty(1500.0, 2.5, 3) == 3
    finally:
        restore(o)


def test_fixed_mode_and_off_switches_keep_the_flat_floor():
    o = setcfg(ENTRY_MIN_QTY_MODE="fixed", SCALP_COST_GATE_ENABLED=True)
    try:
        assert cg.effective_min_qty(1500.0, 2.5, 3) == 3
        config.ENTRY_MIN_QTY_MODE = "cost"; config.SCALP_COST_GATE_ENABLED = False
        assert cg.effective_min_qty(1500.0, 2.5, 3) == 3                 # no cost gate = nothing to judge by
        config.SCALP_COST_GATE_ENABLED = True
        assert cg.effective_min_qty(1500.0, 2.5, 0) == 0 and cg.effective_min_qty(1500.0, 2.5, 1) == 1
        assert cg.effective_min_qty(0.0, 2.5, 3) == 3 and cg.effective_min_qty(1500.0, 0.0, 3) == 3 and cg.effective_min_qty("x", 2.5, 3) == 3
        config.ENTRY_MIN_QTY_MODE = "junk"
        assert cg.min_qty_mode() == "cost"
    finally:
        restore(o)


def test_never_above_the_flat_floor():
    o = setcfg(ENTRY_MIN_QTY_MODE="cost", SCALP_COST_GATE_ENABLED=True)
    try:
        for px in (50.0, 400.0, 1500.0, 9000.0):
            assert 1 <= cg.effective_min_qty(px, 2.0, 3) <= 3
    finally:
        restore(o)


def test_wiring_and_defaults():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert "cost_gate.effective_min_qty(candidate.current_ltp, levels.target_pct" in open(os.path.join(base, "orders", "entry.py")).read()
    assert 'ENTRY_MIN_QTY_MODE = _get_str("ENTRY_MIN_QTY_MODE", "cost")' in open(os.path.join(base, "config.py")).read()
    assert 'Flag("ENTRY_MIN_QTY_MODE"' in open(os.path.join(base, "control", "catalog.py")).read()


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
