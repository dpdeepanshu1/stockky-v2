"""group227: /history?period=5d on the AngelOne candle path.

"5d" was missing from the AngelOne/NSE period->days maps, so it fell back to the 180-day default: the candidate
engine's "1w" return was computed over six months whenever AngelOne served the candles (and a bogus first bar
looked like a +211% week). The window is now 7 calendar days, and 5d is never widened by MAX_HISTORY_PERIOD.
"""
from __future__ import annotations
import os, sys, types, asyncio
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as m


class _FakeSession:
    def __init__(self):
        self.calls = []

    def is_configured(self):
        return True

    async def get_candles(self, exch, token, interval, from_str, to_str, lane=None):
        self.calls.append((from_str, to_str))
        return [["2026-10-05T00:00:00+05:30", 1, 2, 0.5, 1.5, 10], ["2026-10-06T00:00:00+05:30", 1, 2, 0.5, 1.6, 12]]


@pytest.fixture()
def fake_angel(monkeypatch):
    sess = _FakeSession()
    mod = types.ModuleType("angelone_client")
    mod.get_session = lambda: sess
    monkeypatch.setitem(sys.modules, "angelone_client", mod)
    import angelone_scrip_master
    monkeypatch.setattr(angelone_scrip_master, "get_token", lambda s: "123")
    return sess


def _days_between(sess):
    f, t = sess.calls[-1]
    fmt = "%Y-%m-%d %H:%M"
    return (datetime.strptime(t, fmt) - datetime.strptime(f, fmt)).days


def test_5d_asks_angelone_for_a_week_not_six_months(fake_angel):
    out = m._angelone_history_candles("TCS", "5d", "1d", None, None, None)
    assert out and len(out) == 2
    assert _days_between(fake_angel) == 7


def test_other_periods_unchanged(fake_angel):
    for period, span in (("1mo", 30), ("3mo", 90), ("6mo", 180), ("1y", 365)):
        m._angelone_history_candles("TCS", period, "1d", None, None, None)
        assert _days_between(fake_angel) == span


def test_unknown_period_still_defaults_to_180(fake_angel):
    m._angelone_history_candles("TCS", "weird", "1d", None, None, None)
    assert _days_between(fake_angel) == 180


def test_5d_is_not_widened_by_a_small_max_history_period(monkeypatch):
    """With MAX_HISTORY_PERIOD=1mo the old default rank (4) pushed 5d up to 1mo; rank 0 keeps it 5d."""
    monkeypatch.setattr(m, "MAX_HISTORY_PERIOD", "1mo")
    seen = {}

    def fake(sym, period, interval, days, start_date, end_date):
        seen["period"] = period
        return [{"date": "2026-10-06 00:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1}]

    monkeypatch.setattr(m, "_angelone_history_candles", fake)
    monkeypatch.setattr(m, "cache", None)
    m._mem._d.clear()
    out = m._get_history_impl("TCS", "5d", "1d", False, None)
    assert seen["period"] == "5d" and out["period"] == "5d"
    m._mem._d.clear()
