"""E286 round 216: a live price already ABOVE the cached resistance is sized down, not refused (entry_engine/soft_gates.structure). Plain runner: python3 tests/test_e286_room_beyond.py."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from entry_engine import soft_gates


def _ver(room, extra=()):
    txt = f"only {max(0.0, room):.1f} ATR of room to the next resistance (min 0.5)" + (f" [raw {room:.1f}: live price is {abs(room):.1f} ATR beyond the cached resistance - possible breakout]" if room < 0 else "")
    return {"would_reject": True, "reasons": [txt, *extra], "reason": "STRUCTURE: good stock, bad entry - " + txt, "metrics": {"room_atr": room, "chase_atr": 0.2, "stretch_atr": 0.5, "rr": 3.0}}


def _set(monkeypatch, mult):
    monkeypatch.setattr(config, "STRUCTURE_ROOM_BEYOND_MULT", mult, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_MIN_ROOM_ATR", 0.5, raising=False)
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "penalty", raising=False)


class _MP:
    def __init__(self):
        self._u = []

    def setattr(self, o, k, v, raising=True):
        self._u.append((o, k, getattr(o, k, None), hasattr(o, k)))
        setattr(o, k, v)

    def undo(self):
        for o, k, old, had in reversed(self._u):
            setattr(o, k, old) if had else delattr(o, k)


def test_price_beyond_resistance_is_a_size_cut_not_a_refusal(monkeypatch):
    _set(monkeypatch, 0.6)
    why, pens = soft_gates.structure(_ver(-0.4))
    assert why is None and pens == {"structure_room": 0.6}


def test_switch_off_keeps_the_old_refusal(monkeypatch):
    _set(monkeypatch, 0.0)
    why, pens = soft_gates.structure(_ver(-0.4))
    assert why and not pens


def test_price_under_resistance_with_almost_no_room_still_refused(monkeypatch):
    _set(monkeypatch, 0.6)
    why, pens = soft_gates.structure(_ver(0.1))                       # under a quarter of the 0.5 floor, not beyond
    assert why and not pens


def test_veto_mode_and_hard_reasons_still_refuse(monkeypatch):
    _set(monkeypatch, 0.6)
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "veto", raising=False)
    assert soft_gates.structure(_ver(-0.4))[0]
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "penalty", raising=False)
    why, pens = soft_gates.structure(_ver(-0.4, extra=("breakout 2 bar(s) ago came on thin volume (0.80x the 20-day average, wants 1.2x)",)))
    assert why and not pens                                           # thin-volume breakout is a hard reason


def test_chase_past_its_ceiling_still_refuses_even_beyond_resistance(monkeypatch):
    _set(monkeypatch, 0.6)
    monkeypatch.setattr(config, "STRUCTURE_MAX_CHASE_ATR", 1.5, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_HARD_MULT", 1.75, raising=False)
    v = _ver(-0.4, extra=("chase 3.0 ATR above the nearest demand/imbalance zone (max 1.5)",))
    v["metrics"]["chase_atr"] = 3.0
    why, pens = soft_gates.structure(v)
    assert why and not pens


def test_config_default():
    assert config.STRUCTURE_ROOM_BEYOND_MULT == 0.6


if __name__ == "__main__":
    import inspect
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_") and callable(f):
            if inspect.signature(f).parameters:
                mp = _MP()
                try:
                    f(mp)
                finally:
                    mp.undo()
            else:
                f()
            n += 1
    print(f"{n} passed")
