#!/usr/bin/env python3
"""movers_review.py - E263 (2026-10-30): "did the scan find today's movers, and what stopped the ones it did not take?" Read-only, stdlib only.

For each symbol it asks the running position-stocks-service (GET /scan/why/{symbol} and GET /scan/hits, both open routes, nothing is written) and prints one line:
whether the live feed carries it, whether the scan listed it today (best rank, quality-checked cycles), and the FIRST check that blocks it. It then adds the two
rules that decide before the scan is even asked, worked out from the price and day move you give:

  PRICE   a slot is about Rs 2,000 (the pool is small) and an entry needs ENTRY_MIN_QTY (3) shares, so a long above ~Rs 733 can never be sized.
  DAYGAIN a long already up more than MAX_DAY_GAIN_PCT (7 %) is refused at entry; a short already down more than SHORT_MAX_DAY_FALL_PCT (5 %) likewise.

  python3 scripts/movers_review.py                          # the built-in list (names from the 30 Oct Groww screenshots)
  python3 scripts/movers_review.py --symbols AYMSYNTEX,HIKAL --base http://127.0.0.1:8006
  python3 scripts/movers_review.py --file symbols.txt       # one NSE symbol per line (optional ",price,day_pct" after it)

A symbol the feed "does not carry" may simply be spelt differently from NSE's ticker; check it before reading it as a universe gap. This tool changes no setting.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

SLOT_PRICE_CEILING = 733.0      # ~Rs 2,000 slot, 3 shares, +10 % round-up allowance (orders/sizing.py)
MAX_DAY_GAIN_PCT = 7.0
SHORT_MAX_DAY_FALL_PCT = 5.0

# NSE ticker, price, day % (from the 30 Oct Groww screenshots; tickers are best guesses - a wrong one shows up as "not carried")
BUILTIN = [
    ("AYMSYNTEX", 259.55, 5.66), ("SMSPHARMA", 509.80, 4.94), ("HIKAL", 213.06, 4.24), ("PPLPHARMA", 236.20, 2.35), ("ETERNAL", 345.15, 2.16),
    ("GREAVESCOT", 203.20, 8.78), ("MENONBE", 289.50, 14.72), ("QUICKHEAL", 155.73, 13.85), ("MARSONS", 148.09, 19.38), ("BOROLTD", 282.45, 18.33),
    ("PINELABS", 158.33, -3.58), ("OLAELEC", 33.66, -4.27), ("VGUARD", 274.85, -3.51), ("PCJEWELLER", 15.45, -3.26), ("BIRLASOFT", 324.15, -5.36),
    ("PARADEEP", 126.79, -9.30), ("SUZLON", 33.86, -7.96), ("EQUITASBNK", 50.02, -12.51), ("MOTILALOFS", 948.50, -3.81), ("MAZDOCK", 1715.40, -2.68),
    ("GAIL", 168.34, 1.92), ("PNB", 123.95, 0.04), ("KOTAKBANK", 461.20, 0.48), ("HDFCBANK", 693.25, 1.46), ("VEDL", 238.45, 1.66),
]


def _get(url: str, timeout: float = 8.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, OSError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def static_blockers(price, day_pct):
    out = []
    if price is not None and price > SLOT_PRICE_CEILING:
        out.append(f"PRICE {price:g} > ~{SLOT_PRICE_CEILING:g} (3 shares do not fit a ~Rs 2,000 slot)")
    if day_pct is not None:
        if day_pct > MAX_DAY_GAIN_PCT:
            out.append(f"DAYGAIN long refused: up {day_pct:.2f}% > {MAX_DAY_GAIN_PCT:g}%")
        if day_pct < -SHORT_MAX_DAY_FALL_PCT:
            out.append(f"DAYGAIN short refused: down {abs(day_pct):.2f}% > {SHORT_MAX_DAY_FALL_PCT:g}%")
    return out


def parse_file(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        parts = [p.strip() for p in line.strip().split(",")]
        if not parts or not parts[0] or parts[0].startswith("#"):
            continue
        price = float(parts[1]) if len(parts) > 1 and parts[1] else None
        day = float(parts[2]) if len(parts) > 2 and parts[2] else None
        rows.append((parts[0].upper(), price, day))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8006", help="position-stocks-service base URL")
    ap.add_argument("--symbols", default="", help="comma separated NSE symbols (replaces the built-in list)")
    ap.add_argument("--file", default="", help="file with one symbol per line, optional ',price,day_pct'")
    a = ap.parse_args(argv)
    if a.file:
        rows = parse_file(a.file)
    elif a.symbols:
        rows = [(s.strip().upper(), None, None) for s in a.symbols.split(",") if s.strip()]
    else:
        rows = list(BUILTIN)
    hits = _get(a.base.rstrip("/") + "/scan/hits?limit=2000")
    if "_error" in hits:
        print(f"cannot reach {a.base}: {hits['_error']}", file=sys.stderr)
        return 2
    scans = hits.get("scans")
    print(f"scan record: {scans} scans today; {len(rows)} symbols to check\n")
    print(f"{'SYMBOL':12s} {'LISTED':7s} {'CHECKED':8s} FIRST BLOCKER")
    for sym, price, day in rows:
        why = _get(a.base.rstrip("/") + f"/scan/why/{sym}")
        if "_error" in why:
            print(f"{sym:12s} {'?':7s} {'?':8s} route error: {why['_error']}")
            continue
        listed = "yes" if why.get("picked_by_scan_today") else "no"
        checked = 0
        for side in ("hits_long", "hits_short"):
            checked += int((why.get(side) or {}).get("checked_cycles") or 0)
        pre = static_blockers(price, day)
        first = (pre + (why.get("blocking") or []) + [why.get("verdict") or ""])[0]
        print(f"{sym:12s} {listed:7s} {checked:<8d} {first}")
        for extra in (pre + (why.get("blocking") or []))[1:]:
            print(f"{'':29s}+ {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
