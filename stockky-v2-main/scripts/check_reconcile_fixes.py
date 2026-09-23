#!/usr/bin/env python3
"""
scripts/check_reconcile_fixes.py — verifies the session20 reconcile fixes
actually did what they were supposed to, against your LIVE deployment.

Checks, in order:
  1. Logs in as admin (needed for every REAL-mode call).
  2. Snapshots REAL positions BEFORE reconcile — records which ones are
     PENDING_EXIT, and whether the 4 CSV-only symbols exist yet.
  3. Calls POST /reconcile/REAL — the same thing clicking "Reconcile" in
     the dashboard does.
  4. Re-fetches REAL positions AFTER reconcile.
  5. Compares before/after and prints a plain PASS/FAIL verdict for each
     of the two fixes, plus the raw reconcile response — paste the
     printed output back and that's enough for me to tell what's right
     and what's still wrong, no live access needed on my end.

Usage:
  export REAL_TRADE_SERVICE_URL=https://your-real-trade-service.onrender.com
  export STOCKKY_ADMIN_USER=your_admin_username
  export STOCKKY_ADMIN_PASS=your_admin_password
  python scripts/check_reconcile_fixes.py

No extra packages beyond stdlib (urllib, json) — runs anywhere Python 3 does.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Symbols this session's fixes specifically targeted — edit if your
# tickers differ, but these match the screenshot/CSV from this session.
WATCH_PENDING_EXIT = {"ASHOKLEY", "ADANIPOWER"}
WATCH_MISSING_HOLDINGS = {"DEVYANI", "PARADEEP", "SUZLON", "IDEA"}
# ^ loose substring match against each broker holding's tradingSymbol,
# since Dhan's own symbol for these may not exactly match the CSV's
# display name (e.g. "Devyani International" -> tradingSymbol "DEVYANI").


def _request(method: str, url: str, token: str | None = None, body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw_error": raw}
    except Exception as e:
        return None, {"exception": str(e)}


def login(base: str, username: str, password: str) -> str:
    status, body = _request("POST", f"{base}/auth/login", body={"username": username, "password": password})
    if status != 200 or "token" not in body:
        print(f"❌ Login failed (HTTP {status}): {body}", file=sys.stderr)
        sys.exit(1)
    return body["token"]


def get_positions(base: str, token: str) -> list[dict]:
    status, body = _request("GET", f"{base}/positions/REAL", token=token)
    if status != 200 or not isinstance(body, list):
        print(f"❌ GET /positions/REAL failed (HTTP {status}): {body}", file=sys.stderr)
        sys.exit(1)
    return body


def by_symbol(positions: list[dict]) -> dict[str, dict]:
    return {p["symbol"]: p for p in positions}


def main() -> None:
    base = (os.environ.get("REAL_TRADE_SERVICE_URL") or "").rstrip("/")
    username = os.environ.get("STOCKKY_ADMIN_USER") or ""
    password = os.environ.get("STOCKKY_ADMIN_PASS") or ""
    if not base or not username or not password:
        print(
            "Set REAL_TRADE_SERVICE_URL, STOCKKY_ADMIN_USER, STOCKKY_ADMIN_PASS first.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"== Logging in to {base} ==")
    token = login(base, username, password)
    print("✅ Logged in\n")

    print("== BEFORE reconcile: current REAL positions ==")
    before = get_positions(base, token)
    before_by_symbol = by_symbol(before)
    for p in before:
        print(f"  {p['symbol']:<14} status={p['status']:<14} qty={p['qty_open']}")
    if not before:
        print("  (no REAL positions at all)")
    print()

    stuck_before = {sym for sym in WATCH_PENDING_EXIT if before_by_symbol.get(sym, {}).get("status") == "PENDING_EXIT"}
    missing_before = {
        sym for sym in WATCH_MISSING_HOLDINGS
        if not any(sym in p["symbol"] for p in before)
    }
    print(f"PENDING_EXIT among watched symbols before: {sorted(stuck_before) or 'none'}")
    print(f"Watched holdings still missing before:     {sorted(missing_before) or 'none'}\n")

    print("== Calling POST /reconcile/REAL ==")
    status, reconcile_result = _request("POST", f"{base}/reconcile/REAL", token=token)
    print(f"HTTP {status}: {json.dumps(reconcile_result, indent=2)}\n")

    print("== AFTER reconcile: current REAL positions ==")
    after = get_positions(base, token)
    after_by_symbol = by_symbol(after)
    for p in after:
        print(f"  {p['symbol']:<14} status={p['status']:<14} qty={p['qty_open']}")
    if not after:
        print("  (no REAL positions at all)")
    print()

    # ---- Verdict 1: PENDING_EXIT orphan fix ----
    print("== VERDICT 1: stuck PENDING_EXIT positions ==")
    if not stuck_before:
        print("  N/A — nothing was stuck before this run (nothing to prove either way).")
    else:
        for sym in sorted(stuck_before):
            after_status = after_by_symbol.get(sym, {}).get("status")
            if after_status in ("OPEN", "PARTIALLY_CLOSED"):
                print(f"  ✅ PASS — {sym} moved PENDING_EXIT -> {after_status}")
            elif after_status == "PENDING_EXIT":
                print(f"  ❌ FAIL — {sym} is STILL PENDING_EXIT after reconcile")
            elif after_status is None:
                print(f"  ⚠️  {sym} no longer appears at all (fully closed/sold? check /orders/REAL)")
            else:
                print(f"  ⚠️  {sym} now shows unexpected status: {after_status}")
    print()

    # ---- Verdict 2: broker-holdings import fix ----
    print("== VERDICT 2: missing broker holdings import ==")
    if not missing_before:
        print("  N/A — all watched holdings were already tracked before this run.")
    else:
        for sym in sorted(missing_before):
            now_present = any(sym in p["symbol"] for p in after)
            if now_present:
                match = next(p for p in after if sym in p["symbol"])
                print(f"  ✅ PASS — {sym} imported as {match['symbol']} (qty={match['qty_open']}, status={match['status']})")
            else:
                print(f"  ❌ FAIL — {sym} still not showing in /positions/REAL")
    print()

    # ---- Verdict 3: reconcile tally sanity ----
    print("== VERDICT 3: reconcile response tallies ==")
    if isinstance(reconcile_result, dict):
        unstuck = reconcile_result.get("positions_unstuck")
        imported = reconcile_result.get("holdings_imported")
        print(f"  positions_unstuck reported: {unstuck}")
        print(f"  holdings_imported reported: {imported}")
        if unstuck is None or imported is None:
            print("  ❌ FAIL — these keys are missing from the response entirely; "
                  "the session20 backend fix likely wasn't deployed.")
        else:
            print("  ✅ Keys present — backend fix is deployed and running.")
    else:
        print("  ❌ FAIL — reconcile did not return a JSON object at all.")

    print("\nDone. Paste this whole output back and it's enough to tell what's right and what's still wrong.")


if __name__ == "__main__":
    main()
