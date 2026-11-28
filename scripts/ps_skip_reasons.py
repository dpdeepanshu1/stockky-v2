#!/usr/bin/env python3
"""ps_skip_reasons.py - why did Position Stocks skip its candidates? One table per day, by reason family. (read-only, round 693)

Run INSIDE the position-stocks container, from ~/stockky-v2, after the close:
  docker compose exec -T position-stocks-service python - --day 2026-11-28 < scripts/ps_skip_reasons.py
  ... --days 3   (the day and the 2 before it, one table each)   --top 25   --samples 2

Reads scalp_candidate_log (decision ENTERED | SKIPPED, reason text like "RANGE_GATE:..." / "MAX_POSITIONS:3"). The reason FAMILY is the text before the first ':'
(digits and '=' parts after it are the detail, so "MAX_POSITIONS:3" and "MAX_POSITIONS:4" are one family). Per family: rows (a skip row is written every cycle, so
rows overstate), DISTINCT SYMBOLS (the number to compare with a trade count), share of all skipped rows, first row (IST) and up to --samples raw reasons.
ENTERED rows are counted at the top so the ratio is visible. Writes nothing; a day with no rows says so instead of printing zeros.
"""
import argparse
import re
from datetime import datetime, timedelta

IST = timedelta(hours=5, minutes=30)


def window_utc(day):
    """The IST trading day as naive UTC bounds (created_at is naive UTC): 03:45 to 10:00 UTC = 09:15 to 15:30 IST."""
    d = datetime.strptime(day, "%Y-%m-%d")
    return d + timedelta(hours=3, minutes=45), d + timedelta(hours=10)


def family(reason):
    """'RANGE_GATE:chop 1.2%' -> 'RANGE_GATE'; 'SERVICE_NOT_ARMED' -> itself; empty -> '(no reason)'. A reason with no ':' keeps its first word."""
    r = (reason or "").strip()
    if not r:
        return "(no reason)"
    head = r.split(":", 1)[0].strip()
    head = re.split(r"\s", head, 1)[0] if ":" not in r else head
    return head[:48] or "(no reason)"


def summarize(rows, samples=2):
    """rows: [(symbol, decision, reason, created_utc)] -> (entered_rows, entered_symbols, skipped_rows, [family dicts sorted by rows desc])."""
    entered, entered_syms, fam = 0, set(), {}
    skipped = 0
    for sym, dec, reason, ts in rows:
        if (dec or "").upper() == "ENTERED":
            entered += 1
            entered_syms.add(sym)
            continue
        skipped += 1
        f = fam.setdefault(family(reason), {"family": family(reason), "rows": 0, "syms": set(), "first": None, "samples": []})
        f["rows"] += 1
        f["syms"].add(sym)
        if ts and (f["first"] is None or ts < f["first"]):
            f["first"] = ts
        txt = (reason or "").strip().replace("\n", " ")[:110]
        if len(f["samples"]) < samples and txt and txt not in f["samples"]:
            f["samples"].append(txt)
    out = sorted(fam.values(), key=lambda x: (-x["rows"], x["family"]))
    return entered, len(entered_syms), skipped, out


def format_day(day, rows, top=25, samples=2):
    if not rows:
        return f"no scalp_candidate_log rows on {day} (check --day; a holiday or a day the service was off has none)"
    entered, entered_syms, skipped, fams = summarize(rows, samples)
    syms_all = {r[0] for r in rows}
    out = [f"POSITION STOCKS SKIP REASONS  day {day}  rows {len(rows)}  symbols {len(syms_all)}  ENTERED {entered} rows / {entered_syms} symbols  SKIPPED {skipped} rows", ""]
    out.append(f"{'family':<34}{'rows':>6}{'symbols':>9}{'share':>8}{'1st (IST)':>11}")
    for f in fams[:top]:
        share = 100.0 * f["rows"] / skipped if skipped else 0.0
        hhmm = (f["first"] + IST).strftime("%H:%M") if f["first"] else "?"
        out.append(f"{f['family']:<34}{f['rows']:>6}{len(f['syms']):>9}{share:>7.1f}%{hhmm:>11}")
        for s in f["samples"]:
            out.append(f"      {s}")
    if len(fams) > top:
        out.append(f"... {len(fams) - top} more families (raise --top)")
    return "\n".join(out)


def fetch(day):
    import db  # the service's own module; only present inside the container
    from sqlalchemy import MetaData, Table, select
    s = db.get_session_factory()()
    t = Table("scalp_candidate_log", MetaData(), autoload_with=s.get_bind())
    lo, hi = window_utc(day)
    q = select(t.c["symbol"], t.c["decision"], t.c["reason"], t.c["created_at"]).where(t.c["created_at"] >= lo, t.c["created_at"] < hi)
    return [(r[0], r[1], r[2], r[3]) for r in s.execute(q)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--samples", type=int, default=2)
    a = ap.parse_args()
    d0 = datetime.strptime(a.day, "%Y-%m-%d")
    for i in range(max(1, a.days)):
        day = (d0 - timedelta(days=i)).strftime("%Y-%m-%d")
        print(format_day(day, fetch(day), a.top, a.samples))
        print()


if __name__ == "__main__":
    main()
