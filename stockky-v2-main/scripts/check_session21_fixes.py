#!/usr/bin/env python3
"""
scripts/check_session21_fixes.py — verifies the three session-21 fixes
against your LIVE deployment.

Fix 1 — PnL shows ₹0 in Positions tab
  Root cause: /positions/{mode} was returning the stale DB unrealized_pnl
  (only updated during exit cycles), not the live LTP-computed value. Every
  new position showed +₹0 until an exit-cycle ran. Fixed in main.py: live
  unrealized_pnl is now computed from LTP inline in the API response.

Fix 2 — Telegram notifications silent failure
  Root cause: _send_telegram() used parse_mode=Markdown. Telegram's legacy
  Markdown parser silently drops the ENTIRE message when the text contains
  unescaped special chars (_  .  (  )  -  +) — common in stock symbol names,
  rupee amounts and Dhan error strings. Switched to HTML parse_mode with
  *bold* → <b>bold</b> conversion. Added plain-text last-resort fallback.

Fix 3 — PARADEEP security_id float-cast (decision #27, already in this zip)
  The _clean_security_id() fix strips the spurious ".0" suffix that pandas
  float-upcasting adds to security IDs, preventing "Invalid SecurityId" SELL
  rejections.

Usage:
  export REAL_TRADE_SERVICE_URL=https://your-real-trade-service.onrender.com
  export NOTIFICATION_SERVICE_URL=https://your-notification-service.onrender.com
  export STOCKKY_ADMIN_USER=admin
  export STOCKKY_ADMIN_PASS=yourpassword
  python scripts/check_session21_fixes.py

No extra packages beyond stdlib. All checks are read-only (GET) except the
Telegram test which sends one test message.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse


def _request(method, url, token=None, body=None, timeout=30.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw_error": raw}
    except Exception as e:
        return None, {"exception": str(e)}


def login(base, username, password):
    status, body = _request("POST", f"{base}/auth/login",
                             body={"username": username, "password": password})
    if status != 200 or "token" not in body:
        print(f"❌ Login failed (HTTP {status}): {body}", file=sys.stderr)
        sys.exit(1)
    return body["token"]


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    real_base = (os.environ.get("REAL_TRADE_SERVICE_URL") or "").rstrip("/")
    notif_base = (os.environ.get("NOTIFICATION_SERVICE_URL") or "").rstrip("/")
    username = os.environ.get("STOCKKY_ADMIN_USER") or ""
    password = os.environ.get("STOCKKY_ADMIN_PASS") or ""

    if not real_base or not username or not password:
        print("Set REAL_TRADE_SERVICE_URL, STOCKKY_ADMIN_USER, STOCKKY_ADMIN_PASS first.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Real-trade-service: {real_base}")
    print(f"Notification service: {notif_base or '(not set — skipping telegram test)'}")

    token = login(real_base, username, password)
    print("✅ Logged in\n")

    passes = []
    fails = []

    # ── FIX 1: PnL not ₹0 ───────────────────────────────────────────────────
    section("FIX 1 — PnL should not all be ₹0 in Positions tab")
    status, positions = _request("GET", f"{real_base}/positions/REAL", token=token)
    print(f"HTTP {status} — {len(positions) if isinstance(positions, list) else '?'} positions")

    if status != 200 or not isinstance(positions, list):
        print(f"❌ FAIL — could not fetch positions: {positions}")
        fails.append("Fix 1: GET /positions/REAL failed")
    elif not positions:
        print("  ⚠️  No open positions — cannot verify PnL values (nothing to check).")
        print("  N/A — open a position first then re-run.")
    else:
        all_zero_pnl = True
        for p in positions:
            sym = p.get("symbol", "?")
            upnl = p.get("unrealized_pnl")
            ltp = p.get("current_price")
            entry = p.get("avg_entry_price")
            qty = p.get("qty_open", 0)
            pnl_pct = p.get("pnl_pct")

            # If ltp is available, unrealized_pnl must match (ltp-entry)*qty
            if ltp is not None and entry and qty:
                expected = round((ltp - entry) * qty, 2)
                if abs((upnl or 0) - expected) > 0.10:  # 10p tolerance for rounding
                    print(f"  ❌ {sym}: unrealized_pnl={upnl} but live calc={(expected)} "
                          f"(ltp={ltp}, entry={entry}, qty={qty})")
                    fails.append(f"Fix 1: {sym} stale PnL mismatch")
                else:
                    print(f"  ✅ {sym}: ₹{upnl} ({pnl_pct}%) — matches live LTP")
                    if upnl != 0:
                        all_zero_pnl = False
            else:
                print(f"  ⚠️  {sym}: no LTP available — ltp={ltp}, unrealized_pnl={upnl}")

        if all_zero_pnl and positions:
            print("  ⚠️  All positions show ₹0 PnL — either all flat at entry price or fix not deployed.")
        elif not fails:
            passes.append("Fix 1: live PnL values match LTP calculation")

    # ── FIX 2: Telegram ──────────────────────────────────────────────────────
    section("FIX 2 — Telegram notification (HTML parse_mode)")
    if not notif_base:
        print("  ⚠️  NOTIFICATION_SERVICE_URL not set — skipping.")
    else:
        # Check /config to see Telegram is configured
        cfg_status, cfg = _request("GET", f"{notif_base}/config")
        print(f"  /config → HTTP {cfg_status}")
        tg = cfg.get("telegram", {}) if isinstance(cfg, dict) else {}
        tg_configured = tg.get("configured", False)
        tg_enabled = tg.get("enabled", False)
        print(f"  Telegram configured={tg_configured}, enabled={tg_enabled}")

        if not tg_configured or not tg_enabled:
            print("  ⚠️  Telegram not configured/enabled — skipping send test.")
            print("  To test: configure TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and enable.")
        else:
            # Send a test notification with special chars that used to break Markdown
            test_status, test_result = _request(
                "POST", f"{notif_base}/notify",
                body={
                    "title": "✅ Stockky session-21 test",
                    "message": (
                        "*PARADEEP* ×7 SELL_REJECTED — Dhan error: Validate Qty (CDSL)\n"
                        "Entry: ₹166.70 | Stop: ₹161.36 | Target: ₹177.53\n"
                        "P&L: -₹59.50 (special chars: _ . ( ) + - test)"
                    ),
                    "channel": "telegram",
                    "urgency": "normal",
                }
            )
            print(f"  POST /notify → HTTP {test_status}: {test_result}")
            if test_status == 200 and isinstance(test_result, dict):
                if test_result.get("delivered"):
                    print("  ✅ PASS — Telegram delivered (check your Telegram for the message)")
                    passes.append("Fix 2: Telegram HTML mode delivered")
                else:
                    note = test_result.get("note", "")
                    print(f"  ❌ FAIL — not delivered: {note}")
                    fails.append(f"Fix 2: Telegram not delivered — {note}")
            else:
                print(f"  ❌ FAIL — unexpected response")
                fails.append("Fix 2: Telegram /notify failed")

    # ── FIX 3: PARADEEP security_id (decision #27) ───────────────────────────
    section("FIX 3 — PARADEEP security_id float-cast (_clean_security_id)")
    # Check if dhan_client has _clean_security_id deployed — we can only infer
    # this from the /health or a code-inspection route. The live test is:
    # a SELL on PARADEEP no longer returns "Invalid SecurityId".
    # We can't place a real order here, so just check the service is alive
    # and log what we know.
    health_status, health = _request("GET", f"{real_base}/health")
    print(f"  /health → HTTP {health_status}: {health}")
    if health_status == 200:
        print("  ✅ Service reachable — decision #27 fix (_clean_security_id) is in this zip.")
        print("  To fully verify: attempt a manual SELL on PARADEEP from the dashboard.")
        print("  It should no longer return 'Dhan API error: Invalid SecurityId'.")
        passes.append("Fix 3: service alive (PARADEEP fix in zip — verify via live SELL)")
    else:
        print(f"  ❌ Service unhealthy — check deployment")
        fails.append("Fix 3: service unreachable")

    # ── Summary ──────────────────────────────────────────────────────────────
    section("SUMMARY")
    for p in passes:
        print(f"  ✅ {p}")
    for f in fails:
        print(f"  ❌ {f}")
    if not passes and not fails:
        print("  ⚠️  All checks were N/A — open positions and configure Telegram to get real results.")
    print()


if __name__ == "__main__":
    main()
