"""E322: the F&O candle converter and the backtest loaders apply the engine's bar rule (stockky_ms.prep._valid)."""
from __future__ import annotations

import math

from backtest import cli, eod
from fno.provider import BAR_TOLERANCE_PCT, bar_ok, convert_candles

CASES = [(100, 101, 99, 100), (100, 101, 99, 140), (60, 101, 100, 100), (100, 99, 101, 100), (0, 1, 1, 1), (100, 101, 99, 101.02), (100, 101, 99, float("nan")), (100, 101, 99, -5)]


def test_bar_ok_agrees_with_prep_valid():
    from stockky_ms.prep import _valid
    for o, h, l, c in CASES:
        assert bar_ok(o, h, l, c) == _valid((0.0, o, h, l, c, 0.0), BAR_TOLERANCE_PCT), (o, h, l, c)


def _row(d, o, h, l, c, v=1):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_convert_candles_drops_bad_rows():
    raw = [_row("2026-11-10 09:15", 100, 101, 99, 100), _row("2026-11-10 09:20", 100, 101, 99, 140), _row("2026-11-10 09:25", 100, 101, 99, float("nan")),
           _row("2026-11-10 09:30", 100, 99, 101, 100), _row("2026-11-10 09:35", 100, 101, 99, 101.02)]
    assert [x["t"] for x in convert_candles(raw)] == [x["t"] for x in convert_candles([raw[0], raw[4]])] and len(convert_candles(raw)) == 2


def test_convert_candles_bad_volume_and_duplicates():
    raw = [_row("2026-11-10 09:15", 100, 101, 99, 100, "-"), _row("2026-11-10 09:15", 100, 102, 99, 101, 7)]
    out = convert_candles(raw)
    assert len(out) == 1 and out[0]["h"] == 102 and out[0]["v"] == 7
    assert convert_candles([_row("2026-11-10 09:15", 100, 101, 99, 100, "-")])[0]["v"] == 0.0


def test_cli_load_candles_uses_the_rule(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("date,open,high,low,close,volume\n2026-11-02 09:15,100,101,99,100,1\n2026-11-02 09:20,100,101,99,140,1\n2026-11-02 09:25,60,101,100,100,1\n"
                 "2026-11-02 09:30,100,99,101,100,1\n2026-11-02 09:35,0,101,99,100,1\n")
    assert len(cli.load_candles(str(p))) == 1


def test_bhavcopy_row_with_bad_ohlc_is_skipped():
    head = "TradDt,FinInstrmTp,TckrSymb,FininstrmActlXpryDt,StrkPric,OptnTp,OpnPric,HghPric,LwPric,ClsPric,OpnIntrst,TtlTradgVol\n"
    good = "2026-11-10,IDO,NIFTY,2026-11-13,25000,CE,100,110,95,105,1,10\n"
    bad = "2026-11-10,IDO,NIFTY,2026-11-13,25050,CE,100,90,95,200,1,10\n"
    assert len(eod.parse_bhavcopy(head + good + bad, "NIFTY")) == 1
