"""group320: run_once.is_holiday() also treats a price-proven closed weekday as a holiday (fail-open).

Loaded from file under a unique name (run_once needs API_GATEWAY_URL just to import).
Run from services/notification-scheduler-service:  python -m pytest tests/test_session_proof_is_holiday.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

import pytest

_SCHED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler")


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
    if _SCHED not in sys.path:
        monkeypatch.syspath_prepend(_SCHED)
    import session_proof_client as spc
    spec = importlib.util.spec_from_file_location("_run_once_g320", os.path.join(_SCHED, "run_once.py"))
    ro = importlib.util.module_from_spec(spec)
    sys.modules["_run_once_g320"] = ro
    spec.loader.exec_module(ro)
    spc.reset_state()
    yield ro, spc
    spc.reset_state()
    sys.modules.pop("_run_once_g320", None)


MON = datetime(2026, 10, 12, 11, 0)


def test_ordinary_weekday_is_not_a_holiday(mods):
    ro, _ = mods
    assert ro.is_holiday(MON) is False


def test_closed_verdict_makes_the_weekday_a_holiday(mods):
    ro, spc = mods
    spc.set_verdict("2026-10-12", True)
    assert ro.is_holiday(MON) is True


def test_verdict_for_another_day_is_ignored(mods):
    ro, spc = mods
    spc.set_verdict("2026-10-09", True)
    assert ro.is_holiday(MON) is False


def test_calendar_and_weekend_rules_are_unchanged(mods):
    ro, _ = mods
    assert ro.is_holiday(datetime(2026, 10, 20, 11, 0)) is True      # Dussehra
    assert ro.is_holiday(datetime(2026, 10, 17, 11, 0)) is True      # Saturday


def test_client_error_is_fail_open(mods, monkeypatch):
    ro, spc = mods
    monkeypatch.setattr(spc, "closed_for_day", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert ro.is_holiday(MON) is False
