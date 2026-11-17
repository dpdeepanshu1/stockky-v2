"""Round 598: what did the names held by one entry gate do AFTER their first block? (read-only)

Run INSIDE the real-trade container, from ~/stockky-v2, after the close (15:45 IST or later):
    docker compose exec -T real-trade-service python - --day 2026-11-17 < scripts/blocked_outcome.py
    ... --gate no_setup_window (default) | time_machine | structure | ...   --up 2.0 (percent)   --top 40
    ... --gate soft_stack --shadow A      only the rows the round 599 shadow rule A would have let in (B: --gate no_setup_window --shadow B)

Reads: trade_decisions (REAL, ENTRY, WAIT rows with that gate_tag, 09:15-15:30 IST of --day) and the 1-minute bars position-stocks records
under /data/bars/<day>/<SYMBOL>.json. Writes nothing. A symbol with no recorded bars is counted as no_bars, never guessed.

For each symbol: price at its FIRST block, then the best / worst price and the close afterwards, in percent of that first price.
  RAN        best price after the block was >= --up percent above it AND the close was above it
  SPIKE      it touched --up percent but closed at or below the first price
  NO_MOVE    it never reached --up percent
This is a description of the day, not a trading result: the best price is not one you could have sold at, and a stop would have fired on many
of the RAN names. Use it with 2-3 sessions to decide keep / narrow / relax, not on one day.
Stdlib only for the analysis; the DB part uses the service's own db module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

IST = timedelta(hours=5, minutes=30)


def window_utc(day: str):
    """09:15-15:30 IST of `day` as naive UTC datetimes (trade_decisions.created_at is naive UTC)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    return d + timedelta(hours=3, minutes=45), d + timedelta(hours=10)


def load_bars(bars_dir: str, day: str, symbol: str):
    """[[t, o, h, l, c, v], ...] sorted by t, or None when the file is missing / unreadable / empty."""
    path = os.path.join(bars_dir, day, quote(symbol, safe="") + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            bars = json.load(f).get("bars")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(bars, list):
        return None
    out = []
    for b in bars:
        try:
            out.append([float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])])
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    return out or None


def outcome(bars, first_ts: float, up_pct: float):
    """None when the bars do not cover first_ts. Else {px0, up, down, close, label} (percents of px0)."""
    idx = next((i for i, b in enumerate(bars) if b[0] + 60 > first_ts), None)
    if idx is None:
        return None
    after = bars[idx:]
    px0 = after[0][4]
    if px0 <= 0:
        return None
    hi, lo, last = max(b[2] for b in after), min(b[3] for b in after), after[-1][4]
    up, down, close = (hi / px0 - 1) * 100, (lo / px0 - 1) * 100, (last / px0 - 1) * 100
    label = "NO_MOVE" if up < up_pct else ("RAN" if close > 0 else "SPIKE")
    return {"px0": px0, "up": up, "down": down, "close": close, "label": label}


def analyse(blocks, bars_dir: str, day: str, up_pct: float = 2.0):
    """blocks: [(symbol, n_waits, first_utc_naive, last_utc_naive)]. Returns (rows, counts)."""
    rows, counts = [], {"RAN": 0, "SPIKE": 0, "NO_MOVE": 0, "no_bars": 0}
    for sym, n, first, _last in blocks:
        bars = load_bars(bars_dir, day, sym)
        res = outcome(bars, first.replace(tzinfo=timezone.utc).timestamp(), up_pct) if bars else None
        if res is None:
            counts["no_bars"] += 1
            rows.append({"symbol": sym, "n": n, "first_ist": (first + IST).strftime("%H:%M"), "label": "no_bars"})
            continue
        counts[res["label"]] += 1
        rows.append({"symbol": sym, "n": n, "first_ist": (first + IST).strftime("%H:%M"), **res})
    return rows, counts


def format_report(day, gate, up_pct, rows, counts, top):
    L = [f"BLOCKED-NAME OUTCOMES  day {day}  gate {gate}  (percent of the price at the first block; RAN = best >= +{up_pct:g}% and close above)",
         f"symbols {len(rows)}: RAN {counts['RAN']}  SPIKE {counts['SPIKE']}  NO_MOVE {counts['NO_MOVE']}  no_bars {counts['no_bars']}", ""]
    L.append(f"{'symbol':<12}{'waits':>6}{'1st block':>10}{'price':>10}{'best %':>8}{'worst %':>9}{'close %':>9}  label")
    for r in rows[:top]:
        if r["label"] == "no_bars":
            L.append(f"{r['symbol']:<12}{r['n']:>6}{r['first_ist']:>10}{'':>10}{'':>8}{'':>9}{'':>9}  no_bars")
        else:
            L.append(f"{r['symbol']:<12}{r['n']:>6}{r['first_ist']:>10}{r['px0']:>10.2f}{r['up']:>8.2f}{r['down']:>9.2f}{r['close']:>9.2f}  {r['label']}")
    if len(rows) > top:
        L.append(f"... {len(rows) - top} more symbols not shown (--top)")
    L += ["", "The best price is not a price you could sell at, and a stop would have fired on many RAN names. This is one day: read it together with the",
          "next 2-3 sessions before keeping, narrowing or relaxing the rule. no_bars = position-stocks did not record that symbol today."]
    return "\n".join(L)


def fetch_blocks(day: str, gate: str, mode: str, shadow: str = ""):
    import db  # noqa: E402  (the service's own module; only present inside the container)
    from sqlalchemy import MetaData, Table, desc, func, select
    s = db.get_session_factory()()
    t = Table("trade_decisions", MetaData(), autoload_with=s.get_bind())
    lo, hi = window_utc(day)
    conds = [t.c["mode"] == mode, t.c["decision_type"] == "ENTRY", t.c["gate_tag"] == gate, t.c["created_at"] >= lo, t.c["created_at"] < hi]
    if shadow:                                   # round 599: only rows carrying flow_json["or_shadow"] with that rule
        conds += [t.c["flow_json"].like('%"or_shadow"%'), t.c["flow_json"].like('%"rule":"' + shadow.upper()[:1] + '"%')]
    q = (select(t.c["symbol"], func.count().label("n"), func.min(t.c["created_at"]), func.max(t.c["created_at"]))
         .where(*conds).group_by(t.c["symbol"]).order_by(desc("n")))
    return [(r[0], int(r[1]), r[2], r[3]) for r in s.execute(q)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default=(datetime.now(timezone.utc) + IST).strftime("%Y-%m-%d"))
    ap.add_argument("--gate", default="no_setup_window")
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--shadow", default="", help="A or B: only rows the round 599 shadow rule recorded")
    ap.add_argument("--up", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--bars-dir", default="/data/bars")
    a = ap.parse_args(argv)
    blocks = fetch_blocks(a.day, a.gate, a.mode, a.shadow)
    if not blocks:
        print(f"no {a.gate} WAIT rows for {a.mode} on {a.day} (check --day / --gate)")
        return 0
    rows, counts = analyse(blocks, a.bars_dir, a.day, a.up)
    print(format_report(a.day, a.gate, a.up, rows, counts, a.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
