"""E272: typed event fields on the quality signal and the negative-event veto.
Plain-function runner: python3 tests/test_e272_typed_event_veto.py (also collected by pytest). sqlalchemy / httpx are stubbed when missing."""
from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object
if "models" not in sys.modules:
    _mm = types.ModuleType("models")
    _mm.ScalpQualityCache = type("ScalpQualityCache", (), {})
    sys.modules["models"] = _mm

import config
from screening import quality_gate as qg


def body(*events, ok=True):
    return {"ok": ok, "events": list(events)}


def ev(pol, strength, typ="results", age_s=600, pub="2026-11-02T03:30:00+00:00"):
    return {"type": typ, "polarity": pol, "strength": strength, "age_s": age_s, "published": pub}


def test_typed_event_picks_the_strongest_known_polarity_and_ignores_unknown():
    t = qg._typed_event_from(body(ev(0, 0.9), ev(1, 0.4, "order_win"), ev(-1, 0.7, "probe", 1200)))
    assert t["type"] == "probe" and t["polarity"] == -1 and t["strength"] == 0.7 and t["age_min"] == 20.0
    assert qg._typed_event_from(body(ev(0, 0.9))) is None            # unknown polarity never triggers or vetoes


def test_feed_down_or_malformed_is_none():
    assert qg._typed_event_from(body(ev(-1, 0.9), ok=False)) is None
    assert qg._typed_event_from(None) is None and qg._typed_event_from("x") is None and qg._typed_event_from({"ok": True, "events": [None, 3]}) is None


def test_negative_event_vetoes_a_long_at_or_above_the_floor():
    s = qg.QualitySignal("ABC", event_type="probe", event_polarity=-1, event_strength=0.5, event_age_min=12)
    ok, why = s.passes()
    assert not ok and why.startswith("NEG_EVENT: probe") and "0.50" in why
    s.event_strength = 0.29
    assert s.passes()[0]


def test_positive_unknown_and_missing_events_never_veto():
    assert qg.QualitySignal("ABC", event_polarity=1, event_strength=0.9).passes()[0]
    assert qg.QualitySignal("ABC").passes()[0]
    assert qg.QualitySignal("ABC", event_polarity=0, event_strength=0.9).passes()[0]


def test_switches_turn_the_veto_off_and_move_the_floor():
    s = qg.QualitySignal("ABC", event_type="probe", event_polarity=-1, event_strength=0.5)
    old = (config.NEG_EVENT_VETO_ENABLED, config.NEG_EVENT_VETO_MIN_STRENGTH)
    try:
        config.NEG_EVENT_VETO_MIN_STRENGTH = 0.6
        assert s.passes()[0]
        config.NEG_EVENT_VETO_MIN_STRENGTH = 0.3
        config.NEG_EVENT_VETO_ENABLED = False
        assert s.passes()[0]
    finally:
        config.NEG_EVENT_VETO_ENABLED, config.NEG_EVENT_VETO_MIN_STRENGTH = old


def test_fetch_failure_returns_none_so_the_feed_being_down_changes_nothing():
    assert asyncio.run(qg._fetch_typed_event("ABC")) is None          # httpx is a stub here (or unreachable): never raises


def test_defaults_and_wiring_by_source_text():
    cfg = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()
    for line in ('TYPED_EVENT_ENABLED = _get_bool("TYPED_EVENT_ENABLED", True)', 'NEG_EVENT_VETO_ENABLED = _get_bool("NEG_EVENT_VETO_ENABLED", True)',
                 '_get_float("NEG_EVENT_VETO_MIN_STRENGTH", 0.3)'):
        assert line in cfg                                          # shipped defaults (conftest turns the fetch off for older tests)
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screening", "quality_gate.py"), encoding="utf-8").read()
    assert "_fetch_typed_event(symbol)" in src and "_sig.event_polarity" in src
    cat = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "control", "catalog.py"), encoding="utf-8").read()
    assert all(f'Flag("{n}"' in cat for n in ("TYPED_EVENT_ENABLED", "NEG_EVENT_VETO_ENABLED", "NEG_EVENT_VETO_MIN_STRENGTH"))


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n} passed")
