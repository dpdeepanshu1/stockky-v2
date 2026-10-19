#!/usr/bin/env python3
"""control_snapshot.py - group 442: what is switched ON / OFF right now in real-trade-service and position-stocks-service, as a text report.

  TOKEN=<admin session token> python3 scripts/control_snapshot.py [--rt-url http://127.0.0.1:8005] [--ps-url http://127.0.0.1:8006] [--only-changes]
  python3 scripts/control_snapshot.py --rt-file rt_flags.json --ps-file ps_flags.json            # offline, from saved GET /control/flags replies

Reads GET /control/flags of each service (the page behind the Real Money Calibration tab) and prints, per group: every switch with its value, whether a change is
live or needs a restart, where the value comes from (override / .env / default), and flags
  PENDING RESTART   a saved value the running process is not using yet
  != RECOMMENDED    differs from the value the repo's own guidance names (docs/GROUP415, GROUP434, GROUP440), with LOOSER / TIGHTER when that is clear
  OVERRIDE          set from the page (who and when)
Read-only. Exit 0 = report printed; 1 = a service could not be read (the other is still printed); 2 = nothing could be read. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import OrderedDict
from typing import Any, Dict, List, Optional


def _get(url: str, token: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return json.loads(r.read().decode("utf-8", "replace"))


def _val(v: Any) -> str:
    return "-" if v is None else str(v)


def render(doc: Dict[str, Any], only_changes: bool = False) -> str:
    L: List[str] = []
    s = doc.get("summary") or {}
    L.append(f"== {doc.get('service')} ==  {s.get('flags')} switches, {s.get('overrides')} overridden, {s.get('pending_restart')} pending restart, {s.get('off_recommended')} differ from recommended")
    if not doc.get("persistent", True):
        L.append(f"!! state directory {doc.get('state_dir')} is NOT writable: switch changes are lost on restart ({doc.get('store_error')})")
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for f in doc.get("flags") or []:
        groups.setdefault(f.get("group") or "other", []).append(f)
    shown = 0
    for g, flags in groups.items():
        rows = []
        for f in flags:
            marks = []
            if f.get("pending_restart"):
                marks.append(f"PENDING RESTART (running {_val(f.get('running'))}, saved {_val(f.get('desired'))})")
            if f.get("matches_recommended") is False:
                marks.append(f"!= RECOMMENDED ({_val(f.get('recommended'))})")
            if f.get("override"):
                o = f["override"]
                marks.append(f"OVERRIDE by {o.get('by')} {str(o.get('at'))[:16]}")
            if only_changes and not marks:
                continue
            rows.append(f"  {str(f.get('title'))[:60]:<60} {_val(f.get('effective')):<9} {'live   ' if f.get('live') else 'restart'}  {f.get('source'):<8} {'; '.join(marks)}   [{f.get('key')}]")
        if rows:
            L.append(f"-- {g}")
            L += rows
            shown += len(rows)
    if not shown:
        L.append("  (nothing to show" + (": no overrides, nothing pending, nothing off recommended)" if only_changes else ")"))
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rt-url", default=os.environ.get("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--rt-file", default="")
    ap.add_argument("--ps-file", default="")
    ap.add_argument("--only-changes", action="store_true", help="only overridden / pending / off-recommended switches")
    a = ap.parse_args(argv)
    out: List[str] = ["SWITCH SNAPSHOT (Real Money Calibration)", ""]
    ok = 0
    errors = 0
    for name, url, file in (("real-trade-service", a.rt_url, a.rt_file), ("position-stocks-service", a.ps_url, a.ps_file)):
        try:
            doc = json.load(open(file, encoding="utf-8")) if file else _get(url.rstrip("/") + "/control/flags", a.token)
            out.append(render(doc, a.only_changes))
            ok += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            out.append(f"== {name} ==\n  could not read /control/flags: {type(e).__name__}: {e}\n  (needs an admin token: TOKEN=... or --token, and the service must be running the group 442 build)\n")
    print("\n".join(out))
    return 2 if ok == 0 else (1 if errors else 0)


if __name__ == "__main__":
    sys.exit(main())
