"""group231: short daily /history periods (5d/1mo/3mo/6mo) are answered from ONE AngelOne 1y/1d fetch."""
from __future__ import annotations
import os, sys, threading, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as m


def _candles(span_days: int):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return [{"date": f"{(today - timedelta(days=b)).isoformat()} 00:00", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 10} for b in range(span_days, -1, -1)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    m._mem._d.clear()
    m._history_flights.clear()
    monkeypatch.setattr(m, "cache", None)
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 60.0)
    monkeypatch.setattr(m, "_history_candle_cooling", lambda: False)
    monkeypatch.setattr(m, "_history_last_good_get", lambda k: None)
    monkeypatch.delenv("HISTORY_WIDEN_DAILY", raising=False)
    yield
    m._mem._d.clear()
    m._history_flights.clear()


@pytest.fixture()
def angel(monkeypatch):
    calls = []

    def fake(sym, period, interval, days, start_date, end_date):
        calls.append(period)
        return _candles(m._HISTORY_PERIOD_DAYS.get(period, 180))

    monkeypatch.setattr(m, "_angelone_history_candles", fake)
    return calls


def _get(period, force=False):
    return m._get_history_impl("TCS", period, "1d", force, None)


def test_four_short_periods_cost_one_angelone_call(angel):
    out = {p: _get(p) for p in ("5d", "1mo", "3mo", "6mo")}
    assert angel == ["1y"]
    assert [out[p]["period"] for p in out] == ["5d", "1mo", "3mo", "6mo"]
    assert all(out[p]["derived_from"] == "1y" for p in out)
    assert len(out["5d"]["candles"]) < len(out["1mo"]["candles"]) < len(out["3mo"]["candles"]) < len(out["6mo"]["candles"])
    assert m._cache_get("history:TCS.NS:1y:1d:") is not None
    assert m._cache_get("history:TCS.NS:1mo:1d:") is None


def test_5d_window_is_one_week(angel):
    r = _get("5d")
    cutoff = (datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=7)).isoformat()
    assert min(c["date"][:10] for c in r["candles"]) >= cutoff


def test_one_year_request_is_not_widened(angel):
    r = _get("1y")
    assert angel == ["1y"] and "derived_from" not in r


def test_widen_off_restores_per_period_calls(angel, monkeypatch):
    monkeypatch.setenv("HISTORY_WIDEN_DAILY", "0")
    _get("1mo"); _get("3mo")
    assert angel == ["1mo", "3mo"]


def test_hourly_and_days_window_not_widened(angel):
    m._get_history_impl("TCS", "1mo", "1h", False, None)
    m._get_history_impl("TCS", "1mo", "1d", False, 20)
    assert "1y" not in angel


def test_small_max_history_period_disables_widening(angel, monkeypatch):
    monkeypatch.setattr(m, "MAX_HISTORY_PERIOD", "6mo")
    _get("1mo")
    assert angel == ["1mo"]


def test_failed_1y_call_is_not_repeated_for_short_period(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_angelone_history_candles", lambda *a: calls.append(a[1]))
    monkeypatch.setattr(m, "_history_angel_available", lambda s: True)
    with pytest.raises(Exception):
        _get("1mo")           # yfinance stub yields nothing -> HTTP error; the point is the call list
    assert calls == ["1y"]


def test_angelone_unavailable_keeps_old_path(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_angelone_history_candles", lambda *a: calls.append(a[1]))
    monkeypatch.setattr(m, "_history_angel_available", lambda s: False)
    with pytest.raises(Exception):
        _get("1mo")
    assert calls == ["1y", "1mo"]


def test_candle_cooldown_skips_widening(angel, monkeypatch):
    monkeypatch.setattr(m, "_history_candle_cooling", lambda: True)
    r = m._history_widen_from_angelone("TCS", "TCS.NS", "1mo", "1d", None, False)
    assert r == (None, False) and angel == []


def test_concurrent_short_periods_share_one_fetch(monkeypatch):
    calls = []

    def slow(sym, period, interval, days, start_date, end_date):
        calls.append(period)
        time.sleep(0.15)
        return _candles(365)

    monkeypatch.setattr(m, "_angelone_history_candles", slow)
    res = {}

    def run(p):
        res[p] = _get(p)

    ts = [threading.Thread(target=run, args=(p,)) for p in ("5d", "1mo", "3mo", "6mo")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert calls == ["1y"] and set(res) == {"5d", "1mo", "3mo", "6mo"}


def test_last_good_1y_serves_short_period_when_all_failed(monkeypatch):
    full = {"symbol": "TCS.NS", "period": "1y", "interval": "1d", "candles": _candles(365),
            "stale": True, "stale_age_s": 5, "source": "last_good"}
    monkeypatch.setattr(m, "_history_last_good_get",
                        lambda k: dict(full) if k == "history:TCS.NS:1y:1d:" else None)
    out = m._history_last_good_for("history:TCS.NS:1mo:1d:", "TCS.NS", "1mo", "1d", None)
    assert out["period"] == "1mo" and out["stale"] is True and out["source"] == "last_good"
    assert 15 < len(out["candles"]) < 40
    assert m._history_last_good_for("history:TCS.NS:1mo:1d:", "TCS.NS", "1mo", "1d", 20) is None
