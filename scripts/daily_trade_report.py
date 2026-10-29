#!/usr/bin/env python3
"""daily_trade_report.py - group 442: every closed trade of both services, per IST day, with entry time, exit time, hold time and profit / loss.

  python3 scripts/daily_trade_report.py [--days 10] [--rt-url http://127.0.0.1:8005] [--ps-url http://127.0.0.1:8006] [--token T | TOKEN=T]
                                        [--origin all|system|imported] [--min-n 8] [--out daily_trades.txt] [--csv daily_trades.csv]
  python3 scripts/daily_trade_report.py --rt-file rt_2026-10-18.json --ps-file ps_hist.json        # offline, from saved exports

Sources (read-only GETs):
  real-trade-service      GET /positions/REAL/records?days=N&limit=1000&include_imported=true   (admin bearer token; the same record as records_report.py)
  position-stocks-service GET /trades/history?limit=1000                                       (settled trades only: a real exit price and P&L)
Rows: one per closed trade -> day (IST close date), service, symbol, entry HH:MM, exit HH:MM, held, P&L (NET of charges for both services; E231: position-stocks' realized P&L is before charges, so its row's own charges are subtracted),
return %, exit reason. Then totals per day and, over the whole period, by entry hour, exit hour, hold length and exit reason, with the best and worst trades.
A bucket under --min-n trades is marked "(few)": a negative hour with 3 trades is not a finding. Nothing here changes a setting or a trade.
Exit 0 = report written; 1 = no trades found. Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
HOLD_BUCKETS = (("< 15 min", 0, 15), ("15-60 min", 15, 60), ("1-3 h", 60, 180), ("3 h - close", 180, 24 * 60), ("overnight+", 24 * 60, 10 ** 9))


def parse_ts(v: Any) -> Optional[datetime]:
    """ISO string (with or without a zone; no zone = UTC, as the services store it) -> aware datetime."""
    if not v:
        return None
    try:
        s = str(v).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _row(service: str, tid: Any, sym: Any, opened: Any, closed: Any, pnl: Any, ret: Any, reason: Any, qty: Any = None, origin: str = "system") -> Optional[Dict[str, Any]]:
    o, c = parse_ts(opened), parse_ts(closed)
    p = num(pnl)
    if o is None or c is None or p is None:
        return None
    o, c = o.astimezone(IST), c.astimezone(IST)
    return {"service": service, "id": tid, "symbol": str(sym or "?"), "entry": o, "exit": c, "day": c.date().isoformat(),
            "held_min": max(0.0, (c - o).total_seconds() / 60.0), "pnl": p, "ret": num(ret), "reason": str(reason or "-"), "qty": num(qty), "origin": origin}


def is_unbooked(r: Dict[str, Any]) -> bool:
    """2026-10-20: a record with P&L exactly 0 and no return % has no real fill behind it (reconcile / ghost close, e.g. HDFCLIFE closed at
    08:57 before the open). It is not a trade: counting it as a non-losing day reset the losing streak and flipped service_advice."""
    pnl = r.get("net_pnl") if r.get("net_pnl") is not None else r.get("gross_pnl")
    return num(pnl) == 0.0 and num(r.get("return_pct")) is None


def rows_from_rt(doc: Any, origin: str = "all") -> List[Dict[str, Any]]:
    recs = doc.get("records") if isinstance(doc, dict) else doc
    out = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        og = str(r.get("origin") or "system")
        if origin != "all" and og != origin:
            continue
        if is_unbooked(r):
            continue
        pnl = r.get("net_pnl") if r.get("net_pnl") is not None else r.get("gross_pnl")
        x = _row("real-trade", r.get("position_id"), r.get("symbol"), r.get("opened_at"), r.get("closed_at"), pnl, r.get("return_pct"), r.get("exit_reason"),
                 r.get("quantity"), og)
        if x:
            out.append(x)
    return out


_PS_BAD = ("ERROR", "REJECTED", "PENDING_RECONCILE", "OPEN", "PENDING")


def rows_from_ps(doc: Any) -> List[Dict[str, Any]]:
    trades = doc.get("trades") if isinstance(doc, dict) else doc
    out = []
    for r in trades or []:
        if not isinstance(r, dict):
            continue
        st = str(r.get("status") or "").upper()
        if st in _PS_BAD or r.get("exit_price") is None or r.get("realized_pnl") is None:
            continue
        if str(r.get("window_source") or "").upper() == "PROBE":
            continue   # E257: the 1-share Dhan SELL probe is a wiring test, not a trade; counted, it adds a fake row to wins, losses and the on/off sample
        ep, xp = num(r.get("entry_price")), num(r.get("exit_price"))
        ret = ((xp - ep) / ep * 100.0) if ep and xp is not None else None
        if ret is not None and str(r.get("side") or "").upper() in ("SHORT", "SELL"):
            ret = -ret   # E257: a short earns when the price falls; the return column was computed as if every row were a long
        # E231: position-stocks' realized_pnl is GROSS (before charges) while real-trade's net_pnl is net; the two were summed as if alike. Subtract the row's
        # own round-trip charges (null on an unbookable row -> left as is). Win / loss is counted on this net figure.
        ch = num(r.get("charges"))
        pnl = num(r.get("realized_pnl"))
        if pnl is not None and ch is not None:
            pnl = round(pnl - ch, 2)
        reason = r.get("exit_reason") or st.lower()
        rule = str(r.get("exit_rule") or "").strip().lower()
        if st == "STAGNATION_EXIT" and rule:
            reason = f"{reason}:{rule}"   # E231: stagnation_exit:flat_band | stagnation_exit:no_followthrough (older rows carry no rule)
        x = _row("position-stocks", r.get("id"), r.get("symbol"), r.get("opened_at"), r.get("closed_at"), pnl, ret,
                 reason, r.get("quantity"))
        if x:
            out.append(x)
    return out


def dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for r in rows:
        k = (r["service"], r["id"], r["symbol"], r["entry"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ---------------------------------------------------------------- stats
def stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    pn = [r["pnl"] for r in rows]
    wins = sum(1 for x in pn if x > 0)
    losses = sum(1 for x in pn if x < 0)
    rets = [r["ret"] for r in rows if r["ret"] is not None]
    return {"n": n, "wins": wins, "losses": losses, "win_pct": round(100.0 * wins / n, 1) if n else None, "net": round(sum(pn), 2),
            "avg": round(sum(pn) / n, 2) if n else None, "avg_ret": round(sum(rets) / len(rets), 3) if rets else None}


def group(rows: List[Dict[str, Any]], keyf) -> Dict[Any, Dict[str, Any]]:
    d: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        d[keyf(r)].append(r)
    return {k: stats(v) for k, v in d.items()}


def hold_bucket(minutes: float) -> str:
    for name, lo, hi in HOLD_BUCKETS:
        if lo <= minutes < hi:
            return name
    return HOLD_BUCKETS[-1][0]


def fmt_hold(m: float) -> str:
    if m < 90:
        return f"{m:.0f}m"
    if m < 24 * 60:
        return f"{m / 60:.1f}h"
    return f"{m / 1440:.1f}d"


def money(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:+,.0f}"


def pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _table(title: str, g: Dict[Any, Dict[str, Any]], order, min_n: int, label: str) -> List[str]:
    L = [title, f"{label:<14}{'trades':>7}{'win %':>8}{'net P&L':>12}{'avg P&L':>10}{'avg ret %':>11}", "-" * 62]
    for k in order:
        s = g[k]
        few = " (few)" if s["n"] < min_n else ""
        L.append(f"{str(k):<14}{s['n']:>7}{(s['win_pct'] if s['win_pct'] is not None else '-')!s:>8}{money(s['net']):>12}{money(s['avg']):>10}{pct(s['avg_ret']):>11}{few}")
    return L + [""]


def build_report(rows: List[Dict[str, Any]], days: int, min_n: int = 8, header: Optional[List[str]] = None) -> str:
    rows = sorted(rows, key=lambda r: (r["exit"], r["entry"]))
    L: List[str] = ["DAILY TRADE REPORT - entry / exit time and profit / loss (IST)", f"generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}   window: last {days} day(s)"]
    L += header or []
    tot = stats(rows)
    L += [f"TOTAL: {tot['n']} trades, {tot['wins']} wins / {tot['losses']} losses, win {tot['win_pct']}%, net {money(tot['net'])}, avg {money(tot['avg'])}", ""]
    by_svc = group(rows, lambda r: r["service"])
    for svc, s in sorted(by_svc.items()):
        L.append(f"  {svc:<16} {s['n']:>4} trades  win {s['win_pct']}%  net {money(s['net'])}")
    L.append("")
    days_sorted = sorted({r["day"] for r in rows}, reverse=True)
    L.append("=" * 100)
    L.append("PER DAY (newest first)")
    L.append("=" * 100)
    for d in days_sorted:
        dr = [r for r in rows if r["day"] == d]
        s = stats(dr)
        L += ["", f"{d}   {s['n']} trades   {s['wins']}W / {s['losses']}L   net {money(s['net'])}"]
        L.append(f"  {'service':<16}{'symbol':<14}{'entry':>6}{'exit':>7}{'held':>7}{'P&L':>10}{'ret %':>8}  reason")
        for r in dr:
            e_day = "" if r["entry"].date() == r["exit"].date() else f" ({r['entry'].strftime('%d %b')})"
            L.append(f"  {r['service']:<16}{r['symbol']:<14}{r['entry'].strftime('%H:%M'):>6}{r['exit'].strftime('%H:%M'):>7}{fmt_hold(r['held_min']):>7}"
                     f"{money(r['pnl']):>10}{pct(r['ret']):>8}  {r['reason']}{e_day}")
    L += ["", "=" * 100, "OVER THE WHOLE WINDOW", "=" * 100, ""]
    g = group(rows, lambda r: r["entry"].hour)
    L += _table("By ENTRY hour (IST)", {f"{h:02d}:00": v for h, v in g.items()}, [f"{h:02d}:00" for h in sorted(g)], min_n, "hour")
    g = group(rows, lambda r: r["exit"].hour)
    L += _table("By EXIT hour (IST)", {f"{h:02d}:00": v for h, v in g.items()}, [f"{h:02d}:00" for h in sorted(g)], min_n, "hour")
    g = group(rows, lambda r: hold_bucket(r["held_min"]))
    L += _table("By HOLD length", g, [b[0] for b in HOLD_BUCKETS if b[0] in g], min_n, "held")
    g = group(rows, lambda r: r["reason"])
    L += _table("By EXIT reason", g, sorted(g, key=lambda k: g[k]["net"]), min_n, "reason")
    if rows:
        best, worst = max(rows, key=lambda r: r["pnl"]), min(rows, key=lambda r: r["pnl"])
        L += [f"Best : {best['symbol']} {best['entry'].strftime('%d %b %H:%M')} -> {best['exit'].strftime('%H:%M')}  {money(best['pnl'])}  ({best['reason']})",
              f"Worst: {worst['symbol']} {worst['entry'].strftime('%d %b %H:%M')} -> {worst['exit'].strftime('%H:%M')}  {money(worst['pnl'])}  ({worst['reason']})",
              f"Net without the best trade: {money(tot['net'] - best['pnl'])}", ""]
    # plain-words flags, only where there are enough trades
    flags: List[str] = []
    he = group(rows, lambda r: r["entry"].hour)
    for h, s in sorted(he.items()):
        if s["n"] >= min_n and s["net"] < 0 and (s["avg_ret"] or 0) < 0:
            flags.append(f"entries in the {h:02d}:00 hour lost {money(s['net'])} over {s['n']} trades (win {s['win_pct']}%): look at that hour before changing anything else")
    for reason, s in group(rows, lambda r: r["reason"]).items():
        if s["n"] >= min_n and s["net"] < 0 and reason in ("stop_hit", "STOP_HIT", "STOP_BREACH_EXIT"):
            flags.append(f"stop exits cost {money(s['net'])} over {s['n']} trades")
    L += ["FLAGS (only buckets with >= %d trades):" % min_n] + (["  - " + f for f in flags] if flags else ["  none"]) + [""]
    L.append("This is a description of what already happened, not a forecast. Small samples move a lot: read '(few)' rows as noise.")
    return "\n".join(L) + "\n"


def to_csv(rows: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["day", "service", "symbol", "entry_ist", "exit_ist", "held_min", "pnl", "return_pct", "exit_reason", "quantity", "origin"])
    for r in sorted(rows, key=lambda r: (r["exit"], r["entry"])):
        w.writerow([r["day"], r["service"], r["symbol"], r["entry"].strftime("%Y-%m-%d %H:%M"), r["exit"].strftime("%Y-%m-%d %H:%M"), round(r["held_min"], 1), r["pnl"],
                    "" if r["ret"] is None else round(r["ret"], 3), r["reason"], r["qty"] if r["qty"] is not None else "", r["origin"]])
    return buf.getvalue()


# ---------------------------------------------------------------- io
def _get(url: str, token: str = "", timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied base URL
        return json.loads(r.read().decode("utf-8", "replace"))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--rt-url", default=os.environ.get("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--rt-file", action="append", default=[])
    ap.add_argument("--ps-file", action="append", default=[])
    ap.add_argument("--origin", default="all", choices=["all", "system", "imported"])
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--out", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--no-rt", action="store_true")
    ap.add_argument("--no-ps", action="store_true")
    ap.add_argument("--as-of", default="", help="treat this ISO time as 'now' (tests / re-running an old export)")
    a = ap.parse_args(argv)
    rows: List[Dict[str, Any]] = []
    notes: List[str] = []
    if a.rt_file:
        for f in a.rt_file:
            rows += rows_from_rt(json.load(open(f, encoding="utf-8")), a.origin)
    elif not a.no_rt:
        try:
            if not a.token:
                notes.append("real-trade: no admin token given (TOKEN / --token), skipped")
            else:
                rows += rows_from_rt(_get(f"{a.rt_url.rstrip('/')}/positions/REAL/records?days={max(1, min(a.days, 60))}&limit=1000&include_imported=true", a.token), a.origin)
        except Exception as e:  # noqa: BLE001
            notes.append(f"real-trade: could not read ({type(e).__name__}: {e})")
    if a.ps_file:
        for f in a.ps_file:
            rows += rows_from_ps(json.load(open(f, encoding="utf-8")))
    elif not a.no_ps:
        try:
            rows += rows_from_ps(_get(f"{a.ps_url.rstrip('/')}/trades/history?limit=1000"))
        except Exception as e:  # noqa: BLE001
            notes.append(f"position-stocks: could not read ({type(e).__name__}: {e})")
    rows = dedupe(rows)
    now = (parse_ts(a.as_of) or datetime.now(IST)).astimezone(IST)
    cutoff = now - timedelta(days=max(1, a.days))
    rows = [r for r in rows if r["exit"] >= cutoff]
    header = [f"NOTE: {n}" for n in notes]
    if not rows:
        text = "DAILY TRADE REPORT\nNo closed trades found in the window.\n" + "\n".join(header) + "\n"
        print(text)
        if a.out:
            open(a.out, "w", encoding="utf-8").write(text)
        return 1
    text = build_report(rows, a.days, a.min_n, header)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    if a.csv:
        open(a.csv, "w", encoding="utf-8").write(to_csv(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
