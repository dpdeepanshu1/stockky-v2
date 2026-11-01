"""E273 round 27: Real Trade's opening gate - a price under the previous close is a SIZE PENALTY down to OPENING_GATE_HARD_CHANGE_PCT, refused below it.
Plain runner: python3 tests/test_e273_below_prev_close_penalty.py (service folder). Stdlib only."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import config  # noqa: E402
from entry_engine import opening_gate as og  # noqa: E402
from test_e273_opening_gate_penalty import Env, NOW  # noqa: E402


def tk(price):
    return SimpleNamespace(price=price, prev_close=100.0, day_high=max(100.5, price + 0.5), day_low=price - 1.0, atr=2.0, symbol="ABC")


def test_inside_the_limit_nothing_happens():
    e = Env()
    try:
        t = tk(99.7)                                       # -0.3 %, inside -0.5 %
        assert og.reject_reason("REAL", t, NOW) is None and og.soft_down_reason("REAL", t, NOW) is None
        assert og.penalty("REAL", t, NOW) == {"mult": 1.0, "reasons": []}
    finally:
        e.done()


def test_between_limit_and_ceiling_is_a_penalty_not_a_refusal():
    e = Env()
    try:
        t = tk(99.0)                                       # -1.0 %
        assert og.reject_reason("REAL", t, NOW) is None
        assert og.soft_down_reason("REAL", t, NOW).startswith("OPENING_GATE:BELOW_PREV_CLOSE:-1.00%")
        p = og.penalty("REAL", t, NOW)
        assert 0.40 <= p["mult"] < 0.85 and p["reasons"][0].startswith("below_prev_close")
        deeper = og.penalty("REAL", tk(98.2), NOW)["mult"]  # -1.8 %: closer to the ceiling = smaller size
        assert deeper < p["mult"] >= 0.40 and deeper >= 0.40
    finally:
        e.done()


def test_past_the_ceiling_is_refused():
    e = Env()
    try:
        t = tk(97.5)                                       # -2.5 % < -2.0 %
        r = og.reject_reason("REAL", t, NOW)
        assert r.startswith("OPENING_GATE:BELOW_PREV_CLOSE") and og.soft_down_reason("REAL", t, NOW) is None
    finally:
        e.done()


def test_penalty_switch_off_restores_the_old_refusal():
    e = Env(OPENING_GATE_PENALTY_ENABLED=False)
    try:
        t = tk(99.0)
        assert og.reject_reason("REAL", t, NOW).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
        assert og.soft_down_reason("REAL", t, NOW) is None and og.penalty("REAL", t, NOW)["mult"] == 1.0
    finally:
        e.done()


def test_combines_with_other_opening_penalties_and_respects_floor():
    e = Env(close_pos=0.12)                                 # weak previous close (penalty) + under previous close (penalty)
    try:
        p = og.penalty("REAL", tk(98.3), NOW)
        assert len(p["reasons"]) == 2 and config.OPENING_GATE_PENALTY_FLOOR <= p["mult"] < 0.5
    finally:
        e.done()


def test_config_default_and_wiring_marker():
    assert config.OPENING_GATE_HARD_CHANGE_PCT == -2.0
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "soft_down_reason" in src and "round 27" in src


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", n, repr(ex))
    sys.exit(1 if fails else 0)
