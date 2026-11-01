"""E273 round 3: BELOW_OPEN is a soft penalty. Plain runner: python3 tests/test_e273_below_open_penalty.py"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

import config
from screening import opening_gate as og, prev_day
from orders import setup_live as sl, setup_engine as se

STATS = {"v": (100.0, 103.0, 99.0, 99.0)}          # open, high, low, prev_close


def _setup(mode="penalty", atr_pct=3.0, active=True):
    fw = types.ModuleType("feed.ws_client"); fw.get_day_stats = lambda s: STATS["v"]; fw.get_tick_buffer = lambda s: []
    feed = sys.modules.get("feed") or types.ModuleType("feed")
    feed.ws_client = fw
    sys.modules["feed"], sys.modules["feed.ws_client"] = feed, fw
    og.is_active = lambda now=None: active
    prev_day.peek = lambda s: (types.SimpleNamespace(atr_pct=atr_pct) if atr_pct else None)
    config.OPENING_BELOW_OPEN_MODE = mode


def test_penalty_is_the_drop_in_units_of_the_stocks_own_noise():
    _setup(atr_pct=3.0)                                   # unit = 0.1 x 3 % = 0.3 %
    r = og.below_open_penalty("ABC", 99.7)                # 0.3 % under the open
    assert abs(r["penalty"] - 1.0) < 0.01 and abs(r["pct"] - 0.3) < 0.01
    _setup(atr_pct=1.0)                                   # a calmer stock: unit 0.1 %, same drop = 3 units
    assert abs(og.below_open_penalty("ABC", 99.7)["penalty"] - 3.0) < 0.05


def test_fallback_unit_without_atr_and_none_when_not_below_open():
    _setup(atr_pct=None)
    assert abs(og.below_open_penalty("ABC", 99.7)["penalty"] - 1.0) < 0.01     # fallback 0.3 %
    assert og.below_open_penalty("ABC", 100.0) is None and og.below_open_penalty("ABC", 101.0) is None


def test_veto_mode_and_inactive_gate_give_no_penalty():
    _setup(mode="veto"); assert og.below_open_penalty("ABC", 99.5) is None
    _setup(active=False); assert og.below_open_penalty("ABC", 99.5) is None
    _setup(); STATS["v"] = None; assert og.below_open_penalty("ABC", 99.5) is None; STATS["v"] = (100.0, 103.0, 99.0, 99.0)


def test_unknown_mode_falls_back_to_penalty():
    config.OPENING_BELOW_OPEN_MODE = "weird"; assert og.below_open_mode() == "penalty"
    config.OPENING_BELOW_OPEN_MODE = "VETO"; assert og.below_open_mode() == "veto"


def test_penalty_size_floor_and_skip_threshold():
    assert sl.penalty_size(None) is None and sl.penalty_size({}) is None and sl.penalty_size({"below_open": 0}) is None
    assert sl.penalty_size({"below_open": 1.0}) == 0.5
    assert sl.penalty_size({"below_open": 2.0}) == round(1 / 3.0, 4)
    assert sl.penalty_size({"below_open": 3.9}) == se.SIZE_FLOOR          # 1/4.9 = 0.204 -> floor
    assert se.PENALTY_SKIP == 4.0


def test_a_matched_setup_carries_the_penalty_in_its_own_size():
    fj = '{"orb":{"range_high":100.0,"close_trigger":true,"trigger_close":100.6}}'
    kw = dict(symbol="A", price=100.8, now=datetime(2026, 11, 2, 10, 15), event_polarity=1, flow_json=fj,
              quote={"previous_close": 100.0, "avg_price": 100.2}, gap_pct=0.5, tape={"reliable": True, "buy_pct_10m": 62, "delta_agrees": True})
    assert sl.classify_long(**kw)["size_mult"] == 0.5
    r = sl.classify_long(penalties={"below_open": 1.0}, **kw)
    assert r["id"] == se.S1 and r["size_mult"] == 0.25 and r["stamp"]["penalties"] == {"below_open": 1.0}
    assert sl.classify_long(penalties={"below_open": 4.0}, **kw)["skip"] == "PENALTY_TOO_HIGH"


def test_gate_no_longer_refuses_below_open_in_penalty_mode_by_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    g = open(os.path.join(root, "screening", "opening_gate.py")).read()
    assert 'if ltp < day_open and below_open_mode() == "veto"' in g and "BELOW_PREV_CLOSE" in g
    e = open(os.path.join(root, "orders", "entry.py")).read()
    assert e.index("opening_gate.below_open_penalty") < e.index("setup_live.classify_long") < e.index("setup_live.extra_factor")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print("ok", f.__name__)
    print(len(fns), "passed")
