"""E285 round 106: the entry stamps the typed event it saw, and event_stats reports follow-through per event type (record only).
Plain-function runner: python3 tests/test_e285_round106_event_stats.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import event_stats, setup_stamp

_HERE = os.path.dirname(os.path.abspath(__file__))


def _row(etype="results", pol=1, age=10.0, strength=0.7, pnl=100.0, day=2, risk_pnl=100.0):
    st = setup_stamp.build(setup_id="S1_CATALYST_UP", now=datetime(2026, 11, 2, 9, 40), event={"type": etype, "pol": pol, "strength": strength, "age_min": age})
    return {"flow_json": json.dumps({"setup": st}), "realized_pnl": pnl, "entry_price": 100.0, "stop_price": 99.0, "quantity": risk_pnl / 1.0,
            "opened_at": f"2026-11-{day:02d} 09:40:00"}


def test_stamp_carries_the_event_from_a_dict_and_from_a_quality_object():
    st = setup_stamp.build(event={"type": "Order", "pol": 1, "strength": 0.6234, "age_min": 12.0})
    assert st["event"] == {"type": "order", "pol": 1, "strength": 0.623, "age_min": 12.0}
    q = types.SimpleNamespace(event_type="results", event_polarity=-1, event_strength=0.8, event_age_min=3.5, announced_at="x")
    assert setup_stamp.build(event=q)["event"] == {"type": "results", "pol": -1, "strength": 0.8, "age_min": 3.5}


def test_no_event_no_key_and_bad_input_never_raises():
    assert "event" not in setup_stamp.build()
    assert "event" not in setup_stamp.build(event=None)
    assert "event" not in setup_stamp.build(event=types.SimpleNamespace(event_type=None, event_polarity=None, event_strength=None, event_age_min=None))
    assert "event" not in setup_stamp.build(event={"type": "results", "pol": 0})          # unknown polarity is not recorded
    assert "event" not in setup_stamp.build(event="junk")
    st = setup_stamp.build(event={"type": "deal", "pol": 1, "strength": "x", "age_min": float("nan")})
    assert st["event"] == {"type": "deal", "pol": 1}


def test_an_old_stamp_without_event_is_ignored():
    rows = [{"flow_json": json.dumps({"setup": setup_stamp.build()}), "realized_pnl": 50.0, "entry_price": 100.0, "stop_price": 99.0, "quantity": 10}]
    assert event_stats.summarize(rows) == {}


def test_under_30_trades_is_not_enough_data():
    rows = [_row(pnl=100.0 if i % 2 else -50.0) for i in range(12)]
    out = event_stats.summarize(rows)
    c = out["results|UP"]
    assert c["n"] == 12 and c["verdict"] == "NOT_ENOUGH_DATA" and c["lower_bound_r"] is None and c["age_effect"] == "NOT_ENOUGH_DATA"
    assert c["win_rate"] == 0.5 and c["avg_strength"] == 0.7


def test_30_winning_trades_over_several_days_are_positive():
    rows = [_row(pnl=100.0 + (i % 3), day=2 + i % 6) for i in range(30)]
    c = event_stats.summarize(rows)["results|UP"]
    assert c["n"] == 30 and c["r_n"] == 30 and c["verdict"] == "POSITIVE" and c["lower_bound_r"] > 0


def test_types_and_directions_are_kept_apart():
    rows = [_row("results", 1), _row("order", 1), _row("results", -1)]
    assert sorted(event_stats.summarize(rows)) == ["order|UP", "results|DOWN", "results|UP"]


def test_age_split_says_fades_only_with_30_in_each_group():
    fresh = [_row(age=10.0, pnl=150.0, day=2 + i % 6) for i in range(30)]
    aged = [_row(age=90.0, pnl=20.0, day=2 + i % 6) for i in range(30)]
    c = event_stats.summarize(fresh + aged)["results|UP"]
    assert c["ages"]["fresh"]["n"] == 30 and c["ages"]["aged"]["n"] == 30 and c["age_effect"] == "FADES"
    assert event_stats.summarize(fresh + aged[:5])["results|UP"]["age_effect"] == "NOT_ENOUGH_DATA"
    flat = event_stats.summarize([_row(age=10.0, pnl=20.0, day=2 + i % 6) for i in range(30)] + [_row(age=90.0, pnl=150.0, day=2 + i % 6) for i in range(30)])
    assert flat["results|UP"]["age_effect"] == "NO_FADE"


def test_junk_rows_never_raise():
    assert event_stats.summarize(None) == {}
    assert event_stats.summarize([None, 5, {}, {"flow_json": "{bad"}, {"flow_json": json.dumps({"setup": {"id": "X", "event": {"type": "results", "pol": 1}}})}]) == {}


def test_wiring_in_entry_and_route_by_source_text():
    ent = open(os.path.join(_HERE, "..", "orders", "entry.py"), encoding="utf-8").read()
    assert ent.count("event=quality") == 2
    mn = open(os.path.join(_HERE, "..", "main.py"), encoding="utf-8").read()
    assert "event_stats" in mn and '"event_types": _events' in mn


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("ok", k)
    print(n, "passed")
