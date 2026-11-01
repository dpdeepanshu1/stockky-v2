"""E273 round 24: VWAP-below, spike chase and gain room are size penalties in the Real Trade long entry. Plain runner: python3 tests/test_e273_soft_gates.py (service folder)."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from entry_engine import soft_gates as sg


def setcfg(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def tick(**kw):
    d = dict(symbol="ZZTEST", price=100.0, vwap=100.0, prev_close=100.0, day_high=100.0, day_low=95.0)
    d.update(kw)
    return NS(**d)


def test_vwap_none_penalty_then_refusal():
    o = setcfg(SOFT_GATES_MODE="penalty", ENTRY_VWAP_GATE_ENABLED=True, ENTRY_VWAP_TOLERANCE_PCT=0.15, VWAP_HARD_UNDER_PCT=1.5)
    try:
        assert sg.vwap(tick(price=100.0, vwap=100.0)) == (None, 1.0)
        why, m = sg.vwap(tick(price=99.5, vwap=100.0))                      # 0.5 % under: penalty, no refusal
        assert why is None and 0.40 < m < 0.85
        m2 = sg.vwap(tick(price=98.9, vwap=100.0))[1]                       # 1.1 % under: a bigger penalty
        assert sg.vwap(tick(price=98.9, vwap=100.0))[0] is None and m2 < m
        why3, m3 = sg.vwap(tick(price=98.0, vwap=100.0))                    # 2 % under: past the ceiling
        assert why3 and why3.startswith("VWAP_BELOW") and m3 == 1.0
    finally:
        restore(o)


def test_spike_penalty_below_ceiling_refusal_above():
    o = setcfg(SOFT_GATES_MODE="penalty", SPIKE_GUARD_MODE="live", SPIKE_DAY_PCT=4.0, SPIKE_RANGE_POS=0.85, SPIKE_HARD_MULT=1.75)
    try:
        why, m = sg.spike(tick(price=105.0, prev_close=100.0, day_high=105.0, day_low=100.0))     # +5 % at the high, limit 4 %
        assert why is None and m < 0.85
        why2, m2 = sg.spike(tick(price=108.0, prev_close=100.0, day_high=108.0, day_low=100.0))   # +8 % >= 7 % ceiling
        assert why2 and why2.startswith("SPIKE_CHASE") and m2 == 1.0
        assert sg.spike(tick(price=105.0, prev_close=100.0, day_high=110.0, day_low=100.0)) == (None, 1.0)   # not at the top of its range: no gate fired
    finally:
        restore(o)


def test_gain_room_penalty_and_refusal():
    o = setcfg(SOFT_GATES_MODE="penalty", GAIN_ROOM_MODE="live", GAIN_ROOM_MIN_TARGET_FRAC=0.5, GAIN_ROOM_RANGE_MULT=2.0, GAIN_ROOM_HARD_FRAC=0.25)
    try:
        import entry_engine.day_spike_gate as dg
        dg._atr_pct = lambda t: 2.0                                       # expected high = low x 1.04
        why, m = sg.gain_room(tick(price=103.0, day_low=100.0), 4.0)      # room 0.97 %, needs 2 % (hard room 0.5 %)
        assert why is None and m < 0.85
        why2, m2 = sg.gain_room(tick(price=103.8, day_low=100.0), 4.0)    # room 0.19 % < 0.5 %
        assert why2 and why2.startswith("GAIN_ROOM") and m2 == 1.0
    finally:
        restore(o)
        import importlib, entry_engine.day_spike_gate as dg2
        importlib.reload(dg2)


def test_veto_mode_restores_the_old_refusals():
    o = setcfg(SOFT_GATES_MODE="veto", ENTRY_VWAP_GATE_ENABLED=True, ENTRY_VWAP_TOLERANCE_PCT=0.15)
    try:
        why, m = sg.vwap(tick(price=99.5, vwap=100.0))
        assert why and m == 1.0
        config.SOFT_GATES_MODE = "junk"
        assert sg.mode() == "penalty"
    finally:
        restore(o)


def test_combine_floor_and_ramp():
    o = setcfg(SOFT_GATE_FLOOR=0.25)
    try:
        assert sg.combine([]) == 1.0 and sg.combine([0.8, 0.8]) == 0.64
        assert sg.combine([0.4, 0.4, 0.4]) == 0.25
        assert sg.ramp(0) == 0.85 and sg.ramp(1) == 0.40 and sg.ramp(5) == 0.40 and sg.ramp(-1) == 0.85
    finally:
        restore(o)


def test_wiring_and_catalog():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ent = open(os.path.join(base, "entry_engine", "entry.py")).read()
    assert "soft_gates.vwap(tick)" in ent and "soft_gates.spike(tick)" in ent and "soft_gates.gain_room(tick, target_pct)" in ent
    assert ent.index("_soft_pen = {\"drift_chase\"") < ent.index("soft_gates.combine(") < ent.index("setup_stamp.build(")
    assert "*_soft_pen.items()" in ent
    assert 'Flag("SOFT_GATES_MODE"' in open(os.path.join(base, "control", "catalog.py")).read()


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
