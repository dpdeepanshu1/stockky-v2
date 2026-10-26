"""E210: GET /quote/{symbol} answered from a live-feed tick stamped fetched_at = the time of the READ (a 14 s old tick came back as 0 s old, and was
cached under that stamp). Same defect E188 fixed for /quotes/bulk; the row now carries the tick's own time (`ts`).

Run from services/market-data-service:
    python3 -m pytest tests/test_e210_single_quote_live_tick_fetched_at.py -v
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("QUOTE_BULK_LIVE_TICK_TS", None)

import pytest

import main


@pytest.fixture()
def harness(monkeypatch):
    cached: dict = {}
    state = types.SimpleNamespace(ao=None, yh=None)
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper()}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_dhan_quote_position", lambda: "off")
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cached.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_set", lambda k, v, *a, **k2: None)
    ao = types.ModuleType("angelone_ws_feed")
    ao.get_live_quote = lambda sym, max_age_sec=20.0: state.ao
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", ao)
    yh = types.ModuleType("yahoo_ws_feed")
    yh.get_live_quote = lambda sym, max_age_sec=20.0: state.yh
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", yh)
    state.cached = cached
    return state


def _tick(age_s, **extra):
    return {"price": 100.0, "ts": time.time() - age_s, **extra}


@pytest.mark.parametrize("feed", ["ao", "yh"])
def test_row_carries_the_ticks_own_time(harness, feed):
    setattr(harness, feed, _tick(14.0))
    row = main._get_quote_inner("TCS")
    assert row["source"] == ("angelone_ws" if feed == "ao" else "yahoo_ws")
    assert 13.0 <= main._quote_row_age_s(row) <= 16.0          # was ~0 s: stamped with the read time


def test_the_cached_copy_keeps_the_tick_time(harness):
    harness.ao = _tick(14.0)
    main._get_quote_inner("TCS")
    cached_row = harness.cached["quote:TCS.NS"]
    assert 13.0 <= main._quote_row_age_s(cached_row) <= 16.0


def test_fresh_tick_is_about_zero(harness):
    harness.ao = _tick(0.0)
    assert main._quote_row_age_s(main._get_quote_inner("TCS")) < 2.0


@pytest.mark.parametrize("ts", [None, "junk", 0, -5, float("nan")])
def test_missing_or_unusable_ts_falls_back_to_now(harness, ts):
    harness.ao = {"price": 100.0, "ts": ts}
    assert main._quote_row_age_s(main._get_quote_inner("TCS")) < 2.0


def test_future_ts_is_clamped_to_now(harness):
    harness.ao = _tick(-60.0)
    assert main._quote_row_age_s(main._get_quote_inner("TCS")) < 2.0


def test_switch_restores_the_old_stamping(harness, monkeypatch):
    monkeypatch.setenv("QUOTE_BULK_LIVE_TICK_TS", "0")
    harness.ao = _tick(14.0)
    assert main._quote_row_age_s(main._get_quote_inner("TCS")) < 2.0
