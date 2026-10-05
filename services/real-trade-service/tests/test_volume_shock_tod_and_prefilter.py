"""
tests/test_volume_shock_tod_and_prefilter.py

group154 (2026-10-05, item 5 of the open-market log):

1. Time-of-day volume. The last daily candle is today's PARTIAL session while the market is
   open, but it was divided by a 20-day average of FULL sessions, so a stock already running at
   2x its normal full-day volume showed ~0.4x at 10:00 IST and was rejected. The partial volume
   is now projected to a full session with an intraday cumulative-volume curve (approximate,
   floored at 15 %, off switch VOLUME_SHOCK_TOD_ADJUST=0).

2. Quote pre-check. The daily history call (one AngelOne getCandleData per symbol, shed under
   load) is no longer made for a mover whose live return, from the quote's previous_close, is
   clearly below the return gate.

3. One WARNING per cycle when history was unavailable for some symbols.

No network: the async client is a routing fake; the clock is passed in explicitly.

Run from services/real-trade-service:
    python3 -m pytest tests/test_volume_shock_tod_and_prefilter.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import intraday_eligibility
import models
from candidate_engine import candidates as cd

IST = ZoneInfo("Asia/Kolkata")
_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


def _at(h, m, day="2026-10-05"):
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=IST)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload, ""

    def json(self):
        return self._p


class _Client:
    def __init__(self, quote, candles):
        self.quote, self.candles, self.urls = quote, candles, []

    async def get(self, url, timeout=None, params=None):
        self.urls.append(url)
        if "/quote/" in url:
            return _Resp(200, self.quote) if self.quote is not None else _Resp(404, None)
        if "/history/" in url:
            return _Resp(200, {"candles": self.candles})
        return _Resp(404, None)

    def history_calls(self):
        return [u for u in self.urls if "/history/" in u]


def _candles(today_date, today_vol, n=21, base=100.0, ret_pct=5.0, avg_vol=1000.0):
    out = [{"date": f"2026-09-{i + 1:02d} 00:00", "open": base, "high": base + 1, "low": base - 1,
            "close": base, "volume": avg_vol} for i in range(n - 1)]
    out.append({"date": f"{today_date} 00:00", "open": base, "high": base * 1.06, "low": base,
                "close": round(base * (1 + ret_pct / 100), 2), "volume": today_vol})
    return out


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(cd, "VOLUME_SHOCK_TOD_ADJUST", True)
    monkeypatch.setattr(cd, "VOLUME_SHOCK_TOD_MIN_FRACTION", 0.15)
    monkeypatch.setattr(cd, "VOLUME_SHOCK_QUOTE_PREFILTER", True)
    monkeypatch.setattr(cd, "VOLUME_SHOCK_PREFILTER_MARGIN_PCT", 1.0)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


# ── session volume curve ─────────────────────────────────────────────────────

def test_fraction_is_one_outside_the_session_and_monotonic_inside():
    assert cd._session_volume_fraction(_at(9, 0)) == 1.0
    assert cd._session_volume_fraction(_at(15, 30)) == 1.0
    assert cd._session_volume_fraction(_at(18, 0)) == 1.0
    fr = [cd._session_volume_fraction(_at(h, m)) for h, m in
          [(9, 20), (9, 30), (10, 0), (11, 0), (12, 0), (13, 30), (14, 30), (15, 0), (15, 20)]]
    assert fr == sorted(fr) and fr[-1] < 1.0
    assert abs(cd._session_volume_fraction(_at(10, 0)) - 0.20) < 1e-6      # curve point (45 min)


def test_fraction_floor_stops_the_first_minutes_from_inflating_ticks():
    assert cd._session_volume_fraction(_at(9, 16)) == 0.15                   # raw curve ~0.006, floored
    assert cd._session_volume_fraction(_at(9, 25)) >= 0.15


def test_today_fraction_only_applies_when_last_candle_is_today_and_switch_on(monkeypatch):
    now = _at(10, 0)
    todays = _candles("2026-10-05", 500)
    assert cd._today_volume_fraction(todays, now) == pytest.approx(0.20)
    assert cd._today_volume_fraction(_candles("2026-10-02", 500), now) == 1.0   # last candle is a past day
    assert cd._today_volume_fraction([{"close": 1, "volume": 1}], now) == 1.0   # no date: no adjustment
    assert cd._today_volume_fraction([], now) == 1.0
    monkeypatch.setattr(cd, "VOLUME_SHOCK_TOD_ADJUST", False)
    assert cd._today_volume_fraction(todays, now) == 1.0


def test_today_fraction_never_raises_on_garbage():
    assert cd._today_volume_fraction([None], _at(10, 0)) == 1.0
    assert cd._today_volume_fraction([{"date": None}], _at(10, 0)) == 1.0


def test_today_fraction_swallows_a_bad_clock_object():
    # a clock without strftime/hour raises inside the try -> falls back to "no adjustment"
    assert cd._today_volume_fraction(_candles("2026-10-05", 500), "not-a-datetime") == 1.0


def test_today_fraction_defaults_to_the_real_clock_without_raising():
    # no explicit clock: uses datetime.now(IST); last candle is far in the past -> no adjustment
    assert cd._today_volume_fraction(_candles("2020-01-01", 500)) == 1.0


# ── volume ratio in _volume_shock_analysis ───────────────────────────────────

def _freeze(monkeypatch, now):
    real = cd._today_volume_fraction
    monkeypatch.setattr(cd, "_today_volume_fraction", lambda candles, now_ist=None: real(candles, now))


def test_morning_partial_volume_is_projected_so_a_real_shock_passes(monkeypatch):
    _freeze(monkeypatch, _at(10, 0))
    # 500 shares by 10:00 against a 1000-share full-day average: raw 0.5x (old code: rejected),
    # projected 0.5 / 0.20 = 2.5x (>= 1.5x).
    client = _Client({"price": 105.0, "previous_close": 100.0}, _candles("2026-10-05", 500))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert out["reject_reason"] is None
    assert out["vol_multiple"] == 2.5 and out["vol_multiple_raw"] == 0.5 and out["tod_fraction"] == 0.2


def test_morning_genuinely_quiet_stock_is_still_rejected_and_the_reason_shows_both(monkeypatch):
    _freeze(monkeypatch, _at(10, 0))
    client = _Client({"price": 105.0, "previous_close": 100.0}, _candles("2026-10-05", 200))   # 0.2 / 0.2 = 1.0x
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert "1.0x 20-day average" in out["reject_reason"] and "raw 0.2x" in out["reject_reason"]
    assert "20% of the session elapsed" in out["reject_reason"]


def test_after_close_or_off_switch_uses_the_raw_ratio(monkeypatch):
    _freeze(monkeypatch, _at(16, 0))
    client = _Client({"price": 105.0, "previous_close": 100.0}, _candles("2026-10-05", 500))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert "0.5x 20-day average" in out["reject_reason"] and "elapsed" not in out["reject_reason"]
    _freeze(monkeypatch, _at(10, 0))
    monkeypatch.setattr(cd, "VOLUME_SHOCK_TOD_ADJUST", False)
    out2 = run(cd._volume_shock_analysis(client, "TCS"))
    assert "0.5x 20-day average" in out2["reject_reason"]


# ── quote pre-check ──────────────────────────────────────────────────────────

def test_flat_mover_is_rejected_on_the_quote_without_a_history_request():
    client = _Client({"price": 100.5, "previous_close": 100.0}, _candles("2026-10-05", 5000))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert "volume-shock breakout threshold" in out["reject_reason"] and "history not fetched" in out["reject_reason"]
    assert client.history_calls() == []


def test_symbol_near_the_gate_still_gets_history_because_of_the_margin():
    # 2.0% < 2.5% gate but within the 1.0-point margin -> history is fetched and decides.
    client = _Client({"price": 102.0, "previous_close": 100.0}, _candles("2026-10-05", 5000, ret_pct=3.0))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert len(client.history_calls()) == 1
    assert out["reject_reason"] is None


@pytest.mark.parametrize("quote", [
    {"price": 100.5},                                   # no previous_close
    {"price": 100.5, "previous_close": 0},
    {"price": 100.5, "previous_close": "abc"},
    {"price": 0, "previous_close": 100.0},
])
def test_unusable_quote_return_never_blocks_the_history_call(quote):
    client = _Client(quote, _candles("2026-10-05", 5000, ret_pct=5.0))
    run(cd._volume_shock_analysis(client, "TCS"))
    assert len(client.history_calls()) == 1


def test_prefilter_off_switch_restores_the_history_call(monkeypatch):
    monkeypatch.setattr(cd, "VOLUME_SHOCK_QUOTE_PREFILTER", False)
    client = _Client({"price": 100.5, "previous_close": 100.0}, _candles("2026-10-05", 5000, ret_pct=0.5))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert len(client.history_calls()) == 1
    assert "history not fetched" not in out["reject_reason"]


def test_no_quote_returns_before_any_history_request():
    client = _Client(None, _candles("2026-10-05", 5000))
    out = run(cd._volume_shock_analysis(client, "TCS"))
    assert "No quote available" in out["reject_reason"]
    assert client.history_calls() == []


def test_quote_and_history_exceptions_are_handled(monkeypatch):
    async def bad_quote(client, symbol):
        raise RuntimeError("boom")
    monkeypatch.setattr(cd, "_fetch_quote", bad_quote)
    assert "No quote available" in run(cd._volume_shock_analysis(object(), "X"))["reject_reason"]

    async def ok_quote(client, symbol):
        return {"price": 105.0, "previous_close": 100.0}

    async def bad_hist(client, symbol, period, interval="1d"):
        raise RuntimeError("boom")
    monkeypatch.setattr(cd, "_fetch_quote", ok_quote)
    monkeypatch.setattr(cd, "_fetch_history", bad_hist)
    assert "Insufficient daily history" in run(cd._volume_shock_analysis(object(), "X"))["reject_reason"]


def test_quote_return_helper():
    assert cd._quote_return_pct({"price": 110, "previous_close": 100}) == pytest.approx(10.0)
    assert cd._quote_return_pct({"cmp": 90, "previous_close": 100}) == pytest.approx(-10.0)
    assert cd._quote_return_pct({}) is None
    assert cd._quote_return_pct(None) is None


# ── per-cycle history-missing warning (orchestrator) ─────────────────────────

async def _noop_prefetch(client, symbols):
    return None


def _drive_cycle(db, monkeypatch, universe, reasons):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda d: set())
    monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

    async def fake_universe(client):
        return list(universe)
    monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

    async def fake_vs(client, symbol):
        return {"reject_reason": reasons[symbol], "atr_pct": None}
    monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)
    return run(cd._refresh_volume_shock_candidates(db, "REAL", set()))


def test_cycle_logs_one_warning_when_history_was_unavailable(db, monkeypatch, caplog):
    reasons = {
        "A": "Insufficient daily history for volume-shock check.",
        "B": "Insufficient daily history for volume-shock check.",
        "C": "No quote available for volume-shock check.",
        "D": "Today's return 0.1% < 2.5% volume-shock breakout threshold (quote pre-check, history not fetched).",
    }
    with caplog.at_level("INFO"):
        inserted = _drive_cycle(db, monkeypatch, list(reasons), reasons)
    assert inserted == 0
    warns = [r for r in caplog.records if r.levelname == "WARNING" and "daily history unavailable" in r.getMessage()]
    assert len(warns) == 1
    assert "2 of 4" in warns[0].getMessage()


def test_cycle_logs_no_history_warning_when_history_was_fine(db, monkeypatch, caplog):
    reasons = {"A": "No quote available for volume-shock check.", "B": "Today's volume 1.0x 20-day average < 1.5x volume-shock threshold."}
    with caplog.at_level("INFO"):
        _drive_cycle(db, monkeypatch, list(reasons), reasons)
    assert not any("daily history unavailable" in r.getMessage() for r in caplog.records)
