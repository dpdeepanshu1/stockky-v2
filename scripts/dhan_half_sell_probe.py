#!/usr/bin/env python3
"""dhan_half_sell_probe.py - E283 / E285 round 98: the Dhan HALF-SELL probe, started by hand or from vm_all.sh. It PLACES REAL ORDERS: BUY 2 shares, SELL 1, SELL 1.

  TOKEN=<admin session token> python3 scripts/dhan_half_sell_probe.py --yes [--symbol ITC] [--ps-url http://127.0.0.1:8006]
  TOKEN=<admin session token> python3 scripts/dhan_half_sell_probe.py --status          (reads the last result, places nothing)

Without --yes nothing is sent: it prints what would happen and `HALF-SELL PROBE: SKIPPED - ...`. With --yes it calls position-stocks POST /half-sell/probe, which runs the whole
sequence inside the service (docs/E283_HALF_SELL_PROBE_PROTOCOL.md): BUY 2 with a Super Order, cancel the target leg, cancel the stop leg, MARKET SELL 1, SELL stop for the other
share, read everything back, cancel the stop, SELL the last share. The service refuses outside 09:30-14:30 IST, when not armed, when the kill switch is tripped, for a stock above
Rs 500 and for any symbol that is held. If it cannot get flat the leftover is adopted as a normal long position and Telegram shouts.
Prints the steps, the answers Q1..Q7 and ends with `HALF-SELL PROBE: PASS | FAIL - <reason> | SKIPPED - <reason> | NOT_RUN - <reason>`.
Exit 0 = PASS, SKIPPED, NOT_RUN; 1 = FAIL or the service did not answer; 2 = bad usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple

CONFIRM_PHRASE = "PLACE_REAL_HALF_SELL_PROBE"        # the same phrase orders/half_sell_probe.py demands; a test pins the two together
Call = Callable[[str, str, Optional[dict]], Tuple[int, Any]]


def http_call(token: str, timeout: float = 240.0) -> Call:
    def call(method: str, url: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
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
    return call


def render(doc: Any) -> Tuple[int, str]:
    """Pure. (exit code, text) for a probe result dict (or the 'last result' document)."""
    if not isinstance(doc, dict) or not doc.get("line"):
        return 1, "HALF-SELL PROBE: FAIL - the service gave no usable answer"
    out = []
    for s in doc.get("steps") or []:
        out.append(f"step  {s}")
    for r in doc.get("rules") or []:
        out.append(f"dhan  {r}")
    for k, v in (doc.get("q") or {}).items():
        out.append(f"ans   {k}: {json.dumps(v, default=str)[:400]}")
    if doc.get("timeline"):
        out.append(f"time  {json.dumps(doc['timeline'], default=str)}")
    if str(doc.get("verdict")).upper() == "FAIL":
        for k, v in (doc.get("answers") or {}).items():
            out.append(f"raw   {k}: {json.dumps(v, default=str)[:600]}")
    out.append(str(doc["line"]))
    return (1 if str(doc.get("verdict")).upper() == "FAIL" else 0), "\n".join(out)


def run(call: Call, ps_url: str, yes: bool, symbol: str = "", status_only: bool = False) -> Tuple[int, str]:
    base = ps_url.rstrip("/")
    if status_only:
        code, doc = call("GET", f"{base}/half-sell/probe", None)
        if code != 200:
            return 1, f"HALF-SELL PROBE: FAIL - GET /half-sell/probe returned {'no answer (' + str(doc) + ')' if code == 0 else 'HTTP ' + str(code)}"
        return render(doc)
    if not yes:
        return 0, ("This job places REAL orders on a liquid stock under Rs 500: BUY 2 shares with a Super Order, cancel both legs, SELL 1, place a stop for the other share, then sell it\n"
                   "(about a minute, market hours 09:30-14:30 IST, watch Dhan's order book while it runs).\n"
                   "Nothing was sent. Pass --yes to run it.\nHALF-SELL PROBE: SKIPPED - not confirmed")
    code, doc = call("POST", f"{base}/half-sell/probe", {"symbol": symbol or None, "confirm": CONFIRM_PHRASE})
    if code == 401 or code == 403:
        return 1, f"HALF-SELL PROBE: FAIL - the service refused the token (HTTP {code})"
    if code != 200:
        return 1, f"HALF-SELL PROBE: FAIL - POST /half-sell/probe returned {'no answer (' + str(doc) + ')' if code == 0 else 'HTTP ' + str(code)}; CHECK DHAN for open shares or a resting stop"
    return render(doc)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--symbol", default="")
    ap.add_argument("--yes", action="store_true", help="really place the orders")
    ap.add_argument("--status", action="store_true", help="only read the last result")
    a = ap.parse_args(argv)
    if a.yes and a.status:
        print("--yes and --status cannot be combined", file=sys.stderr)
        return 2
    rc, text = run(http_call(a.token), a.ps_url, a.yes, a.symbol.strip().upper(), a.status)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
