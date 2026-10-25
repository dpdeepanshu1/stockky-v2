#!/usr/bin/env python3
"""swing_snapshot.py - E132 (swing plan S3a): save today's momentum ranking to a file so its forward returns can be measured later. READ-ONLY.

  TOKEN=<admin session token> python3 scripts/swing_snapshot.py --hist-dir HIST_DIR --out-dir DIR [--rt-url http://127.0.0.1:8005] [--chunk 150] [--benchmark SYMBOL]

Run once per trading day after the close (the service's own evening scan, SWING_MODE shadow/live, already stores the same rankings; this script is the independent
archive and works with any service version that has GET /swing/rank). It lists the symbols of HIST_DIR (the same folder the replays use), asks real-trade-service
GET /swing/rank in chunks (a chunk is ranked on its own there, so the rows are re-ranked here over the WHOLE list with the same swing/momentum.py) and saves one compact
file per bar date: DIR/swing_snap_YYYY-MM-DD.json (a second run for the same bar date overwrites it). scripts/swing_shadow_report.py turns the saved days into forward returns.
Nothing is ordered, no switch is read or changed. Exit 0 = saved; 1 = nothing usable (the reason is printed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "services" / "real-trade-service"))

from ms_ab_report import BENCH_NAMES  # noqa: E402
from swing import momentum  # noqa: E402

KEEP = ("symbol", "status", "last_close", "last_open", "last_high", "last_low", "last_t", "ret_12_1", "ret_6_1", "mr_12", "mr_6", "above_sma200",
        "dist_52w_high_pct", "adx14", "atr14_pct", "sma10", "rank", "pct_rank", "screen")
STALE = ("rank", "pct_rank", "screen", "z12", "z6", "norm_score")


def symbols_from_hist(folder: Path) -> List[str]:
    return sorted({f.stem.upper() for f in Path(folder).glob("*.json") if f.stem.upper() not in BENCH_NAMES})


def _get(url: str, token: str, timeout: float = 180.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return json.loads(r.read().decode())


def fetch_chunk(rt_url: str, token: str, symbols: Sequence[str], benchmark: str = "") -> Dict[str, Any]:
    q = {"symbols": ",".join(symbols), "limit": "200"}
    if benchmark:
        q["benchmark"] = benchmark
    doc = _get(rt_url.rstrip("/") + "/swing/rank?" + urllib.parse.urlencode(q), token)
    if not isinstance(doc, dict) or doc.get("error"):
        raise RuntimeError(str((doc or {}).get("error") if isinstance(doc, dict) else "bad answer")[:120])
    return doc


def merge_and_rank(chunk_rows: Sequence[Dict[str, Any]], min_pct_rank: float = 80.0) -> List[Dict[str, Any]]:
    """Rows of every chunk, chunk-level ranks dropped, ranked once over the whole list."""
    rows = []
    for r in chunk_rows:
        d = {k: v for k, v in dict(r).items() if k not in STALE}
        d["reasons"] = []
        rows.append(d)
    ranked = momentum.rank_scores(rows, min_pct_rank=min_pct_rank)
    return [{k: r.get(k) for k in KEEP} for r in ranked]


def build(rt_url: str, token: str, symbols: Sequence[str], chunk: int = 150, benchmark: str = "", pause_s: float = 0.0,
          fetch=fetch_chunk) -> Dict[str, Any]:
    out: Dict[str, Any] = {"read_only": True, "n_requested": len(symbols), "n_chunks": 0, "failed_chunks": [], "failed_symbols": [], "asof_bar_date": None, "rows": []}
    rows: List[Dict[str, Any]] = []
    asofs: Counter = Counter()
    for i in range(0, len(symbols), max(1, chunk)):
        part = list(symbols[i: i + chunk])
        try:
            doc = fetch(rt_url, token, part, benchmark)
        except Exception as e:  # noqa: BLE001 - one bad chunk must not lose the others
            out["failed_chunks"].append({"first": part[0], "n": len(part), "reason": str(e)[:100]})
            continue
        out["n_chunks"] += 1
        rows += doc.get("rows") or []
        out["failed_symbols"] += [f.get("symbol") for f in (doc.get("failed") or []) if isinstance(f, dict)]
        if doc.get("asof_bar_date"):
            asofs[doc["asof_bar_date"]] += 1
        if pause_s:
            time.sleep(pause_s)
    if asofs:
        out["asof_bar_date"] = max(asofs)                       # the newest bar date any chunk saw
    out["rows"] = merge_and_rank(rows) if rows else []
    out["n_ranked"] = sum(1 for r in out["rows"] if r.get("rank") is not None)
    out["n_screen"] = sum(1 for r in out["rows"] if r.get("screen"))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hist-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--rt-url", default=os.getenv("REAL_TRADE_URL", "http://127.0.0.1:8005"))
    ap.add_argument("--chunk", type=int, default=150)
    ap.add_argument("--benchmark", default="")
    a = ap.parse_args(argv)
    syms = symbols_from_hist(Path(a.hist_dir))
    if not syms:
        print(f"NO SYMBOLS: {a.hist_dir} has no symbol files", file=sys.stderr)
        return 1
    snap = build(a.rt_url, os.getenv("TOKEN", ""), syms, a.chunk, a.benchmark)
    for f in snap["failed_chunks"]:
        print(f"chunk starting {f['first']} ({f['n']} symbols) failed: {f['reason']}", file=sys.stderr)
    if not snap["asof_bar_date"] or snap["n_ranked"] < 5:
        print(f"NOTHING SAVED: ranked {snap['n_ranked']} symbols, bar date {snap['asof_bar_date']}", file=sys.stderr)
        return 1
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"swing_snap_{snap['asof_bar_date']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, separators=(",", ":")))
    os.replace(tmp, path)
    print(f"saved {path.name}: {snap['n_ranked']} ranked of {snap['n_requested']} requested, {snap['n_screen']} on the screen, {len(snap['failed_symbols'])} without data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
