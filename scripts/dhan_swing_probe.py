#!/usr/bin/env python3
"""dhan_swing_probe.py - E132 (swing plan S4): does a resting Dhan stop on a CNC holding survive the night? The `open` phase PLACES A REAL 1-SHARE ORDER.

  TOKEN=<admin session token> python3 scripts/dhan_swing_probe.py --phase open --yes [--symbol ITC] [--rt-url http://127.0.0.1:8005]    (market hours 09:30-14:30 IST, REAL armed)
  TOKEN=<admin session token> python3 scripts/dhan_swing_probe.py --phase check --yes                                                  (the NEXT trading morning 09:20-14:30)
  TOKEN=<admin session token> python3 scripts/dhan_swing_probe.py --phase status                                                      (the last result, places nothing)

Calls real-trade-service POST /swing/probe, which does the work with the service's own Dhan credentials, symbol lock and order budget: open = BUY 1 share CNC, wait for the fill,
place a STOP_LOSS sell 4% below, read it back; check = read the stop back the next morning (PASS only if it is still live), cancel it and sell the share (a CDSL TPIN refusal is reported).
Without --yes nothing is sent. Ends with `SWING PROBE (...): PASS | FAIL - <reason> | NOT_RUN - <reason>`. Exit 0 = PASS / NOT_RUN / SKIPPED, 1 = FAIL or no answer, 2 = bad usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Tuple


def call(url: str, token: str, timeout: float = 240.0) -> Tuple[int, Any]:
    req = urllib.request.Request(url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
            raw, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"[:200]
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw[:200]


def render(doc: Any) -> Tuple[int, str]:
    if not isinstance(doc, dict) or not doc.get("line"):
        return 1, "SWING PROBE: FAIL - the service gave no usable answer"
    out = [f"step  {s}" for s in doc.get("steps") or []] + [str(doc["line"])]
    return (1 if str(doc.get("verdict")).upper() == "FAIL" else 0), "\n".join(out)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=("open", "check", "status"), default="status")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--rt-url", default=os.getenv("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    a = ap.parse_args(argv)
    q = {"phase": a.phase, "yes": "true" if a.yes else "false"}
    if a.symbol:
        q["symbol"] = a.symbol.strip().upper()
    code, doc = call(a.rt_url.rstrip("/") + "/swing/probe?" + urllib.parse.urlencode(q), os.getenv("TOKEN", ""))
    if code != 200:
        print(f"SWING PROBE: FAIL - the service answered {code}: {str(doc)[:160]}")
        return 1
    rc, text = render(doc)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
