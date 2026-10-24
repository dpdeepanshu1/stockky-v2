#!/usr/bin/env python3
"""wiring_check.py - E98 P0 item 3: is Market Weather really wired into BOTH trading services, right now? (the real-time test; changes nothing that lasts)

  TOKEN=<admin session token> python3 scripts/wiring_check.py [--rt-url http://127.0.0.1:8005] [--ps-url http://127.0.0.1:8006] [--expect-mode live]

For each of real-trade-service and position-stocks-service it checks, in order:
  1. GET /control/flags answers, MARKET_STATE_MODE is present and reads --expect-mode (default live: the compose default; use `any` to only report it)
  2. every dial in regime.profiles.DIALS that belongs to the service exists in the catalog with overlayable=True and live=True (a dial the layer would send but the
     service cannot take is exactly the "wired to nothing" failure)
  3. the overlay round trip: POST a STAMP-ONLY overlay (entries {}, state normal, version wiring-check, 10 minutes), read it back from GET /control/overlay, then
     DELETE it and read again. It never moves a dial. If an overlay or state stamp is ALREADY active (today's real one) this step is SKIPPED, never cleared.
  4. the in-force multiplier is reported and agrees with the switch: real-trade GET /risk-config/{mode}, position-stocks GET /ledger carry market_state_mode and
     market_state_risk_mult, equal to MARKET_STATE_MODE / MARKET_STATE_RISK_MULT as /control/flags sees them
  5. position-stocks only: GET /short/shadow answers, and SHORT_MODE is present
Prints one line per check and ends with `WIRING: PASS` or `WIRING: FAIL - <first failing line>`. Exit 0 = PASS, 1 = FAIL, 2 = bad usage. Stdlib only.
For a few seconds a trade created during step 3 would carry the stamp version `wiring-check`; run it outside the busiest minutes (09:15-09:30) if that matters.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE.parent / "services" / "calibration-runner-service"):     # /app in the runner image; the repo layout otherwise
    if (_p / "regime" / "__init__.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from regime import profiles  # noqa: E402

STAMP_VERSION = "wiring-check"
Call = Callable[[str, str, Optional[Dict[str, Any]]], Tuple[int, Any]]      # (method, url, body) -> (http status, parsed json or text)


def http_call(token: str, timeout: float = 20.0) -> Call:
    def call(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                raw = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode("utf-8", "replace"), e.code
        except Exception as e:  # noqa: BLE001 - unreachable, refused, timed out
            return 0, f"{type(e).__name__}: {e}"[:200]
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw[:200]
    return call


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.first_fail: Optional[str] = None

    def ok(self, svc: str, msg: str) -> None:
        self.lines.append(f"ok    {svc}: {msg}")

    def skip(self, svc: str, msg: str) -> None:
        self.lines.append(f"skip  {svc}: {msg}")

    def fail(self, svc: str, msg: str) -> None:
        self.lines.append(f"FAIL  {svc}: {msg}")
        if self.first_fail is None:
            self.first_fail = f"{svc}: {msg}"

    def text(self) -> str:
        verdict = "WIRING: PASS" if self.first_fail is None else f"WIRING: FAIL - {self.first_fail}"
        return "\n".join(self.lines + ["", verdict]) + "\n"


def _flag_map(doc: Any) -> Dict[str, Dict[str, Any]]:
    return {f.get("key"): f for f in (doc.get("flags") or []) if isinstance(f, dict)} if isinstance(doc, dict) else {}


def _same_number(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def check_service(rep: Report, svc: str, base: str, call: Call, expect_mode: str, now: Optional[datetime] = None) -> None:
    """svc is the profiles name: real-trade or position-stocks."""
    base = base.rstrip("/")
    # 1. flags + mode
    st, doc = call("GET", base + "/control/flags", None)
    if st != 200 or not isinstance(doc, dict):
        rep.fail(svc, f"GET /control/flags answered {st or 'no response'} ({str(doc)[:80]})")
        return
    flags = _flag_map(doc)
    mode_row = flags.get("MARKET_STATE_MODE")
    if not mode_row:
        rep.fail(svc, "MARKET_STATE_MODE is not in the switch catalog")
        return
    mode = str(mode_row.get("effective"))
    if expect_mode != "any" and mode != expect_mode:
        rep.fail(svc, f"MARKET_STATE_MODE is {mode}, expected {expect_mode}")
    else:
        rep.ok(svc, f"MARKET_STATE_MODE = {mode}" + (" (running" + f" {mode_row.get('running')}" + ")" if mode_row.get("running") is not None else ""))
    if mode_row.get("pending_restart"):
        rep.fail(svc, f"MARKET_STATE_MODE is saved as {mode_row.get('desired')} but the running process uses {mode_row.get('running')} (restart pending)")
    # 2. every dial exists, overlayable, live
    bad: List[str] = []
    mine = [k for k, d in profiles.DIALS.items() if svc in d["services"]]
    for k in mine:
        f = flags.get(k)
        if not f:
            bad.append(f"{k} missing")
        elif not f.get("overlayable"):
            bad.append(f"{k} not overlayable")
        elif not f.get("live"):
            bad.append(f"{k} not live")
    if bad:
        rep.fail(svc, "dials the layer sends but the service cannot take: " + ", ".join(bad))
    else:
        rep.ok(svc, f"all {len(mine)} dials exist with overlayable=True and live=True")
    # 3. stamp-only overlay round trip
    st, ov = call("GET", base + "/control/overlay", None)
    if st != 200 or not isinstance(ov, dict):
        rep.fail(svc, f"GET /control/overlay answered {st or 'no response'}")
    elif ov.get("active") or ov.get("overlays"):
        a = ov.get("active") or {}
        rep.skip(svc, f"an overlay is already active (state {a.get('state')}, until {a.get('until')}): left untouched, write test skipped")
    else:
        until = ((now or datetime.now(timezone.utc)) + timedelta(minutes=10)).astimezone(timezone.utc).isoformat(timespec="seconds")
        st, res = call("POST", base + "/control/overlay", {"entries": {}, "until": until, "state": "normal", "version": STAMP_VERSION, "note": "wiring_check (stamp only)"})
        posted = st == 200 and isinstance(res, dict) and res.get("ok")
        try:
            if not posted:
                rep.fail(svc, f"stamp-only POST /control/overlay answered {st or 'no response'}: {str(res)[:120]}")
            else:
                st2, back = call("GET", base + "/control/overlay", None)
                act = (back or {}).get("active") if isinstance(back, dict) else None
                if st2 == 200 and act and act.get("version") == STAMP_VERSION and act.get("state") == "normal":
                    rep.ok(svc, "stamp-only overlay accepted and read back")
                else:
                    rep.fail(svc, f"stamp posted but GET /control/overlay does not show it ({str(back)[:100]})")
        finally:
            if posted:
                call("DELETE", base + "/control/overlay", None)
                st3, after = call("GET", base + "/control/overlay", None)
                if st3 == 200 and isinstance(after, dict) and not after.get("active") and not after.get("overlays"):
                    rep.ok(svc, "test overlay cleared again")
                else:
                    rep.fail(svc, f"test overlay NOT cleared: {str(after)[:100]} - clear it with DELETE /control/overlay")
    # 4. in-force multiplier reported
    want_mult = (flags.get("MARKET_STATE_RISK_MULT") or {}).get("effective")
    if svc == "real-trade":
        body, where = None, []
        for m in ("REAL", "DEMO"):
            st, b = call("GET", f"{base}/risk-config/{m}", None)
            where.append(f"{m}:{st}")
            if st == 200 and isinstance(b, dict):
                body = b
                break
        label = "GET /risk-config/{mode}"
        if body is None:
            rep.fail(svc, f"{label} gave no 200 ({', '.join(where)})")
    else:
        st, body = call("GET", base + "/ledger", None)
        label = "GET /ledger"
        if st != 200 or not isinstance(body, dict):
            rep.fail(svc, f"{label} answered {st or 'no response'}")
            body = None
    if body is not None:
        if "market_state_mode" not in body or "market_state_risk_mult" not in body:
            rep.fail(svc, f"{label} does not report market_state_mode / market_state_risk_mult")
        elif str(body["market_state_mode"]) != mode:
            rep.fail(svc, f"{label} says mode {body['market_state_mode']} but /control/flags says {mode}")
        elif not _same_number(body["market_state_risk_mult"], want_mult):
            rep.fail(svc, f"{label} reports risk multiplier {body['market_state_risk_mult']} but the switch reads {want_mult}")
        else:
            rep.ok(svc, f"{label} reports mode {mode} and risk multiplier {body['market_state_risk_mult']} (matches the switch)")
    # 5. short selling (position-stocks only)
    if svc == "position-stocks":
        sm = flags.get("SHORT_MODE")
        rep.ok(svc, f"SHORT_MODE = {sm.get('effective')}") if sm else rep.fail(svc, "SHORT_MODE is not in the switch catalog")
        st, sh = call("GET", base + "/short/shadow", None)
        if st == 200 and isinstance(sh, dict):
            rep.ok(svc, "GET /short/shadow answers")
        else:
            rep.fail(svc, f"GET /short/shadow answered {st or 'no response'}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rt-url", default=os.environ.get("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--expect-mode", default="live", choices=["live", "shadow", "off", "any"])
    a = ap.parse_args(argv)
    rep = Report()
    call = http_call(a.token)
    for svc, url in (("real-trade", a.rt_url), ("position-stocks", a.ps_url)):
        check_service(rep, svc, url, call, a.expect_mode)
    sys.stdout.write(rep.text())
    return 0 if rep.first_fail is None else 1


if __name__ == "__main__":
    sys.exit(main())
