#!/usr/bin/env python3
"""swing_shadow_report.py - E132 (swing plan S3a): what did the screened stocks do AFTER the day they were screened, net of cost, against all the others? READ-ONLY.

  python3 scripts/swing_shadow_report.py SNAP_DIR [--out DIR] [--hold 5] [--size 20000] [--slip-pct 0.10] [--min-n 30] [--min-days 20] [--boot 500]
  TOKEN=<admin token> python3 scripts/swing_shadow_report.py --service http://127.0.0.1:8005 [...]        (reads GET /swing/snapshots instead of files)

Input: the daily files of scripts/swing_snapshot.py, or the service's own saved evening rankings. For each saved day D and each stock on its `screen` (top 20% by momentum AND
above its 200-day average) the return is close(D) -> close of the h-th LATER saved day (h = 1, 3, 5), when that day is close enough in the calendar (1 -> 4 days, 3 -> 6,
5 -> 9: a weekend and a holiday, not a week of missed runs). The control is every other ranked stock of the same days. Costs: the real delivery round trip at --size plus
--slip-pct on the entry.
VERDICT (the --hold horizon): NOT_ENOUGH_DATA under --min-n screened entries or --min-days saved days with an outcome; USEFUL when the net mean has its 5th percentile above zero
(resampling whole days) AND the screened-minus-rest edge also does; NO_EDGE otherwise. The ledger of the service (GET /swing/report) judges the exact lane trades (stop, ratchet);
this report judges the SIGNAL with a control group. READ THIS: entry here is the CLOSE of the screening day (the lane buys the next open), no stop is modelled, and a few weeks are
a handful of independent days: NOT_ENOUGH_DATA is the normal answer for a long while. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "services" / "real-trade-service"))
import swing_backtest as sb  # noqa: E402
from swing import stats  # noqa: E402

MAX_GAP_DAYS = {1: 4, 3: 6, 5: 9}
HORIZONS = (1, 3, 5)


def load_snapshots(folder: Path) -> List[Dict[str, Any]]:
    snaps = []
    for f in sorted(Path(folder).glob("swing_snap_*.json")):
        try:
            d = json.loads(f.read_text())
            snaps.append(_snap(d.get("asof_bar_date") or f.stem.replace("swing_snap_", ""), d.get("rows") or []))
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {type(e).__name__}: {e}", file=sys.stderr)
    snaps.sort(key=lambda s: s["day"])
    return snaps


def _snap(day: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    date.fromisoformat(day)
    return {"day": day, "rows": {r["symbol"]: r for r in rows if r.get("last_close") and r.get("rank") is not None}}


def load_service(url: str, token: str, days: int = 120) -> List[Dict[str, Any]]:
    req = urllib.request.Request(url.rstrip("/") + f"/swing/snapshots?days={days}", headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - operator-supplied URL
        doc = json.loads(r.read().decode())
    if doc.get("error"):
        raise RuntimeError(doc["error"])
    return sorted((_snap(s["asof_bar_date"], s.get("rows") or []) for s in doc.get("snapshots") or []), key=lambda s: s["day"])


def forward_rows(snaps: List[Dict[str, Any]], h: int, cost_pct: float, slip_pct: float) -> List[Dict[str, Any]]:
    """One row per (saved day, ranked stock) that has a close h saved days later within the calendar window: gross and net % (net = gross - cost - slippage)."""
    out = []
    for i, s in enumerate(snaps):
        j = i + h
        if j >= len(snaps):
            break
        if (date.fromisoformat(snaps[j]["day"]) - date.fromisoformat(s["day"])).days > MAX_GAP_DAYS.get(h, 3 * h):
            continue
        later = snaps[j]["rows"]
        for sym, r in s["rows"].items():
            b = later.get(sym)
            if not b:
                continue
            g = (float(b["last_close"]) / float(r["last_close"]) - 1.0) * 100.0
            out.append({"entry_day": s["day"], "sym": sym, "screen": bool(r.get("screen")), "gross_pct": round(g, 4), "net_pct": round(g - cost_pct - slip_pct, 4)})
    return out


def edge_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per day: mean net of the screened minus mean net of the rest (days with both)."""
    by: Dict[str, Dict[str, List[float]]] = {}
    for r in rows:
        by.setdefault(r["entry_day"], {"s": [], "r": []})["s" if r["screen"] else "r"].append(r["net_pct"])
    return [{"entry_day": d, "edge": sum(v["s"]) / len(v["s"]) - sum(v["r"]) / len(v["r"])} for d, v in sorted(by.items()) if v["s"] and v["r"]]


def build_report(snaps: List[Dict[str, Any]], hold: int, size: float, slip: float, min_n: int, min_days: int, n_boot: int) -> Dict[str, Any]:
    cost = sb.delivery_round_trip_pct(size, sb.cost_constants()[0])
    rep: Dict[str, Any] = {"days_saved": len(snaps), "size": size, "cost_pct": round(cost, 3), "slip_pct": slip, "hold": hold, "horizons": {}}
    for h in HORIZONS:
        rows = forward_rows(snaps, h, cost, slip)
        sel = [r for r in rows if r["screen"]]
        rest = [r for r in rows if not r["screen"]]
        rep["horizons"][str(h)] = {"screened": stats.summarize(sel, "net_pct", n_boot, min_days), "rest": stats.summarize(rest, "net_pct", 0, min_days),
                                   "edge_ci90": stats.day_bootstrap_ci(edge_rows(rows), "edge", "entry_day", n_boot, min_days=min_days),
                                   "gross_screened_mean": stats.summarize(sel, "gross_pct", 0, min_days)["mean"]}
    h = rep["horizons"].get(str(hold)) or rep["horizons"]["5"]
    sel = h["screened"]
    reasons: List[str] = []
    if sel["n"] < min_n or sel["days"] < min_days:
        v = "NOT_ENOUGH_DATA"
        reasons.append(f"{sel['n']} screened entries on {sel['days']} days with an outcome; needs {min_n} on {min_days} (saved days: {len(snaps)})")
    else:
        ok_net = bool(sel["ci90"] and sel["ci90"][0] > 0)
        ok_edge = bool(h["edge_ci90"] and h["edge_ci90"][0] > 0)
        v = "USEFUL" if (ok_net and ok_edge) else "NO_EDGE"
        if not ok_net:
            reasons.append("net mean per trade: the 90% interval does not stay above zero")
        if not ok_edge:
            reasons.append("edge over the other ranked stocks is not proven")
        if v == "NO_EDGE" and (h["gross_screened_mean"] or 0) > 0 and (sel["mean"] or 0) <= 0:
            reasons.append("gross mean is positive but costs eat it")
    rep.update(verdict=v, reasons=reasons)
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda x: "-" if x is None else str(x)  # noqa: E731
    L = [f"VERDICT: {rep['verdict']}", "", f"# Swing shadow report: screened stocks after the screening day, hold {rep['hold']} sessions, net of cost", "",
         f"{rep['days_saved']} saved days; cost {rep['cost_pct']}% round trip at Rs {int(rep['size'])} + {rep['slip_pct']}% slippage.", ""]
    if rep["reasons"]:
        L += ["Why:"] + [f"- {x}" for x in rep["reasons"]] + [""]
    L += ["| horizon | screened n | days | net mean % | win % | 90% interval | rest net mean % | edge 90% interval |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for h, x in rep["horizons"].items():
        s = x["screened"]
        L.append(f"| {h} | {s['n']} | {s['days']} | {f(s['mean'])} | {f(s['win_pct'])} | {f(s['ci90'])} | {f(x['rest']['mean'])} | {f(x['edge_ci90'])} |")
    L += ["", "- entry here is the close of the screening day; the lane buys the next open and has a stop (GET /swing/report judges those exact trades).",
          "- a few weeks are a handful of independent days: NOT_ENOUGH_DATA is the normal answer for a long while."]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir", nargs="?", default="")
    ap.add_argument("--service", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--hold", type=int, default=5, choices=HORIZONS)
    ap.add_argument("--size", type=float, default=20000.0)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=20)
    ap.add_argument("--boot", type=int, default=500)
    a = ap.parse_args(argv)
    if not a.dir and not a.service:
        print("give SNAP_DIR or --service URL", file=sys.stderr)
        return 2
    try:
        snaps = load_service(a.service, os.getenv("TOKEN", "")) if a.service else load_snapshots(Path(a.dir))
    except Exception as e:  # noqa: BLE001
        print(f"VERDICT: NOT_ENOUGH_DATA\n\ncould not read the snapshots: {type(e).__name__}: {str(e)[:120]}")
        return 1
    if len(snaps) < 2:
        print(f"VERDICT: NOT_ENOUGH_DATA\n\n{len(snaps)} saved day(s): need at least 2 (and weeks of them).")
        return 0
    rep = build_report(snaps, a.hold, a.size, a.slip_pct, a.min_n, a.min_days, a.boot)
    md = to_markdown(rep)
    if a.out:
        o = Path(a.out)
        o.mkdir(parents=True, exist_ok=True)
        (o / "swing_shadow_report.json").write_text(json.dumps(rep, indent=1, default=str))
        (o / "swing_shadow_report.md").write_text(md)
    print(md)
    return 1 if rep["verdict"] == "NO_EDGE" else 0


if __name__ == "__main__":
    sys.exit(main())
