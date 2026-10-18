#!/usr/bin/env python3
"""calibration_status.py - did the calibration run really finish? (group 434). Stdlib only, read-only.

Two uses, both called by scripts/vm_calibrate.sh:

  per seed, right after the history fetch (stops a seed whose download mostly failed from feeding a meaningless A/B):
    python3 scripts/calibration_status.py --hist OUT/hist_7 [--manifest OUT/sample_manifest_7.json] [--min-symbols N] [--label seed_7]
  whole run (also usable by hand on a finished OUT_DIR):
    python3 scripts/calibration_status.py --out-dir OUT --seeds "7 11 13" [--min-symbols N] [--min-signals 30]

Why it exists: on 18 Oct two VM runs hit a restarting gateway and left hist_7 with 2-3 symbols; the backtests that followed ran on nothing and printed
numbers anyway. A seed is COMPLETE only when its history folder holds enough symbols (default: 60% of the manifest's drawn symbols, else 50), and, in the
whole-run mode, its ab_report.json has a real verdict (not no_data) and at least --min-signals baseline signals. Exit 0 = every seed complete; 1 otherwise.
The last lines say which seeds to re-run. A seed counts for the G2 / G3 gates only when COMPLETE.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BENCH = ("^NSEI", "NIFTY", "NSEI", "NIFTY50")


def count_symbols(hist: Path) -> int:
    return sum(1 for f in hist.glob("*.json") if f.stem.upper() not in BENCH) if hist.is_dir() else 0


def _json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def threshold(min_symbols: int, manifest: Optional[Dict[str, Any]]) -> int:
    if min_symbols and min_symbols > 0:
        return min_symbols
    drawn = int((manifest or {}).get("drawn") or 0)
    return max(10, int(0.6 * drawn)) if drawn else 50


def check_hist(hist: Path, manifest_path: Optional[Path], min_symbols: int = 0, label: str = "") -> Dict[str, Any]:
    man = _json(manifest_path)
    need, have = threshold(min_symbols, man), count_symbols(hist)
    ok = have >= need
    msg = f"{label or hist.name}: {have} symbol file(s), need {need}" + (f" (manifest drew {man.get('drawn')})" if man and man.get("drawn") else "")
    if not ok:
        msg += " -> too few: the download mostly failed (gateway restarting? market-data rate limit?). Not running the A/B on this."
    return {"ok": ok, "symbols": have, "need": need, "message": msg}


def check_seed(out: Path, seed: str, min_symbols: int, min_signals: int) -> Dict[str, Any]:
    hist, ab = out / f"hist_{seed}", out / f"ab_{seed}" / "ab_report.json"
    man_path = out / f"sample_manifest_{seed}.json"
    man = _json(man_path)
    h = check_hist(hist, man_path if man else None, min_symbols, f"hist_{seed}")
    row: Dict[str, Any] = {"seed": seed, "symbols": h["symbols"], "need": h["need"], "signals": None, "verdict": None, "edge_pp": None,
                           "status": "COMPLETE", "reason": "", "drawn": sorted((man or {}).get("symbols") or [])}
    if not h["ok"]:
        row.update(status="INCOMPLETE", reason=f"history has {h['symbols']} symbols, needs {h['need']}")
        return row
    rep = _json(ab)
    if rep is None:
        row.update(status="INCOMPLETE", reason="no A/B report (ab_report.json missing or unreadable)")
        return row
    sig = int(((rep.get("signals") or {}).get("baseline")) or 0)
    row.update(signals=sig, verdict=rep.get("verdict"), edge_pp=rep.get("edge_pp"))
    if rep.get("verdict") in (None, "no_data"):
        row.update(status="INCOMPLETE", reason=f"A/B verdict is {rep.get('verdict')}")
    elif sig < min_signals:
        row.update(status="INCOMPLETE", reason=f"only {sig} baseline signals (needs {min_signals})")
    return row


def check_run(out: Path, seeds: List[str], min_symbols: int = 0, min_signals: int = 30) -> Dict[str, Any]:
    rows = [check_seed(out, s, min_symbols, min_signals) for s in seeds]
    complete = [r for r in rows if r["status"] == "COMPLETE"]
    notes: List[str] = []
    if len(complete) < 2:
        notes.append(f"{len(complete)} complete seed(s): the gates need at least 2")
    drawn = [(r["seed"], set(r["drawn"])) for r in complete if r["drawn"]]
    for i in range(len(drawn)):
        for j in range(i + 1, len(drawn)):
            a, b = drawn[i], drawn[j]
            if a[1] and a[1] == b[1]:
                notes.append(f"seeds {a[0]} and {b[0]} drew the identical symbols: that is one sample, not two")
    return {"complete": len(complete) == len(rows) and bool(rows), "rows": rows, "notes": notes,
            "rerun": [r["seed"] for r in rows if r["status"] != "COMPLETE"]}


def render(res: Dict[str, Any]) -> str:
    L = ["Calibration run status", "", "| seed | symbols (need) | baseline signals | A/B verdict | edge pp | status |", "|---|---|---|---|---|---|"]
    for r in res["rows"]:
        L.append(f"| {r['seed']} | {r['symbols']} ({r['need']}) | {'-' if r['signals'] is None else r['signals']} | {r['verdict'] or '-'} | "
                 f"{'-' if r['edge_pp'] is None else r['edge_pp']} | {r['status']}{' - ' + r['reason'] if r['reason'] else ''} |")
    L += [""] + [f"note: {n}" for n in res["notes"]]
    if res["rerun"]:
        L.append(f're-run the missing seed(s): SEEDS="{" ".join(res["rerun"])}" OUT_DIR=<same dir> bash scripts/vm_calibrate.sh   (complete seeds are kept when RESUME=1)')
    else:
        L.append("all seeds complete: run scripts/structure_gate_check.py with every ab_*/ab_report.json (G2 + G3)")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hist", default="")
    ap.add_argument("--manifest", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--seeds", default="7 11 13")
    ap.add_argument("--min-symbols", type=int, default=0, help="0 = automatic (60%% of the manifest's drawn symbols, else 50)")
    ap.add_argument("--min-signals", type=int, default=30)
    a = ap.parse_args(argv)
    if a.hist:
        r = check_hist(Path(a.hist), Path(a.manifest) if a.manifest else None, a.min_symbols, a.label)
        print(r["message"])
        return 0 if r["ok"] else 1
    if not a.out_dir:
        ap.error("--hist or --out-dir is required")
    res = check_run(Path(a.out_dir), a.seeds.split(), a.min_symbols, a.min_signals)
    print(render(res))
    return 0 if res["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
