#!/usr/bin/env python3
"""scripts/drift_monitor.py - has a strategy's recent result drifted below its own history? (E579, round 554)

Reads the same exports as evidence_report.py / records_report.py (real-trade-service GET /positions/{mode}/records) and, for each strategy (source_tab | decision_label)
and for the whole book, splits the closed trades by close time into the last --recent trades (default 20) and everything before them (the baseline, at least --min-base = 30).
It compares the mean NET return per trade (net P&L / money put in, after charges) of the two halves with a Welch z-score and reports:

  NOT_ENOUGH_DATA  fewer than --min-base earlier trades or fewer than --recent recent trades with a readable return
  DECAY            recent mean is clearly below the baseline (z <= -2.33, one-sided 1 %) AND the recent mean is below 0
  WARNING          recent mean is below the baseline (z <= -1.645, one-sided 5 %)
  IMPROVING        recent mean is above the baseline (z >= +1.645)
  STABLE           none of the above

Also shown for information: win % and net rupees of the recent trades. It is a SMOKE ALARM, not a verdict: (a) trades of one session day share the market, so the z-score is optimistic
(more significant than it should be), which is why DECAY also needs a negative recent mean; (b) with many strategies, some will cross 5 % by luck; (c) a slow decay shows up late.
Never a trading instruction: it changes no switch. strategy_registry.py can read the JSON (--drift) and move a PROVEN strategy to `watch` after 3 separate days of DECAY.

Pure standard library, read-only, writes only the --json file you name.

  python3 scripts/drift_monitor.py rt_latest.json --json drift_monitor.json
  python3 scripts/drift_monitor.py rt_latest.json --recent 30 --min-base 40

Exit 0 report written and no WARNING / DECAY, 2 report written with at least one WARNING or DECAY (the runner shows "needs a look"), 1 no usable trades.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import evidence_report as er  # noqa: E402  (net_return_pct, _key_fn)
import records_report as rr  # noqa: E402

RECENT = 20
MIN_BASE = 30
Z_WARN = 1.645
Z_DECAY = 2.33
GROUP_FIELD = "source_tab+decision_label"      # the same strategy key as the evidence report's `strategy` group


def _mean_var(vals: List[float]) -> Tuple[float, float]:
    n = len(vals)
    m = sum(vals) / n
    v = sum((x - m) ** 2 for x in vals) / (n - 1) if n > 1 else 0.0
    return m, v


def welch_z(recent: List[float], base: List[float]) -> Optional[float]:
    """(mean recent - mean base) / standard error of the difference. None when it cannot be computed (fewer than 2 in a half, or no spread at all)."""
    if len(recent) < 2 or len(base) < 2:
        return None
    mr, vr = _mean_var(recent)
    mb, vb = _mean_var(base)
    se = math.sqrt(vr / len(recent) + vb / len(base))
    if se <= 1e-12:
        return None
    return (mr - mb) / se


def classify(z: Optional[float], recent_mean: float) -> str:
    if z is None:
        return "STABLE"
    if z <= -Z_DECAY and recent_mean < 0:
        return "DECAY"
    if z <= -Z_WARN:
        return "WARNING"
    if z >= Z_WARN:
        return "IMPROVING"
    return "STABLE"


def _when(r: Dict[str, Any]) -> str:
    return str(r.get("closed_at") or r.get("opened_at") or "")


def cell(rows: List[Dict[str, Any]], recent_n: int, min_base: int) -> Dict[str, Any]:
    """One drift cell from the trades of one group. Pure; never raises on odd rows (a row without a readable return is skipped)."""
    usable = []
    for r in sorted(rows, key=_when):
        v = er.net_return_pct(r)
        if v is not None:
            usable.append((v, rr.num(r.get("net_pnl")) or 0.0))
    n = len(usable)
    out: Dict[str, Any] = {"n": n, "recent_n": min(recent_n, n), "base_n": max(0, n - recent_n), "state": "NOT_ENOUGH_DATA", "z": None}
    if n - recent_n < min_base or n < recent_n:
        return out
    rec, base = usable[-recent_n:], usable[:-recent_n]
    rv, bv = [x[0] for x in rec], [x[0] for x in base]
    z = welch_z(rv, bv)
    mr, mb = sum(rv) / len(rv), sum(bv) / len(bv)
    out.update(state=classify(z, mr), z=None if z is None else round(z, 2), recent_mean_pct=round(mr, 3), base_mean_pct=round(mb, 3),
               recent_win_pct=round(100.0 * sum(1 for x in rv if x > 0) / len(rv), 1), base_win_pct=round(100.0 * sum(1 for x in bv if x > 0) / len(bv), 1),
               recent_net=round(sum(x[1] for x in rec), 2))
    return out


def build_report(records: List[Dict[str, Any]], recent_n: int = RECENT, min_base: int = MIN_BASE, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fn = er._key_fn(GROUP_FIELD)
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        buckets[fn(r)].append(r)
    strategies = {k: cell(v, recent_n, min_base) for k, v in buckets.items()}
    counts: Dict[str, int] = defaultdict(int)
    for c in strategies.values():
        counts[c["state"]] += 1
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "filters": filters or {}, "recent": recent_n, "min_base": min_base,
            "book": cell(records, recent_n, min_base), "strategies": dict(sorted(strategies.items(), key=lambda kv: -kv[1]["n"])), "state_counts": dict(counts),
            "note": "A smoke alarm, not a verdict: trades of one day share the market, so the z-score is optimistic. It changes no switch."}


def alarms(rep: Dict[str, Any]) -> List[str]:
    return [k for k, c in rep["strategies"].items() if c["state"] in ("WARNING", "DECAY")] + (["(whole book)"] if rep["book"]["state"] in ("WARNING", "DECAY") else [])


def render(rep: Dict[str, Any]) -> str:
    f = rep.get("filters") or {}
    lines = [f"DRIFT MONITOR  {rep['generated_at']}  mode={f.get('mode', '-')} origin={f.get('origin', '-')}  recent={rep['recent']} trades vs earlier (at least {rep['min_base']})", ""]

    def row(name: str, c: Dict[str, Any]) -> str:
        if c["state"] == "NOT_ENOUGH_DATA":
            return f"   {name[:34]:<34} n={c['n']:<4} NOT_ENOUGH_DATA (needs {rep['recent'] + rep['min_base']}+ trades)"
        return (f"   {name[:34]:<34} n={c['n']:<4} recent mean {c['recent_mean_pct']:+.3f}% vs earlier {c['base_mean_pct']:+.3f}%  z={c['z']}  "
                f"win {c['recent_win_pct']}% vs {c['base_win_pct']}%  recent net {c['recent_net']:+,.0f}  {c['state']}")
    lines.append(row("(whole book)", rep["book"]))
    lines += [row(k, c) for k, c in rep["strategies"].items()]
    al = alarms(rep)
    lines += ["", ("LOOK AT: " + "; ".join(al)) if al else "No strategy shows a WARNING or DECAY.",
              "VERDICTS: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["state_counts"].items())), rep["note"]]
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
    ap.add_argument("--recent", type=int, default=RECENT, help="how many of the latest closed trades form the recent half")
    ap.add_argument("--min-base", type=int, default=MIN_BASE, help="earlier trades needed before a strategy is judged")
    ap.add_argument("--json", default="", help="write the report as JSON here")
    a = ap.parse_args(argv)
    recs = rr.load_records(expand(a.files), a.mode)
    if a.origin != "all":
        recs = [r for r in recs if (r.get("origin") or "system") == a.origin]
    if not recs:
        print("drift_monitor: no usable closed trades for these filters (run export_records first)", file=sys.stderr)
        return 1
    rep = build_report(recs, max(2, a.recent), max(2, a.min_base), {"mode": a.mode, "origin": a.origin})
    print(render(rep))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
        os.replace(a.json + ".tmp", a.json)
    return 2 if alarms(rep) else 0


if __name__ == "__main__":
    sys.exit(main())
