#!/usr/bin/env python3
"""wait_dedupe_sim.py - round 681: how many of a day's REAL entry WAIT rows would a stricter dedupe drop? Read-only, writes nothing, places no order.

23 Nov: 1,491 REAL entry WAIT rows (target under about 300) although round 659's dedupe (wait_dedupe.py) was on. That rule compares a new WAIT row only with the LAST row
written for the symbol: a name that flips between two gate tags (A, B, A, B ...) writes a row at every flip. This script replays the day's rows from the database and counts, per rule:
  current   the rule of wait_dedupe.py (drop only when the signature equals the LAST written one and the window has not passed). The rows in the table already went
            through it, so this should drop about nothing: that is the check that the replay is faithful.
  any_sig   drop a row when the SAME signature was written for the symbol inside the window, whatever was written in between (A, B, A, B -> A, B).
  sym_gate  drop a row when the same (symbol, gate_tag) was written inside the window (coarser: ignores a change of the text only).
It also lists the names with the most rows and how often their signature flipped back to one it had before (an A -> B -> A pattern).

Run INSIDE the real-trade container, after 15:30 IST (the rows of a day are the evidence):
  docker compose exec -T real-trade-service python - --day 2026-11-24 < scripts/wait_dedupe_sim.py
  options: --window-min 15 (default = WAIT_DEDUPE_MINUTES)   --top 8
Nothing is changed in the service: this is the number to read BEFORE touching wait_dedupe.py."""
import argparse
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

_NUM = re.compile(r"-?\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"\s+")


def signature(gate_tag, reasoning, verdict, verdict_reason) -> str:
    """Same text as wait_dedupe.signature (numbers -> '#', whitespace collapsed, 300 chars)."""
    txt = "|".join(str(p or "") for p in (gate_tag, reasoning, verdict, verdict_reason))
    return _SPACE.sub(" ", _NUM.sub("#", txt)).strip()[:300]


def simulate(rows, window_s: float) -> dict:
    """rows: iterable of (symbol, gate_tag, reasoning, verdict, verdict_reason, t_seconds), any order. Returns counts per rule and per-symbol facts."""
    by_sym = defaultdict(list)
    for sym, tag, rs, vd, vr, t in rows:
        by_sym[sym].append((float(t), str(tag or ""), signature(tag, rs, vd, vr)))
    out = {"rows": 0, "current": 0, "any_sig": 0, "sym_gate": 0, "symbols": len(by_sym), "per_symbol": {}}
    for sym, lst in by_sym.items():
        lst.sort()
        out["rows"] += len(lst)
        last_sig, last_t = None, None                       # current rule: only the last written row counts
        seen_sig = {}                                       # any_sig: every signature written, with its time
        seen_tag = {}                                       # sym_gate: every gate tag written, with its time
        keep_cur = keep_any = keep_tag = 0
        flips_back, prev_sig, sigs = 0, None, []
        for t, tag, sig in lst:
            if last_sig is None or sig != last_sig or (t - last_t) >= window_s:
                keep_cur += 1
                last_sig, last_t = sig, t
            if sig not in seen_sig or (t - seen_sig[sig]) >= window_s:
                keep_any += 1
                seen_sig[sig] = t
            if tag not in seen_tag or (t - seen_tag[tag]) >= window_s:
                keep_tag += 1
                seen_tag[tag] = t
            if prev_sig is not None and sig != prev_sig and sig in sigs[:-1]:
                flips_back += 1                             # went back to a signature it had before: A -> B -> A
            if prev_sig != sig:
                sigs.append(sig)
            prev_sig = sig
        out["current"] += keep_cur
        out["any_sig"] += keep_any
        out["sym_gate"] += keep_tag
        out["per_symbol"][sym] = {"rows": len(lst), "distinct": len(set(s for _, _, s in lst)), "flips_back": flips_back,
                                  "any_sig": keep_any, "sym_gate": keep_tag}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--window-min", type=float, default=None)
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()
    win = (a.window_min if a.window_min is not None else float((os.environ.get("WAIT_DEDUPE_MINUTES") or "").strip() or 15)) * 60.0
    from db import get_session_factory                       # only present inside the real-trade container
    from sqlalchemy import MetaData, Table, select
    d = datetime.strptime(a.day, "%Y-%m-%d")
    s = get_session_factory()()
    t = Table("trade_decisions", MetaData(), autoload_with=s.get_bind())
    c = t.c
    q = select(c["symbol"], c["gate_tag"], c["reasoning"], c["risk_verdict"], c["risk_verdict_reason"], c["created_at"]).where(
        c["mode"] == "REAL", c["decision_type"] == "ENTRY", c["action"] == "WAIT",
        c["created_at"] >= d - timedelta(hours=5, minutes=30), c["created_at"] < d + timedelta(hours=18, minutes=30))
    epoch = datetime(1970, 1, 1)
    rows = [(r[0], r[1], r[2], r[3], r[4], (r[5].replace(tzinfo=None) - epoch).total_seconds()) for r in s.execute(q)]
    res = simulate(rows, win)
    n = res["rows"]
    print(f"WAIT DEDUPE REPLAY  day {a.day}  window {win / 60:g} min  rows {n}  names {res['symbols']}")
    if not n:
        print("no REAL entry WAIT rows for that day")
        return
    for k, label in (("current", "current rule (should drop about nothing: these rows already went through it)"),
                     ("any_sig", "any_sig  (same signature written inside the window)"), ("sym_gate", "sym_gate (same gate tag written inside the window)")):
        kept = res[k]
        print(f"  {label:<78} keeps {kept:>5}  drops {n - kept:>5}  ({100.0 * (n - kept) / n:.0f}% of rows)")
    ps = sorted(res["per_symbol"].items(), key=lambda kv: -kv[1]["rows"])[: a.top]
    print(f"\n  {'name':<12}{'rows':>6}{'signatures':>12}{'A->B->A':>9}{'any_sig keeps':>15}{'sym_gate keeps':>16}")
    for sym, f in ps:
        print(f"  {sym:<12}{f['rows']:>6}{f['distinct']:>12}{f['flips_back']:>9}{f['any_sig']:>15}{f['sym_gate']:>16}")
    print("\nRead it as: a rule that drops under about 30% of the rows is not worth a code change; the rows are not the DB problem unless the open-window 'oracle slow' count says so.")


if __name__ == "__main__":
    main()
