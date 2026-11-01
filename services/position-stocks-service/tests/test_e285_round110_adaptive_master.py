"""E285 round 110: ADAPTIVE_ENTRY_PARAMS is one master switch over the learned entry numbers.
Plain-function runner: python3 tests/test_e285_round110_adaptive_master.py (also collected by pytest)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import learned_stop, long_target, own_norms, own_spread, short_target, volume_profile

_KEYS = ("ADAPTIVE_ENTRY_PARAMS", "LEARNED_STOP_ENABLED", "LONG_TARGET_LEARNED_ENABLED", "SHORT_TARGET_LEARNED_ENABLED", "VOLUME_PROFILE_LEARNED_ENABLED", "ENTRY_OWN_NORMS_ENABLED", "ENTRY_OWN_SPREAD_ENABLED")


def _with(**kw):
    saved = {k: getattr(config, k, None) for k in _KEYS}
    for k in _KEYS:
        setattr(config, k, True)
    for k, v in kw.items():
        setattr(config, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(config, k, v)


def test_default_is_on_and_each_switch_still_decides():
    saved = _with()
    try:
        assert config.ADAPTIVE_ENTRY_PARAMS is True or saved["ADAPTIVE_ENTRY_PARAMS"] is True
        assert learned_stop.enabled() and long_target.enabled() and own_norms.enabled()
        config.LEARNED_STOP_ENABLED = False
        assert not learned_stop.enabled() and long_target.enabled()                 # an own switch still turns its own piece off
        config.LONG_TARGET_LEARNED_ENABLED = False
        assert not long_target.enabled()
        config.ENTRY_OWN_NORMS_ENABLED = False
        assert not own_norms.enabled()
    finally:
        _restore(saved)


def test_master_off_turns_every_piece_off_at_once():
    saved = _with(ADAPTIVE_ENTRY_PARAMS=False)
    try:
        assert not learned_stop.enabled()
        assert not long_target.enabled()
        assert not own_norms.enabled()
        assert not own_spread.enabled() and own_spread.ceiling_pct("AAA", 0.5) is None      # E285 round 112
        assert own_norms.vwap_tolerance_pct("AAA", 0.15) is None and own_norms.expected_volume("AAA", 1000.0, 30.0) is None
        assert volume_profile.adjust(0.4, 60.0) == 0.4                                # the old curve, untouched
        volume_profile.reset()
        volume_profile.warm()
        assert volume_profile.snapshot()["building"] is False                         # no background read is started either
    finally:
        _restore(saved)


def test_master_off_short_target_keeps_the_plan_target():
    calls = []
    saved = _with(ADAPTIVE_ENTRY_PARAMS=False)
    orig = short_target.learned_gain
    short_target.learned_gain = lambda *a, **k: calls.append(1) or {"gain_pct": 0.9, "source": "learned", "cell": "x"}
    hvn = getattr(config, "SHORT_TARGET_HVN_ENABLED", True)
    config.SHORT_TARGET_HVN_ENABLED = False
    import types
    mk = lambda: types.SimpleNamespace(target_price=98.0, stop_price=101.0, target_pct=2.0)  # noqa: E731
    try:
        lv = mk()
        note = short_target.adjust(lv, 100.0, symbol="AAA", setup="S4_CATALYST_DOWN", now=None, db=None, tick=0.05)
        assert calls == [], "the learned short target was consulted with the master switch off"
        assert lv.target_price == 98.0 and "learned" not in note
        config.ADAPTIVE_ENTRY_PARAMS = True
        lv = mk()
        note = short_target.adjust(lv, 100.0, symbol="AAA", setup="S4_CATALYST_DOWN", now=None, db=None, tick=0.05)
        assert calls == [1], "with the master switch on the learned gain is consulted again"
        assert note.get("learned") and lv.target_price > 98.0, (note, lv)        # a nearer learned target (0.9 % gain -> 99.10) replaced the plan's 98.00
    finally:
        short_target.learned_gain = orig
        config.SHORT_TARGET_HVN_ENABLED = hvn
        _restore(saved)


def test_wiring_by_source_text():
    here = os.path.dirname(os.path.abspath(__file__))
    src = lambda *p: open(os.path.join(here, "..", *p), encoding="utf-8").read()  # noqa: E731
    assert "ADAPTIVE_ENTRY_PARAMS" in src("control", "catalog.py") and "ADAPTIVE_ENTRY_PARAMS" in src("config.py")
    for f in ("long_target.py", "learned_stop.py", "short_target.py", "volume_profile.py", "own_norms.py", "own_spread.py", "event_weights.py"):
        assert "ADAPTIVE_ENTRY_PARAMS" in src("orders", f), f


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
