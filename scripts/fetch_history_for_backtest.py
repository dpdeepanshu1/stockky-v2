#!/usr/bin/env python3
"""fetch_history_for_backtest.py - download daily candles into one JSON per symbol, for scripts/ms_ab_report.py and scripts/structure_backtest.py.

  MARKET_DATA_URL=https://... python3 scripts/fetch_history_for_backtest.py OUT_DIR [--symbols RELIANCE,TCS | --symbols-file f.txt | --universe-url URL]
        [--period 5y] [--sample 400 | --limit 0] [--seed 7] [--manifest sample.json] [--sleep 1.0] [--retries 3] [--min-bars 250] [--benchmark ^NSEI]
        [--max-failures-pct 60]

Symbols come from (first that is given): --symbols, --symbols-file (one per line, '#' comments), --universe-url (the gateway's /api/universe: a JSON list).
The benchmark (default ^NSEI) is always fetched first and saved as ^NSEI.json; the A/B report needs it to tag regimes. Existing files with >= --min-bars
candles are kept (re-run to resume) unless --refresh.
--sample N draws N symbols at random from the de-duplicated, SORTED list, so the same --seed gives the same sample whatever order the universe endpoint
returns (the old draw depended on that order). --manifest FILE writes the seed, the universe size and the exact symbols drawn (keep it outside OUT_DIR:
the A/B report reads every *.json in OUT_DIR); feed its "symbols" back with --symbols to replay the identical sample. Stdlib only. Exit 0 when the benchmark and at least one symbol were saved and the failure rate is
under --max-failures-pct; otherwise 1, so a CI run does not report on a hollow dataset.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List, Optional


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "stockky-backtest/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return json.loads(r.read().decode("utf-8"))


def candles_of(doc: Any) -> list:
    c = doc.get("candles") if isinstance(doc, dict) else doc
    return c if isinstance(c, list) else []


def read_symbols(args: argparse.Namespace) -> List[str]:
    raw: List[str] = []
    if args.symbols:
        raw = [s for s in args.symbols.split(",")]
    elif args.symbols_file:
        raw = [ln.split("#", 1)[0] for ln in Path(args.symbols_file).read_text().splitlines()]
    elif args.universe_url:
        # group433: the gateway can be restarting or busy when this runs (the 18 Oct VM runs got "Connection refused" and "Connection
        # reset by peer" and every seed failed at once). Retry with a growing pause before giving up.
        body, last = None, None
        for k in range(max(1, int(getattr(args, "universe_retries", 5)))):
            try:
                body = _get_json(args.universe_url, 60)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                if k + 1 < max(1, int(getattr(args, "universe_retries", 5))):
                    time.sleep(min(60.0, float(getattr(args, "universe_wait", 5.0)) * (k + 1)))
        if body is None:
            raise last if last is not None else RuntimeError("empty symbol list response")
        if isinstance(body, dict):
            body = body.get("symbols") or body.get("universe") or []
        raw = [str(s) for s in body]
    seen, out = set(), []
    for s in raw:
        s = s.strip().upper().replace(".NS", "").replace(".BO", "")
        if s and s not in seen and "/" not in s and "\\" not in s and not s.startswith(".."):
            seen.add(s)
            out.append(s)
    return out


def draw_sample(symbols: List[str], n: int, seed: int) -> List[str]:
    """N symbols drawn at random (seeded) from the sorted, de-duplicated list. The whole sorted list when n <= 0 or n >= its length.
    Sorting first makes the draw independent of the order the universe came in, so a seed is a reproducible sample."""
    import random
    pool = sorted(set(symbols))
    if n <= 0 or n >= len(pool):
        return pool
    return sorted(random.Random(seed).sample(pool, n))


def write_manifest(path: str, universe: int, drawn: List[str], sample: int, seed: int, period: str) -> None:
    """Record exactly which symbols a run used, so a result can be reproduced or compared across runs."""
    import datetime
    doc = {"created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "seed": seed,
           "requested_sample": sample, "universe_size": universe, "drawn": len(drawn), "period": period, "symbols": drawn}
    Path(path).write_text(json.dumps(doc, indent=1))


def fetch_one(base: str, sym: str, period: str, retries: int, sleep: float, timeout: float) -> Optional[list]:
    url = f"{base}/history/{urllib.parse.quote(sym, safe='^')}?period={period}&interval=1d"
    for k in range(max(1, retries)):
        try:
            c = candles_of(_get_json(url, timeout))
            if c:
                return c
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):  # delisted / unknown: retrying will not help
                return None
        except Exception:  # noqa: BLE001 - network noise; back off and retry
            pass
        time.sleep(sleep * (k + 1) * 2)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir")
    ap.add_argument("--base", default=os.getenv("MARKET_DATA_URL", ""))
    ap.add_argument("--symbols", default="")
    ap.add_argument("--symbols-file", default="")
    ap.add_argument("--universe-url", default="")
    ap.add_argument("--universe-retries", type=int, default=5, help="attempts to read --universe-url (group433)")
    ap.add_argument("--universe-wait", type=float, default=5.0, help="seconds between those attempts, growing by that much each time")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--benchmark", default="^NSEI")
    ap.add_argument("--limit", type=int, default=0, help="first N symbols of the list (alphabetical universe = biased sample; prefer --sample)")
    ap.add_argument("--sample", type=int, default=0, help="N symbols drawn at random (seeded) from the list - an unbiased cross-section")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--manifest", default="", help="write the drawn symbols (and the seed) to this JSON file; keep it outside OUT_DIR")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--min-bars", type=int, default=250)
    ap.add_argument("--max-failures-pct", type=float, default=60.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--benchmark-only", action="store_true", help="fetch / refresh just the benchmark (^NSEI.json, the exchange calendar the entry-rules backtest needs) and stop")
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        print("MARKET_DATA_URL (or --base) must be set and start with https://", file=sys.stderr)
        return 2
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if a.benchmark_only:                      # group427: hist400 had no ^NSEI.json; this adds it without touching the 400 stock files
        bpath = out / f"{a.benchmark}.json"
        if not a.refresh and bpath.exists():
            try:
                if len(candles_of(json.loads(bpath.read_text()))) >= a.min_bars:
                    print(f"{bpath} already there")
                    return 0
            except Exception:  # noqa: BLE001
                pass
        c = fetch_one(base, a.benchmark, a.period, a.retries, a.sleep, a.timeout)
        if not c or len(c) < a.min_bars:
            print(f"benchmark {a.benchmark} unavailable ({len(c or [])} candles)", file=sys.stderr)
            return 1
        bpath.write_text(json.dumps({"symbol": a.benchmark, "candles": c}))
        print(f"saved {bpath} ({len(c)} candles)")
        return 0
    try:
        syms = read_symbols(a)
    except Exception as e:  # noqa: BLE001
        print(f"could not read the symbol list: {e}", file=sys.stderr)
        return 2
    universe_size = len(syms)
    if a.sample > 0 and len(syms) > a.sample:
        syms = draw_sample(syms, a.sample, a.seed)
    elif a.limit > 0:
        syms = syms[: a.limit]
    if not syms:
        print("no symbols (use --symbols, --symbols-file or --universe-url)", file=sys.stderr)
        return 2

    if a.manifest:
        try:
            write_manifest(a.manifest, universe_size, syms, a.sample, a.seed, a.period)
        except OSError as e:
            print(f"could not write the sample manifest {a.manifest}: {e}", file=sys.stderr)
            return 2

    def keep(path: Path) -> bool:
        if a.refresh or not path.exists():
            return False
        try:
            return len(candles_of(json.loads(path.read_text()))) >= a.min_bars
        except Exception:  # noqa: BLE001
            return False

    bm = a.benchmark
    bpath = out / f"{bm}.json"
    if not keep(bpath):
        c = fetch_one(base, bm, a.period, a.retries, a.sleep, a.timeout)
        if not c or len(c) < a.min_bars:
            print(f"benchmark {bm} unavailable ({len(c or [])} candles) - the report cannot tag regimes", file=sys.stderr)
            return 1
        bpath.write_text(json.dumps({"symbol": bm, "candles": c}))
    ok = skipped = 0
    failed: List[str] = []
    for n, sym in enumerate(syms, 1):
        p = out / f"{sym}.json"
        if keep(p):
            skipped += 1
            continue
        c = fetch_one(base, sym, a.period, a.retries, a.sleep, a.timeout)
        if c and len(c) >= a.min_bars:
            p.write_text(json.dumps({"symbol": sym, "candles": c}))
            ok += 1
        else:
            failed.append(sym)
        if n % 25 == 0:
            print(f"{n}/{len(syms)} saved={ok} kept={skipped} failed={len(failed)}", flush=True)
        time.sleep(a.sleep)
    total = len(syms)
    pct = 100.0 * len(failed) / total
    print(json.dumps({"requested": total, "saved": ok, "kept": skipped, "failed": len(failed), "failed_pct": round(pct, 1),
                      "failed_sample": failed[:20]}))
    return 0 if (ok + skipped) > 0 and pct <= a.max_failures_pct else 1


if __name__ == "__main__":
    sys.exit(main())
