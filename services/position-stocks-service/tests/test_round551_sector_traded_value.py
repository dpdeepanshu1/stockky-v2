"""Round 551 (P2): the board's sectors carry traded value, its share of the tracked market, and up / down counts. Display data only: ranking, rows and every switch are unchanged."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import feed.ws_client as wsc
from feed import bar_store as bs
from feed import fno_list as fl
from screening import board, prev_day
from tz_utils import ist_today_str

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 10, 12, 9, 15, tzinfo=IST).timestamp()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bs.reset(); board.reset(); prev_day.reset()
    wsc._day_stats.clear(); wsc._exchange_vwap.clear(); wsc._tick_buffers.clear()
    monkeypatch.setattr(config, "BOARD_MIN_DAY_VOLUME", 1000, raising=False)
    monkeypatch.setattr(config, "BOARD_CACHE_S", 0.0, raising=False)
    monkeypatch.setattr(fl, "_CACHE_PATH", "/nonexistent-dir/x.json", raising=False)
    yield
    bs.reset(); board.reset(); prev_day.reset()


def _st(count=1, chg=0.0, up=0, down=0, turn=0.0, turn_n=0):
    return {"name": "x", "count": count, "sum_chg": chg, "bull": 0, "bear": 0, "up": up, "down": down, "turn": turn, "turn_n": turn_n}


def test_shares_add_to_100_and_use_traded_value_of_all_sectors():
    rows = board.sector_rows({"A": {**_st(2, 3.0, 2, 0, 600.0, 2), "name": "A"}, "B": {**_st(1, -1.0, 0, 1, 300.0, 1), "name": "B"}, "C": {**_st(1, 0.0, 0, 0, 100.0, 1), "name": "C"}})
    by = {r["name"]: r for r in rows}
    assert by["A"]["turnover_share_pct"] == 60.0 and by["B"]["turnover_share_pct"] == 30.0 and by["C"]["turnover_share_pct"] == 10.0
    assert abs(sum(r["turnover_share_pct"] for r in rows) - 100.0) < 0.05
    assert by["A"]["turnover"] == 600.0 and by["A"]["up"] == 2 and by["B"]["down"] == 1 and by["A"]["avg_change_pct"] == 1.5


def test_a_sector_with_no_volume_is_none_not_zero_and_does_not_take_a_share():
    rows = board.sector_rows({"A": {**_st(1, 1.0, 1, 0, 500.0, 1), "name": "A"}, "Z": {**_st(2, 0.0, 0, 0, 0.0, 0), "name": "Z"}})
    by = {r["name"]: r for r in rows}
    assert by["Z"]["turnover"] is None and by["Z"]["turnover_share_pct"] is None
    assert by["A"]["turnover_share_pct"] == 100.0


def test_no_volume_anywhere_gives_no_shares_at_all():
    rows = board.sector_rows({"A": {**_st(1, 1.0, 1, 0), "name": "A"}})
    assert rows[0]["turnover"] is None and rows[0]["turnover_share_pct"] is None and rows[0]["up"] == 1


def test_empty_gives_empty():
    assert board.sector_rows({}) == []


def _feed(sym, a, b, vol_step):
    n = 30
    for i in range(n):
        bs.on_tick(sym, a + (b - a) * i / (n - 1), 10_000 + vol_step * i, OPEN + 60 * i + 5)
    prev_day._cache[sym] = (ist_today_str(), prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-09", 2.0))
    wsc._day_stats[sym] = (100.0, max(a, b), min(a, b), 99.0)


def test_build_payload_has_the_new_fields_and_they_match_each_stock_turnover(monkeypatch):
    monkeypatch.setattr(board.sector_map, "sector_of", lambda s: "UP" if s.startswith("U") else "DN")
    _feed("U1", 100, 104, 1000); _feed("U2", 100, 102, 3000); _feed("D1", 100, 97, 2000)
    out = board.build(use_cache=False, now=OPEN + 3600)
    by = {s["name"]: s for s in out["sectors"]}
    want_up = bs.turnover("U1") + bs.turnover("U2"); want_dn = bs.turnover("D1")
    assert abs(by["UP"]["turnover"] - round(want_up)) <= 1 and abs(by["DN"]["turnover"] - round(want_dn)) <= 1
    assert abs(by["UP"]["turnover_share_pct"] + by["DN"]["turnover_share_pct"] - 100.0) < 0.05
    assert by["UP"]["up"] == 2 and by["UP"]["down"] == 0 and by["DN"]["down"] == 1
    assert {"name", "count", "avg_change_pct", "bull", "bear"} <= set(by["UP"])        # nothing existing was removed


def test_ranking_is_unchanged_by_the_new_fields():
    _feed("BULL1", 100, 104, 1000); _feed("BEAR1", 100, 97, 1000)
    out = board.build(use_cache=False, now=OPEN + 3600)
    assert [r["symbol"] for r in out["bullish"]] == ["BULL1"] and [r["symbol"] for r in out["bearish"]] == ["BEAR1"]
