#!/usr/bin/env python3
"""scripts/records_report.py - one-command report on closed trades (group 430). Replaces the throwaway ~/rt_split.py.

Reads the exports of real-trade-service GET /positions/{mode}/records (JSON files holding {"records": [...]}), de-duplicates by position_id, and prints how the
trades did by label, source, tier, entry hour, exit reason, hold length and - once group 427 has been recording - by order-flow bucket (best-5 imbalance,
20-level imbalance, spread). Pure standard library, read-only, changes nothing.

  # on the VM, in the folder holding the exports (python3 scripts/export_records.py makes a fresh one):
  python3 scripts/records_report.py rt_*.json                           # everything, REAL mode
  python3 scripts/records_report.py rt_*.json --since 2026-10-01        # only trades opened on/after that day (UTC date of opened_at)
  python3 scripts/records_report.py rt_*.json --mode DEMO --json out.json

What it adds over the old script: per-group MEDIAN return next to the mean (one big winner or a -40 % gap cannot hide behind a mean), the totals with the best
trade and with every |return| over --outlier-pct removed, a stop-overshoot line (how far past its stop the stopped trades really lost), the order-flow buckets,
and a "(few)" mark on any group under --min-n trades - do not act on those. A rule is worth changing only when a bucket is clearly worse on ~100 trades.
Win % is the share of trades with return_pct above zero.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional


def num(x: Any) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def imb_bucket(v: Any) -> str:
    x = num(v)
    if x is None:
        return "unknown"
    return "ask_heavy" if x < -0.2 else ("bid_heavy" if x > 0.2 else "balanced")


def spread_bucket(v: Any) -> str:
    x = num(v)
    if x is None:
        return "unknown"
    return "tight" if x < 0.15 else ("normal" if x < 0.30 else "wide")


def load_records(paths: Iterable[str], mode: Optional[str]) -> List[Dict[str, Any]]:
    """All records of the given files, one per position_id (a later file wins), only `mode` when given. Unreadable files are skipped with a note on stderr."""
    out: Dict[Any, Dict[str, Any]] = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"skip {p}: {e}", file=sys.stderr)
            continue
        rows = doc.get("records") if isinstance(doc, dict) else doc
        for r in rows or []:
            if not isinstance(r, dict) or r.get("position_id") is None:
                continue
            if mode and str(r.get("mode") or "").upper() != mode.upper():
                continue
            out[r["position_id"]] = r
    return list(out.values())


def _day(r: Dict[str, Any]) -> str:
    return str(r.get("opened_at") or "")[:10]


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rets = [v for v in (num(x.get("return_pct")) for x in rows) if v is not None]
    return {
        "n": len(rows),
        "mean_pct": round(st.mean(rets), 2) if rets else None,
        "median_pct": round(st.median(rets), 2) if rets else None,
        "win_pct": round(100.0 * sum(1 for v in rets if v > 0) / len(rets), 0) if rets else None,
        "gross": round(sum(num(x.get("gross_pnl")) or 0.0 for x in rows)),
        "net": round(sum(num(x.get("net_pnl")) or 0.0 for x in rows)),
        "charges": round(sum(num(x.get("charges")) or 0.0 for x in rows)),
    }


GROUPS: List[tuple] = [
    ("origin (system entry vs broker-imported)", lambda r: r.get("origin")),
    ("decision_label", lambda r: r.get("decision_label")),
    ("source_tab", lambda r: r.get("source_tab")),
    ("tier_name", lambda r: r.get("tier_name")),
    ("entry hour IST", lambda r: r.get("entry_hour_ist")),
    ("exit_reason", lambda r: r.get("exit_reason")),
    ("hold_bucket", lambda r: r.get("hold_bucket")),
    ("stop_basis", lambda r: r.get("stop_basis")),
    ("order flow: best-5 imbalance", lambda r: imb_bucket(r.get("imbalance_5"))),
    ("order flow: 20-level imbalance", lambda r: imb_bucket(r.get("imbalance_20"))),
    ("order flow: spread at entry", lambda r: spread_bucket(r.get("flow_spread_pct"))),
]


def build_report(records: List[Dict[str, Any]], since: str = "", until: str = "", min_n: int = 10, outlier_pct: float = 15.0) -> Dict[str, Any]:
    rows = [r for r in records if (not since or _day(r) >= since) and (not until or _day(r) <= until)]
    rep: Dict[str, Any] = {"since": since or None, "until": until or None, "min_n": min_n, "outlier_pct": outlier_pct, "trades": len(rows), "groups": {}}
    if not rows:
        return rep
    rep["first_opened"] = min(_day(r) for r in rows if _day(r)) if any(_day(r) for r in rows) else None
    rep["last_closed"] = max((str(r.get("closed_at") or "")[:10] for r in rows), default=None) or None
    rep["all"] = _stats(rows)
    for title, key in GROUPS:
        g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            g[str(key(r))].append(r)
        rep["groups"][title] = {k: _stats(v) for k, v in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0]))}
    by_ret = sorted(rows, key=lambda r: num(r.get("return_pct")) if num(r.get("return_pct")) is not None else 0.0)
    best = by_ret[-1]
    big = [r for r in rows if abs(num(r.get("return_pct")) or 0.0) > outlier_pct]
    keep_best = [r for r in rows if r is not best]
    keep_big = [r for r in rows if r not in big]
    rep["robust_totals"] = {
        "best_trade": {"position_id": best.get("position_id"), "symbol": best.get("symbol"), "return_pct": num(best.get("return_pct")), "net_pnl": num(best.get("net_pnl"))},
        "net_without_best_trade": round(sum(num(r.get("net_pnl")) or 0.0 for r in keep_best)),
        "outliers": [{"position_id": r.get("position_id"), "symbol": r.get("symbol"), "return_pct": num(r.get("return_pct")), "exit_reason": r.get("exit_reason")} for r in big],
        "net_without_outliers": round(sum(num(r.get("net_pnl")) or 0.0 for r in keep_big)),
        "trades_without_outliers": len(keep_big),
        "charges_share_of_gross_loss_pct": (round(100.0 * rep["all"]["charges"] / -rep["all"]["gross"], 0) if rep["all"]["gross"] < 0 and len(rows) >= min_n else None),
    }
    sl = [v for v in (num(r.get("entry_slippage_pct")) for r in rows) if v is not None]
    rep["entry_slippage_pct"] = {"n": len(sl), "mean": round(st.mean(sl), 3), "max": round(max(sl), 3)} if sl else None
    stops = [r for r in rows if r.get("exit_reason") == "stop_hit"]
    over = [(-num(r["return_pct"]) - num(r["stop_pct"])) for r in stops if num(r.get("return_pct")) is not None and num(r.get("stop_pct")) is not None]
    mg = [v for v in (num(r.get("max_gain_pct")) for r in stops) if v is not None]
    beyond = [o for o in over if o > 0.05]                                  # lost MORE than the planned initial stop (a gap / slow fill)
    rep["stop_hits"] = {"n": len(stops), "with_stop_pct": len(over), "n_beyond": len(beyond), "n_inside": len(over) - len(beyond),
                        "mean_overshoot_pct": round(st.mean(beyond), 2) if beyond else None, "max_overshoot_pct": round(max(beyond), 2) if beyond else None,
                        "with_max_gain": len(mg), "mean_max_gain_pct_before_stop": round(st.mean(mg), 2) if mg else None}
    rep["late_eod_squareoff"] = [{"position_id": r.get("position_id"), "symbol": r.get("symbol"), "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at"),
                                  "held_days": num(r.get("held_days")), "return_pct": num(r.get("return_pct"))}
                                 for r in sorted(rows, key=lambda r: str(r.get("opened_at"))) if r.get("exit_reason") == "eod_squareoff" and (num(r.get("held_days")) or 0) >= 1]
    rep["gap_or_ghost"] = [{"position_id": r.get("position_id"), "symbol": r.get("symbol"), "exit_reason": r.get("exit_reason"), "opened_at": r.get("opened_at"),
                            "closed_at": r.get("closed_at"), "return_pct": num(r.get("return_pct"))}
                           for r in rows if r.get("exit_reason") in ("ghost_closed", "emergency_gap_down")]
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v, nd=2: "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))  # noqa: E731
    span = f"{rep.get('first_opened') or '-'} .. {rep.get('last_closed') or '-'}"
    L = [f"# Closed-trade report - {rep['trades']} trades, opened {span}" + (f", since {rep['since']}" if rep.get("since") else "") + (f", until {rep['until']}" if rep.get("until") else ""), ""]
    if not rep["trades"]:
        return "\n".join(L + ["No trades in range."])
    a, rt = rep["all"], rep["robust_totals"]
    L += [f"Totals: gross {a['gross']}, net {a['net']}, charges {a['charges']}" + (f" ({rt['charges_share_of_gross_loss_pct']:.0f}% of the gross loss)" if rt["charges_share_of_gross_loss_pct"] is not None else "")
          + f"; mean {f(a['mean_pct'])}%, median {f(a['median_pct'])}%, win {f(a['win_pct'], 0)}%.",
          f"Best trade: {rt['best_trade']['symbol']} (#{rt['best_trade']['position_id']}) {f(rt['best_trade']['return_pct'])}% / net {f(rt['best_trade']['net_pnl'], 0)}. Net without it: {rt['net_without_best_trade']}.",
          f"Net without the {len(rt['outliers'])} trade(s) beyond +/-{rep['outlier_pct']:g}%: {rt['net_without_outliers']} ({rt['trades_without_outliers']} trades)"
          + (": " + ", ".join(f"{o['symbol']} {f(o['return_pct'])}% ({o['exit_reason']})" for o in rt["outliers"]) if rt["outliers"] else "") + ".", ""]
    sl = rep["entry_slippage_pct"]
    if sl:
        L.append(f"Entry slippage: mean {sl['mean']}%, max {sl['max']}% over {sl['n']} trades.")
    sh = rep["stop_hits"]
    if sh["n"]:
        part = f"Stop hits: {sh['n']}"
        if sh["with_stop_pct"]:
            part += (f"; {sh['n_beyond']} lost MORE than the planned initial stop (on average {f(sh['mean_overshoot_pct'])} points more, worst {f(sh['max_overshoot_pct'])}); " if sh["n_beyond"] else "; none lost more than the planned initial stop; ")
            part += f"{sh['n_inside']} closed inside it (a trailed / ratcheted stop)"
        else:
            part += "; no stop distance recorded on them"
        part += (f"; mean best gain before the stop {f(sh['mean_max_gain_pct_before_stop'])}% ({sh['with_max_gain']} trades)." if sh["with_max_gain"] else "; no max-gain recorded on them.")
        L.append(part)
    for title, g in rep["groups"].items():
        if title.startswith("order flow") and set(g) == {"unknown"}:
            L += ["", f"## {title}", "", "No trade carries these numbers yet (the group 427 record only exists on entries made after it was deployed)."]
            continue
        L += ["", f"## {title}", "", "| group | n | mean % | median % | win % | gross | net | charges |", "|---|---|---|---|---|---|---|---|"]
        for k, s in g.items():
            L.append(f"| {k[:32]}{' (few)' if s['n'] < rep['min_n'] else ''} | {s['n']} | {f(s['mean_pct'])} | {f(s['median_pct'])} | {f(s['win_pct'], 0)} | {s['gross']} | {s['net']} | {s['charges']} |")
    if rep["late_eod_squareoff"]:
        L += ["", "## Held a day or more, then closed by eod_squareoff (should close the same day unless overnight-eligible)", "", "| id | symbol | opened | closed | held days | return % |", "|---|---|---|---|---|---|"]
        for r in rep["late_eod_squareoff"]:
            L.append(f"| {r['position_id']} | {r['symbol']} | {str(r['opened_at'])[:16]} | {str(r['closed_at'])[:16]} | {f(r['held_days'], 0)} | {f(r['return_pct'])} |")
    if rep["gap_or_ghost"]:
        L += ["", "## ghost_closed / emergency_gap_down", "", "| id | symbol | reason | opened | closed | return % |", "|---|---|---|---|---|---|"]
        for r in rep["gap_or_ghost"]:
            L.append(f"| {r['position_id']} | {r['symbol']} | {r['exit_reason']} | {str(r['opened_at'])[:16]} | {str(r['closed_at'])[:16]} | {f(r['return_pct'])} |")
    L += ["", f"Groups marked (few) have fewer than {rep['min_n']} trades: noise, do not act on them. A rule is worth changing only when a bucket is clearly worse on about 100 trades."]
    return "\n".join(L)


def stale_note(last_closed: Optional[str], today: str, max_age_days: int = 5) -> Optional[str]:
    """A warning when the newest trade in the files closed more than max_age_days before `today` (YYYY-MM-DD): the export is probably old. Pure."""
    try:
        from datetime import date
        age = (date.fromisoformat(today) - date.fromisoformat(str(last_closed)[:10])).days
    except (TypeError, ValueError):
        return None
    if age > max_age_days:
        return (f"NOTE: the newest trade in these files closed on {str(last_closed)[:10]} ({age} days ago). If that is not what you expect, the export is stale: "
                "run python3 scripts/export_records.py and report on the new file.")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="record export files (default rt_*.json in the current folder); shell globs work")
    ap.add_argument("--mode", default="REAL", help="REAL (default), DEMO, or '' for all")
    ap.add_argument("--since", default="", help="YYYY-MM-DD: only trades opened on/after this UTC date")
    ap.add_argument("--until", default="", help="YYYY-MM-DD: only trades opened on/before this UTC date")
    ap.add_argument("--min-n", type=int, default=10, help="groups under this many trades are marked (few)")
    ap.add_argument("--outlier-pct", type=float, default=15.0, help="a trade whose |return| is above this %% is an outlier in the robust totals")
    ap.add_argument("--json", default="", help="also write the full report to this JSON file")
    a = ap.parse_args(argv)
    paths = [p for pat in (a.files or ["rt_*.json"]) for p in (sorted(glob.glob(pat)) or [pat])]
    recs = load_records(paths, a.mode or None)
    if not recs:
        print(f"no records found in {len(paths)} file(s) (mode {a.mode or 'any'}); export them first - see docs/GROUP430_RECORDS_REPORT.md", file=sys.stderr)
        return 1
    rep = build_report(recs, a.since, a.until, a.min_n, a.outlier_pct)
    print(to_markdown(rep))
    from datetime import date
    note = stale_note(max((str(r.get("closed_at") or "")[:10] for r in recs), default=None), date.today().isoformat())
    if note:
        print("\n" + note)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
