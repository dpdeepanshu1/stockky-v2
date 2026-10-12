"""group331: GET /global-cues - overseas index / futures day moves for real-trade's carry decision.

Not served by /quote: that waterfall appends ".NS" to "ES=F" and applies NSE-equity rules. This route asks Yahoo directly for an allow-listed
symbol, caches per symbol (a miss too), drops a market whose last bar is older than a day, and never raises.
Run from services/market-data-service:  python3 -m pytest tests/test_group331_global_cues_route.py -v"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import main


def _hist(closes, last_date=None):
    last = last_date or datetime.now(timezone.utc).date()
    idx = pd.to_datetime([last - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))])
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    main._GLOBAL_CUE_STATE["rows"].clear()
    monkeypatch.delenv("GLOBAL_CUE_ALLOWED", raising=False)
    monkeypatch.delenv("GLOBAL_CUE_TTL_S", raising=False)
    monkeypatch.delenv("GLOBAL_CUE_MAX_BAR_AGE_DAYS", raising=False)
    monkeypatch.setattr(main, "_in_cooldown", lambda name: False)
    yield
    main._GLOBAL_CUE_STATE["rows"].clear()


def _patch_yf(monkeypatch, table, calls=None):
    class T:
        def __init__(self, sym):
            self.sym = sym

        def history(self, period=None, interval=None):
            if calls is not None:
                calls.append(self.sym)
            v = table.get(self.sym)
            if isinstance(v, Exception):
                raise v
            return v
    monkeypatch.setattr(main.yf, "Ticker", T)


def test_futures_symbol_is_not_turned_into_an_nse_ticker(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"ES=F": _hist([5000.0, 4900.0])}, seen)
    body = main.get_global_cues(symbols="ES=F")
    assert seen == ["ES=F"]                                   # asked exactly as given, no ".NS"
    assert round(body["rows"]["ES=F"]["day_change_pct"], 3) == -2.0 and body["missing"] == []
    assert body["rows"]["ES=F"]["age_s"] is None


def test_rows_and_missing_are_reported_honestly(monkeypatch):
    _patch_yf(monkeypatch, {"^GDAXI": _hist([100.0, 101.0]), "^FTSE": None, "^N225": RuntimeError("boom"),
                            "^HSI": _hist([100.0])})          # one bar only: no previous close
    body = main.get_global_cues(symbols="^GDAXI,^FTSE,^N225,^HSI")
    assert list(body["rows"]) == ["^GDAXI"] and round(body["rows"]["^GDAXI"]["day_change_pct"], 3) == 1.0
    assert sorted(body["missing"]) == ["^FTSE", "^HSI", "^N225"]


def test_unlisted_symbol_is_rejected_not_fetched(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"RELIANCE.NS": _hist([1.0, 2.0])}, seen)
    body = main.get_global_cues(symbols="RELIANCE.NS")
    assert body["rows"] == {} and body["missing"] == ["RELIANCE.NS"] and seen == []


def test_blank_symbols_means_every_allowed_one(monkeypatch):
    monkeypatch.setenv("GLOBAL_CUE_ALLOWED", " ^GDAXI , ES=F ")
    seen = []
    _patch_yf(monkeypatch, {"^GDAXI": _hist([100.0, 100.5]), "ES=F": _hist([100.0, 99.0])}, seen)
    body = main.get_global_cues(symbols="")
    assert sorted(seen) == ["ES=F", "^GDAXI"] and set(body["rows"]) == {"^GDAXI", "ES=F"}


def test_stale_last_bar_is_dropped(monkeypatch):
    old = datetime.now(timezone.utc).date() - timedelta(days=4)
    _patch_yf(monkeypatch, {"^N225": _hist([100.0, 95.0], last_date=old)})
    body = main.get_global_cues(symbols="^N225")
    assert body["rows"] == {} and body["missing"] == ["^N225"]
    monkeypatch.setenv("GLOBAL_CUE_MAX_BAR_AGE_DAYS", "10")
    main._GLOBAL_CUE_STATE["rows"].clear()
    assert "^N225" in main.get_global_cues(symbols="^N225")["rows"]


def test_cache_serves_second_call_and_caches_a_miss(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"^GDAXI": _hist([100.0, 99.0]), "^FTSE": None}, seen)
    main.get_global_cues(symbols="^GDAXI,^FTSE")
    main.get_global_cues(symbols="^GDAXI,^FTSE")
    assert sorted(seen) == ["^FTSE", "^GDAXI"]               # each fetched once, the miss included
    monkeypatch.setenv("GLOBAL_CUE_TTL_S", "0")
    main.get_global_cues(symbols="^GDAXI")
    assert seen.count("^GDAXI") == 2


def test_yfinance_cooldown_means_no_fetch_and_missing(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"^GDAXI": _hist([100.0, 99.0])}, seen)
    monkeypatch.setattr(main, "_in_cooldown", lambda name: True)
    body = main.get_global_cues(symbols="^GDAXI")
    assert seen == [] and body["rows"] == {} and body["missing"] == ["^GDAXI"]


def test_nan_and_nonpositive_closes_are_unusable(monkeypatch):
    _patch_yf(monkeypatch, {"^GDAXI": _hist([float("nan"), 100.0]), "^FTSE": _hist([0.0, 100.0])})
    body = main.get_global_cues(symbols="^GDAXI,^FTSE")
    assert body["rows"] == {} and sorted(body["missing"]) == ["^FTSE", "^GDAXI"]


def test_duplicate_symbols_are_asked_once(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"ES=F": _hist([100.0, 101.0])}, seen)
    main.get_global_cues(symbols="es=f, ES=F ,ES=F")
    assert seen == ["ES=F"]


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("GLOBAL_CUE_TTL_S", "abc")
    monkeypatch.setenv("GLOBAL_CUE_MAX_BAR_AGE_DAYS", "  ")
    assert main._global_cue_cfg() == (300.0, 1.0)


def test_route_is_registered():
    assert any(getattr(r, "path", "") == "/global-cues" for r in main.app.routes)
