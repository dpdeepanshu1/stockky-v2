#!/usr/bin/env python3
"""scripts/fno_live_check.py - READ-ONLY look at the F&O engine while the market is live (round 293).
Opens fno.db with mode=ro, sends nothing to Dhan, changes nothing. Prints: is today a trading day, the switches, open positions with every doubt mark,
what the engine did in the last N minutes, today's closed P&L, and a RED list of things that need a human.

Run it inside the service container (no restart, no deploy needed):
  docker compose exec -T fno-options-service python - --minutes 60 < scripts/fno_live_check.py
or on the host against the data file:  python3 scripts/fno_live_check.py --db /path/to/fno.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _holidays() -> tuple:
    for p in ("/app", os.getcwd(), os.path.join(os.path.dirname(os.path.abspath(__file__ if "__file__" in globals() else ".")), "..", "services", "fno-options-service")):
        try:
            sys.path.insert(0, p)
            sys.modules.pop("config", None)          # the repo root also has a config/ folder: only the service's config.py has HOLIDAYS
            import config  # type: ignore
            if hasattr(config, "HOLIDAYS"):
                return tuple(config.HOLIDAYS)
        except Exception:  # noqa: BLE001
            pass
    return ()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("FNO_DATA_DIR", "/data/fno"), "fno.db"))
    ap.add_argument("--minutes", type=int, default=60)
    a = ap.parse_args()
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    hol = _holidays()
    is_hol = today in hol or now.weekday() >= 5
    in_hours = (9, 15) <= (now.hour, now.minute) < (15, 30)
    red: list = []
    print(f"== now {now:%a %d %b %Y %H:%M:%S} IST | holiday-or-weekend: {is_hol} | inside 09:15-15:30: {in_hours}")
    if is_hol:
        red.append(f"{today} is in the repo's holiday list (or a weekend): the engine trades nothing today. If NSE really is open, add the date's removal to config.HOLIDAYS and tell me.")
    # E401: a Calibration > Switches change lives in <CONTROL_DIR>/overrides.json and reaches os.environ only inside the RUNNING service process (control/boot.py), so a fresh
    # `docker compose exec` process saw plain env and printed FNO_ENGINE_ENABLED=false while /health said true. Read the override file too and mark what it set.
    ov: dict = {}
    try:
        _p = os.path.join((os.environ.get("CONTROL_DIR") or "").strip() or "/data/control", "overrides.json")
        with open(_p, encoding="utf-8") as _fh:
            ov = {k: v.get("value") for k, v in (json.load(_fh).get("overrides") or {}).items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 - no file / unreadable: fall back to env only
        ov = {}
    def _sw(k: str) -> str:
        return f"{k}={ov[k]} (switch)" if k in ov else f"{k}={os.environ.get(k, '-')}"
    print("== switches (live: Calibration override, else container env): " + " ".join(_sw(k) for k in ("FNO_ENGINE_ENABLED", "FNO_MODE", "FNO_REAL_ENABLED", "FNO_UNDERLYINGS")))
    if not os.path.exists(a.db):
        print(f"!! {a.db} not found (run inside the fno-options-service container, or pass --db)")
        return 2
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    kv = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM kv")}
    print(f"== armed day: {kv.get('fno_armed_day', '-')}  (REAL needs it to be today: {today})")

    print("\n== OPEN positions")
    opens = [(r, json.loads(r["data"])) for r in db.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY id")]
    if not opens:
        print("   none")
    for r, d in opens:
        ids = d.get("broker_order_ids") or {}
        marks = [k for k in ("entry_state", "entry_cid", "sl_doubt", "exit_doubt") if ids.get(k)]
        print(f"   #{r['id']} {r['mode']} {d.get('underlying')} {d.get('strike')}{d.get('opt_type')} {d.get('expiry')} {d.get('lots')}x{d.get('lot_size')} entry {d.get('entry_premium')} "
              f"SL {d.get('sl_price')} tgt {d.get('target_price')} strat {d.get('strategy')} | entry-id {ids.get('entry', '-')} stop-id {ids.get('sl', '-')} | marks: {', '.join(marks) or 'none'}")
        if ids.get("entry_state") == "UNKNOWN":
            red.append(f"#{r['id']}: ENTRY UNKNOWN (no stop, engine will not sell). Look in the Dhan app for the contract" + (f" / correlationId {ids['entry_cid']}" if ids.get("entry_cid") else "") + ".")
        if ids.get("sl_doubt"):
            red.append(f"#{r['id']}: STOP IN DOUBT (correlationId {ids['sl_doubt']}). Look in the Dhan app for a resting SELL stop.")
        if ids.get("exit_doubt"):
            red.append(f"#{r['id']}: EXIT IN DOUBT (correlationId {ids['exit_doubt']}). Look in the Dhan app for a resting SELL.")
        if r["mode"] == "REAL" and not ids.get("sl") and not ids.get("sl_doubt") and ids.get("entry_state") != "UNKNOWN" and not ids.get("exit_doubt"):
            red.append(f"#{r['id']}: REAL position with NO broker-side stop (software stop only).")

    since = (now - timedelta(minutes=a.minutes)).timestamp()
    print(f"\n== events in the last {a.minutes} min (count by kind)")
    ev = [dict(r) for r in db.execute("SELECT id, ts, kind, underlying, data FROM events WHERE ts>=? ORDER BY id", (since,))]
    c = Counter(e["kind"] for e in ev)
    print("   " + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none - no engine activity at all"))
    last_any = db.execute("SELECT MAX(ts) FROM events").fetchone()[0]
    last_snap = db.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    for name, ts in (("last event", last_any), ("last chain snapshot", last_snap)):
        print(f"   {name}: " + (f"{datetime.fromtimestamp(ts, IST):%a %d %b %H:%M:%S} IST ({int((now.timestamp() - ts) / 60)} min ago)" if ts else "never"))
    if in_hours and not is_hol and last_snap and now.timestamp() - last_snap > 15 * 60:
        red.append("market hours but no chain snapshot for over 15 min: the runner or market-data may be stuck.")
    print(f"\n== latest reconcile / order_failed / exit / entry events (max 15)")
    for e in [x for x in ev if x["kind"] in ("reconcile", "order_failed", "exit", "entry", "real_blocked")][-15:]:
        d = json.loads(e["data"])
        print(f"   {datetime.fromtimestamp(e['ts'], IST):%H:%M:%S} {e['kind']:<13} {e['underlying'] or '':<7} " + str(d.get("msg") or d.get("message") or d.get("reason") or d.get("label") or "")[:150])
    for e in ev:
        if e["kind"] == "order_failed":
            red.append(f"order_failed at {datetime.fromtimestamp(e['ts'], IST):%H:%M:%S}: " + str(json.loads(e["data"]).get("message", ""))[:120])
            break

    print("\n== today's closed P&L (net, after charges)")
    day_rows = db.execute("SELECT mode, data FROM positions WHERE day=? AND status!='OPEN'", (today,)).fetchall()
    by: dict = {}
    for r in day_rows:
        d = json.loads(r["data"])
        by.setdefault(r["mode"], []).append(float(d.get("net_pnl") or 0))
    for m, v in by.items():
        print(f"   {m}: {len(v)} closed, net {sum(v):+.2f}")
    if not by:
        print("   no closed trades today")

    print("\n== RED list (needs a human)")
    for x in dict.fromkeys(red):
        print("   !! " + x)
    if not red:
        print("   none")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
