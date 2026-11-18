#!/usr/bin/env python3
"""scripts/check_event_sources.py - run this ON THE VM before trusting the typed event feed (task 1).

Fetches every source in event/event_feed.FEEDS once and prints a verdict table: HTTP status, number of items, number with a readable publish time,
age of the newest item, and OK / STALE / EMPTY / BLOCKED / DOWN. Exchange filings (NSE / BSE announcements) are added to FEEDS only after at least
one source here reads OK.

    python3 scripts/check_event_sources.py            # table
    python3 scripts/check_event_sources.py --json     # machine readable
    python3 scripts/check_event_sources.py --no-extra # only the FEEDS list
Exit code 0 when at least one FEEDS source is OK, 1 when none is.
Round 617: below the FEEDS rows the table also probes EXTRA feeds that the news service and the after-hours scan still use but FEEDS does not list
(NDTV Profit's bloombergquint Atom feed, Moneycontrol latestnews.xml, CNBC TV18). They are information only: they never change the exit code or the
"switch on" line, so a verdict there decides whether to drop or keep them in the news service / afterhours_scan.py.
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


EXTRA = [
    ("NDTVProfit-bloombergq", "https://prod-qt-images.s3.amazonaws.com/production/bloombergquint/feed.xml"),
    ("Moneycontrol-latest", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("CNBCTV18", "https://www.cnbctv18.com/feed/"),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-extra", action="store_true", help="probe only the FEEDS list")
    a = ap.parse_args(argv)
    rows = [probe(n, u) for n, u in _load_feeds()]
    n_feeds = len(rows)
    if not a.no_extra:
        rows += [dict(probe(n, u), extra=True) for n, u in EXTRA]
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'SOURCE':<24}{'STATUS':<8}{'ITEMS':<7}{'DATED':<7}{'NEWEST(min)':<13}VERDICT")
        for i, r in enumerate(rows):
            if i == n_feeds and not a.no_extra:
                print("-- not in FEEDS (information only, never switched on here):")
            print(f"{r['source']:<24}{str(r['status']):<8}{r['items']:<7}{r['dated']:<7}{str(r['newest_age_min']):<13}{r['verdict']} {r['note']}")
    ok = [r for r in rows[:n_feeds] if r["verdict"] == "OK"]
    if not a.json:
        print("\nswitch on:", ", ".join(r["source"] for r in ok) if ok else "NONE reachable - the event feed will answer ok:false until one is")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
