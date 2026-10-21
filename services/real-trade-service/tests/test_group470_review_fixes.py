"""tests/test_group470_review_fixes.py - group 470: late-entry cut-off, overnight default off, ORB shadow recorder, overshoot sizing."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import calendar_gate, orb_shadow
from tz_utils import IST

WED = (2026, 10, 14)
SAT = (2026, 10, 17)


def at(day, h, m, s=0):
    return datetime(*day, h, m, s, tzinfo=IST)


def _dec():
    return SimpleNamespace(symbol="ABC", flow_json=None)


def _tick(**kw):
    d = dict(price=102.0, day_high=101.0, day_low=100.0, vwap=100.8, spread_pct=0.1)
    d.update(kw)
    return SimpleNamespace(**d)


def test_cutoff_before_and_after(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    assert calendar_gate.late_entry_block_reason(at(WED, 13, 59)) is None
    r = calendar_gate.late_entry_block_reason(at(WED, 14, 0))
    assert r and r.startswith("LATE_ENTRY_CUTOFF")


def test_cutoff_blank_weekend_and_bad_value(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    assert calendar_gate.late_entry_block_reason(at(WED, 15, 0)) is None
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    assert calendar_gate.late_entry_block_reason(at(SAT, 15, 0)) is None
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "garbage")      # falls back to 14:00
    assert calendar_gate.late_entry_block_reason(at(WED, 14, 30)) is not None


def test_cutoff_independent_of_calendar_switch(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", False)
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    assert calendar_gate.late_entry_block_reason(at(WED, 14, 5)) is not None


def test_shipped_defaults():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py")).read()
    assert '"OVERNIGHT_HOLD_ENABLED") or "").strip() or "false"' in src
    assert '_cal_str("ENTRY_LAST_TIME_IST", "14:00")' in src
    assert '_og_float("STOP_OVERSHOOT_ALLOWANCE_PCT", 1.0)' in src
    assert config.ORB_MODE in ("off", "shadow")


def test_orb_range_then_breakout(monkeypatch):
    orb_shadow._reset_for_tests()
    monkeypatch.setattr(config, "ORB_MODE", "shadow")
    d = _dec()
    assert orb_shadow.record(d, _tick(), at(WED, 9, 21)) is True          # captures range 100-101 (1.0% wide)
    v = json.loads(d.flow_json)["orb"]
    assert v["range_high"] == 101.0 and v["range_low"] == 100.0 and v["would_enter"] is True
    assert v["limit_price"] == round(101.0 * 1.001, 2) and v["stop_price"] == 100.0
    d2 = _dec()
    orb_shadow.record(d2, _tick(price=100.5), at(WED, 9, 40))
    v2 = json.loads(d2.flow_json)["orb"]
    assert v2["would_enter"] is False and "below_range_high" in v2["reasons"]


def test_orb_reasons():
    orb_shadow._reset_for_tests()
    orb_shadow.record(_dec(), _tick(), at(WED, 9, 21))
    for kw, reason in ((dict(vwap=103.0), "below_vwap"), (dict(spread_pct=0.9), "wide_spread")):
        x = _dec()
        orb_shadow.record(x, _tick(**kw), at(WED, 9, 40))
        assert reason in json.loads(x.flow_json)["orb"]["reasons"]
    orb_shadow._reset_for_tests()
    x = _dec(); orb_shadow.record(x, _tick(day_high=100.1, day_low=100.0), at(WED, 9, 21))
    assert "range_too_narrow" in json.loads(x.flow_json)["orb"]["reasons"]
    orb_shadow._reset_for_tests()
    x = _dec(); orb_shadow.record(x, _tick(day_high=110.0, day_low=100.0), at(WED, 9, 21))
    assert "range_too_wide" in json.loads(x.flow_json)["orb"]["reasons"]


def test_orb_no_range_late_off_weekend_and_merge(monkeypatch):
    orb_shadow._reset_for_tests()
    x = _dec(); orb_shadow.record(x, _tick(), at(WED, 9, 40))
    assert json.loads(x.flow_json)["orb"]["reason"] == "no_range"
    assert orb_shadow.record(_dec(), _tick(), at(WED, 10, 31)) is False
    assert orb_shadow.record(_dec(), _tick(), at(WED, 9, 10)) is False
    assert orb_shadow.record(_dec(), _tick(), at(SAT, 9, 40)) is False
    monkeypatch.setattr(config, "ORB_MODE", "off")
    assert orb_shadow.record(_dec(), _tick(), at(WED, 9, 40)) is False
    monkeypatch.setattr(config, "ORB_MODE", "shadow")
    y = _dec(); y.flow_json = json.dumps({"v": 1, "spread_pct": 0.1})
    orb_shadow.record(y, _tick(), at(WED, 9, 40))
    j = json.loads(y.flow_json)
    assert j["spread_pct"] == 0.1 and "orb" in j
    y.flow_json = "not json"
    assert orb_shadow.record(y, _tick(), at(WED, 9, 40)) is True
    assert orb_shadow.record(None, None) is False
