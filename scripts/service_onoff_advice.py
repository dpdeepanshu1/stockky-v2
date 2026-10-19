#!/usr/bin/env python3
"""service_onoff_advice.py - group 446: from the REAL closed trades of both services, say which one to keep ON and which to pause.

  python3 scripts/service_onoff_advice.py [--days 10] [--rt-url http://127.0.0.1:8005] [--ps-url http://127.0.0.1:8006] [--token T | TOKEN=T]
                                          [--min-n 8] [--min-days 3] [--pf-off 0.8] [--pf-keep 1.1] [--streak-off 3]
                                          [--out service_advice.txt] [--json service_advice.json]
  python3 scripts/service_onoff_advice.py --rt-file rt_2026-10-18.json --ps-file ps_hist.json     # offline, from saved exports
  python3 scripts/service_onoff_advice.py --from-csv daily_trades.csv                              # offline, from the daily_trades.csv this repo writes

It reads exactly what daily_trade_report.py reads (read-only GETs, same parsing, same IST day, P&L net of charges for real-trade) and judges
each service on its own record in the window:

  KEEP ON          net > 0, still > 0 without its best trade, profit factor >= --pf-keep, and no losing streak at the end.
  WATCH            anything in between (a small loss, a profit that rests on one trade, a bad last few days, a thin profit factor).
  TURN OFF (pause) net < 0 AND still < 0 without its best trade AND profit factor < --pf-off; or --streak-off losing days in a row with net < 0.
  NOT ENOUGH DATA  fewer than --min-n trades or fewer than --min-days trading days: no call is made (a bad hour with 3 trades is not a finding).

Both services trade the same Dhan account, so it also reports the overlap (same symbol bought by both on the same day), the combined result,
and which of the two has the better record when you can only keep one.

It only RECOMMENDS. It never changes a switch, an order or a service. Turning a service off is your decision (its own Auto-Pilot / master switch).
Exit 0 = report written, nothing to act on; 1 = at least one service is TURN OFF, or no trades were found. Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_trade_report as d  # noqa: E402

IST = d.IST
KEEP, WATCH, OFF, THIN = "KEEP ON", "WATCH", "TURN OFF", "NOT ENOUGH DATA"
LABEL = {"real-trade": "real-trade-service", "position-stocks": "position-stocks-service"}


# ---------------------------------------------------------------- input
def rows_from_csv(path: str) -> List[Dict[str, Any]]:
    """Rows from the daily_trades.csv written by daily_trade_report.py (times are IST 'YYYY-MM-DD HH:MM')."""
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                o = datetime.strptime(r["entry_ist"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                c = datetime.strptime(r["exit_ist"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except (KeyError, ValueError, TypeError):
                continue
            p = d.num(r.get("pnl"))
            if p is None:
                continue
            out.append({"service": r.get("service") or "?", "id": None, "symbol": r.get("symbol") or "?", "entry": o, "exit": c, "day": c.date().isoformat(),
                        "held_min": max(0.0, (c - o).total_seconds() / 60.0), "pnl": p, "ret": d.num(r.get("return_pct")),
                        "reason": r.get("exit_reason") or "-", "qty": d.num(r.get("quantity")), "origin": r.get("origin") or "system"})
    return out


# ---------------------------------------------------------------- judgement (pure)
def service_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pn = [r["pnl"] for r in rows]
    wins, losses = sum(x for x in pn if x > 0), -sum(x for x in pn if x < 0)
    by_day: Dict[str, float] = defaultdict(float)
    for r in rows:
        by_day[r["day"]] += r["pnl"]
    days = sorted(by_day)
    streak = 0
    for day in reversed(days):
        if by_day[day] < 0:
            streak += 1
        else:
            break
    net = round(sum(pn), 2)
    best = max(pn) if pn else 0.0
    return {"n": len(rows), "days": len(days), "net": net, "win_pct": round(100.0 * sum(1 for x in pn if x > 0) / len(pn), 1) if pn else None,
            "avg": round(net / len(pn), 2) if pn else None, "profit_factor": round(wins / losses, 2) if losses > 0 else None,
            "gross_win": round(wins, 2), "gross_loss": round(losses, 2), "net_without_best": round(net - best, 2) if pn else 0.0,
            "worst_day": round(min(by_day.values()), 2) if by_day else None, "worst_day_date": min(by_day, key=by_day.get) if by_day else None,
            "losing_streak_days": streak, "last3_net": round(sum(by_day[x] for x in days[-3:]), 2), "by_day": {k: round(by_day[k], 2) for k in days}}


def judge(rows: List[Dict[str, Any]], min_n: int = 8, min_days: int = 3, pf_off: float = 0.8, pf_keep: float = 1.1, streak_off: int = 3) -> Dict[str, Any]:
    s = service_stats(rows)
    why: List[str] = []
    pf = s["profit_factor"]            # None = no losing trade at all
    if s["n"] < min_n or s["days"] < min_days:
        return {**s, "verdict": THIN, "why": [f"{s['n']} trades over {s['days']} day(s); needs at least {min_n} trades over {min_days} days before any call is made"]}
    pf_txt = "no losing trade" if pf is None else f"profit factor {pf}"
    if s["net"] < 0 and s["net_without_best"] < 0 and pf is not None and pf < pf_off:
        why.append(f"lost {d.money(s['net'])} over {s['n']} trades, still {d.money(s['net_without_best'])} without its best trade, {pf_txt} (below {pf_off})")
    if s["net"] < 0 and s["losing_streak_days"] >= streak_off:
        why.append(f"{s['losing_streak_days']} losing days in a row at the end of the window, net {d.money(s['net'])}")
    if why:
        return {**s, "verdict": OFF, "why": why}
    if s["net"] > 0 and s["net_without_best"] > 0 and (pf is None or pf >= pf_keep) and s["losing_streak_days"] < 2:
        return {**s, "verdict": KEEP, "why": [f"net {d.money(s['net'])} over {s['n']} trades, {d.money(s['net_without_best'])} without its best trade, {pf_txt}"]}
    if s["net"] < 0:
        why.append(f"net {d.money(s['net'])} over {s['n']} trades, but not bad enough for a pause ({pf_txt}, {s['losing_streak_days']} losing day(s) at the end)")
    elif s["net_without_best"] <= 0:
        why.append(f"profit {d.money(s['net'])} rests on one trade: {d.money(s['net_without_best'])} without it")
    if s["net"] >= 0 and pf is not None and pf < pf_keep:
        why.append(f"thin edge, {pf_txt} (keep needs {pf_keep})")
    if s["losing_streak_days"] >= 2:
        why.append(f"{s['losing_streak_days']} losing days in a row at the end")
    return {**s, "verdict": WATCH, "why": why or ["mixed result"]}


def overlap(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Same symbol opened by both services on the same IST date (they share one Dhan account)."""
    seen: Dict[tuple, Dict[str, float]] = defaultdict(dict)
    for r in rows:
        seen[(r["symbol"], r["entry"].date().isoformat())][r["service"]] = seen[(r["symbol"], r["entry"].date().isoformat())].get(r["service"], 0.0) + r["pnl"]
    both = {k: v for k, v in seen.items() if len(v) >= 2}
    return {"symbols": len(both), "net": round(sum(sum(v.values()) for v in both.values()), 2),
            "list": sorted(f"{k[0]} {k[1]}" for k in both)[:10]}


def advise(rows: List[Dict[str, Any]], **kw) -> Dict[str, Any]:
    svcs = sorted({r["service"] for r in rows})
    res = {s: judge([r for r in rows if r["service"] == s], **kw) for s in svcs}
    out: Dict[str, Any] = {"services": res, "overlap": overlap(rows), "combined": service_stats(rows) if rows else None, "better": None}
    ranked = [s for s in svcs if res[s]["verdict"] != THIN]
    if len(ranked) >= 2:
        ranked.sort(key=lambda s: (res[s]["net"], res[s]["profit_factor"] or 99.0), reverse=True)
        if res[ranked[0]]["net"] > res[ranked[1]]["net"]:
            out["better"] = ranked[0]
    return out


# ---------------------------------------------------------------- report
def build_text(adv: Dict[str, Any], days: int, header: Optional[List[str]] = None, th: Optional[Dict[str, Any]] = None) -> str:
    th = th or {}
    L = ["SERVICE ON / OFF ADVICE - from the real closed trades (IST)", f"generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}   window: last {days} day(s)"]
    L += header or []
    L += ["", f"rules: KEEP ON = net>0, >0 without best trade, profit factor>={th.get('pf_keep', 1.1)}; TURN OFF = net<0 and <0 without best trade and profit factor<{th.get('pf_off', 0.8)},"
              f" or {th.get('streak_off', 3)} losing days in a row; fewer than {th.get('min_n', 8)} trades / {th.get('min_days', 3)} days = NOT ENOUGH DATA", ""]
    L += ["=" * 100, "RECOMMENDATION", "=" * 100]
    for s, r in adv["services"].items():
        L.append(f"  {LABEL.get(s, s):<26} {r['verdict']}")
    for s, r in adv["services"].items():
        pf = "-" if r["profit_factor"] is None else r["profit_factor"]
        L += ["", f"{LABEL.get(s, s)}: {r['verdict']}",
              f"  {r['n']} trades over {r['days']} day(s), win {r['win_pct']}%, net {d.money(r['net'])}, avg {d.money(r['avg'])}, profit factor {pf}, "
              f"net without best trade {d.money(r['net_without_best'])}, last 3 days {d.money(r['last3_net'])}",
              f"  worst day {r['worst_day_date']} {d.money(r['worst_day'])}, losing days in a row at the end: {r['losing_streak_days']}"]
        L += [f"  - {w}" for w in r["why"]]
        if r["by_day"]:
            L.append("  per day: " + "  ".join(f"{k[5:]} {d.money(v)}" for k, v in list(r["by_day"].items())[-10:]))
    ov, cb = adv["overlap"], adv["combined"]
    L += ["", "BOTH SERVICES (one Dhan account)"]
    if cb:
        L.append(f"  combined: {cb['n']} trades, net {d.money(cb['net'])}, win {cb['win_pct']}%")
    L.append(f"  same symbol opened by both on the same day: {ov['symbols']}" + (f" (net {d.money(ov['net'])}): " + ", ".join(ov["list"]) if ov["symbols"] else ""))
    if adv["better"]:
        b = adv["better"]
        L.append(f"  better record in this window: {LABEL.get(b, b)} (net {d.money(adv['services'][b]['net'])}). If only one can run, prefer it.")
    else:
        L.append("  no clear winner (one side has too little data, or they are level): no ranking given.")
    offs = [LABEL.get(s, s) for s, r in adv["services"].items() if r["verdict"] == OFF]
    L += ["", "WHAT TO DO"]
    if offs:
        L.append("  " + ", ".join(offs) + ": pause it (its Auto-Pilot / master switch), outside market hours, and re-run this after more days. Open positions still need their exits.")
    else:
        L.append("  nothing to switch off on this record. WATCH lines: look again tomorrow, change nothing today.")
    L += ["", "This describes what already happened, not a forecast. It only recommends: it never changes a switch, an order or a service. Small samples move a lot."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- io
def load_rows(a: argparse.Namespace, notes: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for f in a.from_csv:
        rows += rows_from_csv(f)
    if a.rt_file:
        for f in a.rt_file:
            rows += d.rows_from_rt(json.load(open(f, encoding="utf-8")), a.origin)
    elif not a.no_rt and not a.from_csv:
        try:
            if not a.token:
                notes.append("real-trade: no admin token given (TOKEN / --token), skipped")
            else:
                rows += d.rows_from_rt(d._get(f"{a.rt_url.rstrip('/')}/positions/REAL/records?days={max(1, min(a.days, 60))}&limit=1000&include_imported=true", a.token), a.origin)
        except Exception as e:  # noqa: BLE001
            notes.append(f"real-trade: could not read ({type(e).__name__}: {e})")
    if a.ps_file:
        for f in a.ps_file:
            rows += d.rows_from_ps(json.load(open(f, encoding="utf-8")))
    elif not a.no_ps and not a.from_csv:
        try:
            rows += d.rows_from_ps(d._get(f"{a.ps_url.rstrip('/')}/trades/history?limit=1000"))
        except Exception as e:  # noqa: BLE001
            notes.append(f"position-stocks: could not read ({type(e).__name__}: {e})")
    return d.dedupe(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--rt-url", default=os.environ.get("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--rt-file", action="append", default=[])
    ap.add_argument("--ps-file", action="append", default=[])
    ap.add_argument("--from-csv", action="append", default=[])
    ap.add_argument("--origin", default="system", choices=["all", "system", "imported"],
                    help="real-trade entries to judge (default system: the bot's own; imported demat holdings are not the service's decisions)")
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--pf-off", type=float, default=0.8)
    ap.add_argument("--pf-keep", type=float, default=1.1)
    ap.add_argument("--streak-off", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--no-rt", action="store_true")
    ap.add_argument("--no-ps", action="store_true")
    ap.add_argument("--as-of", default="", help="treat this ISO time as 'now' (tests / re-running an old export)")
    a = ap.parse_args(argv)
    notes: List[str] = []
    rows = load_rows(a, notes)
    now = (d.parse_ts(a.as_of) or datetime.now(IST)).astimezone(IST)
    cutoff = now - timedelta(days=max(1, a.days))
    rows = [r for r in rows if r["exit"] >= cutoff and (a.origin == "all" or r.get("origin", "system") == a.origin)]
    header = [f"NOTE: {n}" for n in notes]
    if not rows:
        text = "SERVICE ON / OFF ADVICE\nNo closed trades found in the window: no recommendation.\n" + "\n".join(header) + "\n"
        print(text)
        if a.out:
            open(a.out, "w", encoding="utf-8").write(text)
        return 1
    th = {"min_n": a.min_n, "min_days": a.min_days, "pf_off": a.pf_off, "pf_keep": a.pf_keep, "streak_off": a.streak_off}
    adv = advise(rows, **th)
    text = build_text(adv, a.days, header, th)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    if a.json:
        slim = {"generated": now.isoformat(), "days": a.days, "thresholds": th, "better": adv["better"], "overlap": adv["overlap"],
                "services": {s: {k: v for k, v in r.items()} for s, r in adv["services"].items()}}
        open(a.json, "w", encoding="utf-8").write(json.dumps(slim, indent=2))
    return 1 if any(r["verdict"] == OFF for r in adv["services"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
