#!/usr/bin/env python3
"""gate_rule_summary.py - round 679: apply the P2 rule of the 23 Nov plan to the saved daily gate reads. Read-only, stdlib only, no network.

  python3 scripts/gate_rule_summary.py [gates_out/gates_2026-11-23.txt ...]     (no argument = every gates_out/gates_*.txt)
      [--charges 0.25] [--stop 1.5] [--min-days 3] [--min-names 60] [--min-ran-pct 15]

Input = the text that `bash scripts/gates_daily.sh` prints and saves (gates_out/gates_<DAY>.txt): per gate the BLOCKED NAMES table (a trailing * marks a name
that came from an event tab) and the "DHAN 1-MIN OUTCOMES" tables. The "structure gate, recorded bars" section repeats the same names from partial bars and is skipped.
A name counts once per day and gate; in the all-names group once per day (first row seen). A name Dhan could not price (no_data) is counted, not guessed.

A group may be loosened only when ALL of these hold (the rule of the plan, section 2 P2):
  (a) at least --min-days sessions read,  (b) at least --min-names priced names,  (c) at least --min-ran-pct % of them RAN (best >= +2% and close above),
  (d) the mean close beats --charges (round-trip charges seen in the records, about 0.25 points per trade),
  (e) the worst dip of every RAN name stayed inside --stop (the planned stop, in %).
Nothing here changes a gate: it prints KEEP or CANDIDATE and the numbers behind it."""
from __future__ import annotations

import argparse
import glob
import re
import statistics
import sys
from collections import OrderedDict

HDR = re.compile(r"^#{4,}\s+gate\s+(\S+)\s+day\s+(\d{4}-\d{2}-\d{2})")
REC = re.compile(r"^#{4,}\s+structure gate, recorded bars")
OTHER_HDR = re.compile(r"^#{4,}")
BLK = re.compile(r"^([A-Z0-9&\-]+)\s+(\d+)\s+\d\d:\d\d\s+\S+\s+.*?(\s\*)?\s*$")
OUT = re.compile(r"^([A-Z0-9&\-]+)\s+(\d\d:\d\d)\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(RAN|SPIKE|NO_MOVE)\s*$")
NODATA = re.compile(r"^([A-Z0-9&\-]+)\s+(\d\d:\d\d)\s+no_data")


def parse(text: str) -> list[dict]:
    """One dict per priced or unpriced row: day, gate, symbol, block, best, worst, close, label, event (bool)."""
    rows, gate, day, in_blocked, in_rec, starred, waits = [], None, None, False, False, set(), {}
    for line in text.splitlines():
        m = HDR.match(line)
        if m:
            gate, day, in_blocked, in_rec, starred, waits = m.group(1), m.group(2), False, False, set(), {}
            continue
        if REC.match(line):
            in_rec = True
            continue
        if OTHER_HDR.match(line):
            in_rec = False
            continue
        if gate is None or in_rec:
            continue
        if line.startswith("BLOCKED NAMES"):
            in_blocked = True
            continue
        if line.startswith("symbols by source_tab") or line.startswith("Dhan outcome"):
            in_blocked = False
            continue
        if in_blocked:
            b = BLK.match(line)
            if b and not line.startswith("symbol "):
                waits[b.group(1)] = int(b.group(2))                      # round 688: how many times the engine said WAIT for this name that day
                if b.group(3):
                    starred.add(b.group(1))
            continue
        o = OUT.match(line)
        if o:
            sym, hhmm, px, best, worst, close, label = o.groups()
            rows.append(dict(day=day, gate=gate, symbol=sym, block=hhmm, best=float(best), worst=float(worst), close=float(close), label=label, event=sym in starred, waits=waits.get(sym)))
            continue
        n = NODATA.match(line)
        if n:
            rows.append(dict(day=day, gate=gate, symbol=n.group(1), block=n.group(2), best=None, worst=None, close=None, label="no_data", event=n.group(1) in starred, waits=waits.get(n.group(1))))
    return rows


def dedupe(rows: list[dict], per_gate: bool) -> list[dict]:
    seen: "OrderedDict[tuple, dict]" = OrderedDict()
    for r in rows:
        key = (r["day"], r["gate"], r["symbol"]) if per_gate else (r["day"], r["symbol"])
        old = seen.get(key)
        if old is None or (old["label"] == "no_data" and r["label"] != "no_data"):   # a priced row replaces an unpriced one
            seen[key] = r
    return list(seen.values())


def stats(rows: list[dict], charges: float, stop: float) -> dict:
    p = [r for r in rows if r["close"] is not None]
    n = len(p)
    ran = [r for r in p if r["label"] == "RAN"]
    out = dict(rows=len(rows), priced=n, no_data=len(rows) - n, ran=len(ran), spike=sum(1 for r in p if r["label"] == "SPIKE"),
               days=len({r["day"] for r in rows}), ran_names=ran)
    if n:
        closes = [r["close"] for r in p]
        out.update(ran_pct=100.0 * len(ran) / n, mean_close=statistics.fmean(closes), median_close=statistics.median(closes),
                   below_1=sum(1 for r in p if r["worst"] <= -1.0),
                   stop_mean=statistics.fmean([(-stop if r["worst"] <= -stop else r["close"]) for r in p]),
                   mean_best=statistics.fmean([r["best"] for r in p]))
    return out


def block_bucket(r: dict) -> str:
    """Round 688: when the first block of the name happened (IST wall clock)."""
    h, m = (int(x) for x in r["block"].split(":"))
    t = h * 60 + m
    return "a 09:30-09:59" if t < 600 else "b 10:00-10:59" if t < 660 else "c 11:00-11:59" if t < 720 else "d 12:00 or later"


def waits_bucket(r: dict) -> str:
    """Round 688: how persistent the signal was (how many WAIT rows the name got that day). Unknown = the blocked table was not in the file."""
    w = r.get("waits")
    return "unknown" if w is None else "a 1 wait" if w <= 1 else "b 2-3 waits" if w <= 3 else "c 4-9 waits" if w <= 9 else "d 10+ waits"


def slices(rows: list[dict], key, charges: float, stop: float) -> list[tuple[str, dict]]:
    """Round 688: the same numbers as a group, per slice (information only: a slice is never a verdict and loosens nothing)."""
    out: dict = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return [(k, stats(v, charges, stop)) for k, v in sorted(out.items())]


def show_slices(title: str, rows: list[dict], key, a) -> None:
    print(f"\n== slices: {title} ==")
    print(f"  {'slice':<18}{'names':>6}{'RAN%':>7}{'mean close':>12}{'median':>8}{'with stop':>11}{'after chg':>10}")
    for name, s in slices(rows, key, a.charges, a.stop):
        if not s["priced"]:
            continue
        after = s["stop_mean"] - a.charges
        note = "  few" if s["priced"] < a.min_slice else ("  beats charges: only a lead, read it again after more sessions" if after > 0 else "")
        print(f"  {name[2:] if name[1:2] == ' ' else name:<18}{s['priced']:>6}{s['ran_pct']:>6.1f}%{s['mean_close']:>+11.2f}%{s['median_close']:>+7.2f}%{s['stop_mean']:>+10.2f}%{after:>+9.2f}%{note}")


def verdict(s: dict, a) -> tuple[str, list[str]]:
    fails = []
    if s["days"] < a.min_days:
        fails.append(f"(a) only {s['days']} session(s) read, need {a.min_days}")
    if s["priced"] < a.min_names:
        fails.append(f"(b) only {s['priced']} priced names, need {a.min_names}")
    if s["priced"] == 0:
        return "KEEP", fails + ["no priced names"]
    if s["ran_pct"] < a.min_ran_pct:
        fails.append(f"(c) RAN {s['ran_pct']:.1f}% < {a.min_ran_pct:g}%")
    if s["mean_close"] <= a.charges:
        fails.append(f"(d) mean close {s['mean_close']:+.2f}% does not beat charges {a.charges:.2f}" + (" (it is not above zero even before any charge)" if s["mean_close"] <= 0 else ""))
    deep = [r for r in s["ran_names"] if r["worst"] <= -a.stop]
    if deep:
        fails.append("(e) RAN names dipped past the stop: " + ", ".join(f"{r['symbol']} {r['worst']:.2f}" for r in deep))
    return ("CANDIDATE" if not fails else "KEEP"), fails


def show(title: str, s: dict, a) -> None:
    print(f"\n== {title} ==")
    if not s["rows"]:
        print("  no rows")
        return
    print(f"  sessions {s['days']}   rows {s['rows']}   priced {s['priced']}   no_data {s['no_data']}   RAN {s['ran']}   SPIKE {s['spike']}")
    if s["priced"]:
        print(f"  RAN {s['ran_pct']:.1f}%   mean close {s['mean_close']:+.2f}%   median close {s['median_close']:+.2f}%   mean best {s['mean_best']:+.2f}%   "
              f"worst <= -1%: {s['below_1']} of {s['priced']}")
        print(f"  mean close if a stop at -{a.stop:g}% had fired: {s['stop_mean']:+.2f}%   after charges {a.charges:.2f}: {s['stop_mean'] - a.charges:+.2f}%  (buy-everything, before sizing)")
        if s["ran_names"]:
            print("  RAN names (best / worst dip / close): " + ", ".join(f"{r['symbol']} {r['best']:+.1f}/{r['worst']:+.1f}/{r['close']:+.1f}" for r in s["ran_names"]))
    v, fails = verdict(s, a)
    print(f"  VERDICT: {v}" + ("" if v == "CANDIDATE" else "  - " + "; ".join(fails)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--charges", type=float, default=0.25)
    ap.add_argument("--stop", type=float, default=1.5)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--min-names", type=int, default=60)
    ap.add_argument("--min-ran-pct", type=float, default=15.0)
    ap.add_argument("--min-slice", type=int, default=30, help="round 688: fewer priced names than this in a slice is marked few")
    ap.add_argument("--no-slices", action="store_true", help="round 688: skip the by-hour / by-waits breakdown")
    a = ap.parse_args(argv)
    files = a.files or sorted(glob.glob("gates_out/gates_*.txt"))
    if not files:
        print("no gates_out/gates_*.txt yet: run  bash scripts/gates_daily.sh  after 15:45 IST first")
        return 1
    rows = []
    for f in files:
        rows += parse(open(f, encoding="utf-8", errors="ignore").read())
    per_gate = dedupe(rows, True)
    print(f"GATE RULE CHECK  files {len(files)}  rows {len(rows)}  (charges {a.charges:.2f}, stop {a.stop:g}%, need {a.min_days} sessions / {a.min_names} names / {a.min_ran_pct:g}% RAN)")
    for g in sorted({r["gate"] for r in per_gate}):
        show(f"gate {g}", stats([r for r in per_gate if r["gate"] == g], a.charges, a.stop), a)
    show("all blocked names (one row per name and day)", stats(dedupe(rows, False), a.charges, a.stop), a)
    show("event-sourced names only (* in the blocked table), first candidate of P2", stats([r for r in dedupe(rows, False) if r["event"]], a.charges, a.stop), a)
    if not a.no_slices:
        allrows = dedupe(rows, False)
        show_slices("all blocked names by hour of the first block", allrows, block_bucket, a)
        show_slices("all blocked names by number of waits that day", allrows, waits_bucket, a)
    print("\nOne number never loosens a gate: the verdict only says whether the rule of the plan is met. Not met = keep every gate as it is.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
