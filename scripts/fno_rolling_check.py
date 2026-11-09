#!/usr/bin/env python3
"""fno_rolling_check.py - look at what scripts/fno_rolling_fetch.py wrote BEFORE trusting a backtest on it (round 256). Reads files only: no network, no token.

  python3 scripts/fno_rolling_check.py --dir data/fno_rolling [--holidays 2026-11-10,2026-11-24]

Per file: candles, days, bars per day, first / last bar time, whether every timestamp sits on the 5-minute grid from 09:15 (= the START of a bar), strike-vs-spot gap of the ATM series, duplicates,
candles outside the requested range, weekdays with no candles. Exit 1 when any file has a PROBLEM. Logic: services/fno-options-service/backtest/rolling_check.py.
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service"))
from backtest import rolling_check  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default="data/fno_rolling")
    ap.add_argument("--holidays", default="", help="comma list of YYYY-MM-DD market holidays (only to quiet the 'no candles' note)")
    ap.add_argument("--partial", action="store_true", help="also check the *.json.partial files")
    a = ap.parse_args(argv)
    paths = sorted(glob.glob(os.path.join(a.dir, "*.json")) + (sorted(glob.glob(os.path.join(a.dir, "*.json.partial"))) if a.partial else []))
    if not paths:
        print(f"no rolling files in {a.dir}: run scripts/fno_rolling_fetch.py first", file=sys.stderr)
        return 2
    hol = [h for h in a.holidays.split(",") if h]
    bad = 0
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"{os.path.basename(p)}: unreadable ({type(e).__name__})")
            bad += 1
            continue
        rep = rolling_check.check_doc(doc, hol)
        print(f"[{os.path.basename(p)}]")
        for line in rolling_check.format_report(rep):
            print("  " + line)
        bad += 1 if rep["problems"] else 0
    print(f"{len(paths)} file(s), {bad} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
