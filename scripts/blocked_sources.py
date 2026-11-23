#!/usr/bin/env python3
"""blocked_sources.py - which tab / label did each gate-blocked name come from, when was it first blocked? (read-only)

Run INSIDE the real-trade container, from ~/stockky-v2, after the close:
  docker compose exec -T real-trade-service python - --day 2026-11-24 --gate no_setup_window < scripts/blocked_sources.py
  ... --gate structure | soft_stack | time_machine ...   --top 80

For every symbol with a WAIT/ENTRY row of that gate_tag today it prints: waits, first block (IST), source_tab and decision_label of its candidate
(trade_candidates, joined by candidate_id). The last line is a ready --syms string for scripts/gate_outcome_dhan.py (market-data container),
so the outcome of EVERY blocked name can be read from Dhan 1-minute candles, not only the ones position-stocks recorded.
event-driven tabs (afterhours_news_scan, surprise) are marked with *. Writes nothing.
"""
import argparse
from datetime import datetime, timedelta

IST = timedelta(hours=5, minutes=30)
EVENT_TABS = {"afterhours_news_scan", "surprise"}


def window_utc(day):
    d = datetime.strptime(day, "%Y-%m-%d")
    return d + timedelta(hours=3, minutes=45), d + timedelta(hours=10)


def format_rows(rows, top):
    """rows: [(symbol, waits, first_utc, source_tab, label)] -> (text, syms_string)."""
    out = [f"{'symbol':<12}{'waits':>6}{'1st block':>10}  {'source_tab':<24}label"]
    for sym, n, first, tab, label in rows[:top]:
        hhmm = (first + IST).strftime("%H:%M") if first else "?"
        mark = "*" if (tab or "") in EVENT_TABS else " "
        out.append(f"{sym:<12}{n:>6}{hhmm:>10}  {(tab or '-'):<24}{label or '-'} {mark}".rstrip())
    by_tab = {}
    for _, _, _, tab, _ in rows:
        by_tab[tab or "-"] = by_tab.get(tab or "-", 0) + 1
    out.append("")
    out.append("symbols by source_tab: " + ", ".join(f"{k} {v}" for k, v in sorted(by_tab.items(), key=lambda kv: -kv[1])))
    syms = " ".join(f"{r[0]}:{(r[2] + IST).strftime('%H:%M')}" for r in rows[:top] if r[2])
    return "\n".join(out), syms


def fetch(day, gate, mode):
    import db  # the service's own module; only present inside the container
    from sqlalchemy import MetaData, Table, desc, func, select
    s = db.get_session_factory()()
    md = MetaData()
    d = Table("trade_decisions", md, autoload_with=s.get_bind())
    c = Table("trade_candidates", md, autoload_with=s.get_bind())
    lo, hi = window_utc(day)
    q = (select(d.c["symbol"], func.count().label("n"), func.min(d.c["created_at"]), func.max(c.c["source_tab"]), func.max(c.c["decision_label"]))
         .select_from(d.outerjoin(c, d.c["candidate_id"] == c.c["id"]))
         .where(d.c["mode"] == mode, d.c["decision_type"] == "ENTRY", d.c["gate_tag"] == gate, d.c["created_at"] >= lo, d.c["created_at"] < hi)
         .group_by(d.c["symbol"]).order_by(desc("n")))
    return [(r[0], int(r[1]), r[2], r[3], r[4]) for r in s.execute(q)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--gate", default="no_setup_window")
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--top", type=int, default=80)
    a = ap.parse_args()
    rows = fetch(a.day, a.gate, a.mode)
    if not rows:
        print(f"no {a.gate} rows for {a.mode} on {a.day} (check --day / --gate)")
        return
    text, syms = format_rows(rows, a.top)
    print(f"BLOCKED NAMES  day {a.day}  gate {a.gate}  ({len(rows)} symbols)\n")
    print(text)
    print("\nDhan outcome (run in market-data container, <= 25 names per call):")
    print(f'--syms "{syms}"')


if __name__ == "__main__":
    main()
