"""Round 553 (E578, Tredye plan P4): GET /global-strip - the display-only strip route. Its own fixed symbols, cache and bar-age limit; the real-trade
carry route /global-cues and its allow-list are untouched. No network: yfinance is faked.

Run from services/market-data-service:  python3 -m pytest tests/test_round553_global_strip.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import main

STRIP = ["^GSPC", "^IXIC", "BZ=F", "DX-Y.NYB", "INR=X", "GC=F"]


def _hist(closes, last_date=None):
    last = last_date or datetime.now(timezone.utc).date()
    idx = pd.to_datetime([last - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))])
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    main._GLOBAL_STRIP_STATE["rows"].clear()
    main._GLOBAL_CUE_STATE["rows"].clear()
    for k in ("GLOBAL_STRIP_ENABLED", "GLOBAL_STRIP_TTL_S", "GLOBAL_STRIP_MAX_BAR_AGE_DAYS", "GLOBAL_CUE_ALLOWED", "GLOBAL_CUE_ERROR_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(main, "_in_cooldown", lambda name: False)
    yield
    main._GLOBAL_STRIP_STATE["rows"].clear()
    main._GLOBAL_CUE_STATE["rows"].clear()


def _patch_yf(monkeypatch, table, calls=None):
    class T:
        def __init__(self, sym):
            self.sym = sym

        def history(self, period=None, interval=None):
            if calls is not None:
                calls.append(self.sym)
            v = table.get(self.sym, _hist([100.0, 101.0]))
            if isinstance(v, Exception):
                raise v
            return v
    monkeypatch.setattr(main.yf, "Ticker", T)


def test_asks_exactly_the_six_fixed_symbols_and_each_row_carries_its_bar_date(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {"^GSPC": _hist([6000.0, 6030.0])}, seen)
    body = main.get_global_strip()
    assert seen == STRIP and body["missing"] == [] and body["enabled"] is True
    row = body["rows"]["^GSPC"]
    assert round(row["day_change_pct"], 3) == 0.5 and row["bar_date"] == datetime.now(timezone.utc).date().isoformat()


def test_unreadable_rows_are_missing_not_invented(monkeypatch):
    _patch_yf(monkeypatch, {"BZ=F": None, "GC=F": RuntimeError("boom"), "INR=X": _hist([83.0])})      # none / error / one bar only
    body = main.get_global_strip()
    assert sorted(body["missing"]) == ["BZ=F", "GC=F", "INR=X"] and set(body["rows"]) == {"^GSPC", "^IXIC", "DX-Y.NYB"}


def test_own_bar_age_limit_is_five_days_by_default(monkeypatch):
    four = datetime.now(timezone.utc).date() - timedelta(days=4)
    seven = datetime.now(timezone.utc).date() - timedelta(days=7)
    _patch_yf(monkeypatch, {"^GSPC": _hist([100.0, 101.0], last_date=four), "^IXIC": _hist([100.0, 101.0], last_date=seven)})
    body = main.get_global_strip()
    assert "^GSPC" in body["rows"] and "^IXIC" in body["missing"]          # a long weekend still shows its last close; a week-old bar does not


def test_cache_is_its_own_and_does_not_touch_the_carry_routes_cache(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {}, seen)
    main.get_global_strip()
    main.get_global_strip()
    assert seen == STRIP                                                    # second call served from the cache
    assert main._GLOBAL_CUE_STATE["rows"] == {}                             # /global-cues cache untouched


def test_carry_route_allow_list_is_unchanged(monkeypatch):
    allowed = main._global_cue_allowed()
    assert allowed == set(main._GLOBAL_CUE_DEFAULT.split(","))
    assert not ({"BZ=F", "GC=F", "INR=X", "DX-Y.NYB"} & allowed)            # the strip's extra symbols are NOT reachable through /global-cues
    seen = []
    _patch_yf(monkeypatch, {}, seen)
    body = main.get_global_cues(symbols="GC=F")
    assert body["rows"] == {} and body["missing"] == ["GC=F"] and seen == []


def test_switch_off_and_cooldown_fetch_nothing(monkeypatch):
    seen = []
    _patch_yf(monkeypatch, {}, seen)
    monkeypatch.setenv("GLOBAL_STRIP_ENABLED", "0")
    body = main.get_global_strip()
    assert seen == [] and body["rows"] == {} and body["enabled"] is False and body["missing"] == STRIP
    monkeypatch.delenv("GLOBAL_STRIP_ENABLED")
    monkeypatch.setattr(main, "_in_cooldown", lambda name: True)
    body = main.get_global_strip()
    assert seen == [] and body["rows"] == {} and body["missing"] == STRIP


def test_a_failed_read_is_remembered_only_the_short_error_ttl(monkeypatch):
    calls = []
    _patch_yf(monkeypatch, {"GC=F": RuntimeError("blip")}, calls)
    main.get_global_strip()
    main.get_global_strip()
    assert calls.count("GC=F") == 1                                         # default 30 s: not asked again at once
    main._GLOBAL_STRIP_STATE["rows"].clear()
    calls.clear()
    monkeypatch.setenv("GLOBAL_CUE_ERROR_TTL_S", "0")                       # read when the failure is stored; 0 = retry on every call
    main.get_global_strip()
    main.get_global_strip()
    assert calls.count("GC=F") == 2 and calls.count("^GSPC") == 1           # the failed one is retried, the good rows stay cached
