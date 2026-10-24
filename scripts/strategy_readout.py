#!/usr/bin/env python3
"""strategy_readout.py - turns your own 14-day reports into a yes / no on the strategy switches that need numbers (group 322).

  python3 scripts/strategy_readout.py --position-stocks http://localhost:8004 --real-trade http://localhost:8005 \\
          [--days 14] [--cutoff 10:30] [--min-trades 30] [--header "Authorization: Bearer <admin token>"]

Reads (read-only GETs): position-stocks /trades/report?days=N (by_entry_hour_ist), real-trade /positions/REAL/breakdown
(by_entry_time_ist, 30-minute buckets), position-stocks /board/shadow-report (chase guard verdict). Prints, per service, the
expectancy of entries before the cutoff against entries after it and one of:
  SET OPENING_ENTRY_NOT_BEFORE_IST=<cutoff>   (>= min-trades early trades, negative expectancy, clearly worse than later entries)
  KEEP THE CURRENT WINDOW                     (early entries are not losing money, or not worse than later ones)
  NOT ENOUGH DATA                             (fewer early or later trades than --min-trades)
A bucket that straddles the cutoff (an hourly bucket for 10:30) is left out of both sides and said so. It is a rule of thumb on a
small sample, not advice; the board gate needs no action (BOARD_GATE_MODE=auto promotes itself when its written rule is met).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Dict, Optional, Tuple


def _bounds(key: str) -> Optional[Tuple[int, int]]:
    """'09' -> (540, 600); '10:30' -> (630, 660); anything else None. Minutes since midnight, [start, end)."""
    try:
        k = str(key).strip()
        if ":" in k:
            h, m = k.split(":")
            start = int(h) * 60 + int(m)
            return start, start + 30
        return int(k) * 60, int(k) * 60 + 60
    except Exception:  # noqa: BLE001
        return None


def entry_window_verdict(buckets: Dict[str, dict], cutoff: str = "10:30", min_trades: int = 30) -> dict:
    """buckets: {bucket key -> {"trades": n, "expectancy": avg P&L per trade}} as in both reports."""
    ch, cm = cutoff.split(":")
    cut = int(ch) * 60 + int(cm)
    pre_n = post_n = 0
    pre_sum = post_sum = 0.0
    ambiguous = []
    for key, agg in (buckets or {}).items():
        b = _bounds(key)
        if b is None or not isinstance(agg, dict):
            continue
        n = int(agg.get("trades") or 0)
        e = agg.get("expectancy")
        if n <= 0 or e is None:
            continue
        if b[1] <= cut:
            pre_n += n; pre_sum += n * float(e)
        elif b[0] >= cut:
            post_n += n; post_sum += n * float(e)
        else:
            ambiguous.append(key)
    pre_e = pre_sum / pre_n if pre_n else None
    post_e = post_sum / post_n if post_n else None
    if pre_n < min_trades or post_n < min_trades:
        verdict = "NOT ENOUGH DATA"
    elif pre_e < 0 and pre_e < post_e:
        verdict = f"SET OPENING_ENTRY_NOT_BEFORE_IST={cutoff}"
    else:
        verdict = "KEEP THE CURRENT WINDOW"
    return {"cutoff": cutoff, "before": {"trades": pre_n, "expectancy": None if pre_e is None else round(pre_e, 2)},
            "after": {"trades": post_n, "expectancy": None if post_e is None else round(post_e, 2)},
            "left_out_straddling": ambiguous, "min_trades": min_trades, "verdict": verdict}


def _get(url: str, headers: Dict[str, str], timeout: float = 20) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL, read-only GET
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  could not read {url}: {type(e).__name__}: {e}")
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--position-stocks"); ap.add_argument("--real-trade")
    ap.add_argument("--days", type=int, default=14); ap.add_argument("--cutoff", default="10:30")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--header", action="append", default=[], help='"Name: value", repeatable')
    a = ap.parse_args(argv)
    headers = {}
    for h in a.header:
        name, _, val = h.partition(":")
        headers[name.strip()] = val.strip()
    if not (a.position_stocks or a.real_trade):
        ap.error("give --position-stocks and / or --real-trade")
    if a.position_stocks:
        base = a.position_stocks.rstrip("/")
        print(f"position-stocks (last {a.days} days)")
        rep = _get(f"{base}/trades/report?days={a.days}", headers)
        if rep:
            print("  entry window:", json.dumps(entry_window_verdict(rep.get("by_entry_hour_ist") or {}, a.cutoff, a.min_trades)))
        sh = _get(f"{base}/board/shadow-report?days={max(a.days, 30)}", headers, timeout=90)   # E125: the report reads up to 60,000 rows; 20 s timed out
        if sh:
            cg = (sh.get("rules") or {}).get("chase_guard") or {}
            print(f"  board chase guard: {cg.get('verdict')}  (gate_mode={sh.get('gate_mode')}, effective={sh.get('effective_gate_mode')})")
    if a.real_trade:
        base = a.real_trade.rstrip("/")
        print(f"real-trade (all closed REAL trades)")
        rep = _get(f"{base}/positions/REAL/breakdown", headers)
        if rep:
            print("  entry window:", json.dumps(entry_window_verdict(rep.get("by_entry_time_ist") or {}, a.cutoff, a.min_trades)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
