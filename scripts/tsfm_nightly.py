#!/usr/bin/env python3
"""tsfm_nightly.py - fill the timesfm-forecast-service cache after the end-of-day bars (group 434). Stdlib only.

  python3 scripts/tsfm_nightly.py [--url http://localhost:8011] [--universe-url http://localhost:8000/api/universe | --symbols A,B | --symbols-file f.txt]
                                  [--chunk 50] [--horizon 5] [--force] [--max-failed-pct 40]

Run it once a day after the daily bars are in (cron / the same place the structure refresh is scheduled). The service skips a symbol whose cached forecast
is already for the newest complete bar, so a second run costs almost nothing. The entry path only reads that cache (no model call in the hot path);
a symbol that is missing here is simply "no opinion" at entry time. Exit 0 when the service answered and the failed share is below --max-failed-pct,
1 otherwise (so a scheduler can alert). Prints one JSON summary line.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_history_for_backtest import read_symbols  # noqa: E402


def post_batch(url: str, symbols: List[str], horizon: int, force: bool, timeout: float, retries: int = 3, wait: float = 5.0) -> Dict[str, Any]:
    body = json.dumps({"symbols": symbols, "horizon": horizon, "force": force}).encode()
    last: Optional[Exception] = None
    for k in range(max(1, retries)):
        try:
            req = urllib.request.Request(url.rstrip("/") + "/batch", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            if k + 1 < retries:
                time.sleep(wait * (k + 1))
    raise RuntimeError(f"batch failed after {retries} attempts: {last}")


def run(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        raw = read_symbols(args)
    except Exception as e:  # noqa: BLE001 - an unreachable universe endpoint is a reportable failure, not a traceback
        raise RuntimeError(f"could not read the symbol list: {str(e)[:120]}") from e
    syms = sorted({s.strip().upper() for s in raw if s and s.strip()})
    if not syms:
        raise RuntimeError("no symbols (use --symbols, --symbols-file or --universe-url)")
    done = skipped = 0
    failed: List[Dict[str, Any]] = []
    unreachable = 0
    for i in range(0, len(syms), args.chunk):
        chunk = syms[i:i + args.chunk]
        try:
            r = post_batch(args.url, chunk, args.horizon, args.force, args.timeout, args.retries, args.retry_wait)
        except RuntimeError as e:
            unreachable += 1
            failed += [{"symbol": s, "error": str(e)[:120]} for s in chunk]
            continue
        done += len(r.get("done") or [])
        skipped += len(r.get("skipped") or [])
        failed += r.get("failed") or []
    return {"symbols": len(syms), "done": done, "skipped": skipped, "failed": len(failed), "unreachable_chunks": unreachable,
            "failed_sample": failed[:10], "failed_pct": round(100.0 * len(failed) / len(syms), 1)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8011")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--symbols-file", default="")
    ap.add_argument("--universe-url", default="")
    ap.add_argument("--universe-retries", type=int, default=5)
    ap.add_argument("--universe-wait", type=float, default=5.0)
    ap.add_argument("--chunk", type=int, default=50, help="symbols per request (the service caps a request at TSFM_BATCH_MAX, default 60)")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-wait", type=float, default=5.0)
    ap.add_argument("--max-failed-pct", type=float, default=40.0)
    a = ap.parse_args(argv)
    # read_symbols() also looks at these two; this tool has no sampling
    a.limit, a.sample, a.seed = 0, 0, 7
    try:
        s = run(a)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(s))
    return 0 if s["unreachable_chunks"] == 0 and s["failed_pct"] <= a.max_failed_pct else 1


if __name__ == "__main__":
    sys.exit(main())
