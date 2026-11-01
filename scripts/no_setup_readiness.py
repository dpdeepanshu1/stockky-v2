#!/usr/bin/env python3
"""no_setup_readiness.py - E276 round 72: is NO_SETUP_MODE=live justified yet? (read-only, stdlib only)

    TOKEN=<admin token> python3 scripts/no_setup_readiness.py --service http://127.0.0.1:8004     (reads GET /setup/stats)
    python3 scripts/no_setup_readiness.py --file setup_stats.json                                  (a saved /setup/stats answer)

The plan's rule for turning the refusal on (Position Stocks first): at least 10 sessions and 30 stamped entries, entries that matched S1 to S4 averaged a result
at least equal to entries stamped LEGACY_SHORTLIST, and fewer than half of all entries are unmatched. The result is measured in R (net P&L over the rupees risked
at the stop), the unit setup_stats keeps. Sessions are a LOWER bound (the largest session count of any one setup cell): the cells do not say how many days overlap.
Exit code 0 = READY, 1 = NOT_READY, 2 = input unreadable. It never changes anything; the switch is one env value, NO_SETUP_MODE=live, set by you.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

LEGACY = "LEGACY_SHORTLIST"
MIN_SESSIONS, MIN_ENTRIES, MAX_UNMATCHED_SHARE = 10, 30, 0.5


def _all_cells(cells: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {k.split("|", 1)[0]: v for k, v in (cells or {}).items() if k.endswith("|ALL") and isinstance(v, dict)}


def assess(cells: Dict[str, Any]) -> Dict[str, Any]:
    allc = _all_cells(cells)
    legacy = allc.get(LEGACY) or {}
    matched = {k: v for k, v in allc.items() if k != LEGACY}
    n_leg = int(legacy.get("n") or 0)
    n_mat = sum(int(v.get("n") or 0) for v in matched.values())
    total = n_leg + n_mat
    sessions = max([int(v.get("days") or 0) for v in allc.values()] or [0])
    rn_m = sum(int(v.get("r_n") or 0) for v in matched.values())
    avg_m = (sum(float(v["avg_r"]) * int(v.get("r_n") or 0) for v in matched.values() if v.get("avg_r") is not None) / rn_m) if rn_m else None
    avg_l = float(legacy["avg_r"]) if legacy.get("avg_r") is not None and int(legacy.get("r_n") or 0) else None
    share = (n_leg / total) if total else None
    checks: List[Dict[str, Any]] = [
        {"name": "sessions", "need": f">= {MIN_SESSIONS}", "have": sessions, "ok": sessions >= MIN_SESSIONS},
        {"name": "stamped entries", "need": f">= {MIN_ENTRIES}", "have": total, "ok": total >= MIN_ENTRIES},
        {"name": "matched entries have a result", "need": "R known", "have": avg_m, "ok": avg_m is not None},
        {"name": "shortlist entries have a result", "need": "R known", "have": avg_l, "ok": avg_l is not None},
        {"name": "matched avg R >= shortlist avg R", "need": "matched >= shortlist", "have": None if avg_m is None or avg_l is None else round(avg_m - avg_l, 4),
         "ok": avg_m is not None and avg_l is not None and avg_m >= avg_l},
        {"name": "unmatched share", "need": f"< {int(MAX_UNMATCHED_SHARE * 100)} %", "have": None if share is None else round(share, 3), "ok": share is not None and share < MAX_UNMATCHED_SHARE},
    ]
    ready = all(c["ok"] for c in checks)
    notes = []
    if ready and avg_m is not None and avg_m < 0:
        notes.append("matched entries are better than shortlist entries but still lose on average: the refusal removes the worse entries, it does not make a profit.")
    return {"verdict": "READY" if ready else "NOT_READY", "checks": checks, "matched_avg_r": avg_m, "shortlist_avg_r": avg_l, "matched_n": n_mat, "shortlist_n": n_leg, "notes": notes}


def render(r: Dict[str, Any]) -> str:
    out = [f"NO_SETUP_MODE readiness: {r['verdict']}", "", "| Check | Need | Have | OK |", "|---|---|---|---|"]
    out += [f"| {c['name']} | {c['need']} | {c['have']} | {'yes' if c['ok'] else 'NO'} |" for c in r["checks"]]
    out += [f"\nmatched n={r['matched_n']} avg R={r['matched_avg_r']} | shortlist n={r['shortlist_n']} avg R={r['shortlist_avg_r']}"] + r["notes"]
    out.append("READY means set NO_SETUP_MODE=live in the position-stocks .env and restart it (outside market hours). NOT_READY: change nothing.")
    return "\n".join(out)


def load(a) -> Optional[Dict[str, Any]]:
    try:
        if a.file:
            with open(a.file, encoding="utf-8") as f:
                doc = json.load(f)
        else:
            tok = os.getenv("TOKEN", "")
            req = urllib.request.Request(a.service.rstrip("/") + "/setup/stats?limit=1000", headers={"Authorization": f"Bearer {tok}"} if tok else {})
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - operator-supplied URL
                doc = json.loads(r.read().decode("utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception as e:  # noqa: BLE001
        print(f"input not read: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Is NO_SETUP_MODE=live justified yet? (read-only)")
    ap.add_argument("--service", default="", help="position-stocks base URL (reads /setup/stats; TOKEN env = admin token)")
    ap.add_argument("--file", default="", help="a saved /setup/stats JSON answer")
    a = ap.parse_args(argv)
    if not a.service and not a.file:
        print("give --service or --file", file=sys.stderr)
        return 2
    doc = load(a)
    if doc is None or doc.get("ok") is False or not isinstance(doc.get("cells"), dict):
        print("no usable /setup/stats answer", file=sys.stderr)
        return 2
    r = assess(doc["cells"])
    print(render(r))
    return 0 if r["verdict"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
