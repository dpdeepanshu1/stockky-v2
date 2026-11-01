#!/usr/bin/env python3
"""scripts/check_event_sources.py - run this ON THE VM before trusting the typed event feed (task 1).

Fetches every source in event/event_feed.FEEDS once and prints a verdict table: HTTP status, number of items, number with a readable publish time,
age of the newest item, and OK / STALE / EMPTY / BLOCKED / DOWN. Exchange filings (NSE / BSE announcements) are added to FEEDS only after at least
one source here reads OK.

    python3 scripts/check_event_sources.py            # table
    python3 scripts/check_event_sources.py --json     # machine readable
Exit code 0 when at least one source is OK, 1 when none is.
Uses only the standard library, so it runs anywhere (it does not need httpx or feedparser).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/rss+xml, application/xml, text/xml, */*"}


def _load_feeds():
    path = os.path.join(ROOT, "services", "analysis-intelligence-service", "event", "event_feed.py")
    spec = importlib.util.spec_from_file_location("event_feed", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FEEDS


def _when(txt):
    try:
        dt = parsedate_to_datetime(txt.strip())
    except Exception:  # noqa: BLE001
        try:
            dt = datetime.fromisoformat(txt.strip().replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def probe(name, url, timeout=10.0):
    row = {"source": name, "url": url, "status": None, "items": 0, "dated": 0, "newest_age_min": None, "verdict": "DOWN", "note": ""}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            row["status"] = r.status
            body = r.read()
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["verdict"] = "BLOCKED" if e.code in (401, 403, 406, 429, 451) else "DOWN"
        row["note"] = f"HTTP {e.code}"
        return row
    except Exception as e:  # noqa: BLE001
        row["note"] = str(e)[:80]
        return row
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        row["verdict"], row["note"] = "DOWN", f"not XML ({str(e)[:40]})"
        return row
    now = datetime.now(timezone.utc)
    ages = []
    for it in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        row["items"] += 1
        for tag in ("pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published"):
            el = it.find(tag)
            if el is not None and el.text and _when(el.text):
                row["dated"] += 1
                ages.append((now - _when(el.text)).total_seconds() / 60.0)
                break
    if ages:
        row["newest_age_min"] = round(min(ages), 1)
    if row["items"] == 0:
        row["verdict"] = "EMPTY"
    elif row["dated"] == 0:
        row["verdict"], row["note"] = "EMPTY", "items have no publish time (the feed drops them)"
    elif row["newest_age_min"] is not None and row["newest_age_min"] > 24 * 60:
        row["verdict"] = "STALE"
    else:
        row["verdict"] = "OK"
    return row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = [probe(n, u) for n, u in _load_feeds()]
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'SOURCE':<24}{'STATUS':<8}{'ITEMS':<7}{'DATED':<7}{'NEWEST(min)':<13}VERDICT")
        for r in rows:
            print(f"{r['source']:<24}{str(r['status']):<8}{r['items']:<7}{r['dated']:<7}{str(r['newest_age_min']):<13}{r['verdict']} {r['note']}")
    ok = [r for r in rows if r["verdict"] == "OK"]
    if not a.json:
        print("\nswitch on:", ", ".join(r["source"] for r in ok) if ok else "NONE reachable - the event feed will answer ok:false until one is")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
