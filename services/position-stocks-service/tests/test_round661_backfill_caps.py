"""round 661 (D5): the restart backfill is capped per symbol and per IST day, so a pruned-and-recreated name cannot be fetched in a loop."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import bar_backfill, bar_store as bs

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=IST).timestamp()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bs.reset(); bar_backfill.reset()
    monkeypatch.setattr(config, "BOARD_BACKFILL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BOARD_BACKFILL_RETRY_S", 60, raising=False)
    yield
    bs.reset(); bar_backfill.reset()


def _fetch(_s):
    return []


def test_symbol_requested_at_most_max_per_symbol(monkeypatch):
    monkeypatch.setattr(config, "BOARD_BACKFILL_MAX_PER_SYMBOL", 2, raising=False)
    now = T0
    for i in range(5):
        now += 4000                          # past the retry window every time
        bar_backfill.request(["AAA"], now)
        bar_backfill.run_pending(now=now, fetch=_fetch)
    assert bar_backfill.state()["fetches_today"] == 2


def test_daily_cap_stops_all_fetches(monkeypatch):
    monkeypatch.setattr(config, "BOARD_BACKFILL_DAILY_MAX", 5, raising=False)
    monkeypatch.setattr(config, "BOARD_BACKFILL_MAX_PER_SYMBOL", 9, raising=False)
    syms = [f"S{i}" for i in range(20)]
    assert bar_backfill.request(syms, T0) == 20
    did = bar_backfill.run_pending(max_n=20, now=T0, fetch=_fetch)
    assert did["tried"] == 5
    assert bar_backfill.request(["NEW"], T0 + 10) == 0
    assert bar_backfill.run_pending(now=T0 + 20, fetch=_fetch)["tried"] == 0


def test_counters_reset_next_ist_day(monkeypatch):
    monkeypatch.setattr(config, "BOARD_BACKFILL_DAILY_MAX", 1, raising=False)
    bar_backfill.request(["AAA"], T0)
    bar_backfill.run_pending(now=T0, fetch=_fetch)
    assert bar_backfill.request(["BBB"], T0 + 60) == 0
    nxt = T0 + 86400
    assert bar_backfill.request(["BBB"], nxt) == 1
    assert bar_backfill.run_pending(now=nxt, fetch=_fetch)["tried"] == 1
