"""E285 round 135: SHORT_STOP_FLOOR_ON_NORMAL_STOP (ships false). The opening / ATR-floor stop check judges a short's stop after the stressed / panic tightening; with the
switch on it judges the NORMAL stop. The stop sent and the size are unchanged either way."""
import config
from screening import opening_gate
from orders import short_entry as se
from tests.test_short_step5_shadow_entry import db, cand, _plan  # noqa: F401  (db is the shared fixture)


def _setup(monkeypatch, seen):
    monkeypatch.setattr(se, "stop_mult_for_state", lambda state: 0.6)

    def _floor(sym, stop_pct, now=None):
        seen.append(round(float(stop_pct), 2))
        return "STOP_ATR_FLOOR:STOP_TOO_TIGHT" if stop_pct < 0.8 else None
    monkeypatch.setattr(opening_gate, "stop_reject", _floor)


def test_default_is_off():
    assert config.SHORT_STOP_FLOOR_ON_NORMAL_STOP is False


def test_off_judges_the_tightened_stop_and_refuses(db, monkeypatch):
    seen = []
    _setup(monkeypatch, seen)
    monkeypatch.setattr(config, "SHORT_STOP_FLOOR_ON_NORMAL_STOP", False, raising=False)
    assert _plan(db, cand()) is None
    assert seen == [0.6]
    assert se._stats["by_code"] == {"SHORT_OPENING_STOP": 1}


def test_on_judges_the_normal_stop_and_keeps_the_tightened_stop_and_size(db, monkeypatch):
    seen = []
    _setup(monkeypatch, seen)
    monkeypatch.setattr(config, "SHORT_STOP_FLOOR_ON_NORMAL_STOP", True, raising=False)
    r = _plan(db, cand(), None, {"conviction": 70})
    assert seen == [1.0] and r is not None
    assert round(r["levels"].stop_pct, 1) == 0.6 and r["qty"] == 200          # the stop sent is the tightened one; size still from the 1% normal stop
