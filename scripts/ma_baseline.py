#!/usr/bin/env python3
"""ma_baseline.py - B16: the dumb baseline every rule must beat. Offline only, never touches live trading.

  python3 scripts/ma_baseline.py HIST_DIR [--out DIR] [--fast 20] [--slow 50] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10]
                                 [--step 3] [--boot 500] [--jobs 1] [--thresholds '{}'] [--sample 400 --seed 7]

Dumb strategy: buy the OPEN of the session after the fast SMA closes above the slow SMA (a fresh crossover), hold T+h, same costs and slippage as
scripts/ms_ab_report.py. It uses no structure, news or ML. Three groups are compared on the same history and the same net-return definition:
  ma_cross   every crossover entry (the dumb baseline)
  baseline   the A/B technical-only proxy days (above EMA20, not extended)
  candidate  baseline minus the days a structure rule rejects (the live gate)
Edge vs dumb = candidate mean net - ma_cross mean net (pp), with a 90 % cluster bootstrap (symbol x month blocks, seed 7) over both groups.
Verdict: insufficient_sample (fewer than 15 candidate or ma_cross entries, the B15 floor) / beats_dumb (CI lower bound > 0 and candidate mean net > 0) /
worse_than_dumb (CI upper bound < 0) / no_better (anything else: the rule has not been shown to beat a plain MA crossover).
Caveat: the groups cover different days, so this is an unpaired comparison, not a proof. Output: ma_baseline.json + ma_baseline.md in --out (default HIST_DIR/ma_out).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ms_ab_report as ab  # noqa: E402
from market_structure.backtest import HORIZON_MAX, HORIZONS, forward_outcome  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from market_structure.ms_types import C, O, T, finite  # noqa: E402

HARD_MIN_N = ab.HARD_MIN_N


def sma(closes: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= n:
            run -= closes[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def cross_rows(bars: Sequence[Sequence[float]], sym: str, fast: int, slow: int, cost: float, slip: float) -> List[Dict[str, Any]]:
    """One row per fresh golden cross (fast <= slow yesterday, fast > slow today). Entry = next open + slippage, outcomes net of cost, complete 20-session outcome only."""
    if fast >= slow or len(bars) < slow + HORIZON_MAX + 2:
        return []
    cl = [float(b[C]) for b in bars]
    f, s = sma(cl, fast), sma(cl, slow)
    rows: List[Dict[str, Any]] = []
    for i in range(slow, len(bars) - 1):
        if None in (f[i], s[i], f[i - 1], s[i - 1]) or not (f[i - 1] <= s[i - 1] and f[i] > s[i]):   # type: ignore[operator]
            continue
        op = float(bars[i + 1][O])
        if not finite(op) or op <= 0:
            continue
        entry = op * (1.0 + slip / 100.0)
        oc = forward_outcome(entry, None, None, bars[i + 1:])
        if oc["ret_20d"] is None:
            continue
        row: Dict[str, Any] = {"sym": sym, "ts": bars[i][T], "month": ist_date(bars[i][T])[:7], "rej": []}
        for h in HORIZONS:
            v = oc[f"ret_{h}d"]
            row[f"r{h}"] = round(v - cost, 3) if v is not None else None
        rows.append(row)
    return rows


def job(args: Tuple[str, str, int, Dict[str, Any], float, float, int, int]) -> Dict[str, Any]:
    path, sym, step, th, cost, slip, fast, slow = args
    try:
        bars = ab.load_bars(Path(path))
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "rows": [], "ma": [], "err": str(e)[:120]}
    return {"sym": sym, "rows": ab._rows(bars, sym, step, th, cost, slip, ab._BENCH), "ma": cross_rows(bars, sym, fast, slow, cost, slip), "err": None}


def _group(rows: Sequence[Dict[str, Any]], h: int) -> Dict[str, Any]:
    v = [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]
    if not v:
        return {"n": 0}
    g, l_ = sum(x for x in v if x > 0), -sum(x for x in v if x < 0)
    return {"n": len(v), "mean_net": round(mean(v), 3), "median_net": round(median(v), 3), "win_pct": round(100.0 * sum(1 for x in v if x > 0) / len(v), 1),
            "profit_factor": round(g / l_, 2) if l_ > 0 else None}


def boot_diff(a_rows: Sequence[Dict[str, Any]], b_rows: Sequence[Dict[str, Any]], h: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """Cluster bootstrap of mean(a) - mean(b); clusters are (symbol, month) blocks drawn from the union so shared market phases move both groups together."""
    cl: Dict[Tuple[str, str], List[float]] = {}
    for k, rows in ((0, a_rows), (1, b_rows)):
        for r in rows:
            v = r.get(f"r{h}")
            if v is None:
                continue
            c = cl.setdefault((r["sym"], r["month"]), [0, 0.0, 0, 0.0])
            c[2 * k] += 1
            c[2 * k + 1] += v
    arr = list(cl.values())
    out: Dict[str, Any] = {"clusters": len(arr), "ci90": None, "p_edge_positive": None}
    if len(arr) < 20 or n_boot < 50:
        return out
    rng = random.Random(seed)
    m = len(arr)
    d: List[float] = []
    for _ in range(n_boot):
        na = sa = nb = sb = 0
        for k in rng.choices(range(m), k=m):
            c = arr[k]
            na, sa, nb, sb = na + c[0], sa + c[1], nb + c[2], sb + c[3]
        if na and nb:
            d.append(sa / na - sb / nb)
    if len(d) < n_boot // 2:
        return out
    d.sort()
    out["ci90"] = [round(d[int(0.05 * (len(d) - 1))], 3), round(d[int(0.95 * (len(d) - 1))], 3)]
    out["p_edge_positive"] = round(sum(1 for x in d if x > 0) / len(d), 3)
    return out


def verdict(cand_n: int, ma_n: int, ci: Optional[List[float]], cand_mean: Optional[float]) -> str:
    if cand_n == 0 and ma_n == 0:
        return "no_data"
    if cand_n < HARD_MIN_N or ma_n < HARD_MIN_N or ci is None:
        return "insufficient_sample"
    if ci[0] > 0 and cand_mean is not None and cand_mean > 0:
        return "beats_dumb"
    if ci[1] < 0:
        return "worse_than_dumb"
    return "no_better"


def build(rows: List[Dict[str, Any]], ma: List[Dict[str, Any]], horizon: int, n_boot: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    cand = [r for r in rows if not r["rej"]]
    g = {"ma_cross": _group(ma, horizon), "baseline": _group(rows, horizon), "candidate": _group(cand, horizon)}
    boot = boot_diff(cand, ma, horizon, n_boot)
    cm, mm = g["candidate"].get("mean_net"), g["ma_cross"].get("mean_net")
    edge = round(cm - mm, 3) if cm is not None and mm is not None else None
    by_h = {str(h): {k: _group(v, h).get("mean_net") for k, v in (("ma_cross", ma), ("baseline", rows), ("candidate", cand))} for h in HORIZONS}
    return {"meta": meta, "horizon": horizon, "groups": g, "edge_vs_dumb_pp": edge, "bootstrap": boot, "by_horizon": by_h,
            "verdict": verdict(len(cand), len(ma), boot["ci90"], cm)}


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    m = rep["meta"]
    L_ = [f"# MA-crossover baseline (T+{rep['horizon']}, net of costs)", "", f"**Verdict: `{rep['verdict']}`**", "",
          f"- dumb baseline: SMA{m['fast']} crosses above SMA{m['slow']}, buy next open; {m['symbols']} symbols (skipped {m['skipped']}), cost {m['cost_pct']}% + {m['slip_pct']}% slippage",
          f"- edge of the gate over the dumb baseline: **{f(rep['edge_vs_dumb_pp'])} pp**, 90% bootstrap CI {f(rep['bootstrap']['ci90'])}, P(edge>0) {f(rep['bootstrap']['p_edge_positive'])}, clusters {rep['bootstrap']['clusters']}", "",
          "| group | n | mean net % | median % | win % | profit factor |", "|---|---|---|---|---|---|"]
    for k, x in rep["groups"].items():
        L_.append(f"| {k} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('median_net'))} | {f(x.get('win_pct'))} | {f(x.get('profit_factor'))} |")
    L_ += ["", "## Mean net % by horizon", "", "| T+ | ma_cross | baseline | candidate |", "|---|---|---|---|"]
    for h, x in rep["by_horizon"].items():
        L_.append(f"| {h} | {f(x['ma_cross'])} | {f(x['baseline'])} | {f(x['candidate'])} |")
    L_ += ["", f"A rule that cannot beat this plain crossover (`no_better` / `worse_than_dumb`) has not earned its complexity. `insufficient_sample` (under {HARD_MIN_N} entries) means nothing may be concluded. "
               "Unpaired comparison: the groups trade different days."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=50)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    if a.fast < 2 or a.slow <= a.fast:
        print("need 2 <= --fast < --slow", file=sys.stderr)
        return 2
    d = Path(a.dir)
    th = json.loads(a.thresholds)
    files = sorted(d.glob("*.json"))
    bench = None
    for fl in files:
        if fl.stem.upper() in ab.BENCH_NAMES:
            try:
                bb = ab.load_bars(fl)
                if len(bb) >= 220:
                    bench = ab.bench_info(bb)
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {fl.name} unreadable: {e}", file=sys.stderr)
            break
    stock_files = [fl for fl in files if fl.stem.upper() not in ab.BENCH_NAMES]
    if a.sample > 0 and len(stock_files) > a.sample:
        stock_files = sorted(random.Random(a.seed).sample(stock_files, a.sample))
    jobs = [(str(fl), fl.stem.upper(), a.step, th, a.cost_pct, a.slip_pct, a.fast, a.slow) for fl in stock_files]
    from _progress import run_jobs
    results = run_jobs(job, jobs, max(1, a.jobs), "ma_baseline", chunksize=2, ckpt="", resume=False, stamp=lambda j: None,
                       initializer=ab._init_pool, initargs=(bench,))
    rows: List[Dict[str, Any]] = []
    ma: List[Dict[str, Any]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
        ma += res["ma"]
    meta = {"fast": a.fast, "slow": a.slow, "symbols": len(jobs) - skipped, "skipped": skipped, "cost_pct": a.cost_pct, "slip_pct": a.slip_pct}
    rep = build(rows, ma, a.horizon, a.boot, meta)
    out = Path(a.out) if a.out else d / "ma_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ma_baseline.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "ma_baseline.md").write_text(md)
    print(md)
    return 0 if (rows or ma) else 1


if __name__ == "__main__":
    sys.exit(main())
