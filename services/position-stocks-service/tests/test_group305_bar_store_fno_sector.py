"""group305: bar store, F&O list, sector map."""
from __future__ import annotations

import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import bar_store as bs
from feed import fno_list as fl
from feed import sector_map as sm

T0 = 1_790_000_100.0 - (1_790_000_100.0 % 60)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bs.reset(); fl.reset()
    monkeypatch.setattr(config, "BOARD_MIN_DAY_VOLUME", 1000, raising=False)
    monkeypatch.setattr(config, "BOARD_TOP_SYMBOLS", 400, raising=False)
    monkeypatch.setattr(fl, "_CACHE_PATH", "/nonexistent-dir/fno.json", raising=False)
    monkeypatch.delenv("FNO_SYMBOLS_EXTRA", raising=False)
    monkeypatch.delenv("FNO_SYMBOLS_EXCLUDE", raising=False)
    yield
    bs.reset(); fl.reset()


def test_one_minute_one_bar_and_gaps():
    for dt, p, v in ((1, 100.0, 5000), (20, 101.0, 5200), (40, 99.5, 5300), (59, 100.2, 5400)):
        bs.on_tick("A", p, v, T0 + dt)
    assert bs.get_bars("A") == [(T0, 100.0, 101.0, 99.5, 100.2, 400.0)]
    bs.on_tick("A", 102.0, 5500, T0 + 301)
    assert [b[0] for b in bs.get_bars("A")] == [T0, T0 + 301 - 301 % 60]


def test_volume_never_negative_and_rebaselines():
    bs.on_tick("A", 100.0, 5000, T0 + 1); bs.on_tick("A", 100.0, 4000, T0 + 2); bs.on_tick("A", 100.0, 4500, T0 + 3)
    assert bs.get_bars("A")[0][5] == 500.0


def test_low_volume_not_tracked_until_it_crosses():
    bs.on_tick("THIN", 100.0, 500, T0)
    assert bs.get_bars("THIN") == [] and bs.stats()["skipped_low_volume"] == 1
    bs.on_tick("THIN", 100.0, 5000, T0 + 1)
    assert len(bs.get_bars("THIN")) == 1


def test_out_of_order_bad_input_day_rollover():
    bs.on_tick("A", 100.0, 5000, T0 + 130); bs.on_tick("A", 50.0, 5100, T0 + 5)
    assert bs.get_bars("A")[-1][4] == 100.0
    for args in (("", 1, 1, 1), ("A", 0, 1, 1), ("A", -1, 1, 1), ("A", None, 1, 1), ("A", 1, "x", 1), ("A", 1, 1, 0)):
        bs.on_tick(*args)
    bs.reset()
    bs.on_tick("A", 100.0, 5000, T0); bs.on_tick("A", 100.0, 6000, T0 + 86400)
    assert [b[0] for b in bs.get_bars("A")] == [T0 + 86400]
    bs.on_tick("A", 100.0, 7000, T0)
    assert [b[0] for b in bs.get_bars("A")] == [T0 + 86400]


def test_prune_copy_cap_install(monkeypatch):
    for i, sym in enumerate("ABCD"):
        bs.on_tick(sym, 100.0, 10_000 * (i + 1), T0)
    assert bs.prune(2) == 2 and set(bs.symbols()) == {"C", "D"} and bs.prune(2) == 0
    b = bs.get_bars("C"); b.clear()
    assert len(bs.get_bars("C")) == 1
    bs.reset()
    for i in range(bs.MAX_BARS + 20):
        bs.on_tick("A", 100.0, 5000 + i, T0 + 60 * i if i < 300 else T0 + 60 * 300)
    assert len(bs.get_bars("A")) <= bs.MAX_BARS
    import feed.ws_client as wsc
    wsc._on_tick_callbacks.clear(); monkeypatch.setattr(bs, "_installed", False)
    bs.install(); bs.install()
    assert wsc._on_tick_callbacks.count(bs.on_tick) == 1
    wsc._on_tick_callbacks.clear()


CSV = ["SEM_EXM_EXCH_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL", "NSE,FUTSTK,BAJAJ-AUTO-Oct2026-FUT", "NSE,FUTSTK,M&M-Nov2026-FUT",
       "NSE,FUTSTK,RELIANCE-Oct2026-FUT", "NSE,FUTSTK,RELIANCE-Nov2026-FUT", "NSE,EQUITY,TCS", "BSE,FUTSTK,SBIN-Oct2026-FUT",
       "NSE,OPTSTK,INFY-Oct2026-1500-CE", "NSE,FUTIDX,NIFTY-Oct2026-FUT", "short,row"]


def test_fno_parse_builtin_env_refresh(monkeypatch):
    assert fl.parse_fut_underlyings(CSV) == {"BAJAJ-AUTO", "M&M", "RELIANCE"}
    assert fl.parse_fut_underlyings(["A,B,C", "1,2,3"]) == set() and fl.parse_fut_underlyings([]) == set()
    s = fl.get_fno_symbols()
    assert "RELIANCE" in s and len(s) >= 150 and fl.is_fno("reliance.NS") and not fl.is_fno("ZZZNOTREAL") and fl.status()["source"] == "builtin"
    monkeypatch.setenv("FNO_SYMBOLS_EXTRA", " zzz1 , ZZZ2 "); monkeypatch.setenv("FNO_SYMBOLS_EXCLUDE", "RELIANCE")
    assert fl.is_fno("ZZZ1") and fl.is_fno("ZZZ2") and not fl.is_fno("RELIANCE")
    monkeypatch.delenv("FNO_SYMBOLS_EXTRA"); monkeypatch.delenv("FNO_SYMBOLS_EXCLUDE")
    big = {f"S{i}" for i in range(80)}
    monkeypatch.setattr(fl, "_download", lambda: big)
    assert fl.refresh(force=True) is True and fl.get_fno_symbols() == big and fl.status()["source"] == "dhan"
    fl.reset()
    monkeypatch.setattr(fl, "_download", lambda: {"ONLY"})
    assert fl.refresh(force=True) is False and "unexpected" in fl.status()["last_error"] and "RELIANCE" in fl.get_fno_symbols()
    def boom():
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(fl, "_download", boom)
    assert fl.refresh(force=True) is False and "403" in fl.status()["last_error"]


def test_fno_snapshot_and_blank_path(tmp_path, monkeypatch):
    path = str(tmp_path / "fno.json")
    monkeypatch.setattr(fl, "_CACHE_PATH", path)
    syms = {f"Q{i}" for i in range(60)}
    fl._save_snapshot(syms)
    assert fl._load_snapshot() == syms
    open(path, "w").write(json.dumps({"symbols": ["A"]}))
    assert fl._load_snapshot() is None
    open(path, "w").write("not json")
    assert fl._load_snapshot() is None
    monkeypatch.setenv("FNO_LIST_CACHE_PATH", "   ")
    importlib.reload(fl)
    assert fl._CACHE_PATH == "/tmp/position_stocks_fno_list.json"
    monkeypatch.delenv("FNO_LIST_CACHE_PATH")
    importlib.reload(fl)


def test_sector_map(monkeypatch):
    assert sm.sector_of("HDFCBANK") == "Banks" and sm.sector_of("tcs.ns") == "IT" and sm.sector_of("NOSUCH") == "Other" and sm.sector_of("") == "Other"
    monkeypatch.setenv("BOARD_SECTOR_EXTRA", "NOSUCH=Gaming, BAD,=x")
    assert sm.sector_of("NOSUCH") == "Gaming" and "Gaming" in sm.sectors()
    monkeypatch.delenv("BOARD_SECTOR_EXTRA")
    core = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "ITC", "LT", "AXISBANK", "SUNPHARMA", "TATASTEEL", "MARUTI",
            "TITAN", "NTPC", "BHARTIARTL", "HINDUNILVR", "ULTRACEMCO", "DLF", "CIPLA", "ONGC"]
    assert sm.coverage(core) == 1.0 and sm.coverage([]) == 0.0 and sm.coverage(sorted(fl._BUILTIN)) >= 0.80
    assert sm.sector_of("LICI") == "Insurance" and sm.sector_of("ADANIENT") == "Infra"
