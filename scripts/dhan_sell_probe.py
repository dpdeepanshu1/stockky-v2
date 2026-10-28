#!/usr/bin/env python3
"""dhan_sell_probe.py - E108, T6: the Dhan SELL probe, started from the runner (or by hand). It PLACES ONE REAL 1-SHARE SELL ORDER and covers it.

  TOKEN=<admin session token> python3 scripts/dhan_sell_probe.py --yes [--symbol ITC] [--ps-url http://127.0.0.1:8006]
  TOKEN=<admin session token> python3 scripts/dhan_sell_probe.py --status          (reads the last result, places nothing)

Without --yes nothing is sent: it prints what would happen and `SELL PROBE: SKIPPED - ...`. With --yes it calls position-stocks POST /short/probe, which does the whole round trip
inside the service (so the Dhan credentials, the symbol lock, the order budget and the exit machinery are the real ones): one 1-share INTRADAY SELL Super Order, the legs read back from
Dhan, the legs cancelled, a 1-share MARKET BUY cover, the cover read back. The service refuses outside 09:30-14:30 IST, when not armed, when the kill switch is tripped, and for any
symbol that is held. If the cover cannot be completed the service adopts the short as a normal SHORT position (14:55 cover, stop at the broker) and sends a Telegram alert.
Prints the service's steps, what Dhan answered ("rules learned"), and ends with `SELL PROBE: PASS | FAIL - <reason> | SKIPPED - <reason> | NOT_RUN - <reason>`.
Exit 0 = PASS, SKIPPED, NOT_RUN (nothing to act on); 1 = FAIL or the service did not answer; 2 = bad usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple

CONFIRM_PHRASE = "PLACE_ONE_REAL_SELL_ORDER"        # the same phrase orders/sell_probe.py demands; a test pins the two together
Call = Callable[[str, str, Optional[dict]], Tuple[int, Any]]


def http_call(token: str, timeout: float = 180.0) -> Call:
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
        return 1, "SELL PROBE: FAIL - the service gave no usable answer"
    out = []
    for s in doc.get("steps") or []:
        out.append(f"step  {s}")
    for r in doc.get("rules") or []:
        out.append(f"dhan  {r}")
    if str(doc.get("verdict")).upper() == "FAIL":
        # E242: on a FAIL show what Dhan actually answered (the compact rows), so a wrong field name or a lagging list can be read off the run itself
        for k, v in (doc.get("answers") or {}).items():
            out.append(f"raw   {k}: {json.dumps(v, default=str)[:600]}")
    out.append(str(doc["line"]))
    return (1 if str(doc.get("verdict")).upper() == "FAIL" else 0), "\n".join(out)


def run(call: Call, ps_url: str, yes: bool, symbol: str = "", status_only: bool = False) -> Tuple[int, str]:
    base = ps_url.rstrip("/")
    if status_only:
        code, doc = call("GET", f"{base}/short/probe", None)
        if code != 200:
            return 1, f"SELL PROBE: FAIL - GET /short/probe returned {'no answer (' + str(doc) + ')' if code == 0 else 'HTTP ' + str(code)}"
        return render(doc)
    if not yes:
        return 0, ("This job places ONE REAL 1-share SELL Super Order on a liquid stock and covers it at once (about a minute, market hours 09:30-14:30 IST).\n"
                   "Nothing was sent. Tick 'I understand this places a real order' (or pass --yes) to run it.\nSELL PROBE: SKIPPED - not confirmed")
    code, doc = call("POST", f"{base}/short/probe", {"symbol": symbol or None, "confirm": CONFIRM_PHRASE})
    if code == 401 or code == 403:
        return 1, f"SELL PROBE: FAIL - the service refused the token (HTTP {code})"
    if code != 200:
        return 1, f"SELL PROBE: FAIL - POST /short/probe returned {'no answer (' + str(doc) + ')' if code == 0 else 'HTTP ' + str(code)}; CHECK DHAN for an open 1-share short"
    return render(doc)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--symbol", default="")
    ap.add_argument("--yes", action="store_true", help="really place the 1-share SELL and cover it")
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
