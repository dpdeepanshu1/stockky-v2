#!/usr/bin/env python3
"""scripts/evidence_report.py - ONE verdict per group of closed trades: is there proof of an edge after costs? (E575, round 550)

Reads the same exports as records_report.py (real-trade-service GET /positions/{mode}/records, saved by scripts/export_records.py or the runner job
`export_records`) and answers, for the whole book and for each group (decision_label, source_tab, tier_name, profile_version, or any key you pass with
--group-by): do the closed trades show a positive average net return that is unlikely to be luck, after costs?

Per group (all net of charges): trades, win %, net rupees, charges, profit factor, mean / median net return per trade (net P&L / money put in),
a 90 % lower bound of that mean (same method as position-stocks setup_stats: standard error across session days when there are 5+ days),
the net result if charges were TWICE as high, and the Deflated Sharpe probability (Bailey / Lopez de Prado): the chance that the true Sharpe is above
zero once you account for how many variants were tried (--trials, or --trials-file agent/trials.json = 1 + the ideas the AI agent has made).

Verdict per group (never a trading instruction):
  NOT_ENOUGH_DATA  fewer than --min-n (30) trades with a readable return
  POSITIVE         lower bound > 0 AND deflated-Sharpe probability >= 0.95 AND net > 0
  NEGATIVE         the 90 % UPPER bound of the mean is below 0 (clearly losing)
  NOT_PROVEN       everything else (could be skill, could be luck)

Pure standard library, read-only, changes nothing, never writes except the --json file you name.

  python3 scripts/evidence_report.py rt_latest.json                       # REAL, system entries, whole file
  python3 scripts/evidence_report.py rt_*.json --since 2026-10-01 --trials 12 --json evidence_report.json
  python3 scripts/evidence_report.py rt_latest.json --trials-file /data/runner/agent/trials.json

Exit 0 report written, 1 no usable trades (the runner shows this as "needs a look", not a failure).
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import records_report as rr  # noqa: E402  (same folder: load_records, num, performance_block)

Z90 = 1.645                      # same constant as services/position-stocks-service/orders/setup_stats.py
MIN_N = 30                       # same minimum as setup_stats.MIN_TRADES
DSR_OK = 0.95                    # a group is POSITIVE only when the deflated-Sharpe probability reaches this
EULER_GAMMA = 0.5772156649015329

DEFAULT_GROUPS: List[Tuple[str, str]] = [
    ("decision_label", "decision_label"),
    ("source_tab", "source_tab"),
    ("tier_name", "tier_name"),
    ("profile_version", "profile_version"),
    ("strategy", "source_tab+decision_label"),                                 # round 551: a strategy = where the idea came from + the call it made (derived, no stamp needed)
    ("strategy_by_profile", "source_tab+decision_label+profile_version"),      # the same, split by the Market Weather profile in force (NOT a code version: see the changelog)
]


# ───────────────────────────── small pure helpers ─────────────────────────────

def net_return_pct(r: Dict[str, Any]) -> Optional[float]:
    """Net P&L as % of the money put in (entry price x quantity). None when it cannot be read. Never raises."""
    try:
        pnl = rr.num(r.get("net_pnl"))
        e, q = rr.num(r.get("entry_price")), rr.num(r.get("quantity"))
        if pnl is None or e is None or q is None or e <= 0 or q <= 0:
            return None
        return pnl / (e * q) * 100.0
    except Exception:  # noqa: BLE001
        return None


def lower_upper_bound(vals: List[float], days: List[str]) -> Tuple[Optional[float], Optional[float]]:
    """90 % lower and upper bound of the mean. With 5+ session days the standard error is taken across day means (trades of one day share the market)."""
    n = len(vals)
    if n < 2:
        return None, None
    mean = sum(vals) / n
    by_day: Dict[str, List[float]] = defaultdict(list)
    for v, d in zip(vals, days):
        by_day[d or "?"].append(v)
    if len(by_day) >= 5:
        dm = [sum(x) / len(x) for x in by_day.values()]
        k = len(dm)
        mu = sum(dm) / k
        se = math.sqrt(sum((x - mu) ** 2 for x in dm) / (k - 1) / k)
    else:
        se = math.sqrt(sum((x - mean) ** 2 for x in vals) / (n - 1) / n)
    return mean - Z90 * se, mean + Z90 * se


def sharpe_moments(vals: List[float]) -> Optional[Dict[str, float]]:
    """Per-trade Sharpe (mean / sample sd), skewness and (raw) kurtosis. None when fewer than 3 trades or no spread."""
    n = len(vals)
    if n < 3:
        return None
    mean = sum(vals) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in vals) / (n - 1))
    sd_pop = math.sqrt(sum((x - mean) ** 2 for x in vals) / n)
    if sd <= 1e-12 or sd_pop <= 1e-12:
        return None
    skew = sum(((x - mean) / sd_pop) ** 3 for x in vals) / n
    kurt = sum(((x - mean) / sd_pop) ** 4 for x in vals) / n
    return {"sr": mean / sd, "skew": skew, "kurt": kurt, "n": float(n)}


def deflated_sharpe_prob(vals: List[float], n_trials: int = 1) -> Optional[float]:
    """Probability that the true Sharpe is above zero after `n_trials` variants were tried on the same data (Bailey and Lopez de Prado, 2014).
    n_trials = 1 is the plain Probabilistic Sharpe Ratio against 0. The spread of the trial Sharpes is approximated by the estimator's own variance
    (documented simplification: with the real trial Sharpes you would use their variance). Never raises; None when it cannot be computed."""
    try:
        m = sharpe_moments(vals)
        if not m:
            return None
        n, sr, skew, kurt = m["n"], m["sr"], m["skew"], m["kurt"]
        denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
        if denom_sq <= 1e-12:
            return None
        nd = NormalDist()
        big_n = max(1, int(n_trials))
        sr0 = 0.0
        if big_n > 1:
            var_sr = denom_sq / (n - 1.0)
            sr0 = math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * nd.inv_cdf(1.0 - 1.0 / big_n) + EULER_GAMMA * nd.inv_cdf(1.0 - 1.0 / (big_n * math.e)))
        z = (sr - sr0) * math.sqrt(n - 1.0) / math.sqrt(denom_sq)
        return nd.cdf(z)
    except Exception:  # noqa: BLE001
        return None


def verdict(n_ret: int, lower: Optional[float], upper: Optional[float], dsr: Optional[float], net: float, min_n: int = MIN_N) -> str:
    if n_ret < min_n or lower is None or upper is None:
        return "NOT_ENOUGH_DATA"
    if upper < 0:
        return "NEGATIVE"
    if lower > 0 and net > 0 and dsr is not None and dsr >= DSR_OK:
        return "POSITIVE"
    return "NOT_PROVEN"


_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2})?")


def stamp(v: Any) -> Optional[str]:
    """A closed_at value as a sortable text 'YYYY-MM-DDTHH:MM:SS' (or just the date), or None when it is not ISO-like. Zones are ignored: one export, one clock. Never raises."""
    try:
        m = _STAMP.match(str(v or "").strip())
        return m.group(0).replace(" ", "T") if m else None
    except Exception:  # noqa: BLE001
        return None


def latest_closed(rows: List[Dict[str, Any]]) -> Optional[str]:
    """The newest readable closed_at among the rows. The registry compares it between days: a rolling export window drops old trades, so a COUNT cannot tell new trades from a shifted window."""
    best = None
    for r in rows:
        t = stamp(r.get("closed_at"))
        if t and (best is None or t > best):
            best = t
    return best


def group_stats(rows: List[Dict[str, Any]], n_trials: int = 1, min_n: int = MIN_N) -> Dict[str, Any]:
    """Evidence numbers for one group of records. Pure; never raises."""
    pnl = [rr.num(r.get("net_pnl")) for r in rows]
    nets = [v for v in pnl if v is not None]
    gross = sum(rr.num(r.get("gross_pnl")) or 0.0 for r in rows)
    charges = sum(rr.num(r.get("charges")) or 0.0 for r in rows)
    wins = sum(v for v in nets if v > 0)
    losses = -sum(v for v in nets if v < 0)
    rets: List[float] = []
    days: List[str] = []
    usable_rows: List[Dict[str, Any]] = []
    for r in rows:
        v = net_return_pct(r)
        if v is not None:
            usable_rows.append(r)
            rets.append(v)
            days.append(str(r.get("opened_at") or "")[:10])
    lower, upper = lower_upper_bound(rets, days)
    dsr = deflated_sharpe_prob(rets, n_trials)
    net_total = sum(nets)
    mean = sum(rets) / len(rets) if rets else None
    srt = sorted(rets)
    median = None if not srt else (srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2.0)
    return {
        "n": len(rows), "n_ret": len(rets), "last_closed": latest_closed(usable_rows),
        "win_pct": round(100.0 * sum(1 for v in nets if v > 0) / len(nets), 1) if nets else None,
        "net": round(net_total, 2), "gross": round(gross, 2), "charges": round(charges, 2),
        "charges_pct_of_gross": round(100.0 * charges / abs(gross), 1) if abs(gross) > 1e-9 else None,
        "profit_factor": round(wins / losses, 2) if losses > 1e-9 else (None if wins <= 0 else 999.0),
        "mean_net_ret_pct": None if mean is None else round(mean, 3),
        "median_net_ret_pct": None if median is None else round(median, 3),
        "lower90_pct": None if lower is None else round(lower, 3),
        "upper90_pct": None if upper is None else round(upper, 3),
        "net_if_charges_x2": round(net_total - charges, 2),
        "survives_2x_charges": (net_total - charges) > 0,
        "dsr_prob": None if dsr is None else round(dsr, 3),
        "trials_used": max(1, int(n_trials)),
        "verdict": verdict(len(rets), lower, upper, dsr, net_total, min_n),
    }


# ───────────────────────────── the report ─────────────────────────────

def _key_fn(field: str) -> Callable[[Dict[str, Any]], str]:
    """One record field, or several joined with "+" (a composite key such as source_tab+decision_label reads "swing | BUY NOW"); a missing part reads "none"."""
    parts = [f.strip() for f in field.split("+") if f.strip()] or [field]

    def one(r: Dict[str, Any], f: str) -> str:
        v = r.get(f)
        return str(v if v not in (None, "") else "none")
    return lambda r: " | ".join(one(r, f) for f in parts)


def trials_from_file(path: str) -> int:
    """1 (the baseline) + the ideas the AI agent has logged in agent/trials.json. 1 when the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return 1 + max(0, int(doc.get("ideas") or 0))
    except Exception:  # noqa: BLE001
        return 1


def build_report(records: List[Dict[str, Any]], n_trials: int = 1, min_n: int = MIN_N,
                 groups: Optional[List[Tuple[str, str]]] = None, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole report as a dict (JSON-safe). Pure; never raises on odd rows."""
    if not groups:
        groups = list(DEFAULT_GROUPS)
        # round 593: once real-trade records carry the round 557 `strategy_version` stamp, judge each strategy per settings version too.
        # Added only when at least one row has a stamp, so an unstamped book (every row reads `none`) gets no extra noise group.
        if any(str((r or {}).get("strategy_version") or "").strip() for r in records if isinstance(r, dict)):
            groups.append(("strategy_by_version", "source_tab+decision_label+strategy_version"))
    book = group_stats(records, n_trials, min_n)
    try:
        perf = rr.performance_block(records)
        book["max_drawdown"] = perf.get("max_drawdown")
        book["days"] = perf.get("days")
        book["return_maxdd"] = perf.get("return_maxdd")
    except Exception:  # noqa: BLE001
        pass
    out_groups: Dict[str, Dict[str, Any]] = {}
    flat: List[Dict[str, Any]] = []
    for title, field in groups:
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        fn = _key_fn(field)
        for r in records:
            buckets[fn(r)].append(r)
        cells = {k: group_stats(v, n_trials, min_n) for k, v in buckets.items()}
        out_groups[title] = dict(sorted(cells.items(), key=lambda kv: -kv[1]["n"]))
        for k, c in cells.items():
            flat.append({"group": title, "key": k, **c})
    judged = [c for c in flat if c["verdict"] != "NOT_ENOUGH_DATA"]
    weakest = sorted(judged, key=lambda c: (c["mean_net_ret_pct"] if c["mean_net_ret_pct"] is not None else 0.0))[:3]
    counts: Dict[str, int] = defaultdict(int)
    for c in flat:
        counts[c["verdict"]] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": filters or {}, "trials": max(1, int(n_trials)), "min_n": min_n,
        "book": book, "groups": out_groups,
        "weakest": [{k: c[k] for k in ("group", "key", "n", "net", "mean_net_ret_pct", "lower90_pct", "verdict")} for c in weakest],
        "verdict_counts": dict(counts),
        "note": "Evidence only. A verdict never changes a switch; auto-calibration and your own click do that.",
    }


def render(rep: Dict[str, Any]) -> str:
    b = rep["book"]
    f = rep.get("filters") or {}
    lines = [f"EVIDENCE REPORT  {rep['generated_at']}  mode={f.get('mode', '-')} origin={f.get('origin', '-')} since={f.get('since') or 'all'}  trials={rep['trials']}  min_n={rep['min_n']}",
             "",
             f"BOOK: {b['n']} trades, net {b['net']:+,.0f}, charges {b['charges']:,.0f} ({b['charges_pct_of_gross']}% of gross), win {b['win_pct']}%, PF {b['profit_factor']}, "
             f"mean net {b['mean_net_ret_pct']}% (90% lower {b['lower90_pct']}%), DSR {b['dsr_prob']}, maxDD {b.get('max_drawdown')}, 2x-charges net {b['net_if_charges_x2']:+,.0f}  ->  {b['verdict']}",
             ""]
    for title, cells in rep["groups"].items():
        lines.append(f"-- {title}")
        for k, c in cells.items():
            lines.append(f"   {k[:28]:<28} n={c['n']:<4} net={c['net']:>+10,.0f} win={c['win_pct']}% PF={c['profit_factor']} mean={c['mean_net_ret_pct']}% lo90={c['lower90_pct']}% dsr={c['dsr_prob']}  {c['verdict']}")
        lines.append("")
    if rep["weakest"]:
        lines.append("WEAKEST (judged groups, lowest mean net return): " + "; ".join(f"{w['group']}={w['key']} ({w['mean_net_ret_pct']}%, {w['verdict']})" for w in rep["weakest"]))
    lines.append("VERDICTS: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["verdict_counts"].items())))
    lines.append(rep["note"])
    return "\n".join(lines)


def expand(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        hits = sorted(glob.glob(p))
        out.extend(hits if hits else [p])
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="export file(s) holding {\"records\": [...]}")
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--origin", default="system", choices=["all", "system", "imported"])
    ap.add_argument("--since", default="", help="only trades opened on/after YYYY-MM-DD (UTC date of opened_at)")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--trials", type=int, default=0, help="how many variants were tried (0 = use --trials-file, else 1)")
    ap.add_argument("--trials-file", default="", help="agent/trials.json: trials = 1 + ideas logged")
    ap.add_argument("--group-by", action="append", default=[], help="extra record field to group by (repeatable)")
    ap.add_argument("--json", default="", help="write the report as JSON here")
    a = ap.parse_args(argv)

    recs = rr.load_records(expand(a.files), a.mode)
    if a.origin != "all":
        recs = [r for r in recs if (r.get("origin") or "system") == a.origin]
    if a.since:
        recs = [r for r in recs if str(r.get("opened_at") or "")[:10] >= a.since]
    if not recs:
        print("evidence_report: no usable closed trades for these filters (run export_records first, or widen --since / --origin)", file=sys.stderr)
        return 1
    n_trials = a.trials if a.trials > 0 else (trials_from_file(a.trials_file) if a.trials_file else 1)
    groups = DEFAULT_GROUPS + [(g, g) for g in a.group_by if g]
    rep = build_report(recs, n_trials=n_trials, min_n=max(2, a.min_n), groups=groups,
                       filters={"mode": a.mode, "origin": a.origin, "since": a.since})
    print(render(rep))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        tmp = a.json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
        os.replace(tmp, a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
