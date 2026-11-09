"""Round 246 (plan C1): intraday backtest on recorded real premiums (backtest/rolling_sim.py). Synthetic rolling files built from the same world as the sim tests."""
from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from backtest import cli, rolling_sim
from fno import bs
from fno.params import Params
from fno.provider import IST
from test_backtests import build_world


def _doc(typ, label, rows, complete=True):
    return {"underlying": "NIFTY", "type": typ, "strike": label, "interval": 5, "expiry_flag": "WEEK", "expiry_code": 1, "rows": rows, "failed_windows": [] if complete else [["a", "b", "x"]], "complete": complete}


def _prem(spot, k, ot):
    return max(0.05, bs.price(spot, k, 3 / 365.0, 0.065, 0.14, ot))


def make_files(intr, offsets=(-2, -1, 0, 1, 2), complete=True):
    """ATM label = the strike nearest the bar's OPEN; ATM+n / ATM-n = n steps of 50 away. Every row carries its strike, like Dhan's reply."""
    docs = []
    for ot, typ in (("CE", "CALL"), ("PE", "PUT")):
        for n in offsets:
            rows = []
            for b in intr:
                k = round(b["o"] / 50) * 50 + 50 * n
                lo, hi = (b["l"], b["h"]) if ot == "CE" else (b["h"], b["l"])
                o, c = _prem(b["o"], k, ot), _prem(b["c"], k, ot)
                h, l = max(_prem(hi if ot == "CE" else lo, k, ot), o, c), min(_prem(lo if ot == "CE" else hi, k, ot), o, c)
                rows.append({"t": b["t"], "o": o, "h": h, "l": l, "c": c, "v": 100.0, "oi": 1.0, "iv": 14.0, "spot": b["c"], "strike": float(k)})
            label = "ATM" if n == 0 else f"ATM{'+' if n > 0 else '-'}{abs(n)}"
            docs.append(_doc(typ, label, rows, complete))
    return docs


def book_from(docs):
    bk = rolling_sim.RollingBook()
    for d in docs:
        bk.add(d)
    return bk


def test_trades_an_uptrend_on_real_premium_candles_with_calls_only():
    daily, intr = build_world()
    bk = book_from(make_files(intr))
    r = rolling_sim.run("NIFTY", daily, intr, bk, p=Params(min_score=30, max_trades_per_day=2))
    assert r["kind"] == "ROLLING" and r["metrics"]["overall"]["trades"] > 0
    assert {t["opt_type"] for t in r["trades"]} == {"CE"}
    assert all(t["status"] == "CLOSED" and t["exit_reason"] for t in r["trades"])
    assert r["unpriced_exits"] == 0 and any("START of each bar" in n for n in r["notes"])


def test_fill_is_at_the_open_of_the_bar_after_the_signal():
    daily, intr = build_world()
    bk = book_from(make_files(intr))
    r = rolling_sim.run("NIFTY", daily, intr, bk, p=Params(min_score=30))
    t = r["trades"][0]
    atm = bk.entry_row("CE", t["entry_ts"])
    assert atm is not None and t["strike"] == atm["strike"]               # a bar that exists, in its ATM contract
    assert t["entry_premium"] >= atm["o"]                                  # open + spread / slippage, never better than the open
    sig_bar = [b for b in intr if b["t"] == t["entry_ts"] - 300]
    assert sig_bar, "the signal bar is the one BEFORE the fill bar"


def test_no_lookahead_on_the_premium_data():
    daily, intr = build_world()
    docs = make_files(intr)
    r1 = rolling_sim.run("NIFTY", daily, intr, book_from(docs), p=Params(min_score=30))
    last_day = datetime.fromtimestamp(intr[-1]["t"], IST).date()
    for d in docs:
        for row in d["rows"]:
            if datetime.fromtimestamp(row["t"], IST).date() == last_day:
                row["o"] = row["h"] = row["l"] = row["c"] = row["c"] * 0.5
    r2 = rolling_sim.run("NIFTY", daily, intr, book_from(docs), p=Params(min_score=30))
    cut = lambda r: [(t["entry_ts"], t["strike"], t["exit_reason"], t["net_pnl"]) for t in r["trades"] if datetime.fromtimestamp(t["entry_ts"], IST).date() != last_day]
    assert cut(r1) == cut(r2)


def test_a_held_strike_with_no_candles_is_closed_at_the_squareoff_and_counted():
    daily, intr = build_world()
    docs = make_files(intr, offsets=(0,))                                   # only the ATM series: the strike held after the entry is found only while it is still the ATM
    # remove every ATM candle after the entry bar so the held strike cannot be priced
    bk = book_from(docs)
    first = rolling_sim.run("NIFTY", daily, intr, bk, p=Params(min_score=30, max_trades_per_day=1))
    assert first["metrics"]["overall"]["trades"] > 0
    t = first["trades"][0]
    for key in [k for k in bk.by_strike if k[0] == "CE" and k[2] > t["entry_ts"] and datetime.fromtimestamp(k[2], IST).date() == datetime.fromtimestamp(t["entry_ts"], IST).date()]:
        del bk.by_strike[key]
    again = rolling_sim.run("NIFTY", daily, intr, bk, p=Params(min_score=30, max_trades_per_day=1))
    assert again["unpriced_exits"] >= 1 and again["unmanaged_bars"] >= 1
    assert all(x["status"] == "CLOSED" for x in again["trades"])


def test_incomplete_or_one_sided_files_are_refused(tmp_path):
    daily, intr = build_world(n_intraday=20)
    for d in make_files(intr, offsets=(0,)):
        name = f"NIFTY_{d['type']}_ATM_WEEK1_5m.json"
        (tmp_path / name).write_text(json.dumps(d))
    assert rolling_sim.load_book(str(tmp_path)).files
    bad = make_files(intr, offsets=(0,), complete=False)[0]
    (tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="not complete"):
        rolling_sim.load_book(str(tmp_path))
    assert rolling_sim.load_book(str(tmp_path), allow_partial=True).files
    os.remove(tmp_path / "NIFTY_PUT_ATM_WEEK1_5m.json")
    with pytest.raises(ValueError, match="BOTH"):
        rolling_sim.load_book(str(tmp_path), allow_partial=True)
    with pytest.raises(FileNotFoundError):
        rolling_sim.load_book(str(tmp_path / "nothing"))


def test_cli_runs_end_to_end_and_refuses_a_missing_folder(tmp_path, capsys):
    daily, intr = build_world(n_intraday=20)
    rd = tmp_path / "roll"
    rd.mkdir()
    for d in make_files(intr):
        label = d["strike"].replace("+", "p").replace("-", "m")
        (rd / f"NIFTY_{d['type']}_{label}_WEEK1_5m.json").write_text(json.dumps(d))
    import csv

    def dump(path, rows, daily_fmt):
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for c in rows:
                dt = datetime.fromtimestamp(c["t"], IST)
                w.writerow([dt.strftime("%Y-%m-%d") if daily_fmt else dt.strftime("%Y-%m-%d %H:%M"), c["o"], c["h"], c["l"], c["c"], c.get("v", 0)])
    dump(tmp_path / "d.csv", daily, True)
    dump(tmp_path / "i.csv", intr, False)
    base = ["--daily", str(tmp_path / "d.csv"), "--intraday", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out"), "--set", "min_score=30"]
    assert cli.main_rolling(base + ["--rolling-dir", str(rd)]) == 0
    assert any(f.startswith("fno_rolling_nifty") and f.endswith(".json") for f in os.listdir(tmp_path / "out" / "backtests"))
    assert "REAL premiums" in capsys.readouterr().out
    assert cli.main_rolling(base + ["--rolling-dir", str(tmp_path / "none")]) == 2
