#!/usr/bin/env python3
"""analyze_latency_check.py - the live half of gate G1 (group 434): is /analyze p95 within 15% of what it was before the structure engine? Stdlib only.

ms_g1_check.py times the engine offline; the plan's G1 also asks that the LIVE endpoint did not get slower. That needs the running service, so:

  1. record a baseline with structure OFF (STRUCTURE_ENABLED=0 on analysis-intelligence-service, restart), then
       python3 scripts/analyze_latency_check.py --save latency_off.json
  2. turn structure back on (remove STRUCTURE_ENABLED=0, restart) and compare:
       python3 scripts/analyze_latency_check.py --compare latency_off.json [--max-increase-pct 15]

  --url   base of the technical sub-app (default http://localhost:8002/technical; the compose service listens on 127.0.0.1:8002 and mounts it at /technical)
  --symbols A,B,..   (default: 20 liquid NSE names)   --rounds 3 (every symbol is timed this many times; the first round also warms caches and is dropped
  when --rounds > 1)   --timeout 30

Verdict PASS when new p95 <= baseline p95 x (1 + max-increase-pct / 100), else FAIL; NOT_ENOUGH_DATA when fewer than 20 successful timings or more than 20%
of the requests failed (a slow answer from a half-dead service must not look like a pass). Exit 0 for PASS / save, 1 for FAIL / NOT_ENOUGH_DATA.
Network is a local call, so a few ms of jitter matters: the check ignores a difference below --min-abs-ms (default 5 ms).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_SYMBOLS = ("RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK,SBIN,ITC,LT,AXISBANK,KOTAKBANK,HINDUNILVR,BHARTIARTL,MARUTI,SUNPHARMA,TITAN,"
                   "WIPRO,NTPC,ONGC,TATASTEEL,BAJFINANCE")


def percentile(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def http_get_ms(url: str, timeout: float) -> float:
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as r:
        r.read()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
    return (time.perf_counter() - t0) * 1000.0


def measure(base: str, symbols: List[str], rounds: int, timeout: float, get: Callable[[str, float], float] = http_get_ms) -> Dict[str, Any]:
    times: List[float] = []
    failed = 0
    total = 0
    for rd in range(max(1, rounds)):
        for sym in symbols:
            total += 1
            try:
                ms = get(f"{base.rstrip('/')}/analyze/{sym}", timeout)
            except Exception:  # noqa: BLE001 - counted, not fatal
                failed += 1
                continue
            if rounds > 1 and rd == 0:
                continue                       # warm-up round: caches, lazy imports
            times.append(ms)
    return {"n": len(times), "requests": total, "failed": failed, "p50_ms": _r(percentile(times, 0.5)), "p95_ms": _r(percentile(times, 0.95)),
            "mean_ms": _r(sum(times) / len(times)) if times else None}


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def verdict(new: Dict[str, Any], base: Dict[str, Any], max_increase_pct: float = 15.0, min_abs_ms: float = 5.0, min_n: int = 20) -> Dict[str, Any]:
    for nm, m in (("new", new), ("baseline", base)):
        if m.get("n", 0) < min_n:
            return {"status": "NOT_ENOUGH_DATA", "reason": f"{nm} run has {m.get('n', 0)} successful timings (needs {min_n})"}
        if m.get("requests") and m.get("failed", 0) > 0.2 * m["requests"]:
            return {"status": "NOT_ENOUGH_DATA", "reason": f"{nm} run: {m['failed']} of {m['requests']} requests failed"}
    np95, bp95 = new["p95_ms"], base["p95_ms"]
    limit = bp95 * (1 + max_increase_pct / 100.0)
    pct = (np95 / bp95 - 1) * 100.0 if bp95 else None
    ok = np95 <= limit or (np95 - bp95) < min_abs_ms
    return {"status": "PASS" if ok else "FAIL", "new_p95_ms": np95, "baseline_p95_ms": bp95, "change_pct": None if pct is None else round(pct, 1),
            "limit_pct": max_increase_pct,
            "reason": f"p95 {bp95} ms -> {np95} ms ({pct:+.1f}%), limit +{max_increase_pct:g}% (differences under {min_abs_ms:g} ms are ignored)"}


def main(argv: Optional[List[str]] = None, get: Callable[[str, float], float] = http_get_ms) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8002/technical")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--save", default="")
    ap.add_argument("--compare", default="")
    ap.add_argument("--max-increase-pct", type=float, default=15.0)
    ap.add_argument("--min-abs-ms", type=float, default=5.0)
    a = ap.parse_args(argv)
    if bool(a.save) == bool(a.compare):
        ap.error("give exactly one of --save FILE (baseline, structure off) or --compare FILE (structure on)")
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    m = measure(a.url, syms, a.rounds, a.timeout, get)
    if a.save:
        with open(a.save, "w") as fh:
            json.dump(m, fh, indent=2)
        print(f"baseline saved to {a.save}: {json.dumps(m)}")
        return 0 if m["n"] >= 1 else 1
    with open(a.compare) as fh:
        base = json.load(fh)
    v = verdict(m, base, a.max_increase_pct, a.min_abs_ms)
    print(f"now: {json.dumps(m)}\nbaseline: {json.dumps(base)}\nG1 live latency: {v['status']} - {v['reason']}")
    return 0 if v["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
