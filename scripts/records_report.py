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
from typing import Any, Dict, Iterable, List, Optional


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


def rs_bucket(v: Any) -> str:
    """group468: Mansfield RS bucket (log-only evidence)."""
    x = num(v)
    return "unknown" if x is None else ("rs<0" if x < 0 else ("rs 0-2" if x < 2 else "rs>=2"))


def breadth_bucket(v: Any) -> str:
    """group468: breadth bucket, % of the candidate engine's universe above its 50-day EMA (log-only evidence)."""
    x = num(v)
    return "unknown" if x is None else ("breadth<35" if x < 35 else ("breadth 35-65" if x <= 65 else "breadth>65"))


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
            if num(r.get("net_pnl")) == 0.0 and num(r.get("return_pct")) is None and num(r.get("gross_pnl")) in (0.0, None):
                continue          # 2026-10-20: unbooked / ghost record (P&L 0, no return %): no real fill, not a trade
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


def performance_block(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Whole-book performance on a daily net-P&L basis (additive rupees, not compounded): cumulative net per exit day (IST),
    max drawdown in rupees with the peak and trough days, days spent underwater, Return/MaxDD, longest run of winning days
    and the best single day. Pure; never raises."""
    by_day: Dict[str, float] = defaultdict(float)
    try:
        for r in records:
            day = r.get("exit_day_ist") or str(r.get("closed_at") or "")[:10]
            v = num(r.get("net_pnl"))
            if day and v is not None:
                by_day[day] += v
    except Exception:  # noqa: BLE001
        pass
    days = sorted(by_day)
    if not days:
        return {"days": 0, "equity": [], "max_drawdown": None, "drawdown_start": None, "drawdown_end": None,
                "days_in_drawdown": None, "return_maxdd": None, "max_win_streak": None, "best_day": None, "total_net": None}
    equity, cum = [], 0.0
    for d in days:
        cum += by_day[d]
        equity.append((d, round(cum, 2)))
    peak, peak_day = 0.0, None          # the book starts at 0 before the first day, so a first-day loss is a drawdown
    mdd, mdd_start, mdd_end, underwater = 0.0, None, None, 0
    for d, c in equity:
        if c > peak:
            peak, peak_day = c, d
        elif c < peak:
            underwater += 1
            if peak - c > mdd + 1e-9:
                mdd, mdd_start, mdd_end = peak - c, peak_day, d
    total = equity[-1][1]
    streak = best_streak = 0
    for d in days:
        if by_day[d] > 0:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    best_day = max(days, key=lambda d: by_day[d])        # ties -> earliest (days is sorted)
    return {
        "days": len(days),
        "equity": [{"day": d, "cum_net": c} for d, c in equity],
        "max_drawdown": round(mdd, 2),
        "drawdown_start": mdd_start if mdd > 0 else None,   # None = the book never fell below a prior high
        "drawdown_end": mdd_end,
        "days_in_drawdown": underwater,
        "return_maxdd": round(total / mdd, 2) if mdd > 0 else None,
        "max_win_streak": best_streak,
        "best_day": {"day": best_day, "net": round(by_day[best_day], 2)},
        "total_net": total,
    }


def exit_slippage_stored(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """group469: exit fill vs the exit trigger stored on each record (exit_slippage_pct: the LTP at the exit decision, or the final stop
    for a broker stop that fired on its own). Overall and per exit reason; None when no record carries one. Pure."""
    by: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        v = num(r.get("exit_slippage_pct"))
        if v is not None:
            by[str(r.get("exit_reason") or "unknown")].append(v)
    if not by:
        return None
    fmt = lambda vs: {"n": len(vs), "mean": round(st.mean(vs), 3), "worst": round(min(vs), 3), "best": round(max(vs), 3)}  # noqa: E731
    return {"overall": fmt([v for vs in by.values() for v in vs]), "by_exit_reason": {k: fmt(v) for k, v in sorted(by.items())}}


def exit_slippage_pct(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Exit fill vs the trigger price. The records carry no per-exit trigger, so only stop_hit exits can be measured, against
    the INITIAL stop entry*(1-stop_pct/100) (the stop may have trailed, so this is a proxy). + = filled above the trigger
    (better for a long exit), - = worse. None when no stop_hit has all the numbers. Pure."""
    vals = []
    for r in records:
        if r.get("exit_reason") != "stop_hit":
            continue
        entry, stop_pct, xp = num(r.get("entry_price")), num(r.get("stop_pct")), num(r.get("exit_price"))
        if entry and entry > 0 and stop_pct and stop_pct > 0 and xp and xp > 0:
            trigger = entry * (1.0 - stop_pct / 100.0)
            if trigger > 0:
                vals.append((xp - trigger) / trigger * 100.0)
    if not vals:
        return None
    return {"n": len(vals), "mean": round(st.mean(vals), 3), "worst": round(min(vals), 3), "best": round(max(vals), 3)}


def orb_trigger_bucket(v: Any) -> str:
    """E113: orb_close_trigger as exported (True / False / None). None = the recorder had no bars: never counted as 'no trigger'."""
    return "no_orb_record" if not isinstance(v, bool) else ("close_trigger" if v else "no_close_trigger")


def orb_volume_bucket(v: Any) -> str:
    """E113: orb_volume_ratio (trigger-bar volume / opening-range average) in buckets; 'none' when not recorded."""
    x = num(v)
    if x is None:
        return "none"
    return "<1.0" if x < 1.0 else "1.0-1.5" if x < 1.5 else "1.5-2.5" if x < 2.5 else ">=2.5"


def orb_verdict_bucket(r: Dict[str, Any]) -> str:
    """group474 field orb_would_enter, grouped here for the first time (E113)."""
    v = r.get("orb_would_enter")
    if not isinstance(v, bool):
        return "no_verdict"
    return "orb_would_enter" if v else "orb_would_skip:" + str(r.get("orb_reasons") or "?")


GROUPS: List[tuple] = [
    ("origin (system entry vs broker-imported)", lambda r: r.get("origin")),
    ("decision_label", lambda r: r.get("decision_label")),
    ("source_tab", lambda r: r.get("source_tab")),
    ("tier_name", lambda r: r.get("tier_name")),
    ("entry hour IST", lambda r: r.get("entry_hour_ist")),
    ("exit_reason", lambda r: r.get("exit_reason")),
    ("hold_bucket", lambda r: r.get("hold_bucket")),
    ("stop_basis", lambda r: r.get("stop_basis")),
    ("market state at entry", lambda r: r.get("market_state") or "none"),
    ("profile version at entry", lambda r: r.get("profile_version") or "none"),
    ("order flow: best-5 imbalance", lambda r: imb_bucket(r.get("imbalance_5"))),
    ("order flow: 20-level imbalance", lambda r: imb_bucket(r.get("imbalance_20"))),
    ("order flow: spread at entry", lambda r: spread_bucket(r.get("flow_spread_pct"))),
    ("log-only: Mansfield RS at entry", lambda r: rs_bucket(r.get("log_mansfield_rs"))),
    ("log-only: market breadth at entry", lambda r: breadth_bucket(r.get("log_breadth_pct"))),
    ("opening range: would an ORB entry have taken it", orb_verdict_bucket),
    ("opening range: close-based trigger", lambda r: orb_trigger_bucket(r.get("orb_close_trigger"))),
    ("opening range: trigger-bar volume vs range average", lambda r: orb_volume_bucket(r.get("orb_volume_ratio"))),
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
        "charges_share_of_gross_profit_pct": (round(100.0 * rep["all"]["charges"] / rep["all"]["gross"], 0) if rep["all"]["gross"] > 0 and len(rows) >= min_n else None),
    }
    rep["performance"] = performance_block(rows)
    rep["exit_slippage_pct"] = exit_slippage_pct(rows)
    rep["exit_slippage"] = exit_slippage_stored(rows)
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
          + (f" ({rt['charges_share_of_gross_profit_pct']:.0f}% of the gross profit)" if rt.get("charges_share_of_gross_profit_pct") is not None else "")
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
    perf = rep.get("performance") or {}
    if perf.get("days"):
        L += ["", "## Whole-book performance (daily net P&L)", "", f"Days traded: {perf['days']}, total net {f(perf['total_net'])}."]
        if perf.get("max_drawdown"):
            L.append(f"Max drawdown {f(perf['max_drawdown'])} from {perf.get('drawdown_start') or 'start'} to {perf.get('drawdown_end') or '-'}; "
                     f"{perf.get('days_in_drawdown')} day(s) underwater; Return/MaxDD {f(perf.get('return_maxdd'))}.")
        else:
            L.append("No drawdown: the book never closed a day below its prior high.")
        txt = f"Longest run of winning days: {perf.get('max_win_streak')}"
        if perf.get("best_day"):
            txt += f"; best day {perf['best_day']['day']} {f(perf['best_day']['net'])}"
        L.append(txt + ".")
    xe = rep.get("exit_slippage")
    if xe:
        o = xe["overall"]
        L.append(f"Exit slippage (fill vs the exit trigger, + = better than the trigger): mean {o['mean']}%, worst {o['worst']}%, best {o['best']}% over {o['n']} exits.")
        for k, g in xe["by_exit_reason"].items():
            L.append(f"- {k}: mean {g['mean']}%, worst {g['worst']}% ({g['n']})")
    xs = rep.get("exit_slippage_pct")
    if xs and not xe:
        L.append(f"Exit slippage on stop hits (fill vs the initial stop; a trailed stop makes this approximate): mean {xs['mean']}%, "
                 f"worst {xs['worst']}%, best {xs['best']}% over {xs['n']} trades.")
    for title, g in rep["groups"].items():
        if title.startswith("log-only") and set(g) == {"unknown"}:
            L += ["", f"## {title}", "", "No trade carries this number yet (recorded only on candidates made after group 468 was deployed)."]
            continue
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


def filter_origin(recs: List[Dict[str, Any]], origin: str) -> List[Dict[str, Any]]:
    """Keep only records of the given origin ('system' or 'imported'); 'all' keeps everything. A record with no origin counts as system (older exports)."""
    if origin in ("", "all", None):
        return recs
    return [r for r in recs if (r.get("origin") or "system") == origin]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="record export files (default rt_*.json in the current folder); shell globs work")
    ap.add_argument("--mode", default="REAL", help="REAL (default), DEMO, or '' for all")
    ap.add_argument("--since", default="", help="YYYY-MM-DD: only trades opened on/after this UTC date")
    ap.add_argument("--until", default="", help="YYYY-MM-DD: only trades opened on/before this UTC date")
    ap.add_argument("--origin", default="all", choices=["all", "system", "imported"], help="system = only entries the bot made itself, imported = only broker-imported positions (their opened_at is the import time, not the buy time); default all")
    ap.add_argument("--min-n", type=int, default=10, help="groups under this many trades are marked (few)")
    ap.add_argument("--outlier-pct", type=float, default=15.0, help="a trade whose |return| is above this %% is an outlier in the robust totals")
    ap.add_argument("--json", default="", help="also write the full report to this JSON file")
    a = ap.parse_args(argv)
    paths = [p for pat in (a.files or ["rt_*.json"]) for p in (sorted(glob.glob(pat)) or [pat])]
    recs = filter_origin(load_records(paths, a.mode or None), a.origin)
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
