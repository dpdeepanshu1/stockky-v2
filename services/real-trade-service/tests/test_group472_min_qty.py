"""tests/test_group472_min_qty.py - ENTRY_MIN_QTY (group 472): shipped default and wiring present. Behaviour runs in the evaluate_mode suite on CI."""
import os


def _src(name):
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)).read()


def test_default_is_three():
    assert '_og_float("ENTRY_MIN_QTY", 3)' in _src("config.py")


def test_gate_is_wired_before_the_cost_gate():
    s = _src("entry_engine/entry.py")
    a, b = s.index('gate_tag = "min_qty"'), s.index("Gate 5.6: transaction-cost")
    assert a < b and 'mode == "REAL" and _min_q > 0' in s
