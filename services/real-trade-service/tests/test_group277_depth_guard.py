"""group277: Dhan depth on the tick (spread_pct, book_value_5) holds back wide-spread / thin-book entries.
Run: python3 -m pytest tests/test_group277_depth_guard.py -q"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from entry_engine import entry
from market_feed import feed


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER1_MAX_DROP_PCT",
              "WATCHLIST_MAX_CHASE_PCT", "WATCHLIST_MAX_SPREAD_PCT", "WATCHLIST_MIN_BOOK_VALUE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(entry, "_watchlist_instrument_reason", lambda s, p: None)


def reason(**tick):
    t = NS(price=100.0, prev_close=None, **tick)
    return entry._watchlist_adverse_reason(NS(symbol="TESTCO", source_tier=2), 0.0, t, baseline_just_set=False)


def test_wide_spread_is_held():
    r = reason(spread_pct=0.9)
    assert r and "spread 0.90%" in r


def test_tight_spread_passes():
    assert reason(spread_pct=0.2) is None


def test_unknown_depth_never_blocks():
    assert reason() is None
    assert reason(spread_pct=None, book_value_5=None) is None


def test_limit_from_env(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_SPREAD_PCT", "1.2")
    assert reason(spread_pct=0.9) is None
    monkeypatch.setenv("WATCHLIST_MAX_SPREAD_PCT", "0")
    assert reason(spread_pct=5.0) is None


def test_thin_book_off_by_default_and_on_with_env(monkeypatch):
    assert reason(book_value_5=1000.0) is None
    monkeypatch.setenv("WATCHLIST_MIN_BOOK_VALUE", "50000")
    r = reason(book_value_5=1000.0)
    assert r and "book" in r
    assert reason(book_value_5=80000.0) is None


def test_master_guard_switch(monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    assert reason(spread_pct=3.0) is None


def test_tick_carries_depth_and_defaults_to_none():
    t = feed.Tick("X", 10.0, datetime.now(timezone.utc), None, "t")
    assert t.spread_pct is None and t.book_value_5 is None
    t2 = feed.Tick("X", 10.0, datetime.now(timezone.utc), None, "t", spread_pct=0.3, book_value_5=9000.0)
    assert (t2.spread_pct, t2.book_value_5) == (0.3, 9000.0)


@pytest.mark.parametrize("raw,want", [("0.4", 0.4), (0, 0.0), (None, None), ("x", None), (-1, None),
                                      (float("nan"), None), (float("inf"), None)])
def test_safe_depth_num(raw, want):
    assert feed._safe_depth_num(raw) == want


# ── group278: price age (source + as_of) ─────────────────────────────────────
from datetime import timedelta


def _aged(seconds, source="market-data-service", naive=False):
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return NS(price=100.0, prev_close=None, as_of=t.replace(tzinfo=None) if naive else t, source=source)


def _age_reason(tick):
    return entry._watchlist_adverse_reason(NS(symbol="TESTCO", source_tier=2), 0.0, tick, baseline_just_set=False)


def test_fresh_tick_passes(monkeypatch):
    monkeypatch.delenv("WATCHLIST_MAX_TICK_AGE_S", raising=False)
    assert _age_reason(_aged(2)) is None


def test_old_tick_is_held_and_names_the_source(monkeypatch):
    monkeypatch.delenv("WATCHLIST_MAX_TICK_AGE_S", raising=False)
    r = _age_reason(_aged(120, source="stale_last_good(dhan)"))
    assert r and "stale_last_good(dhan)" in r and "120s old" in r


def test_age_limit_from_env_and_off(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_TICK_AGE_S", "300")
    assert _age_reason(_aged(120)) is None
    monkeypatch.setenv("WATCHLIST_MAX_TICK_AGE_S", "0")
    assert _age_reason(_aged(5000)) is None


def test_naive_as_of_is_read_as_utc(monkeypatch):
    monkeypatch.delenv("WATCHLIST_MAX_TICK_AGE_S", raising=False)
    assert _age_reason(_aged(120, naive=True)) is not None
    assert _age_reason(_aged(2, naive=True)) is None


def test_tick_without_as_of_never_blocks(monkeypatch):
    monkeypatch.delenv("WATCHLIST_MAX_TICK_AGE_S", raising=False)
    assert _age_reason(NS(price=100.0, prev_close=None)) is None
    assert _age_reason(NS(price=100.0, prev_close=None, as_of="not a date")) is None
