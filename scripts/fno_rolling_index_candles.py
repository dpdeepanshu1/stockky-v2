#!/usr/bin/env python3
"""fno_rolling_index_candles.py - E530 (round 511): write the 5-minute INDEX candles the rolling backtest needs (`--intraday nifty_5m.csv`) from the files scripts/fno_rolling_fetch.py already saved.

Nothing in the repo made that file: fno_export_candles.py writes DAILY candles only, and the rolling files are the only months-long intraday record the VM has. Every rolling row carries `spot`, the index
level at that candle, so the index file is derived from the SAME rows (the same dates and timestamps, so the option candles and the index bars line up by construction). Offline, stdlib only, read-only on the rolling files.

  python3 scripts/fno_rolling_index_candles.py --rolling-dir data/fno_rolling --und NIFTY --out data/fno_real_probe/replay_inputs/nifty_5m.csv

LIMIT, stated on every run: the API gives ONE spot per candle, not an open / high / low / close. close = that spot; open = the previous candle's close of the same day (the first candle opens at its own spot);
high / low = the larger / smaller of the two. A real 5-minute bar's wick is lost, so range- and breakout-based rules (opening range, expansion bars) see FEWER and SMALLER breakouts than live: a rolling result built on
this file is a floor for those rules, the same caveat the recorded-chain replay already carries. Rows stamped 15:30 or later (after the close) are dropped. Exit 0 when the file was written, 1 when there was nothing to write."""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
CLOSE = dtime(15, 30)


def _spots(path: str) -> Tuple[Dict[float, float], bool]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out: Dict[float, float] = {}
    for r in doc.get("rows") or []:
        try:
            t, sp = float(r["t"]), r.get("spot")
            if sp is not None and float(sp) > 0:
                out[t] = float(sp)
        except (KeyError, TypeError, ValueError):
            continue
    return out, doc.get("complete") is True


def build(rolling_dir: str, und: str = "NIFTY", interval: int = 5, expiry_code: int = 1, expiry_flag: str = "WEEK") -> Tuple[List[Dict[str, Any]], List[str]]:
    """-> (bars [{date, open, high, low, close, volume}], notes). The ATM CALL series first, the ATM PUT series fills the candles the CALL file lacks."""
    notes: List[str] = []
    spots: Dict[float, float] = {}
    for typ in ("CALL", "PUT"):
        paths = sorted(glob.glob(os.path.join(rolling_dir, f"{und.upper()}_{typ}_ATM_{expiry_flag}{expiry_code}_{interval}m.json")))
        if not paths:
            notes.append(f"no {typ} ATM file for {und} ({expiry_flag}{expiry_code}, {interval}m) in {rolling_dir}")
            continue
        got, complete = _spots(paths[0])
        if not complete:
            notes.append(f"{os.path.basename(paths[0])} is not complete: its holes are holes in the index file too")
        for t, sp in got.items():
            spots.setdefault(t, sp)
    by_day: Dict[Any, List[Tuple[float, float]]] = {}
    for t in sorted(spots):
        dt = datetime.fromtimestamp(t, IST)
        if dt.time() >= CLOSE:
            continue
        by_day.setdefault(dt.date(), []).append((t, spots[t]))
    bars: List[Dict[str, Any]] = []
    for day in sorted(by_day):
        prev = None
        for t, c in by_day[day]:
            o = prev if prev is not None else c
            bars.append({"date": datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M"), "open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": 0})
            prev = c
    return bars, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rolling-dir", default="data/fno_rolling")
    ap.add_argument("--und", default="NIFTY", choices=["NIFTY", "SENSEX"])
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--expiry-code", type=int, default=1)
    ap.add_argument("--out", required=True, help="the csv to write, e.g. data/fno_real_probe/replay_inputs/nifty_5m.csv")
    a = ap.parse_args(argv)
    bars, notes = build(a.rolling_dir, a.und, a.interval, a.expiry_code)
    for n in notes:
        print("note:", n, file=sys.stderr)
    if not bars:
        print("error: no candle with a spot value: nothing written", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(bars)
    days = sorted({b["date"][:10] for b in bars})
    print(f"wrote {a.out}: {len(bars)} candles, {len(days)} days, {days[0]} .. {days[-1]}")
    print("limit: one spot per candle, so high / low are the open / close only (no wicks): opening-range and expansion rules see fewer breakouts than live", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
