#!/usr/bin/env python3
"""legacy_gate_backtest.py - group380 (E3 tooling): did each legacy real-trade candidate gate remove the WORSE entries, after costs?

  python3 scripts/legacy_gate_backtest.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10] [--min-n 200]
                                         [--boot 500] [--sample 400 --seed 7]

HIST_DIR is the same folder of per-symbol JSON candles that scripts/fetch_history_for_backtest.py writes (the `^NSEI` benchmark file is skipped).
For every replayed day (every --step sessions, 260+ bars of history) the daily-bar form of each gate in real-trade-service/candidate_engine/candidates.py
is evaluated on bars[:i+1] only (no lookahead), then the day is scored as a trade: entry = OPEN of the next session (+ slippage), exit = close of session
T+h (the entry session counts as 1), round-trip cost subtracted. Gates replayed:
  downtrend_6m               6-month return < -10%                                  (check 2)
  range_52w_legacy           price in the top 12% of the 52-week range              (check 4 as it was before group 379)
  range_52w_with_exemption   the same, minus a fresh volume-confirmed 52-week high   (check 4 as of group 379, regime gate NOT replayed)
  resistance_legacy          close within 2% of the 20-day high                     (check 7 as it was before group 379)
  resistance_with_exemption  the same, minus a fresh volume-confirmed 52-week high   (check 7 as of group 379)
  volume_health              5-day average volume < 0.80 x 20-day average          (check 6)
  exemption_test             among days the legacy 52-week rule rejects: the exempted fresh highs vs the ones still rejected
Edge = mean net return of the days the gate lets through - mean net return of the days it rejects (percentage points). Positive = the gate removed the
worse days. For exemption_test the "rejected" side is the days that stay rejected, so a positive edge means the exempted days did better.
Uncertainty: cluster bootstrap (symbol x month), seed 7. Verdict per gate: insufficient_sample (fewer than --min-n days on either side),
helps (5th percentile of the edge > 0), hurts (95th percentile < 0), no_edge (anything else).

READ THIS BEFORE TRUSTING IT: every day of every sampled stock is replayed, NOT only the days a live candidate source (hot picks / IPO / surprise / volume
shock) would have proposed, so a gate that looks useful here may do nothing on the real candidate population and the reverse. The market-regime part of the
52-week rule (market score) cannot be replayed from daily candles; it is left out. Volume uses the exchange daily volume in the file. Stdlib only.
Output: gate_report.json + gate_report.md in --out (default HIST_DIR/gate_out).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_structure.ms_types import C, H, L, O, T, V  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from ms_ab_report import BENCH_NAMES, load_bars  # noqa: E402

HORIZONS = (1, 5, 10, 20)
MIN_BARS = 260
LOOKBACK_52W = 252
TOP_PCT = 12.0              # CANDIDATE_OVEREXTENDED_52W_TOP_PCT
RES_PCT = 2.0               # _near_resistance: within 2% of the 20-day high
DOWNTREND_6M = -10.0        # CANDIDATE_DOWNTREND_6M_PCT
VOL_RATIO_MIN = 0.80        # CANDIDATE_VOLUME_HEALTH_RATIO
FRESH_PCT, FRESH_DAYS, FRESH_VOL = 1.0, 10.0, 1.2   # CANDIDATE_52W_FRESH_HIGH_*

GATES: List[Tuple[str, Any, Any]] = [
    ("downtrend_6m", lambda r: True, lambda r: r["down"]),
    ("range_52w_legacy", lambda r: True, lambda r: r["r52"]),
    ("range_52w_with_exemption", lambda r: True, lambda r: r["r52"] and not r["fresh"]),
    ("resistance_legacy", lambda r: True, lambda r: r["res"]),
    ("resistance_with_exemption", lambda r: True, lambda r: r["res"] and not r["fresh"]),
    ("volume_health", lambda r: True, lambda r: r["vol"]),
    ("exemption_test", lambda r: r["r52"], lambda r: not r["fresh"]),
]


def day_flags(bars: Sequence[Sequence[float]], i: int) -> Optional[Dict[str, bool]]:
    """Gate verdicts for the session ending at bars[i], from bars[:i+1] only. None when there is too little history."""
    if i < LOOKBACK_52W - 1 or i < 126:
        return None
    px = float(bars[i][C])
    if px <= 0:
        return None
    win = bars[i - LOOKBACK_52W + 1:i + 1]
    highs = [float(b[H]) for b in win if float(b[H]) > 0]
    lows = [float(b[L]) for b in win if float(b[L]) > 0]
    if not highs or not lows:
        return None
    h52, l52 = max(highs), min(lows)
    r52 = h52 > l52 and (px - l52) / (h52 - l52) * 100.0 > (100.0 - TOP_PCT)
    # fresh, volume-confirmed 52-week high (mirrors candidates._fresh_high_breakout)
    k_hi = max(range(len(win)), key=lambda k: float(win[k][H]))
    age_days = (float(bars[i][T]) - float(win[k_hi][T])) / 86400.0
    vols = [float(b[V]) for b in bars[i - 19:i + 1] if float(b[V]) > 0]
    ratio = None
    if len(vols) >= 10:
        a20 = sum(vols) / len(vols)
        ratio = (sum(vols[-5:]) / len(vols[-5:])) / a20 if a20 > 0 else None
    fresh = bool(px >= h52 * (1 - FRESH_PCT / 100.0) and 0 <= age_days <= FRESH_DAYS and ratio is not None and ratio >= FRESH_VOL)
    hi20 = max(float(b[H]) for b in bars[i - 19:i + 1])
    o126 = float(bars[i - 125][O])
    return {
        "down": o126 > 0 and (px / o126 - 1.0) * 100.0 < DOWNTREND_6M,
        "r52": bool(r52), "fresh": fresh,
        "res": hi20 > 0 and px >= hi20 * (1.0 - RES_PCT / 100.0),
        "vol": ratio is not None and ratio < VOL_RATIO_MIN,
    }


def rows_for(bars: Sequence[Sequence[float]], sym: str, step: int, cost: float, slip: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if len(bars) < MIN_BARS + max(HORIZONS) + 2:
        return out
    for i in range(MIN_BARS - 1, len(bars) - max(HORIZONS) - 1, max(1, step)):
        f = day_flags(bars, i)
        entry_open = float(bars[i + 1][O])
        if f is None or entry_open <= 0:
            continue
        entry = entry_open * (1.0 + slip / 100.0)
        row: Dict[str, Any] = {"sym": sym, "month": ist_date(bars[i][T])[:7], **f}
        for h in HORIZONS:
            row[f"r{h}"] = round((float(bars[i + h][C]) / entry - 1.0) * 100.0 - cost, 3)
        out.append(row)
    return out


def _job(a: Tuple[str, str, int, float, float]) -> Dict[str, Any]:
    path, sym, step, cost, slip = a
    try:
        return {"sym": sym, "err": None, "rows": rows_for(load_bars(Path(path)), sym, step, cost, slip)}
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "err": f"{type(e).__name__}: {e}", "rows": []}


def gate_report(rows: Sequence[Dict[str, Any]], h: int, min_n: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, pop, rej in GATES:
        pr = [r for r in rows if pop(r)]
        rj = [r for r in pr if rej(r)]
        ps = [r for r in pr if not rej(r)]
        key = f"r{h}"
        mp, mr = (mean(r[key] for r in ps) if ps else None), (mean(r[key] for r in rj) if rj else None)
        edge = round(mp - mr, 3) if (mp is not None and mr is not None) else None
        ci = _bootstrap(pr, rej, key, n_boot, seed) if edge is not None else None
        if len(ps) < min_n or len(rj) < min_n:
            v = "insufficient_sample"
        elif ci and ci[0] > 0:
            v = "helps"
        elif ci and ci[1] < 0:
            v = "hurts"
        else:
            v = "no_edge"
        out[name] = {"population": len(pr), "rejected": len(rj), "passed": len(ps),
                     "rejected_pct": round(100.0 * len(rj) / len(pr), 1) if pr else None,
                     "mean_net_passed": round(mp, 3) if mp is not None else None,
                     "mean_net_rejected": round(mr, 3) if mr is not None else None,
                     "edge_pp": edge, "ci90": ci, "verdict": v}
    return out


def _bootstrap(pop_rows: Sequence[Dict[str, Any]], rej, key: str, n_boot: int, seed: int) -> Optional[List[float]]:
    cl: Dict[Tuple[str, str], List[float]] = {}
    for r in pop_rows:
        a = cl.setdefault((r["sym"], r["month"]), [0, 0.0, 0, 0.0])   # n passed, sum passed, n rejected, sum rejected
        if rej(r):
            a[2] += 1
            a[3] += r[key]
        else:
            a[0] += 1
            a[1] += r[key]
    arr = list(cl.values())
    if len(arr) < 20 or n_boot < 50:
        return None
    rng = random.Random(seed)
    m = len(arr)
    edges: List[float] = []
    for _ in range(n_boot):
        np_ = sp = nr = sr = 0
        for k in rng.choices(range(m), k=m):
            a = arr[k]
            np_, sp, nr, sr = np_ + a[0], sp + a[1], nr + a[2], sr + a[3]
        if np_ and nr:
            edges.append(sp / np_ - sr / nr)
    if len(edges) < n_boot // 2:
        return None
    edges.sort()
    return [round(edges[int(0.05 * (len(edges) - 1))], 3), round(edges[int(0.95 * (len(edges) - 1))], 3)]


def to_markdown(rep: Dict[str, Any]) -> str:
    m = rep["meta"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L_ = [f"# Legacy candidate gates, replayed on daily history (T+{rep['horizon']}, net of costs)", "",
          f"- symbols: {m['symbols']} (skipped {m['skipped']}), step {m['step']}, cost {m['cost_pct']}% round trip + {m['slip_pct']}% slippage, days replayed {rep['days']}",
          *([f"- sample: {m['sample']['drawn']} of {m['sample']['pool']} symbols, random seed {m['sample']['seed']}"] if m["sample"]["seed"] is not None else []), "",
          "| gate | days | rejected % | passed mean % | rejected mean % | edge pp | 90% CI | verdict |", "|---|---|---|---|---|---|---|---|"]
    for k, x in rep["gates"].items():
        L_.append(f"| {k} | {x['population']} | {f(x['rejected_pct'])} | {f(x['mean_net_passed'])} | {f(x['mean_net_rejected'])} | {f(x['edge_pp'])} | {f(x['ci90'])} | {x['verdict']} |")
    L_ += ["", "Edge = mean of the days the gate lets through minus mean of the days it rejects; positive = the gate removed the worse days (for `exemption_test`: the "
           "exempted fresh highs did better than the ones still rejected). Every day of every sampled stock is replayed, not only the days a live candidate source would "
           "have proposed, and the market-score part of the 52-week rule is not replayed. `insufficient_sample` means nothing may be concluded."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--min-n", type=int, default=200)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    files = [f for f in sorted(d.glob("*.json")) if f.stem.upper() not in BENCH_NAMES]
    pool = len(files)
    if a.sample > 0 and pool > a.sample:
        files = sorted(random.Random(a.seed).sample(files, a.sample))
    jobs = [(str(f), f.stem.upper(), a.step, a.cost_pct, a.slip_pct) for f in files]
    from _progress import run_jobs                       # group427: a progress line so a long run does not look hung
    results = run_jobs(_job, jobs, a.jobs, "legacy_gate")
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "step": a.step, "cost_pct": a.cost_pct, "slip_pct": a.slip_pct,
            "sample": {"requested": a.sample, "pool": pool, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep = {"meta": meta, "horizon": a.horizon, "days": len(rows), "gates": gate_report(rows, a.horizon, a.min_n, a.boot)}
    out = Path(a.out) if a.out else d / "gate_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate_report.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "gate_report.md").write_text(md)
    print(md)
    if not rows:
        print("NO DAYS: the history folder needs symbol files with 280+ daily candles", file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
