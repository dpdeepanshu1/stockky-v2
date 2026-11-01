"""E273 round 28: Real Trade entry-drift chase check is a SIZE PENALTY up to DRIFT_HARD_MULT x its limit. Plain runner: python3 tests/test_e273_drift_penalty.py (service folder)."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import soft_gates as sg  # noqa: E402

WHY = "Price ran +1.5% above signal (limit 1.0%). Chasing - skip."


def test_passed_check_has_no_penalty():
    assert sg.drift("", 0.4, 1.0) == (None, 1.0) and sg.drift(None, 0.4, 1.0) == (None, 1.0)


def test_between_limit_and_ceiling_is_a_penalty():
    why, m = sg.drift(WHY, 1.2, 1.0)
    assert why is None and 0.40 < m < 0.85
    why2, m2 = sg.drift(WHY, 1.6, 1.0)
    assert why2 is None and 0.40 <= m2 < m


def test_ceiling_refuses_with_the_old_reason():
    ceil = 1.0 * config.DRIFT_HARD_MULT
    assert sg.drift(WHY, ceil, 1.0) == (WHY, 1.0) and sg.drift(WHY, ceil + 2, 1.0) == (WHY, 1.0)


def test_veto_mode_and_bad_data_keep_the_refusal():
    old = config.SOFT_GATES_MODE
    config.SOFT_GATES_MODE = "veto"
    try:
        assert sg.drift(WHY, 1.2, 1.0) == (WHY, 1.0)
    finally:
        config.SOFT_GATES_MODE = old
    assert sg.drift(WHY, -1.2, 1.0) == (WHY, 1.0)          # a fall below the signal is not a chase
    assert sg.drift(WHY, 1.2, 0.0) == (WHY, 1.0) and sg.drift(WHY, "x", 1.0) == (WHY, 1.0)


def test_default_and_wiring():
    assert config.DRIFT_HARD_MULT == 1.75
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "soft_gates.drift(" in src and '"drift_chase": _drift_pen' in src


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", n, repr(ex))
    sys.exit(1 if fails else 0)
