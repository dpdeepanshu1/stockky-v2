#!/usr/bin/env python3
"""fno_export_candles.py - E522 (round 502): write the daily index candles the F&O backtests read (`--daily nifty_d.csv`) from the SAME market-data endpoint the live engine uses
(GET /history/<symbol>?period=1y&interval=1d, fno/provider.py), so a replay sees exactly the daily picture the engine saw. Read-only, stdlib only.

  python3 scripts/fno_export_candles.py --out /tmp/replay                      # nifty_d.csv (+ vix.csv when market-data serves ^INDIAVIX)
  python3 scripts/fno_export_candles.py --und SENSEX --period 2y --base http://127.0.0.1:8001

Run it on the VM, where market-data-service listens on 127.0.0.1:8001. Rows the engine's bar rule rejects are dropped (the same rule as the engine), the file is ascending with one row
per day, columns date,open,high,low,close,volume. Exit code 0 when the index file was written, 1 when it could not be."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

_SVC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service")
sys.path.insert(0, os.path.abspath(_SVC))

from fno.provider import IST, INDEX_SYMBOL, VIX_SYMBOL, convert_candles  # noqa: E402


def fetch(base: str, symbol: str, period: str, interval: str, timeout: float = 30.0) -> Optional[Any]:
    url = f"{base.rstrip('/')}/history/{urllib.parse.quote(symbol, safe='')}?" + urllib.parse.urlencode({"period": period, "interval": interval})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  {symbol}: cannot read {url}: {e}", file=sys.stderr)
        return None


def to_rows(body: Any) -> List[Dict[str, Any]]:
    candles = convert_candles(body.get("candles") if isinstance(body, dict) else None)
    return [{"date": datetime.fromtimestamp(c["t"], IST).strftime("%Y-%m-%d"), "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"], "volume": int(c["v"])} for c in candles]


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Export the daily index candles the F&O backtests read, from market-data.")
    ap.add_argument("--base", default=os.getenv("MARKET_DATA_URL_HOST", "http://127.0.0.1:8001"))
    ap.add_argument("--und", default="NIFTY", choices=sorted(INDEX_SYMBOL))
    ap.add_argument("--period", default="1y")
    ap.add_argument("--out", default=".")
    ap.add_argument("--no-vix", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    rows = to_rows(fetch(a.base, INDEX_SYMBOL[a.und], a.period, "1d"))
    if not rows:
        print(f"no daily candles for {a.und} from {a.base}: is market-data-service running (docker compose ps)?", file=sys.stderr)
        return 1
    path = os.path.join(a.out, f"{a.und.lower()}_d.csv")
    write_csv(path, rows)
    print(f"{path}: {len(rows)} days, {rows[0]['date']} .. {rows[-1]['date']}")
    if not a.no_vix:
        vrows = to_rows(fetch(a.base, VIX_SYMBOL, a.period, "1d"))
        if vrows:
            write_csv(os.path.join(a.out, "vix.csv"), vrows)
            print(f"{os.path.join(a.out, 'vix.csv')}: {len(vrows)} days, {vrows[0]['date']} .. {vrows[-1]['date']}")
        else:
            print("vix.csv: not written (market-data did not serve ^INDIAVIX daily candles); the replay does not need it", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
