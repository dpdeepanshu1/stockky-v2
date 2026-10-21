#!/usr/bin/env python3
"""scripts/charges_split.py - where do the charges of REAL trades come from? (group 480)

Reads the same rt_*.json exports as records_report.py (python3 scripts/export_records.py makes one) and splits closed trades by how long they were held:
same-day (held_days 0) versus carried overnight. A carried CNC trade pays the delivery sell (STT 0.1 % on both legs, DP charge Rs 12.50 + GST = Rs 14.75),
a same-day round trip is charged at intraday rates. Prints, per group: trades, total charges, charges per trade, charges as % of the entry value,
gross and net P&L, and how many trades carry only ESTIMATED charges (charges_source other than 'ledger'). Also a size table (entry value buckets), because
Dhan brokerage is min(Rs 20, 0.03 %) per order, a PERCENTAGE below about Rs 66,700, so a bigger position does not lower the fee percentage.

  python3 scripts/charges_split.py rt_*.json
  python3 scripts/charges_split.py rt_*.json --since 2026-10-01 --mode REAL --json out.json

Read-only, standard library only. Needs the group 431 export (entry_price, quantity, charges, held_days). A trade without them is counted as 'unknown'.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional


def num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def load(paths: Iterable[str], mode: str = "REAL", since: Optional[str] = None) -> List[dict]:
    seen: Dict[Any, dict] = {}
    for pat in paths:
        for f in sorted(glob.glob(pat)) or [pat]:
            try:
                doc = json.load(open(f))
            except (OSError, ValueError) as e:
                print(f"skip {f}: {e}", file=sys.stderr)
                continue
            for r in (doc.get("records") if isinstance(doc, dict) else doc) or []:
                if not isinstance(r, dict) or r.get("position_id") is None:
                    continue
                if mode and str(r.get("mode") or "").upper() != mode.upper():
                    continue
                if since and str(r.get("opened_at") or "")[:10] < since:
                    continue
                seen[r["position_id"]] = r
    return list(seen.values())


def entry_value(r: dict) -> Optional[float]:
    p, q = num(r.get("entry_price")), num(r.get("quantity"))
    return p * q if p and q else None


def hold_group(r: dict) -> str:
    d = num(r.get("held_days"))
    if d is None:
        return "unknown"
    return "same_day" if d <= 0 else "carried"


def size_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    return "<1500" if v < 1500 else "1500-3000" if v < 3000 else ">=3000"


def summarize(recs: List[dict], key) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in recs:
        groups[key(r)].append(r)
    out = {}
    for k, rows in sorted(groups.items()):
        ch = [num(r.get("charges")) for r in rows]
        chv = [c for c in ch if c is not None]
        vals = [(entry_value(r), num(r.get("charges"))) for r in rows]
        pct = [100.0 * c / v for v, c in vals if v and c is not None]
        out[k] = {
            "trades": len(rows),
            "charges_total": round(sum(chv), 2),
            "charges_per_trade": round(sum(chv) / len(chv), 2) if chv else None,
            "charges_pct_of_entry_value_median": round(st.median(pct), 3) if pct else None,
            "gross": round(sum(num(r.get("gross_pnl")) or 0.0 for r in rows), 2),
            "net": round(sum(num(r.get("net_pnl")) or 0.0 for r in rows), 2),
            "estimated_charges": sum(1 for r in rows if r.get("charges_source") not in (None, "ledger")),
            "no_charges": len(rows) - len(chv),
        }
    return out


def render(title: str, table: Dict[str, dict]) -> str:
    lines = [title, f"  {'group':<11}{'n':>4}{'charges':>10}{'per trade':>11}{'% of value':>12}{'gross':>10}{'net':>10}{'estimated':>11}"]
    for k, v in table.items():
        cpt = "-" if v["charges_per_trade"] is None else f"{v['charges_per_trade']:.2f}"
        pc = "-" if v["charges_pct_of_entry_value_median"] is None else f"{v['charges_pct_of_entry_value_median']:.3f}"
        lines.append(f"  {k:<11}{v['trades']:>4}{v['charges_total']:>10.2f}{cpt:>11}{pc:>12}{v['gross']:>10.2f}{v['net']:>10.2f}{v['estimated_charges']:>11}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--since", default=None, help="only trades opened on/after YYYY-MM-DD (UTC date)")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    recs = load(a.files, a.mode, a.since)
    if not recs:
        print("no trades found")
        return 1
    by_hold = summarize(recs, hold_group)
    by_size = summarize(recs, lambda r: size_bucket(entry_value(r)))
    print(f"{len(recs)} trades ({a.mode})")
    print(render("By holding period (carried = sold on a later day: delivery STT + DP charge):", by_hold))
    print(render("By entry value in Rs (brokerage is a percentage below ~Rs 66,700, so size does not lower the fee %):", by_size))
    sd, ca = by_hold.get("same_day"), by_hold.get("carried")
    if sd and ca and sd["charges_per_trade"] is not None and ca["charges_per_trade"] is not None:
        print(f"Carried trades pay Rs {ca['charges_per_trade']:.2f} each against Rs {sd['charges_per_trade']:.2f} for same-day ones.")
    est = sum(v["estimated_charges"] for v in by_hold.values())
    if est:
        print(f"Note: {est} trade(s) have ESTIMATED charges (the ledger had not booked every order); compare with a Dhan contract note.")
    if a.json:
        json.dump({"by_hold": by_hold, "by_size": by_size}, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
