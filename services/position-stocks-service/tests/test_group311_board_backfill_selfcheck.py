"""group311: restart backfill, zone score / risk, F&O parser layouts + diagnose, board self-check (check B), sector coverage."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import feed.ws_client as wsc
from feed import bar_backfill, bar_store as bs, fno_list as fl, sector_map
from screening import board, board_runner, board_selfcheck, market_structure as ms, prev_day
from tz_utils import ist_today_str

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 10, 12, 9, 15, tzinfo=IST).timestamp()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bs.reset(); board.reset(); prev_day.reset(); bar_backfill.reset()
    wsc._day_stats.clear(); wsc._exchange_vwap.clear(); wsc._tick_buffers.clear()
    monkeypatch.setattr(config, "BOARD_MIN_DAY_VOLUME", 1000, raising=False)
    monkeypatch.setattr(config, "BOARD_CACHE_S", 0.0, raising=False)
    monkeypatch.setattr(config, "BOARD_BACKFILL_ENABLED", True, raising=False)
    monkeypatch.setattr(fl, "_CACHE_PATH", "/nonexistent-dir/x.json", raising=False)
    yield
    bs.reset(); board.reset(); prev_day.reset(); bar_backfill.reset()


def _label(ts):
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M")


def _candles(start, n, p0=100.0, step=0.1):
    out = []
    for i in range(n):
        p = p0 + step * i
        out.append({"date": _label(start + 60 * i), "open": p, "high": p + 0.05, "low": p - 0.05, "close": p + 0.05, "volume": 1000})
    return out


def _late_symbol(sym="LATE", minutes_late=40, n_live=25, p0=104.0, step=0.1):
    start = OPEN + minutes_late * 60
    for i in range(n_live):
        bs.on_tick(sym, p0 + step * i, 50_000 + 1000 * i, start + 60 * i + 5)
    prev_day._cache[sym] = (ist_today_str(), prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-09", 2.0))
    return start


def test_zone_score_bounds_and_ordering():
    best = ms.zone_score({"bos": True, "fvg": True, "status": "fresh", "valid": True}, 3.0, 0.0)
    weak = ms.zone_score({"status": "tested", "valid": False}, 0.5, 1.5)
    assert best == 100 and weak < 20 and best > weak
    assert ms.zone_score(None, 2.0, 0.0) is None
    assert 0 <= ms.zone_score({"status": "fresh", "valid": True}, None, None) <= 100


def test_zone_score_unknown_chase_is_half_credit_and_nan_safe():
    z = {"bos": False, "fvg": False, "status": "tested", "valid": False}
    assert ms.zone_score(z, None, None) == 10
    assert ms.zone_score(z, float("nan"), float("nan")) == 10


def test_zone_risk_sides_and_none_cases():
    zb = {"top": 101.0, "bottom": 99.0}
    assert ms.zone_risk("bull", 103.0, zb) == (99.0, 4.0)
    assert ms.zone_risk("bear", 97.0, zb) == (101.0, 4.0)
    assert ms.zone_risk("bull", 98.0, zb) is None
    assert ms.zone_risk("bull", 103.0, None) is None
    assert ms.zone_risk("bull", float("nan"), zb) is None


def test_insert_history_merges_only_earlier_minutes():
    start = _late_symbol()
    hist = [(OPEN + 60 * i, 100 + i * 0.1, 100.2 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1, 500.0) for i in range(40)]
    hist.append((start, 1.0, 2.0, 0.5, 1.5, 5.0))
    hist.append((start + 3600, 1.0, 2.0, 0.5, 1.5, 5.0))
    n = bs.insert_history("LATE", hist)
    bars = bs.get_bars("LATE")
    assert n == 40 and bars[0][0] == OPEN and bars[40][0] == start
    assert bars[40][1] != 1.0 and [b[0] for b in bars] == sorted(b[0] for b in bars)
    assert bs.is_backfilled("LATE") and bs.stats()["backfilled_symbols"] == 1 and bs.stats()["backfilled_bars"] == 40


def test_insert_history_never_creates_a_symbol_and_drops_bad_rows():
    assert bs.insert_history("NOPE", [(OPEN, 1, 1, 1, 1, 1)]) == 0 and bs.symbols() == []
    _late_symbol()
    bad = [(OPEN, -1, 2, 1, 1, 1), (OPEN + 60, 1, 1, 2, 1, 1), ("x", 1, 1, 1, 1, 1), (OPEN + 120, 5, 6, 4, 5, float("nan"))]
    assert bs.insert_history("LATE", bad) == 0
    assert bs.insert_history("LATE", []) == 0


def test_insert_history_respects_max_bars_by_dropping_oldest():
    start = _late_symbol(n_live=5)
    hist = [(start - 60 * (i + 1), 100, 101, 99, 100, 1.0) for i in range(bs.MAX_BARS + 50)]
    bs.insert_history("LATE", hist)
    bars = bs.get_bars("LATE")
    assert len(bars) == bs.MAX_BARS and bars[-1][0] == start + 60 * 4


def test_candles_to_bars_filters_day_open_and_cutoff():
    start = _late_symbol()
    day = bs.day_number()
    yesterday = [{"date": "2026-10-09 10:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1}]
    pre = [{"date": _label(OPEN - 300), "open": 99, "high": 99, "low": 99, "close": 99, "volume": 1}]
    cs = yesterday + pre + _candles(OPEN, 60) + [{"date": "garbage", "open": 1}, {"date": _label(OPEN + 61 * 60), "open": 0, "high": 1, "low": 1, "close": 1}]
    bars = bar_backfill.candles_to_bars(cs, day, start)
    assert bars and bars[0][0] == OPEN and all(b[0] < start for b in bars) and len(bars) == 40
    assert bar_backfill.candles_to_bars(None, day, start) == []


def test_request_dedupes_and_respects_retry_window(monkeypatch):
    assert bar_backfill.request(["A", "B", "A"], now=1000.0) == 2
    assert bar_backfill.request(["A"], now=1001.0) == 0
    bar_backfill._pending.clear()
    bar_backfill._tried["A"] = 1000.0
    assert bar_backfill.request(["A"], now=1100.0) == 0
    assert bar_backfill.request(["A"], now=1000.0 + 5000) == 1
    monkeypatch.setattr(config, "BOARD_BACKFILL_ENABLED", False, raising=False)
    assert bar_backfill.request(["Z"], now=1.0) == 0


def test_run_pending_merges_counts_and_survives_a_failing_symbol():
    _late_symbol("GOOD"); _late_symbol("BAD"); _late_symbol("NONE")

    def fetch(sym):
        if sym == "BAD":
            raise RuntimeError("market-data down")
        if sym == "NONE":
            return []
        return _candles(OPEN, 60)

    bar_backfill.request(["GOOD", "BAD", "NONE"], now=10.0)
    did = bar_backfill.run_pending(max_n=5, now=20.0, fetch=fetch)
    assert did["tried"] == 3 and did["merged_symbols"] == 1 and did["errors"] == 1 and did["empty"] == 1
    assert bs.get_bars("GOOD")[0][0] == OPEN and not bs.is_backfilled("BAD")
    st = bar_backfill.state()
    assert st["pending"] == 0 and st["errors"] == 1 and "BAD" in st["last_error"]
    assert bar_backfill.request(["GOOD"], now=30.0) == 0
    assert bar_backfill.run_pending(max_n=5, now=40.0, fetch=fetch)["tried"] == 0


def test_run_pending_honours_per_cycle_cap():
    for s in ("A1", "A2", "A3", "A4"):
        _late_symbol(s)
    bar_backfill.request(["A1", "A2", "A3", "A4"], now=1.0)
    assert bar_backfill.run_pending(max_n=2, now=2.0, fetch=lambda s: [])["tried"] == 2 and bar_backfill.pending_count() == 2


def test_board_queues_late_symbol_then_ranks_it_after_backfill():
    start = _late_symbol("LATE", minutes_late=40, p0=104.0)
    out = board.build(use_cache=False, now=start + 3600)
    assert out["coverage"]["no_day_open"] == 1 and out["coverage"]["late_start"] == 1 and not out["bullish"]
    assert bar_backfill.pending_count() == 1
    bar_backfill.run_pending(max_n=5, now=start + 3601, fetch=lambda s: _candles(OPEN, 80, p0=100.0, step=0.05))
    out2 = board.build(use_cache=False, now=start + 3602)
    assert out2["coverage"]["no_day_open"] == 0 and out2["coverage"]["late_start"] == 0
    assert [r["symbol"] for r in out2["bullish"]] == ["LATE"]
    assert out2["coverage"]["backfill"]["merged_symbols"] == 1
    sv = board.symbol_view("LATE")
    assert sv["backfilled"] is True and len(sv["candles"]) > 10 and "zone_score" in sv and "invalidation_risk" in sv


def test_board_rows_carry_zone_score_and_risk_fields():
    for i, p in enumerate([100 + 0.15 * i for i in range(40)]):
        bs.on_tick("TRND", p, 10_000 + 1000 * i, OPEN + 60 * i + 5)
    wsc._day_stats["TRND"] = (100.0, 105.9, 99.95, 99.0)
    prev_day._cache["TRND"] = (ist_today_str(), prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-09", 2.0))
    r = board.build(use_cache=False, now=OPEN + 3600)["bullish"][0]
    assert "zone_score" in r and "risk_per_share" in r and "invalidation" in r
    if r["zone_score"] is not None:
        assert 0 <= r["zone_score"] <= 100
    if r["risk_per_share"] is not None:
        assert r["risk_per_share"] > 0 and r["invalidation"] < r["ltp"]


def test_runner_drains_backfill_queue_and_counts():
    _late_symbol("LATE")
    bar_backfill.request(["LATE"], now=1.0)
    board_runner.reset()
    called = []
    did = board_runner.run_once(OPEN + 7200, True, open_symbols_fn=lambda: [], backfill_fn=lambda: called.append(1) or {"merged_symbols": 2})
    assert called == [1] and did["backfill"] == 2 and board_runner.state()["backfilled"] == 2


COMPACT = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL\n"
           "NSE,D,FUTSTK,RELIANCE-Oct2026-FUT\nNSE,D,FUTSTK,BAJAJ-AUTO-Oct2026-FUT\nNSE,D,FUTSTK,M&M-Nov2026-FUT\n"
           "NSE,D,OPTSTK,TCS-Oct2026-3000-CE\nBSE,D,FUTSTK,XYZ-Oct2026-FUT\nNSE,D,FUTIDX,NIFTY-Oct2026-FUT\n").splitlines()
DETAILED = ("\ufeffEXCH_ID,SEGMENT,INSTRUMENT,UNDERLYING_SYMBOL,SYMBOL_NAME\nNSE,D,FUTSTK,INFY,INFY-Oct2026-FUT\n"
            "NSE,D,FUTSTK,,HDFCBANK-Oct2026-FUT\nNSE,D,FUTIDX,NIFTY,NIFTY-Oct2026-FUT\n").splitlines()


def test_parse_report_compact_layout():
    r = fl.parse_report(COMPACT)
    assert r["shape"] == "compact" and r["underlyings"] == {"RELIANCE", "BAJAJ-AUTO", "M&M"} and r["futstk_rows"] == 3


def test_parse_report_detailed_layout_with_bom_and_blank_underlying():
    r = fl.parse_report(DETAILED)
    assert r["shape"] == "detailed" and r["underlyings"] == {"INFY", "HDFCBANK"}


def test_parse_report_unknown_and_empty_layouts():
    assert fl.parse_report(["A,B,C", "1,2,3"])["shape"] == "unknown"
    assert fl.parse_report([])["underlyings"] == set()
    assert fl.parse_fut_underlyings(COMPACT) == {"RELIANCE", "BAJAJ-AUTO", "M&M"}


def test_diagnose_verdicts():
    d = fl.diagnose(fl.parse_report(DETAILED))
    assert d["ok"] is False and "not plausible" in d["verdict"]
    big = {"shape": "compact", "rows": 9, "futstk_rows": 9, "header": [], "sample": [], "suspicious": [], "underlyings": set(fl._BUILTIN)}
    ok = fl.diagnose(big)
    assert ok["ok"] is True and ok["verdict"].startswith("ok") and ok["in_builtin_not_in_file"] == []
    sus = dict(big, suspicious=["BAD-NAME-2026"])
    assert fl.diagnose(sus)["ok"] is False and "look wrong" in fl.diagnose(sus)["verdict"]
    unk = fl.diagnose(fl.parse_report(["A,B", "1,2"]))
    assert unk["ok"] is False and "not recognised" in unk["verdict"]


def test_diagnose_download_failure_is_reported_not_raised(monkeypatch):
    def boom(report=False):
        raise RuntimeError("no network")
    monkeypatch.setattr(fl, "_download", boom)
    d = fl.diagnose()
    assert d["ok"] is False and "built-in list" in d["verdict"]


def _row(sym, **kw):
    base = {"symbol": sym, "price": 100.0, "open": 99.0, "avg_price": 99.8, "day_high": 101.0, "day_low": 98.5, "previous_close": 98.0}
    base.update(kw)
    return base


def test_evaluate_bulk_ok_and_market_closed_note():
    rows = [_row(s) for s in ("A", "B", "C", "D", "E")]
    r = board_selfcheck.evaluate_bulk(rows, True)
    assert r["ok"] and r["with_open"] == 5 and r["with_avg_price"] == 5 and r["verdict"].startswith("ok")
    assert "market closed" in board_selfcheck.evaluate_bulk(rows, False)["verdict"]


def test_evaluate_bulk_flags_missing_open_and_avg_price():
    r = board_selfcheck.evaluate_bulk([_row(s, open=None, avg_price=None) for s in ("A", "B", "C")], True)
    assert not r["ok"] and "open present on 0/3" in r["verdict"] and "avg_price present on 0/3" in r["verdict"]


def test_evaluate_bulk_flags_inconsistent_values_and_empty():
    r = board_selfcheck.evaluate_bulk([_row("A", open=900.0), _row("B"), _row("C"), _row("D"), _row("E")], True)
    assert not r["ok"] and r["problems"] and "outside day range" in r["problems"][0]
    assert board_selfcheck.evaluate_bulk([], True)["ok"] is False
    assert board_selfcheck.evaluate_bulk([{"symbol": "X", "price": None}], True)["rows"] == 0


def test_run_combines_a_and_b_and_survives_fetch_error():
    big = {"shape": "compact", "rows": 9, "futstk_rows": 9, "header": [], "sample": [], "suspicious": [], "underlyings": set(fl._BUILTIN)}
    out = board_selfcheck.run(True, fno_report=big, fetch_bulk=lambda syms: [_row(s) for s in syms])
    assert out["ok"] is True and out["A_fno_underlyings"]["ok"] and out["B_open_and_avg_price"]["ok"]

    def bad(syms):
        raise RuntimeError("down")
    out2 = board_selfcheck.run(True, fno_report=big, fetch_bulk=bad)
    assert out2["ok"] is False and "failed" in out2["B_open_and_avg_price"]["verdict"]


def test_sector_map_is_bigger_and_new_sectors_exist():
    assert len(sector_map._BASE) > 850
    for s, sec in (("TMPV", "Auto"), ("ETERNAL", "Consumer"), ("ASTRAL", "Building Materials"), ("INDIGO", "Shipping & Aviation"),
                   ("JKPAPER", "Paper & Packaging"), ("BALRAMCHIN", "Agri & Sugar"), ("HDFCBANK", "Banks"), ("HAL", "Capital Goods")):
        assert sector_map.sector_of(s) == sec, s
    assert sector_map.sector_of("NOTASYMBOL") == "Other" and sector_map.sector_of("tcs.ns") == "IT"


def test_sector_extra_env_overrides_and_coverage_report(monkeypatch):
    monkeypatch.setenv("BOARD_SECTOR_EXTRA", "ZZZ=Gems,TCS=Software")
    assert sector_map.sector_of("ZZZ") == "Gems" and sector_map.sector_of("TCS") == "Software"
    monkeypatch.delenv("BOARD_SECTOR_EXTRA")
    rep = sector_map.coverage_report(["TCS", "AAA", "BBB", "CCC"], lambda s: {"AAA": 1, "BBB": 9, "CCC": 5}.get(s, 0), top=2)
    assert rep["tracked"] == 4 and rep["classified"] == 1 and rep["pct"] == 25.0 and rep["top_other"] == ["BBB", "CCC"]
    assert sector_map.coverage_report([])["pct"] is None
