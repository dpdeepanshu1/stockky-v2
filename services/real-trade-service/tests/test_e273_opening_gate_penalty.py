"""E273 round 17: Real Trade's opening gate - day-high position and weak previous close are SIZE PENALTIES, refused only past the hard ceilings.
Plain runner: python3 tests/test_e273_opening_gate_penalty.py (service folder). Stdlib only; the clock and the previous-day data are stubbed."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import opening_gate as og  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 26, 9, 40, tzinfo=IST)       # a Monday, 25 minutes after the open (range check is on)


class Env:
    def __init__(self, close_pos=0.8, **cfg):
        self.keep = {}
        base = dict(OPENING_GATE_ENABLED=True, OPENING_GATE_PENALTY_ENABLED=True, OPENING_ENTRY_GUARD_ENABLED=True, OPENING_ENTRY_NOT_BEFORE_IST="09:15",
                    OPENING_ENTRY_GUARD_MODES=("REAL", "DEMO"), OPENING_GATE_MIN_MINUTES_AFTER_OPEN=0.0)
        base.update(cfg)
        for k, v in base.items():
            self.keep[k] = getattr(config, k, None); setattr(config, k, v)
        self.pd = SimpleNamespace(close_pos=close_pos, date="2026-10-23", low=99.0, high=102.0, close=101.0, atr_pct=2.0)
        self.old_get, self.old_mo = og.prev_day.get, og.is_market_open_ist
        og.prev_day.get = lambda s: self.pd
        og.is_market_open_ist = lambda now=None: True

    def done(self):
        for k, v in self.keep.items():
            setattr(config, k, v)
        og.prev_day.get, og.is_market_open_ist = self.old_get, self.old_mo


def tk(price, hi=102.0, lo=100.0):
    return SimpleNamespace(price=price, prev_close=100.0, day_high=hi, day_low=lo, atr=2.0, symbol="ABC")


def test_good_tick_has_no_penalty_and_no_refusal():
    e = Env()
    try:
        t = tk(101.0)
        assert og.reject_reason("REAL", t, NOW) is None
        assert og.penalty("REAL", t, NOW) == {"mult": 1.0, "reasons": []}
    finally:
        e.done()


def test_near_day_high_is_a_penalty_not_a_refusal():
    e = Env()
    try:
        t = tk(101.9)       # range position 0.95, between 0.88 and 0.98
        assert og.reject_reason("REAL", t, NOW) is None
        p = og.penalty("REAL", t, NOW)
        assert 0.40 <= p["mult"] < 0.85 and p["reasons"][0].startswith("near_day_high")
        # closer to the high = smaller size
        assert og.penalty("REAL", tk(101.95), NOW)["mult"] < p["mult"]
    finally:
        e.done()


def test_the_very_high_is_still_refused():
    e = Env()
    try:
        assert og.reject_reason("REAL", tk(102.0), NOW).startswith("OPENING_GATE:NEAR_DAY_HIGH")     # range position 1.0 >= 0.98
    finally:
        e.done()


def test_weak_previous_close_is_a_penalty_until_the_bottom_tenth():
    e = Env(close_pos=0.3)
    try:
        t = tk(101.0)
        assert og.reject_reason("REAL", t, NOW) is None
        p = og.penalty("REAL", t, NOW)
        assert p["mult"] < 0.85 and p["reasons"][0].startswith("prev_day_weak_close")
        e.pd.close_pos = 0.05
        assert og.reject_reason("REAL", t, NOW).startswith("OPENING_GATE:PREV_DAY_WEAK_CLOSE")
    finally:
        e.done()


def test_two_penalties_multiply_with_a_floor():
    e = Env(close_pos=0.12, OPENING_GATE_PENALTY_FLOOR=0.5)
    try:
        p = og.penalty("REAL", tk(101.95), NOW)
        assert len(p["reasons"]) == 2 and p["mult"] == 0.5
    finally:
        e.done()


def test_switch_off_restores_the_old_refusals_and_no_penalty():
    e = Env(close_pos=0.3, OPENING_GATE_PENALTY_ENABLED=False)
    try:
        assert og.reject_reason("REAL", tk(101.9), NOW).startswith("OPENING_GATE:NEAR_DAY_HIGH")
        assert og.penalty("REAL", tk(101.9), NOW)["mult"] == 1.0
        assert og.reject_reason("REAL", tk(101.0), NOW).startswith("OPENING_GATE:PREV_DAY_WEAK_CLOSE")
    finally:
        e.done()


def test_inactive_after_settle_and_never_raises():
    e = Env()
    try:
        late = NOW.replace(hour=10, minute=30)
        assert og.penalty("REAL", tk(101.9), late)["mult"] == 1.0
        assert og.penalty("REAL", None, NOW)["mult"] == 1.0
    finally:
        e.done()


def test_entry_applies_the_penalty_and_catalog_has_the_switch():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "opening_gate.penalty(mode, _og_tick)" in src and "adj_risk_pct *= _og_pen[\"mult\"]" in src
    assert config.OPENING_GATE_PENALTY_ENABLED is True and config.OPENING_GATE_HARD_RANGE_POS == 0.98
    assert 'Flag("OPENING_GATE_PENALTY_ENABLED"' in (HERE / "control" / "catalog.py").read_text()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
