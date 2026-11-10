"""Round 272: the live engine gets the exchange holiday list (it was built with none, so a weekday holiday counted as a trading day)."""
from __future__ import annotations

import importlib
from datetime import date, datetime

from fno import gates


def _cfg(monkeypatch, value):
    import config
    if value is None:
        monkeypatch.delenv("FNO_HOLIDAYS", raising=False)
    else:
        monkeypatch.setenv("FNO_HOLIDAYS", value)
    return importlib.reload(config)


def test_default_holidays_include_diwali_week_and_block_trading(monkeypatch):
    c = _cfg(monkeypatch, None)
    assert "2026-11-10" in c.HOLIDAYS and "2026-11-24" in c.HOLIDAYS
    assert not gates.is_trading_day(date(2026, 11, 10), c.HOLIDAYS)
    assert gates.is_trading_day(date(2026, 11, 11), c.HOLIDAYS)


def test_env_replaces_default_and_junk_is_dropped(monkeypatch):
    c = _cfg(monkeypatch, "2026-12-01, nonsense ;2026-13-45 2026-12-02")
    assert c.HOLIDAYS == ("2026-12-01", "2026-12-02")
    assert _cfg(monkeypatch, " ").HOLIDAYS == _cfg(monkeypatch, None).HOLIDAYS   # blank = default, never a crash
    _cfg(monkeypatch, None)


def test_position_from_before_a_holiday_is_carried_over_on_the_next_trading_day():
    hol = ("2026-11-10",)
    assert not gates.carried_over("2026-11-09", datetime(2026, 11, 10, 9, 30), hol)   # holiday: not a trading day
    assert gates.carried_over("2026-11-09", datetime(2026, 11, 11, 9, 30), hol)


def test_market_open_is_closed_on_a_weekday_holiday():
    from fno.runtime import market_open
    assert market_open(datetime(2026, 11, 9, 10, 0))
    assert market_open(datetime(2026, 11, 10, 10, 0))                       # no list given: old behaviour
    assert not market_open(datetime(2026, 11, 10, 10, 0), ("2026-11-10",))
    assert market_open(datetime(2026, 11, 11, 10, 0), ("2026-11-10",))
    assert not market_open(datetime(2026, 11, 14, 10, 0), ())               # Saturday


def test_default_is_the_full_official_2026_list(monkeypatch):
    c = _cfg(monkeypatch, None)
    assert len(c.HOLIDAYS) == 16 and "2026-09-14" in c.HOLIDAYS and "2026-04-03" in c.HOLIDAYS
    assert c.HOLIDAYS == tuple(sorted(c._NSE_HOLIDAYS_2026))
