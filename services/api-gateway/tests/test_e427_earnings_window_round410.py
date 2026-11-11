"""E427: `next_earnings_date` sets event_risk only inside the risk window; a past date, empty value or non-date text never does.

The helper is pure (no fastapi needed); the call sites are checked from the source, so this file also runs where the gateway cannot be imported.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GW))

import earnings_window as ew  # noqa: E402

TODAY = date(2026, 11, 11)


def _d(n):
    return (TODAY + timedelta(days=n)).isoformat()


@pytest.mark.parametrize("n, risk", [(-30, False), (-1, False), (0, True), (1, True), (3, True), (4, False), (30, False)])
def test_event_risk_only_from_today_to_the_risk_days(n, risk):
    got, line = ew.earnings_event_risk(_d(n), TODAY)
    assert got is risk
    assert (line == f"Earnings due: {_d(n)}") is (n >= 0)   # the reason line follows the "due" window, past dates have none


def test_due_window_ends_after_earnings_due_days():
    assert ew.earnings_event_risk(_d(ew.EARNINGS_DUE_DAYS), TODAY)[1] is not None
    assert ew.earnings_event_risk(_d(ew.EARNINGS_DUE_DAYS + 1), TODAY) == (False, None)


@pytest.mark.parametrize("v", [None, "", "  ", "x", "soon", "2026-13-45", 5, ["2026-11-12"], {}])
def test_missing_or_unreadable_values_are_neither_risk_nor_reason(v):
    assert ew.earnings_event_risk(v, TODAY) == (False, None)
    assert ew.earnings_days_out(v, TODAY) is None


def test_accepts_iso_timestamps_and_date_objects():
    assert ew.earnings_days_out("2026-11-12T09:00:00Z", TODAY) == 1
    assert ew.earnings_days_out(datetime(2026, 11, 12, 18, 0), TODAY) == 1
    assert ew.earnings_days_out(date(2026, 11, 11), TODAY) == 0


def test_today_defaults_to_ist_today():
    ist_today = ew.datetime.now(ew._IST).date()
    assert ew.earnings_days_out(ist_today.isoformat()) == 0


@pytest.mark.parametrize("rel, text", [
    ("main.py", "_earnings_event_risk(event_data.get(\"next_earnings_date\"))"),
    ("gateway_support/bound_routes_l.py", "earnings_event_risk(ed.get(\"next_earnings_date\"))"),
    ("gateway_support/bound_routes_j.py", "earnings_event_risk(events.get(\"next_earnings_date\"))"),
])
def test_every_event_risk_site_uses_the_helper(rel, text):
    src = (GW / rel).read_text(encoding="utf-8")
    assert text in src
    # no site sets event_risk straight from the raw date any more
    assert not re.search(r"if (event_data|ed|events)\.get\(\"next_earnings_date\"\):\s*\n\s*(normalized|result)\[\"event_risk\"\] = True", src)


def test_hot_picks_results_signal_uses_earnings_due():   # E428
    src = (GW / "main.py").read_text(encoding="utf-8")
    assert '_earnings_due(event_data.get("next_earnings_date"))' in src
    assert not re.search(r"has_results = bool\(\s*event_data\.get\(\"next_earnings_date\"\)", src)


@pytest.mark.parametrize("n, due", [(-1, False), (0, True), (30, True), (31, False)])
def test_earnings_due_window(n, due):   # E428
    assert ew.earnings_due(_d(n), TODAY) is due
