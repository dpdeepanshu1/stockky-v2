#!/usr/bin/env python3
"""ms_ab_report.py - WS-7 / gate G2: does the daily-structure policy beat a technical-only baseline, out of sample, after costs?

  python3 scripts/ms_ab_report.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10] [--min-n 200]
                                  [--jobs 1] [--boot 500] [--thresholds '{}'] [--sample 400 --seed 7]

HIST_DIR: one JSON per symbol (market-data /history reply or a bare candle list; see scripts/fetch_history_for_backtest.py). A file named
`^NSEI.json` (or NIFTY.json) is the benchmark: it is NOT traded, it tags regimes (bull/bear by 200-day SMA, high/low volatility by ATR% against its own
trailing median) and gives NIFTY-relative returns.

A/B on IDENTICAL days:
  baseline  = every replayed day where the stock is above its EMA20 but not extended (0 <= stretch <= 3 ATR) - a technical-only proxy, because
              historical news / fundamentals are not archived point-in-time, so the full decision cannot be replayed.
  candidate = baseline minus the days any structure rule rejects (chase / stretch / room / R:R / wide stop / bearish), same thresholds as the gates.
Entry = OPEN of the next session (+ slippage); T+h counts the entry session as 1; the round-trip cost is subtracted from every return; a bar that
touches both the structural stop and target counts as a STOP. The engine only ever sees bars[:i+1] (replay), so there is no lookahead.
Thresholds are the live defaults (nothing is tuned here), so the whole history is out of sample for them; stability is checked across the two halves
of the history and across market regimes. Tuning stays in scripts/structure_backtest.py (walk-forward).

Edge = candidate mean net return - baseline mean net return (percentage points). Its uncertainty is a cluster bootstrap (symbol x month blocks, because
neighbouring days of one stock are not independent; seed 7). Verdict:
  no_data              no signals at all (empty folder, missing history) - fix the download, do not read the tables
  insufficient_sample  candidate < --min-n signals, or baseline < --min-n, with a complete 20-session outcome  (nothing is promoted)
  pass                 edge > 0 with the bootstrap 5th percentile > 0, both halves of history positive, and the candidate mean net return > 0
  hurts                bootstrap 95th percentile of the edge < 0 (the rules removed the better entries)
  no_edge              anything else
--sample N replays only N symbols drawn at random (seeded) from the sorted symbol files in HIST_DIR (the benchmark is never drawn): download a wide
universe once, then run several seeds to see whether the verdict depends on WHICH 400 stocks were drawn. The seed, the drawn count and the pool size are
written to the report. Without --sample every file is replayed.
Output: ab_report.json + ab_report.md in --out (default HIST_DIR/ab_out). Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from market_structure.atr import atr_series  # noqa: E402
from market_structure.backtest import HORIZON_MAX, HORIZONS, forward_outcome, rule_flags  # noqa: E402
from market_structure.candles import candles_to_bars, day_key, ist_date  # noqa: E402
from market_structure.ms_types import C, O, T, finite  # noqa: E402
from market_structure.replay import replay  # noqa: E402

BENCH_NAMES = ("^NSEI", "NIFTY", "NSEI", "NIFTY50")
RULES = ("chase", "stretch", "room", "rr", "stop_wide", "bearish")
MIN_BARS = 120


def load_bars(path: Path) -> List[Tuple[float, ...]]:
    doc = json.loads(path.read_text())
    return candles_to_bars(doc.get("candles") if isinstance(doc, dict) else doc)


def bench_info(bars: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """Per benchmark day: day key, open, close, bull flag (close > SMA200) and high-vol flag (ATR% above its own trailing 250-day median)."""
    n = len(bars)
    keys = [day_key(b[T]) for b in bars]
    atr = atr_series(bars, 14)
    atrp = [(atr[i] / bars[i][C] * 100.0) if atr[i] is not None and bars[i][C] else None for i in range(n)]
    bull: List[Optional[bool]] = []
    hv: List[Optional[bool]] = []
    for i in range(n):
        bull.append(bars[i][C] > sum(b[C] for b in bars[i - 199: i + 1]) / 200.0 if i >= 199 else None)
        hist = [x for x in atrp[max(0, i - 250): i] if x is not None]
        hv.append(atrp[i] > median(hist) if atrp[i] is not None and len(hist) >= 60 else None)
    return {"keys": keys, "open": [b[O] for b in bars], "close": [b[C] for b in bars], "bull": bull, "hv": hv}


def _pos_le(keys: List[int], k: int) -> int:
    return bisect.bisect_right(keys, k) - 1


def stock_signals(args: Tuple[str, str, int, Dict[str, Any], float, float]) -> Dict[str, Any]:
    """One symbol -> compact signal rows (only days that pass the baseline filter). Top-level so a process pool can run it."""
    path, sym, step, th, cost, slip = args
    try:
        bars = load_bars(Path(path))
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "rows": [], "err": str(e)[:120]}
    return {"sym": sym, "rows": _rows(bars, sym, step, th, cost, slip, _BENCH), "err": None}


_BENCH: Optional[Dict[str, Any]] = None


def _init_pool(bench: Optional[Dict[str, Any]]) -> None:
    global _BENCH
    _BENCH = bench


def _rows(bars: Sequence[Sequence[float]], sym: str, step: int, th: Dict[str, Any], cost: float, slip: float,
          bench: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(bars) < MIN_BARS + HORIZON_MAX + 2:
        return []
    rows: List[Dict[str, Any]] = []
    bk = bench["keys"] if bench else None
    for r in replay(bars, step=step):
        i = r["i"]
        if r["quality"] not in ("ok", "limited") or i + 1 >= len(bars):
            continue
        s = r["summary"]
        st = s.get("ms_stretch_atr")
        if not finite(st) or not (0.0 <= float(st) <= 3.0):  # baseline filter: above EMA20, not extended
            continue
        entry_open = float(bars[i + 1][O])
        if not finite(entry_open) or entry_open <= 0:
            continue
        entry = entry_open * (1.0 + slip / 100.0)
        oc = forward_outcome(entry, s.get("ms_stop"), s.get("ms_target"), bars[i + 1:])
        if oc["ret_20d"] is None:  # only complete 20-session outcomes, so every horizon uses the same days
            continue
        flags = rule_flags(s, th)
        row: Dict[str, Any] = {
            "sym": sym, "ts": bars[i][T], "month": ist_date(bars[i][T])[:7], "rej": [k for k, v in flags.items() if v],
            "stop_first": oc["first_touch"] == "stop", "target_first": oc["first_touch"] == "target", "mae": oc["mae_pct"],
            "bull": None, "hv": None}
        for h in HORIZONS:
            v = oc[f"ret_{h}d"]
            row[f"r{h}"] = round(v - cost, 3) if v is not None else None
            row[f"x{h}"] = None
        if bench:
            k = day_key(bars[i][T])
            j = _pos_le(bk, k)
            if j >= 0:
                row["bull"], row["hv"] = bench["bull"][j], bench["hv"][j]
            e = bisect.bisect_left(bk, day_key(bars[i + 1][T]))
            if e < len(bk):
                for h in HORIZONS:
                    b2 = _pos_le(bk, day_key(bars[i + h][T]))
                    if b2 >= e and bench["open"][e] > 0 and row[f"r{h}"] is not None:
                        nifty = (bench["close"][b2] / bench["open"][e] - 1.0) * 100.0
                        row[f"x{h}"] = round(row[f"r{h}"] - nifty, 3)
        rows.append(row)
    return rows


def _stats(rows: Sequence[Dict[str, Any]], h: int) -> Dict[str, Any]:
    rets = [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]
    ex = [r[f"x{h}"] for r in rows if r.get(f"x{h}") is not None]
    if not rets:
        return {"n": 0}
    gain, loss = sum(x for x in rets if x > 0), -sum(x for x in rets if x < 0)
    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    return {"n": len(rets), "mean_net": round(mean(rets), 3), "median_net": round(median(rets), 3),
            "win_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1),
            "profit_factor": round(gain / loss, 2) if loss > 0 else None,
            "mean_vs_nifty": round(mean(ex), 3) if ex else None,
            "stop_first_pct": round(100.0 * sum(1 for r in rows if r["stop_first"]) / len(rows), 1),
            "target_first_pct": round(100.0 * sum(1 for r in rows if r["target_first"]) / len(rows), 1),
            "mean_mae_pct": round(mean(maes), 2) if maes else None}


def _edge(base: Sequence[Dict[str, Any]], cand: Sequence[Dict[str, Any]], h: int) -> Optional[float]:
    b = [r[f"r{h}"] for r in base if r.get(f"r{h}") is not None]
    c = [r[f"r{h}"] for r in cand if r.get(f"r{h}") is not None]
    return round(mean(c) - mean(b), 3) if b and c else None


def bootstrap_edge(base: Sequence[Dict[str, Any]], h: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """Cluster bootstrap (symbol x month) of mean(candidate) - mean(baseline). Candidate rows are the baseline rows with no rule tripped."""
    cl: Dict[Tuple[str, str], List[float]] = {}
    for r in base:
        v = r.get(f"r{h}")
        if v is None:
            continue
        a = cl.setdefault((r["sym"], r["month"]), [0, 0.0, 0, 0.0])
        a[0] += 1
        a[1] += v
        if not r["rej"]:
            a[2] += 1
            a[3] += v
    arr = list(cl.values())
    out: Dict[str, Any] = {"clusters": len(arr), "resamples": n_boot, "ci90": None, "p_edge_positive": None}
    if len(arr) < 20 or n_boot < 50:
        return out
    rng = random.Random(seed)
    m = len(arr)
    edges: List[float] = []
    for _ in range(n_boot):
        nb = sb = nc = sc = 0
        for k in rng.choices(range(m), k=m):
            a = arr[k]
            nb, sb, nc, sc = nb + a[0], sb + a[1], nc + a[2], sc + a[3]
        if nb and nc:
            edges.append(sc / nc - sb / nb)
    if len(edges) < n_boot // 2:
        return out
    edges.sort()
    out["ci90"] = [round(edges[int(0.05 * (len(edges) - 1))], 3), round(edges[int(0.95 * (len(edges) - 1))], 3)]
    out["p_edge_positive"] = round(sum(1 for e in edges if e > 0) / len(edges), 3)
    return out


def verdict(cand_n: int, base_n: int, edge: Optional[float], ci: Optional[List[float]], half_edges: List[Optional[float]],
            cand_mean: Optional[float], min_n: int) -> str:
    if base_n == 0:
        return "no_data"
    if cand_n < min_n or base_n < min_n or edge is None:
        return "insufficient_sample"
    if ci is not None and ci[1] < 0:
        return "hurts"
    halves_ok = len(half_edges) == 2 and all(e is not None and e > 0 for e in half_edges)
    if edge > 0 and ci is not None and ci[0] > 0 and halves_ok and cand_mean is not None and cand_mean > 0:
        return "pass"
    return "no_edge"


def build_report(rows: List[Dict[str, Any]], horizon: int, min_n: int, n_boot: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    base = rows
    cand = [r for r in rows if not r["rej"]]
    rej = [r for r in rows if r["rej"]]
    ts_sorted = sorted(r["ts"] for r in rows)
    cut = ts_sorted[len(ts_sorted) // 2] if ts_sorted else 0
    halves = [[r for r in base if r["ts"] < cut], [r for r in base if r["ts"] >= cut]]
    half_edges = [_edge(h_, [r for r in h_ if not r["rej"]], horizon) for h_ in halves]
    boot = bootstrap_edge(base, horizon, n_boot)
    edge = _edge(base, cand, horizon)
    cs = _stats(cand, horizon)
    rep: Dict[str, Any] = {
        "meta": meta, "horizon": horizon, "signals": {"baseline": len(base), "candidate": len(cand), "rejected": len(rej)},
        "kept_pct": round(100.0 * len(cand) / len(base), 1) if base else None,
        "baseline": _stats(base, horizon), "candidate": cs, "rejected": _stats(rej, horizon),
        "edge_pp": edge, "bootstrap": boot, "halves": {"first": half_edges[0], "second": half_edges[1], "cut": ist_date(cut) if rows else None},
        "by_horizon": {str(h): {"baseline": _stats(base, h), "candidate": _stats(cand, h), "edge_pp": _edge(base, cand, h)} for h in HORIZONS},
        "by_rule": {}, "by_regime": {}}
    for rule in RULES:
        only = [r for r in base if rule in r["rej"]]
        solo = [r for r in base if rule not in r["rej"]]  # gate made of this rule alone
        no_other = [r for r in base if not (set(r["rej"]) - {rule})]  # full gate minus this rule
        rep["by_rule"][rule] = {"rejected": _stats(only, horizon), "share_pct": round(100.0 * len(only) / len(base), 1) if base else None,
                                "solo_edge_pp": _edge(base, solo, horizon),
                                "drop_one_edge_pp": _edge(no_other, cand, horizon), "drop_one_n": len(no_other)}
    for name, pred in (("bull", lambda r: r["bull"] is True), ("bear", lambda r: r["bull"] is False),
                       ("high_vol", lambda r: r["hv"] is True), ("low_vol", lambda r: r["hv"] is False)):
        b2 = [r for r in base if pred(r)]
        c2 = [r for r in b2 if not r["rej"]]
        rep["by_regime"][name] = {"baseline_n": len(b2), "candidate_n": len(c2), "edge_pp": _edge(b2, c2, horizon),
                                  "candidate_mean_net": _stats(c2, horizon).get("mean_net")}
    # the report counts rows with a complete 20-session outcome, so base/cand sizes are the same for every horizon
    rep["verdict"] = verdict(len(cand), len(base), edge, boot.get("ci90"), half_edges, cs.get("mean_net"), min_n)
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    m, s = rep["meta"], rep["signals"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L_: List[str] = [f"# Structure policy A/B report (T+{rep['horizon']}, net of costs)", "",
                     f"**Verdict: `{rep['verdict']}`**", "",
                     f"- symbols: {m['symbols']} (skipped {m['skipped']}), benchmark: {m['benchmark']}, step {m['step']}, cost {m['cost_pct']}% round trip + {m['slip_pct']}% slippage",
                     *([f"- sample: {m['sample']['drawn']} of {m['sample']['pool']} symbols, random seed {m['sample']['seed']}"]
                       if (m.get("sample") or {}).get("seed") is not None else []),
                     f"- signals: baseline {s['baseline']}, candidate {s['candidate']} ({f(rep['kept_pct'])}% kept), rejected {s['rejected']}",
                     f"- edge (candidate - baseline mean net return): **{f(rep['edge_pp'])} pp**, 90% bootstrap CI {f(rep['bootstrap']['ci90'])}, "
                     f"P(edge>0) {f(rep['bootstrap']['p_edge_positive'])}, clusters {rep['bootstrap']['clusters']}",
                     f"- by history half (cut {f(rep['halves']['cut'])}): first {f(rep['halves']['first'])} pp, second {f(rep['halves']['second'])} pp", "",
                     "| group | n | mean net % | median % | win % | profit factor | vs NIFTY % | stop first % | target first % |", "|---|---|---|---|---|---|---|---|---|"]
    for g in ("baseline", "candidate", "rejected"):
        x = rep[g]
        L_.append(f"| {g} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('median_net'))} | {f(x.get('win_pct'))} | {f(x.get('profit_factor'))} | "
                  f"{f(x.get('mean_vs_nifty'))} | {f(x.get('stop_first_pct'))} | {f(x.get('target_first_pct'))} |")
    L_ += ["", "## By horizon", "", "| T+ | baseline mean | candidate mean | edge pp |", "|---|---|---|---|"]
    for h, x in rep["by_horizon"].items():
        L_.append(f"| {h} | {f(x['baseline'].get('mean_net'))} | {f(x['candidate'].get('mean_net'))} | {f(x['edge_pp'])} |")
    L_ += ["", "## Rejected by each rule (a day can trip several)", "",
           "solo edge = baseline filtered by this rule alone vs baseline. drop-one edge = full gate vs the gate without this rule (positive = the rule earns its place). "
           "Exploratory: six rules are compared, so a single positive number is weak evidence.", "",
           "| rule | share of baseline % | n | mean net % | solo edge pp | drop-one edge pp |", "|---|---|---|---|---|---|"]
    for k, x in rep["by_rule"].items():
        L_.append(f"| {k} | {f(x['share_pct'])} | {x['rejected'].get('n', 0)} | {f(x['rejected'].get('mean_net'))} | {f(x['solo_edge_pp'])} | {f(x['drop_one_edge_pp'])} |")
    L_ += ["", "## By market regime", "", "| regime | baseline n | candidate n | edge pp | candidate mean % |", "|---|---|---|---|---|"]
    for k, x in rep["by_regime"].items():
        L_.append(f"| {k} | {x['baseline_n']} | {x['candidate_n']} | {f(x['edge_pp'])} | {f(x['candidate_mean_net'])} |")
    L_ += ["", "Baseline is a technical-only proxy (no point-in-time news/fundamentals). Thresholds are untuned live defaults. A `pass` is evidence on this history, "
           "not a guarantee; `insufficient_sample` means nothing may be concluded."]
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
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--sample", type=int, default=0, help="replay N symbols drawn at random (seeded) from the files in HIST_DIR; 0 = all")
    ap.add_argument("--seed", type=int, default=7, help="seed for --sample (the bootstrap keeps its own fixed seed)")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    th = json.loads(a.thresholds)
    files = sorted(d.glob("*.json"))
    bench, bname = None, None
    for f in files:
        if f.stem.upper() in BENCH_NAMES:
            try:
                bb = load_bars(f)
                if len(bb) >= 220:
                    bench, bname = bench_info(bb), f.stem
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {f.name} unreadable: {e}", file=sys.stderr)
            break
    if bench is None:
        print("no usable benchmark (^NSEI.json): regimes and NIFTY-relative returns will be empty", file=sys.stderr)
    stock_files = [f for f in files if f.stem.upper() not in BENCH_NAMES]
    pool_size = len(stock_files)
    if a.sample > 0 and pool_size > a.sample:
        import random
        stock_files = sorted(random.Random(a.seed).sample(stock_files, a.sample))
    jobs = [(str(f), f.stem.upper(), a.step, th, a.cost_pct, a.slip_pct) for f in stock_files]
    rows: List[Dict[str, Any]] = []
    skipped = 0
    if a.jobs > 1:
        results = []
        with ProcessPoolExecutor(max_workers=a.jobs, initializer=_init_pool, initargs=(bench,)) as ex:
            for n, res in enumerate(ex.map(stock_signals, jobs, chunksize=2), 1):
                results.append(res)
                if n % 10 == 0 or n == len(jobs):
                    print(f"progress {n}/{len(jobs)} symbols", file=sys.stderr, flush=True)
    else:
        _init_pool(bench)
        results = [stock_signals(j) for j in jobs]
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "benchmark": bname or "none", "step": a.step,
            "cost_pct": a.cost_pct, "slip_pct": a.slip_pct, "thresholds": th,
            "sample": {"requested": a.sample, "pool": pool_size, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep = build_report(rows, a.horizon, a.min_n, a.boot, meta)
    out = Path(a.out) if a.out else d / "ab_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ab_report.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "ab_report.md").write_text(md)
    print(md)
    if not rows:
        print("NO SIGNALS: check that the history folder has symbol files with enough candles (and ^NSEI.json)", file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
