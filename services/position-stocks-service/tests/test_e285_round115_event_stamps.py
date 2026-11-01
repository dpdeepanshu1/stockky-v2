"""E285 round 115: a short entry records the typed (negative) event it saw, so event_stats reports `<type>|DOWN`. Record only: nothing here changes an entry, a size or a learned weight.
Plain-function runner: python3 tests/test_e285_round115_event_stamps.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import event_stats, event_weights, setup_short, setup_stamp

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 2, 10, 5)


def _quality(etype="results", pol=-1, strength=0.7, age=8.0):
    return types.SimpleNamespace(event_type=etype, event_polarity=pol, event_strength=strength, event_age_min=age, announced_at="x")


def test_a_matched_s4_short_stamp_carries_the_event():
    ev = setup_stamp.event_part(_quality())
    out = setup_short.stamp("{}", {"id": "S4_CATALYST_DOWN", "trigger": "t", "confirmations": ["a"], "size_mult": 0.5, "stamp": {"penalties": {}}}, NOW, event=ev)
    st = setup_stamp.read(out)
    assert st["id"] == "S4_CATALYST_DOWN" and st["side"] == "SHORT"
    assert st["event"] == {"type": "results", "pol": -1, "strength": 0.7, "age_min": 8.0}, st


def test_the_legacy_short_stamp_carries_it_too_and_no_event_means_no_key():
    ev = setup_stamp.event_part(_quality("deal", -1, 0.4, 20.0))
    st = setup_stamp.read(setup_short.stamp(None, {"id": None, "skip": "NO_SETUP"}, NOW, event=ev))
    assert st["trigger"] == "shortlist" and st["event"]["type"] == "deal" and st["event"]["pol"] == -1
    st2 = setup_stamp.read(setup_short.stamp(None, None, NOW))
    assert "event" not in st2                                                    # the old call (no event argument) is unchanged
    st3 = setup_stamp.read(setup_short.stamp(None, None, NOW, event=setup_stamp.event_part(_quality(pol=0))))
    assert "event" not in st3                                                    # unknown polarity is not recorded


def test_stamp_off_leaves_flow_json_alone():
    import config
    saved = getattr(config, "SETUP_STAMP_ENABLED", True)
    config.SETUP_STAMP_ENABLED = False
    try:
        assert setup_short.stamp("{\"a\":1}", None, NOW, event={"type": "x", "pol": -1}) == "{\"a\":1}"
    finally:
        config.SETUP_STAMP_ENABLED = saved


def _closed(etype, pol, pnl, n):
    rows = []
    for k in range(n):
        st = setup_stamp.build(setup_id="S4_CATALYST_DOWN", side="SHORT", now=NOW, event={"type": etype, "pol": pol, "strength": 0.6, "age_min": 10.0})
        rows.append({"flow_json": json.dumps({"setup": st}), "realized_pnl": pnl, "entry_price": 100.0, "stop_price": 101.0, "quantity": 100.0,
                     "opened_at": f"2026-10-{(k % 27) + 1:02d} 10:05:00"})
    return rows


def test_event_stats_reports_the_short_under_its_own_down_key():
    out = event_stats.summarize(_closed("results", -1, 150.0, 4) + _closed("results", 1, -50.0, 3))
    assert out["results|DOWN"]["n"] == 4 and out["results|UP"]["n"] == 3, out.keys()
    assert out["results|DOWN"]["verdict"] == "NOT_ENOUGH_DATA"


def test_learned_event_weights_never_read_a_short():
    import config
    saved = (getattr(config, "EVENT_WEIGHTS_LEARNED_ENABLED", True), getattr(config, "ADAPTIVE_ENTRY_PARAMS", True))
    config.EVENT_WEIGHTS_LEARNED_ENABLED, config.ADAPTIVE_ENTRY_PARAMS = True, True
    try:
        event_weights.reset()
        event_weights.update(_closed("results", 1, 500.0, 40))                   # control: 40 winning LONGS on a positive event do move its weight
        assert event_weights.mult("results", 1) > 1.0
        event_weights.reset()
        event_weights.update(_closed("results", -1, 500.0, 40))                  # 40 winning SHORTS on a negative event must not touch it
        assert event_weights.mult("results", 1) == 1.0
        assert event_weights.mult("results", -1) == 1.0                          # and a negative event is never touched
    finally:
        event_weights.reset()
        config.EVENT_WEIGHTS_LEARNED_ENABLED, config.ADAPTIVE_ENTRY_PARAMS = saved


def test_the_short_plan_and_the_stamp_call_are_wired():
    se = open(os.path.join(_HERE, "orders", "short_entry.py"), encoding="utf-8").read()
    assert "_sst.event_part(quality)" in se and '"event": _event' in se
    sl = open(os.path.join(_HERE, "orders", "short_live.py"), encoding="utf-8").read()
    assert 'event=plan.get("event")' in sl


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
