"""E427: `_extract_event_signals` compares calendar dates (IST), so a results date of TODAY is event risk (0 days out), not "recent earnings" (-1).

Before: `(midnight of the results date - now).days` is -1 for any time after 00:00 on the results day, so the day with the most risk fell into the
"Earnings N d ago" branch and event_risk stayed False. main.py is loaded by path, like test_decision_api_gateway_url.py.
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_counter = itertools.count()


@pytest.fixture
def mod():
    path_before, mods_before = list(sys.path), set(sys.modules)
    sys.path.insert(0, _DIR)
    spec = importlib.util.spec_from_file_location(f"_decision_main_e427_{next(_counter)}", os.path.join(_DIR, "main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    yield m
    sys.path[:] = path_before
    repo = os.path.realpath(os.path.join(_DIR, "..", ".."))
    for k in set(sys.modules) - mods_before:
        f = getattr(sys.modules.get(k), "__file__", None) or ""
        if f and os.path.realpath(f).startswith(repo + os.sep):
            sys.modules.pop(k, None)


def _ist_date(offset_days: int) -> str:
    return ((datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date() + timedelta(days=offset_days)).isoformat()


@pytest.mark.parametrize("offset, risk, days", [(0, True, 0), (1, True, 1), (3, True, 3), (4, False, 4)])
def test_results_inside_the_risk_window_is_event_risk(mod, offset, risk, days):
    out = mod._extract_event_signals({"next_earnings_date": _ist_date(offset)})
    assert out["event_risk"] is risk and out["earnings_days_out"] == days


def test_results_yesterday_is_recent_not_risk(mod):
    out = mod._extract_event_signals({"next_earnings_date": _ist_date(-1)})
    assert out["event_risk"] is False and out["earnings_days_out"] == -1
    assert any("Recent earnings (1d ago)" in r for r in out["event_reasons"])


def test_results_today_gets_the_hold_off_reason(mod):
    out = mod._extract_event_signals({"next_earnings_date": _ist_date(0)})
    assert any("Earnings in 0d" in r for r in out["event_reasons"])
