#!/usr/bin/env python3
"""scripts/export_records.py - save real-trade-service closed-trade records to a file for records_report.py / hold_bucket_check.py (group 431).

  python3 scripts/export_records.py                         # last 60 days (the most the endpoint allows), REAL, to rt_<today>.json
  python3 scripts/export_records.py --days 30 --out rt_oct.json
  TOKEN=<admin bearer token> python3 scripts/export_records.py   # reuse a session token instead of logging in
  python3 scripts/records_report.py rt_*.json --since 2026-10-01

REAL mode needs the admin login. With no TOKEN it asks for the admin username and password (the password is typed hidden and only sent to
--base, default http://127.0.0.1:8005, i.e. the real-trade container's published port) and calls POST /auth/login. That is the same login the
dashboard does, so it also marks the REAL admin session as authenticated until it expires - if you would rather not, copy the dashboard's token
into TOKEN. DEMO needs no login. Read-only otherwise. Standard library only. Prints how many trades were saved and the NEWEST closed date, so a stale
export is obvious. Exit 0 saved, 1 nothing saved.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


def _req(url: str, token: str = "", body: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(base: str, mode: str, days: int, limit: int, token: str = "", timeout: float = 60.0) -> Dict[str, Any]:
    return _req(f"{base.rstrip('/')}/positions/{mode.upper()}/records?days={max(1, min(days, 60))}&limit={max(1, min(limit, 1000))}", token, None, timeout)


def login(base: str, username: str, password: str, timeout: float = 30.0) -> str:
    return str(_req(f"{base.rstrip('/')}/auth/login", "", {"username": username, "password": password}, timeout).get("token") or "")


def summarize(doc: Dict[str, Any]) -> str:
    recs = doc.get("records") or []
    closed = [str(r.get("closed_at") or "")[:10] for r in recs if r.get("closed_at")]
    msg = f"{len(recs)} trades saved (server total {doc.get('total')}), newest closed {max(closed) if closed else '-'}, oldest closed {min(closed) if closed else '-'}"
    if doc.get("total") and len(recs) < int(doc["total"]):
        msg += f"\nWARNING: the server holds {doc['total']} trades but only {len(recs)} came back (limit); pass a shorter --days"
    return msg


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.getenv("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--days", type=int, default=60, help="1-60")
    ap.add_argument("--limit", type=int, default=1000, help="1-1000")
    ap.add_argument("--out", default="")
    ap.add_argument("--user", default=os.getenv("ADMIN_USER", ""))
    a = ap.parse_args(argv)
    token = os.getenv("TOKEN", "").strip()
    try:
        if a.mode.upper() == "REAL" and not token:
            user = a.user or input("admin username: ").strip()
            token = login(a.base, user, os.getenv("ADMIN_PASSWORD") or getpass.getpass("admin password: "))
            if not token:
                print("login gave no token", file=sys.stderr)
                return 1
        doc = fetch(a.base, a.mode, a.days, a.limit, token)
    except urllib.error.HTTPError as e:
        print(f"{a.base}: HTTP {e.code} {'(login failed or the session token expired)' if e.code == 401 else ''}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"{a.base}: {e} (is the real-trade container up, and is this its published port? see `docker compose ps`)", file=sys.stderr)
        return 1
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        print("unexpected answer (no 'records' list)", file=sys.stderr)
        return 1
    from datetime import date
    out = a.out or f"rt_{date.today().isoformat()}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print(f"{out}: {summarize(doc)}")
    return 0 if doc["records"] else 1


if __name__ == "__main__":
    sys.exit(main())
