"""E274 round 40: S4_CATALYST_DOWN on a short (orders/setup_short.py), its stamp and its wiring into the short plan and the live short."""
import json
import pathlib
from datetime import datetime
from types import SimpleNamespace

import config
from orders import setup_engine, setup_short

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 9, 50)                       # Monday 09:50 IST -> slot 0930
QUALITY = SimpleNamespace(event_polarity=-1)
QUOTE = {"previous_close": 100.0, "avg_price": 98.5}     # price 97 is below VWAP
ORB = {"range_low": 98.0, "close_trigger": True, "trigger_close": 97.4}
TAPE = {"tape": {"reliable": True, "buy_pct_10m": 40.0}}
INPUTS = {"sector_third": "bottom", "event_priced_in": 0.4}


def run(**over):
    kw = dict(symbol="AAA", price=97.0, now=NOW, quality=QUALITY, tape_json=TAPE, quote=QUOTE, orb=ORB, gap_pct=-1.0, inputs=dict(INPUTS))
    kw.update(over)
    return setup_short.decide(**kw)


def test_all_three_confirmations_match_s4_at_half_size():
    r = run()
    assert r["id"] == setup_engine.S4 and r["side"] == "SHORT" and r["slot"] == "0930" and r["size_mult"] == 0.5
    assert set(r["confirmations"]) == {"tape_selling", "below_vwap", "sector_bottom_third"}


def test_two_of_three_is_enough_and_one_is_not():
    assert run(tape_json=None)["id"] == setup_engine.S4                         # tape unknown: still VWAP + sector
    assert run(tape_json=None, inputs={"event_priced_in": 0.4})["id"] is None    # only below VWAP left


def test_needs_a_negative_event():
    assert run(quality=SimpleNamespace(event_polarity=1))["id"] is None
    assert run(quality=SimpleNamespace(event_polarity=0))["id"] is None
    assert run(quality=None)["id"] is None


def test_needs_the_closed_bar_below_the_range_low():
    assert run(orb={"range_low": 98.0, "close_trigger": False})["id"] is None
    assert run(orb={"range_low": 98.0, "close_trigger": True, "trigger_close": 98.6})["id"] is None   # closed back inside the range
    assert run(orb=None)["id"] is None


def test_price_must_stay_below_the_preopen_price():
    assert run(price=99.5, gap_pct=-1.0)["id"] is None                # pre-open 99.0, price above it: the fall has reversed


def test_a_fall_already_priced_in_is_refused():
    assert run(inputs={"sector_third": "bottom", "event_priced_in": 1.4})["id"] is None


def test_the_09_20_slot_is_quarter_size():
    r = run(now=datetime(2026, 11, 2, 9, 25))
    assert r["slot"] == "0920" and r["size_mult"] == 0.25


def test_switches_turn_it_off(monkeypatch):
    monkeypatch.setattr(config, "SETUP_S4_ENABLED", False, raising=False)
    assert run()["id"] is None
    monkeypatch.setattr(config, "SETUP_S4_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "SETUP_ENGINE_ENABLED", False, raising=False)
    assert run()["id"] is None


def test_s1_cannot_fire_for_a_short_and_a_long_cannot_take_s4():
    f = setup_short.facts_short(symbol="AAA", price=97.0, now=NOW, event_polarity=-1, orb=ORB, quote=QUOTE, gap_pct=-1.0, tape=TAPE["tape"], inputs=INPUTS)
    assert f["allowed"] == {setup_engine.S4} and f["tape"] == -1 and f["above_vwap"] is False
    assert setup_engine.classify(dict(f, side="LONG", allowed=None))["id"] is None


def test_a_failing_read_is_no_setup(monkeypatch):
    r = setup_short.decide(symbol="AAA", price=97.0, now=NOW, quality=QUALITY, quote=object(), orb=ORB, gap_pct="x", inputs=None, tape_json=TAPE)
    assert r["id"] in (None, setup_engine.S4)                                   # never raises


def test_stamp_s4_and_the_legacy_short_stamp():
    r = run()
    st = json.loads(setup_short.stamp(None, r, NOW))["setup"]
    assert st["id"] == "S4_CATALYST_DOWN" and st["side"] == "SHORT" and st["size_mult"] == 0.5 and st["slot"] == "0930"
    legacy = json.loads(setup_short.stamp('{"orb":{}}', {"id": None}, NOW))
    assert legacy["setup"]["id"] == "LEGACY_SHORTLIST" and legacy["setup"]["side"] == "SHORT" and "orb" in legacy


def test_stamp_off_leaves_the_record_alone(monkeypatch):
    monkeypatch.setattr(config, "SETUP_STAMP_ENABLED", False, raising=False)
    assert setup_short.stamp("{}", run(), NOW) == "{}"


def test_the_short_plan_and_live_short_use_it():
    src = (ROOT / "orders" / "short_entry.py").read_text()
    assert "setup_short.decide(" in src and '"setup": _setup' in src and "value * max(0.0, min(1.0, float(_setup_mult)))" in src
    live = (ROOT / "orders" / "short_live.py").read_text()
    assert "_ss.stamp(pos.flow_json, plan.get(\"setup\")" in live
