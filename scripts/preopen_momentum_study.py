#!/usr/bin/env python3
"""preopen_momentum_study.py - E248: does the 09:15 opening move pay in YOUR recorded data, and why do so few opening orders go out?

Read-only. Stdlib only. Changes nothing in the running services.

PART 1 - opening momentum study (needs the 1-minute bar folders written by position-stocks feed/bar_recorder.py)

    python3 scripts/preopen_momentum_study.py BAR_DIR [--days 20] [--out DIR] [--candidate-log cand.json]
    BAR_DIR = the folder feed/bar_recorder.py writes (compose volume `bar-records`, /data/bars inside position-stocks-service), copied to the host
    like for scripts/intraday_gate_backtest.py and scripts/export_intraday.py.

For every symbol-day with a complete recording and a complete previous day it measures what a trade placed at the open would have returned:

  A_follow  buy / sell AT THE 09:15 OPEN in the direction of the pre-open gap (day open vs previous close, |gap| >= --min-gap).
            The 09:15 bar's open is the exchange's pre-open equilibrium price, so the side is knowable at 09:12.
  A_fade    the opposite side at the same price (does the gap revert?).
  B_5m      at 09:20, after the first five minutes: side = direction of the first five minutes (what the live 5-minute scan can see).
  C_agree   at 09:20, only when the gap and the first five minutes point the same way.
  ORB5      opening-range breakout: first close beyond the 09:15-09:19 high (long) / low (short) before 10:00, stop at the other end of
            that range, exit at the horizon (a stop fills at the stop price or the bar's open if that is worse).

Exits are measured at 09:45, 10:00, 10:30 and 15:00 (the price at a time = the open of that minute's bar). Net = gross - --cost-pct (round trip, default 0.15).
Each group reports n, distinct days, mean / median net %, win rate, a 95 % interval and a verdict. The interval is built on the DAILY mean net return
(one number per trading day, because every symbol of a day shares the market's move), so it is as wide as the day-to-day spread says:
  edge (interval above 0) / no_edge (interval below 0) / unclear / insufficient_sample (n < --min-n or days < --min-days).
Also the median adverse excursion before 10:00 and the share of trades that went 1 % against the entry before 10:00 (stop context).

PART 2 - opening funnel (optional)  --candidate-log FILE
    curl -s -H "Authorization: Bearer <admin token>" "http://127.0.0.1:8006/candidates/log?limit=500" > cand.json
Counts the candidate-log rows between 09:15 and 10:00 IST by decision and by the gate that skipped them, and entries by half hour of the day.
The route returns at most 500 rows, so this is a sample of the latest rows, not a full day.

READ THIS BEFORE TRUSTING IT
  * The 95 % interval uses a small-sample correction on the day count; with 10-20 days it is wide, which is the honest answer.
  * Bar-level approximation: no spread, no slippage beyond --cost-pct, no order queue, a stop that is touched inside one bar is filled at the stop.
  * A_follow / A_fade enter at the open price, which a real order at 09:15 may not get (the open has the widest spread of the day).
  * Past results in a recorded sample are not a promise. The study only says whether loosening the 09:15 rules is worth a small live test.
Output: markdown on stdout; with --out DIR also preopen_momentum_study.json and .md.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _intraday_data as idata  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
# horizon label -> minutes after 09:15
HORIZONS: Dict[str, int] = {"09:45": 30, "10:00": 45, "10:30": 75, "15:00": 345}
GAP_EDGES = (-3.0, -1.0, -0.3, 0.3, 1.0, 3.0)
GAP_LABELS = ("gap < -3%", "-3% to -1%", "-1% to -0.3%", "flat (+-0.3%)", "+0.3% to +1%", "+1% to +3%", "gap > +3%")
KINDS = ("A_follow", "A_fade", "B_5m", "C_agree", "ORB5")


def gap_bucket(gap_pct: float) -> str:
    for edge, label in zip(GAP_EDGES, GAP_LABELS):
        if gap_pct < edge:
            return label
    return GAP_LABELS[-1]


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _minute_map(bars, date: str) -> Dict[int, tuple]:
    """{minutes after 09:15: (t, o, h, l, c, v)} for the session of `date`."""
    t0 = idata.open_ts(date)
    out: Dict[int, tuple] = {}
    for b in bars:
        off = int(round((b[0] - t0) / 60.0))
        if 0 <= off < 375:
            out[off] = b
    return out


def _px_at(mb: Dict[int, tuple], off: int) -> Optional[float]:
    """Open of the bar that starts at `off`; if that minute is missing, the close of the latest bar in the 5 minutes before it."""
    b = mb.get(off)
    if b is not None and b[1] > 0:
        return b[1]
    for k in range(off - 1, off - 6, -1):
        b = mb.get(k)
        if b is not None and b[4] > 0:
            return b[4]
    return None


def _adverse(mb: Dict[int, tuple], side: int, entry: float, from_off: int, to_off: int) -> Optional[float]:
    worst: Optional[float] = None
    for off in range(from_off, to_off):
        b = mb.get(off)
        if b is None:
            continue
        a = (entry - b[3]) / entry * 100.0 if side > 0 else (b[2] - entry) / entry * 100.0
        worst = a if worst is None else max(worst, a)
    return None if worst is None else max(worst, 0.0)


def _orb(mb: Dict[int, tuple], hi5: float, lo5: float, exit_off: int) -> Optional[Tuple[int, float, float, bool]]:
    """(side, entry, exit, stopped) for the first close beyond the opening range before 10:00; None when there is none."""
    side, entry, e_off = 0, 0.0, -1
    for off in range(5, 45):
        b = mb.get(off)
        if b is None:
            continue
        if b[4] > hi5:
            side, entry, e_off = 1, b[4], off
            break
        if b[4] < lo5:
            side, entry, e_off = -1, b[4], off
            break
    if not side or entry <= 0:
        return None
    stop = lo5 if side > 0 else hi5
    for off in range(e_off + 1, exit_off):
        b = mb.get(off)
        if b is None:
            continue
        if side > 0 and b[3] <= stop:
            return side, entry, min(stop, b[1]), True
        if side < 0 and b[2] >= stop:
            return side, entry, max(stop, b[1]), True
    px = _px_at(mb, exit_off)
    return None if px is None else (side, entry, px, False)


def observations(days_by_symbol: Dict[str, Dict[str, Any]], min_gap: float, cost_pct: float, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """One dict per (symbol, day, kind, horizon). Pure function of the bar data."""
    obs: List[Dict[str, Any]] = []
    for sym, days in days_by_symbol.items():
        for date, day in days.items():
            if since and date < since:
                continue
            if not day.get("complete"):
                continue
            prev = idata.previous_day(days, date)
            if not prev or not days[prev].get("complete"):
                continue
            prev_close = days[prev]["bars"][-1][4]
            mb = _minute_map(day["bars"], date)
            b0 = mb.get(0)
            if b0 is None or prev_close <= 0 or b0[1] <= 0:
                continue
            open_px = b0[1]
            gap = (open_px / prev_close - 1.0) * 100.0
            if abs(gap) > 25.0:                                   # a corporate action / bad print, not a gap
                continue
            first5 = [mb[o] for o in range(5) if o in mb]
            if len(first5) < 4:
                continue
            hi5, lo5 = max(b[2] for b in first5), min(b[3] for b in first5)
            close5 = first5[-1][4]
            d5 = _sign(close5 - open_px)
            gsign = _sign(gap) if abs(gap) >= min_gap else 0
            bucket = gap_bucket(gap)
            px5 = _px_at(mb, 5)

            def add(kind: str, side: int, entry: float, entry_off: int) -> None:
                if side == 0 or entry <= 0:
                    return
                mae = _adverse(mb, side, entry, entry_off, 45)
                for label, h_off in HORIZONS.items():
                    px = _px_at(mb, h_off)
                    if px is None or h_off <= entry_off:
                        continue
                    gross = side * (px / entry - 1.0) * 100.0
                    obs.append({"date": date, "symbol": sym, "kind": kind, "bucket": bucket, "horizon": label,
                                "gross": gross, "net": gross - cost_pct, "mae": mae})

            if gsign:
                add("A_follow", gsign, open_px, 0)
                if abs(gap) >= 1.0:
                    add("A_fade", -gsign, open_px, 0)
            if d5 and px5:
                add("B_5m", d5, px5, 5)
                if gsign and gsign == d5:
                    add("C_agree", d5, px5, 5)
            for label, h_off in HORIZONS.items():
                if h_off < 45:
                    continue
                r = _orb(mb, hi5, lo5, h_off)
                if r is None:
                    continue
                side, entry, exit_px, stopped = r
                gross = side * (exit_px / entry - 1.0) * 100.0
                obs.append({"date": date, "symbol": sym, "kind": "ORB5", "bucket": bucket, "horizon": label,
                            "gross": gross, "net": gross - cost_pct, "mae": None, "stopped": stopped})
    return obs


def summarize(pairs: List[Tuple[str, float]], min_n: int, min_days: int) -> Dict[str, Any]:
    """pairs = [(date, net %)]. The interval is on the mean of the per-day means (days are the independent units)."""
    n = len(pairs)
    by_day: Dict[str, List[float]] = defaultdict(list)
    for d, v in pairs:
        by_day[d].append(v)
    days = len(by_day)
    if n == 0:
        return {"n": 0, "days": 0, "verdict": "insufficient_sample"}
    vals = [v for _, v in pairs]
    day_means = [sum(x) / len(x) for x in by_day.values()]
    centre = sum(day_means) / days
    if days > 1:
        se = statistics.stdev(day_means) / math.sqrt(days)
        crit = 1.96 + 2.5 / (days - 1)                      # small-sample widening (t is 2.26 at 10 days, 2.05 at 30)
    else:
        se, crit = 0.0, 0.0
    lo, hi = centre - crit * se, centre + crit * se
    if n < min_n or days < min_days:
        verdict = "insufficient_sample"
    elif lo > 0:
        verdict = "edge"
    elif hi < 0:
        verdict = "no_edge"
    else:
        verdict = "unclear"
    return {"n": n, "days": days, "mean": round(sum(vals) / n, 4), "median": round(statistics.median(vals), 4),
            "win_pct": round(100.0 * sum(1 for v in vals if v > 0) / n, 1), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
            "verdict": verdict}


def report(obs: List[Dict[str, Any]], min_n: int, min_days: int) -> Dict[str, Any]:
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for o in obs:
        groups[(o["kind"], o["bucket"], o["horizon"])].append(o)
        groups[(o["kind"], "ALL", o["horizon"])].append(o)
    rows = []
    for (kind, bucket, horizon), lst in groups.items():
        s = summarize([(x["date"], x["net"]) for x in lst], min_n, min_days)
        maes = [x["mae"] for x in lst if x.get("mae") is not None]
        if maes:
            s["median_mae_pct"] = round(statistics.median(maes), 3)
            s["went_1pct_against_pct"] = round(100.0 * sum(1 for m in maes if m >= 1.0) / len(maes), 1)
        stopped = [x for x in lst if "stopped" in x]
        if stopped:
            s["stopped_pct"] = round(100.0 * sum(1 for x in stopped if x["stopped"]) / len(stopped), 1)
        rows.append({"kind": kind, "bucket": bucket, "horizon": horizon, **s})
    order_b = {b: i for i, b in enumerate(("ALL",) + GAP_LABELS)}
    rows.sort(key=lambda r: (KINDS.index(r["kind"]), list(HORIZONS).index(r["horizon"]), order_b.get(r["bucket"], 99)))
    return {"symbol_days": len({(o["symbol"], o["date"]) for o in obs}), "days": len({o["date"] for o in obs}), "rows": rows}


def _parse_ist(v) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(IST)
    except (TypeError, ValueError):
        return None


def funnel(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Candidate-log rows (the /candidates/log shape) -> what happened between 09:15 and 10:00 IST."""
    win: Dict[str, int] = defaultdict(int)
    gates: Dict[str, int] = defaultdict(int)
    entered_by_half_hour: Dict[str, int] = defaultdict(int)
    total = 0
    for r in rows or []:
        d = _parse_ist(r.get("created_at"))
        if d is None:
            continue
        mins = d.hour * 60 + d.minute
        if str(r.get("decision", "")).upper() == "ENTERED":
            entered_by_half_hour[f"{d.hour:02d}:{'00' if d.minute < 30 else '30'}"] += 1
        if not (9 * 60 + 15 <= mins < 10 * 60):
            continue
        total += 1
        dec = str(r.get("decision", "?")).upper()
        win[dec] += 1
        if dec == "SKIPPED":
            reason = str(r.get("reason") or "(no reason)")
            gates[reason.split(":", 1)[0].strip() or "(no reason)"] += 1
    return {"rows_09_15_to_10_00": total, "decisions": dict(win),
            "skipped_by_gate": dict(sorted(gates.items(), key=lambda kv: -kv[1])),
            "entered_by_half_hour": dict(sorted(entered_by_half_hour.items()))}


def render(rep: Dict[str, Any], fun: Optional[Dict[str, Any]], show: List[str], args) -> str:
    out = ["# Opening momentum study (E248)", ""]
    if rep is not None:
        out += [f"Symbol-days measured: {rep['symbol_days']} over {rep['days']} trading day(s). Cost assumed: {args.cost_pct}% round trip. "
                f"Verdict needs n >= {args.min_n} and >= {args.min_days} days.", ""]
        for kind in KINDS:
            krows = [r for r in rep["rows"] if r["kind"] == kind and r["horizon"] in show]
            if not krows:
                continue
            out += [f"## {kind}", "", "| gap bucket | exit | n | days | mean net % | median | win % | 95% interval (by day) | 1% against by 10:00 | verdict |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
            for r in krows:
                if r["n"] == 0:
                    continue
                ci = f"{r['ci_lo']:+.2f} .. {r['ci_hi']:+.2f}" if "ci_lo" in r else "-"
                mean = f"{r['mean']:+.3f}" if "mean" in r else "-"
                med = f"{r['median']:+.3f}" if "median" in r else "-"
                out.append(f"| {r['bucket']} | {r['horizon']} | {r['n']} | {r['days']} | {mean} | {med} | {r.get('win_pct', '-')} | {ci} | "
                           f"{r.get('went_1pct_against_pct', '-')} | {r['verdict']} |")
            out.append("")
    if fun is not None:
        out += ["## Opening funnel (candidate log, 09:15-10:00 IST)", "", f"Rows in the window: {fun['rows_09_15_to_10_00']}  decisions: {fun['decisions']}", ""]
        out += ["| skipped by gate | rows |", "|---|---|"] + [f"| {k} | {v} |" for k, v in list(fun["skipped_by_gate"].items())[:25]]
        out += ["", f"Entered by half hour (whole log sample): {fun['entered_by_half_hour']}", ""]
    out += ["## How to read it", "",
            "* A_follow with verdict `edge` at 10:00 in a gap bucket means entering at the open on the pre-open direction paid in this sample; `no_edge` means it lost after costs.",
            "* Compare A_follow with B_5m: if waiting five minutes is better, the live 09:20 start is right and 09:15 adds only noise.",
            "* Nothing here changes a setting. If a bucket shows `edge` over enough days, test it as a small, switchable live change, not a blanket loosening.", ""]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Opening momentum study + opening funnel (read-only)")
    ap.add_argument("bars_dir", nargs="?", default="/data/bars", help="folder written by feed/bar_recorder.py (default /data/bars)")
    ap.add_argument("--days", type=int, default=20, help="only the latest N recorded dates")
    ap.add_argument("--min-gap", type=float, default=0.3, help="smallest |gap| %% that gives A_follow a side")
    ap.add_argument("--cost-pct", type=float, default=0.15, help="round-trip cost %% taken off every trade")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=10)
    ap.add_argument("--show", default="10:00,15:00", help="exit times to print (JSON always has all)")
    ap.add_argument("--candidate-log", default="", help="JSON from /candidates/log for the opening funnel")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    rep = None
    stats: Dict[str, int] = {}
    data = idata.load_dir(args.bars_dir, stats=stats) if Path(args.bars_dir).is_dir() else {}
    if data:
        dates = sorted({d for days in data.values() for d in days})
        since = dates[-args.days] if args.days > 0 and len(dates) > args.days else None
        rep = report(observations(data, args.min_gap, args.cost_pct, since), args.min_n, args.min_days)
        rep["files"], rep["bad_files"] = stats.get("files", 0), stats.get("bad_files", 0)
    fun = None
    if args.candidate_log:
        try:
            doc = json.loads(Path(args.candidate_log).read_text())
            fun = funnel(doc.get("rows") if isinstance(doc, dict) else doc)
        except Exception as e:  # noqa: BLE001
            print(f"candidate log not read: {e}", file=sys.stderr)
    if rep is None and fun is None:
        print(f"nothing to do: no bar folders under {args.bars_dir} and no --candidate-log", file=sys.stderr)
        return 2
    md = render(rep, fun, [s.strip() for s in args.show.split(",") if s.strip()], args)
    print(md)
    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "preopen_momentum_study.md").write_text(md)
        (outdir / "preopen_momentum_study.json").write_text(json.dumps({"study": rep, "funnel": fun}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
