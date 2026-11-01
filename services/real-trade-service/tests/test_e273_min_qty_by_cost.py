"""E273 round 23: Real Trade's ENTRY_MIN_QTY follows the cost gate. Plain runner: python3 tests/test_e273_min_qty_by_cost.py (service folder)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import cost_model as cm


def setcfg(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def setenv_cost():
    return setcfg(ENTRY_MIN_QTY_MODE="cost", COST_MODEL_ENABLED=True)


def setenv_fixed():
    return setcfg(ENTRY_MIN_QTY_MODE="fixed", COST_MODEL_ENABLED=True)


def test_cost_mode_lets_a_small_trade_through_when_its_edge_pays():
    o = setenv_cost()
    try:
        q = cm.effective_min_qty(1500.0, 4.0, 3, min_trade_value=0.0, min_edge_to_cost_ratio=2.0)
        assert 1 <= q < 3                                                   # a Rs 1,500 share at a 4 % target: one or two shares already pay for the costs
    finally:
        restore(o)


def test_cost_mode_keeps_the_floor_when_nothing_below_it_passes():
    o = setenv_cost()
    try:
        assert cm.effective_min_qty(1500.0, 4.0, 3, min_trade_value=0.0, min_edge_to_cost_ratio=1000.0) == 3
        assert cm.effective_min_qty(1500.0, 4.0, 3, min_trade_value=1e9, min_edge_to_cost_ratio=0.0) == 3   # the minimum trade value is part of the gate
    finally:
        restore(o)


def test_fixed_mode_and_off_switches_keep_the_flat_floor():
    o = setenv_fixed()
    try:
        assert cm.effective_min_qty(1500.0, 4.0, 3, 0.0, 2.0) == 3
        config.ENTRY_MIN_QTY_MODE = "cost"; config.COST_MODEL_ENABLED = False
        assert cm.effective_min_qty(1500.0, 4.0, 3, 0.0, 2.0) == 3          # no cost model = nothing to judge by
        config.COST_MODEL_ENABLED = True
        assert cm.effective_min_qty(1500.0, 4.0, 0) == 0 and cm.effective_min_qty(1500.0, 4.0, 1) == 1
        assert cm.effective_min_qty(0.0, 4.0, 3) == 3 and cm.effective_min_qty(1500.0, 0.0, 3) == 3 and cm.effective_min_qty("x", 4.0, 3) == 3
        config.ENTRY_MIN_QTY_MODE = "junk"
        assert cm.min_qty_mode() == "cost"
    finally:
        restore(o)


def test_never_above_the_flat_floor_and_never_below_one():
    o = setenv_cost()
    try:
        for px in (50.0, 400.0, 1500.0, 9000.0):
            assert 1 <= cm.effective_min_qty(px, 3.0, 3, 0.0, 2.0) <= 3
    finally:
        restore(o)


def test_a_quantity_that_passes_here_passes_the_entry_gate_for_either_product():
    o = setenv_cost()
    try:
        for px in (300.0, 900.0, 2500.0):
            q = cm.effective_min_qty(px, 3.5, 3, 0.0, 2.0)
            for prod, dp in (("CNC", True), ("CNC", False), ("MIS", False)):
                if q < 3:
                    assert cm.evaluate_entry_cost_gate(px, q, 3.5, product_type=prod, min_trade_value=0.0, min_edge_to_cost_ratio=2.0, include_dp=dp).passes, (px, q, prod)
    finally:
        restore(o)


def test_wiring_and_defaults():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ent = open(os.path.join(base, "entry_engine", "entry.py")).read()
    assert "cost_model.effective_min_qty(" in ent
    assert ent.index("cost_model.effective_min_qty(") < ent.index('gate_tag = "min_qty"') < ent.index("Gate 5.6: transaction-cost")
    assert 'ENTRY_MIN_QTY_MODE = ((os.getenv("ENTRY_MIN_QTY_MODE")' in open(os.path.join(base, "config.py")).read()
    assert 'Flag("ENTRY_MIN_QTY_MODE"' in open(os.path.join(base, "control", "catalog.py")).read()
    assert config.ENTRY_MIN_QTY_MODE == "cost"


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
