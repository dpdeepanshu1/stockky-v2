"""E285 round 119: OPENING_0920_PATH, the kill switch for the 09:21 to 09:30 slot (orders/opening_protect.slot_0920_reason, part of pause_reason)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import opening_protect

IST = timezone(timedelta(hours=5, minutes=30))
MON = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=IST)
SAT = datetime(2026, 10, 10, 9, 25, tzinfo=IST)


def test_default_on_refuses_nothing(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", True)
    assert opening_protect.slot_0920_reason(MON(9, 25)) is None


def test_off_refuses_inside_the_slot(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", False)
    monkeypatch.setattr(config, "OPENING_0920_PATH_UNTIL_IST", "09:30")
    for h, m in ((9, 15), (9, 21), (9, 29)):
        r = opening_protect.slot_0920_reason(MON(h, m))
        assert r and r.startswith("OPENING_PROTECT:SLOT_0920_OFF")


def test_off_allows_from_0930_and_before_the_bell(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", False)
    monkeypatch.setattr(config, "OPENING_0920_PATH_UNTIL_IST", "09:30")
    assert opening_protect.slot_0920_reason(MON(9, 30)) is None
    assert opening_protect.slot_0920_reason(MON(9, 10)) is None
    assert opening_protect.slot_0920_reason(MON(11, 0)) is None


def test_off_ignores_weekend(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", False)
    assert opening_protect.slot_0920_reason(SAT) is None


def test_independent_of_opening_protect_switch_and_wired_into_pause_reason(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", False)
    monkeypatch.setattr(config, "OPENING_PROTECT_ENABLED", False)
    r = opening_protect.pause_reason(None, MON(9, 25))
    assert r and "SLOT_0920_OFF" in r
    monkeypatch.setattr(config, "OPENING_0920_PATH", True)
    assert opening_protect.pause_reason(None, MON(9, 25)) is None


def test_bad_clock_fails_open(monkeypatch):
    monkeypatch.setattr(config, "OPENING_0920_PATH", False)
    assert opening_protect.slot_0920_reason("not a time") is None
