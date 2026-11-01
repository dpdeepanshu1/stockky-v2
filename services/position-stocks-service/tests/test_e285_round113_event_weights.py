"""E285 round 113: learned weights for POSITIVE news event types (orders/event_weights) and where they are read.
Plain-function runner: python3 tests/test_e285_round113_event_weights.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import event_weights, setup_stamp

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEYS = ("EVENT_WEIGHTS_LEARNED_ENABLED", "ADAPTIVE_ENTRY_PARAMS", "EVENT_WEIGHT_MIN_MULT", "EVENT_WEIGHT_MAX_MULT")


def _on(**kw):
    saved = {k: getattr(config, k, None) for k in _KEYS}
    config.EVENT_WEIGHTS_LEARNED_ENABLED, config.ADAPTIVE_ENTRY_PARAMS = True, True
    config.EVENT_WEIGHT_MIN_MULT, config.EVENT_WEIGHT_MAX_MULT = 0.6, 1.2
    for k, v in kw.items():
        setattr(config, k, v)
    event_weights.reset()
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(config, k, v)
    event_weights.reset()


def _row(etype, pol, pnl, day):
    st = setup_stamp.build(setup_id="S1_CATALYST_UP", now=datetime(2026, 11, 2, 9, 40), event={"type": etype, "pol": pol, "strength": 0.7, "age_min": 10.0})
    return {"flow_json": json.dumps({"setup": st}), "realized_pnl": pnl, "entry_price": 100.0, "stop_price": 99.0, "quantity": 100.0, "opened_at": f"2026-11-{day:02d} 09:40:00"}


def _rows(etype, pol, n, pnl, days=10):
    return [_row(etype, pol, pnl, 1 + (i % days)) for i in range(n)]            # risk is Rs 100 (1 x 100), so pnl 30 = +0.3 R


def test_under_thirty_trades_is_no_effect():
    saved = _on()
    try:
        event_weights.update(_rows("results", 1, 29, 30.0))
        assert event_weights.mult("results", 1) == 1.0 and event_weights.apply("results", 1, 0.8) == 0.8
        assert event_weights.snapshot()["types"]["results"]["source"] == "constant"
    finally:
        _restore(saved)


def test_a_winning_type_is_lifted_a_losing_type_is_cut_both_shrunk():
    saved = _on()
    try:
        event_weights.update(_rows("results", 1, 30, 30.0) + _rows("rating", 1, 30, -40.0))
        w = 30 / 60.0
        assert abs(event_weights.mult("results", 1) - (w * 1.3 + (1 - w) * 1.0)) < 1e-3, event_weights.snapshot()      # +0.3 R -> 1.3 learned, half weight at 30 trades
        assert abs(event_weights.mult("rating", 1) - (w * 0.6 + (1 - w) * 1.0)) < 1e-3                                  # -0.4 R -> 0.6 learned
        assert event_weights.apply("results", 1, 0.5) > 0.5 and event_weights.apply("rating", 1, 0.5) < 0.5
        assert event_weights.mult("order", 1) == 1.0                                                                    # a type with no trades
    finally:
        _restore(saved)


def test_hard_band_and_strength_cap():
    saved = _on(EVENT_WEIGHT_MAX_MULT=1.05, EVENT_WEIGHT_MIN_MULT=0.9)
    try:
        event_weights.update(_rows("results", 1, 120, 90.0) + _rows("rating", 1, 120, -90.0))
        assert abs(event_weights.mult("results", 1) - 1.05) < 1e-9 and abs(event_weights.mult("rating", 1) - 0.9) < 1e-9
        assert event_weights.apply("results", 1, 0.99) == 1.0                                                           # never above 1
    finally:
        _restore(saved)


def test_negative_events_are_never_touched():
    saved = _on()
    try:
        event_weights.update(_rows("regulatory", -1, 60, -80.0) + _rows("regulatory", 1, 60, -80.0))
        assert "regulatory" in event_weights.snapshot()["types"]                                                        # the UP side was learned
        assert event_weights.mult("regulatory", -1) == 1.0 and event_weights.apply("regulatory", -1, 0.9) == 0.9        # a veto is never weakened
        assert event_weights.mult("regulatory", 0) == 1.0 and event_weights.mult("regulatory", True) == 1.0
        assert event_weights.mult("regulatory", 1) < 1.0
    finally:
        _restore(saved)


def test_switch_master_and_junk_never_raise():
    saved = _on()
    try:
        event_weights.update(_rows("results", 1, 60, 30.0))
        assert event_weights.mult("results", 1) > 1.0
        config.EVENT_WEIGHTS_LEARNED_ENABLED = False
        assert event_weights.mult("results", 1) == 1.0 and not event_weights.snapshot()["enabled"]
        config.EVENT_WEIGHTS_LEARNED_ENABLED, config.ADAPTIVE_ENTRY_PARAMS = True, False                                 # the master switch wins
        assert event_weights.mult("results", 1) == 1.0
        config.ADAPTIVE_ENTRY_PARAMS = True
        assert event_weights.update(None) >= 0 and event_weights.update([{"flow_json": "junk"}, None, 5]) >= 0         # a bad read never raises
        assert event_weights.apply("results", 1, "x") == "x" and event_weights.apply(None, None, None) is None
    finally:
        _restore(saved)


def test_quality_gate_and_premarket_plan_read_the_learned_weight():
    saved = _on()
    try:
        src = lambda *p: open(os.path.join(_HERE, "..", *p), encoding="utf-8").read()  # noqa: E731
        assert "event_weights.apply" in src("screening", "quality_gate.py") and "event_weights.apply" in src("screening", "premarket_plan.py")
        assert "event_weights.update" in src("orders", "slot_ladder.py") and "event_weights" in src("main.py")
        assert "ADAPTIVE_ENTRY_PARAMS" in src("orders", "event_weights.py")
        import ast
        tree = ast.parse(src("screening", "quality_gate.py"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_typed_event_from")
        ns: dict = {"Optional": object, "dict": dict}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "quality_gate._typed_event_from", "exec"), ns)       # that one function, without the module's httpx / sqlalchemy imports
        typed = ns["_typed_event_from"]
        body = {"ok": True, "events": [{"type": "results", "polarity": 1, "strength": 0.5, "age_s": 60, "published": "x"}]}
        assert typed(body)["strength"] == 0.5
        event_weights.update(_rows("results", 1, 60, 30.0))
        assert typed(body)["strength"] > 0.5
        neg = {"ok": True, "events": [{"type": "results", "polarity": -1, "strength": 0.5, "age_s": 60, "published": "x"}]}
        assert typed(neg)["strength"] == 0.5
    finally:
        _restore(saved)


def test_slot_ladder_load_fills_the_cache():
    saved = _on()
    try:
        from orders import slot_ladder
        import types
        rows = []
        for r in _rows("results", 1, 40, 30.0):
            rows.append(types.SimpleNamespace(flow_json=r["flow_json"], realized_pnl=r["realized_pnl"], entry_price=100.0, stop_price=99.0, quantity=100.0,
                                              opened_at=None, max_price_seen=None, min_price_seen=None, side="LONG"))

        class _Q:
            def filter(self, *a, **k): return self
            def order_by(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def all(self): return rows

        class _DB:
            def query(self, *a): return _Q()

        try:
            slot_ladder._load(_DB())
        except ImportError:
            return                                                           # models need sqlalchemy: the VM run covers it
        assert event_weights.mult("results", 1) > 1.0
    finally:
        _restore(saved)


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e)); traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
