"""group305/307: screening/board.py - the board payload from synthetic ticks (no network, no DB)."""
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
    monkeypatch.setattr(config, "BOARD_ZONES_ENABLED", True, raising=False)
    monkeypatch.setattr(fl, "_CACHE_PATH", "/nonexistent-dir/x.json", raising=False)
    yield
    bs.reset(); board.reset(); prev_day.reset()


def feed(sym, prices, start=OPEN, vol_step=1000, day_stats=None, avg_range=2.0, with_prev=True):
    for i, p in enumerate(prices):
        bs.on_tick(sym, p, 10_000 + vol_step * i, start + 60 * i + 5)
    if day_stats is not None:
        wsc._day_stats[sym] = day_stats
    if with_prev:
        prev_day._cache[sym] = (ist_today_str(), prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-09", avg_range))


def ramp(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_ranks_sides_and_drops_choppy_and_warming():
    feed("BULL1", ramp(100, 104, 30), day_stats=(100.0, 104.0, 99.9, 99.0))
    feed("BULL2", ramp(100, 102, 30), day_stats=(100.0, 102.0, 99.95, 99.0))
    feed("BEAR1", ramp(100, 97, 30), day_stats=(100.0, 100.1, 97.0, 101.0))
    feed("CHOP", ramp(100, 101, 30), day_stats=(100.0, 104.0, 97.0, 100.0))
    feed("NOPREV", ramp(100, 103, 30), day_stats=(100.0, 103.0, 100.0, 99.0), with_prev=False)
    out = board.build(use_cache=False, now=OPEN + 3600)
    assert [r["symbol"] for r in out["bullish"]] == ["BULL1", "BULL2"] and [r["symbol"] for r in out["bearish"]] == ["BEAR1"]
    assert out["bullish"][0]["percentile"] > out["bullish"][1]["percentile"]
    assert out["coverage"]["warming"] == 1 and out["coverage"]["tracked"] == 5
    m = out["market"]
    assert m["bull_count"] == 2 and m["bear_count"] == 1 and m["up"] + m["down"] + m["flat"] == 5


def test_row_fields():
    feed("BULL1", ramp(100, 104, 30), day_stats=(100.0, 104.0, 99.9, 99.0))
    r = board.build(use_cache=False, now=OPEN + 3600)["bullish"][0]
    for k in ("symbol", "sector", "is_fno", "ltp", "change_pct", "strength", "rank", "percentile", "conviction", "vwap", "open",
              "or_low", "or_high", "profile", "spark", "in_position", "zone_status", "chase_atr", "rr", "rule", "turnover", "side", "rank_delta"):
        assert k in r, k
    assert r["strength"] == pytest.approx(2.0) and r["change_pct"] == pytest.approx(5.05, abs=0.01)
    assert 0 <= r["conviction"] <= 100 and len(r["profile"]) == 16 and len(r["spark"]) <= 40 and "_bars" not in r and "score" not in r


def test_day_open_fallback_and_exchange_vwap():
    feed("FROMOPEN", ramp(100, 103, 30))
    feed("COLDSTART", ramp(100, 103, 30), start=OPEN + 3600)
    out = board.build(use_cache=False, now=OPEN + 7200)
    assert [r["symbol"] for r in out["bullish"]] == ["FROMOPEN"] and out["coverage"]["no_day_open"] == 1
    wsc._exchange_vwap["FROMOPEN"] = 101.5
    assert board.build(use_cache=False, now=OPEN + 7200)["bullish"][0]["vwap"] == 101.5


def test_universes_held_limit(monkeypatch):
    feed("RELIANCE", ramp(100, 104, 30), day_stats=(100.0, 104.0, 99.9, 99.0), vol_step=5000)
    feed("NOTFNO1", ramp(100, 103, 30), day_stats=(100.0, 103.0, 99.9, 99.0), vol_step=10)
    a = board.build("all", use_cache=False, now=OPEN + 3600, open_symbols=["notfno1"])
    assert {r["symbol"]: r["in_position"] for r in a["bullish"]} == {"RELIANCE": False, "NOTFNO1": True}
    assert {r["symbol"]: r["is_fno"] for r in a["bullish"]} == {"RELIANCE": True, "NOTFNO1": False}
    assert [r["symbol"] for r in board.build("fno", use_cache=False, now=OPEN + 3600)["bullish"]] == ["RELIANCE"]
    monkeypatch.setattr(config, "BOARD_LIQUID_TOP_N", 10, raising=False)
    assert board.build("liquid", use_cache=False, now=OPEN + 3600)["universe"] == "liquid"
    assert board.build("bogus", use_cache=False, now=OPEN + 3600)["universe"] == config.BOARD_UNIVERSE_DEFAULT
    bs.reset()
    for i in range(12):
        feed(f"S{i}", ramp(100, 102 + i * 0.3, 30), day_stats=(100.0, 102 + i * 0.3, 99.99, 99.0))
    out = board.build(limit=5, use_cache=False, now=OPEN + 3600)
    assert len(out["bullish"]) == 5 and out["coverage"]["ranked"] == 12


def test_sector_summary():
    for sym in ("HDFCBANK", "ICICIBANK", "SBIN"):
        feed(sym, ramp(100, 103, 30), day_stats=(100.0, 103.0, 99.95, 99.0))
    for sym in ("TCS", "INFY", "WIPRO"):
        feed(sym, ramp(100, 97, 30), day_stats=(100.0, 100.05, 97.0, 101.0))
    out = board.build(use_cache=False, now=OPEN + 3600)
    names = {s["name"]: s for s in out["sectors"]}
    assert names["Banks"]["avg_change_pct"] > 0 > names["IT"]["avg_change_pct"]
    assert out["market"]["leading_sector"] == "Banks" and out["market"]["lagging_sector"] == "IT"
    assert names["Banks"]["bull"] == 3 and names["IT"]["bear"] == 3


def test_rank_delta():
    feed("A", ramp(100, 102, 30), day_stats=(100.0, 102.0, 99.95, 99.0))
    feed("B", ramp(100, 103, 30), day_stats=(100.0, 103.0, 99.95, 99.0))
    t = OPEN + 3600
    first = board.build(use_cache=False, now=t)
    assert first["bullish"][0]["symbol"] == "B" and first["bullish"][0]["rank_delta"] is None
    feed("A", ramp(102, 106, 5), start=OPEN + 30 * 60, day_stats=(100.0, 106.0, 99.95, 99.0))
    second = board.build(use_cache=False, now=t + 60)
    assert second["bullish"][0]["symbol"] == "A" and second["bullish"][0]["rank_delta"] == 1 and second["bullish"][1]["rank_delta"] == -1


def test_cache_empty_and_version(monkeypatch):
    monkeypatch.setattr(config, "BOARD_CACHE_S", 5.0, raising=False)
    feed("A", ramp(100, 102, 30), day_stats=(100.0, 102.0, 99.95, 99.0))
    a = board.build(now=OPEN + 3600)
    assert board.build(now=OPEN + 3602) is a and board.build(now=OPEN + 3610) is not a
    bs.reset(); board.reset()
    out = board.build(use_cache=False, now=OPEN)
    assert out["bullish"] == [] and out["market"]["tracked"] == 0 and out["params_version"] == config.BOARD_PARAMS_VERSION and "connected" in out["feed"]


def test_symbol_view_and_zone_view(monkeypatch):
    prices = [100.0] * 12 + [99.8, 99.6, 99.5, 102.4, 102.6, 102.8, 103.0, 103.2] + [103.3] * 10
    feed("ZONE", prices, day_stats=(100.0, 103.3, 99.4, 99.0))
    v = board.symbol_view("zone", held={"entry": 101.0, "stop": 100.0, "target": 103.0}, now=OPEN + 3600)
    assert v["symbol"] == "ZONE" and v["position"]["stop"] == 100.0 and len(v["candles"]) >= 5 and len(v["vwap_series"]) == len(v["candles"])
    assert set(v["levels"]) == {"supports", "resistances"} and board.symbol_view("NOPE") is None
    a = board.zone_view("ZONE", "bull", 103.3); b = board.zone_view("ZONE", "bull", 103.3)
    assert a is not None and b["levels"] == a["levels"]
    feed("SHORT", ramp(100, 102, 5), day_stats=(100.0, 102.0, 99.95, 99.0))
    assert board.zone_view("SHORT", "bull", 102.0) is None
    monkeypatch.setattr(config, "BOARD_ZONES_ENABLED", False, raising=False)
    assert board.zone_view("ZONE", "bull", 103.3) is None


def test_rule_label_open_ts_and_prev_day_avg():
    assert board._rule_label("bull", 1, 90, 0.2) == "PICK · BULLISH #1" and board._rule_label("bear", 9, 90, None) == "WATCH · BEARISH #9"
    assert board._rule_label("bull", 1, 95, config.BOARD_CHASE_ATR + 1).startswith("EXTENDED")
    day = int((OPEN + 330 * 60) // 86400)
    assert board.session_open_ts(day) == OPEN and board.session_open_ts(None) is None
    c = [{"date": f"2026-09-{d:02d}", "high": 102, "low": 100, "close": 101} for d in range(1, 30)]
    assert prev_day.parse_candles(c, "2026-10-10").avg_range_pct == pytest.approx(1.98, abs=0.01)
    assert prev_day.parse_candles(c[:5], "2026-10-10").avg_range_pct is None
