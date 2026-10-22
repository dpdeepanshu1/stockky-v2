#!/usr/bin/env python3
"""robustness.py - B14: is the structure gate's edge real, or luck / over-fitting?  Offline only: never imports or touches live trading.

  python3 scripts/robustness.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10]
                                [--perms 1000] [--sweep-pct 20] [--position-pct 10] [--jobs 1] [--thresholds '{}'] [--sample 400 --seed 7]

Replays HIST_DIR exactly like scripts/ms_ab_report.py (same rows, same costs, same next-open entry, no lookahead) and then asks three questions of the
candidate (baseline minus the days a structure rule rejects):

1. RANDOM-ENTRY BENCHMARK  Pick the same NUMBER of entries at random from the baseline days, keeping the calendar mix (same count per month, so a lucky
   market phase cannot fake an edge), `--perms` times. p_random = share of random draws whose mean net return is >= the candidate's. A small p (< 0.05)
   means the gate chooses better days than chance; a large p means a coin flip would have done as well.
2. TRADE-ORDER SHUFFLE  Take the candidate's net returns, size every trade at `--position-pct` of equity, compound them in the real order and in
   `--perms` shuffled orders. Final equity does not depend on order, but the drawdown does: this shows how bad a run of bad luck can get with the SAME trades
   (median / 95th-percentile max drawdown, share of shuffles that draw down more than 20 %, and whether the real order was luckier than the shuffles).
   Trades are treated as sequential (a simplification); at most 5000 are used (seeded sample, time order kept) to keep it fast.
3. PARAMETER SWEEP  Re-apply the gate with chase / stretch / room / R:R / bearish-BOS-bars each nudged by -/+ `--sweep-pct` % (one at a time), plus all
   tighter and all looser together, and recompute the edge. A real edge survives small nudges; one that flips sign or loses more than half its size
   is over-fitted to the exact numbers. The baseline (stretch 0-3 ATR) is fixed in the replay, so stretch cannot be loosened past 3.0 here.

Verdict: insufficient_sample (fewer than 15 candidate or baseline signals - the B15 floor, nothing is concluded) / robust (p_random < 0.05 AND sweep stable
AND candidate mean net > 0) / not_robust. Output: robustness.json + robustness.md in --out (default HIST_DIR/robust_out). Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ms_ab_report as ab  # noqa: E402
from market_structure.backtest import DEFAULT_THRESHOLDS, HORIZONS, rule_flags  # noqa: E402

HARD_MIN_N = ab.HARD_MIN_N
MAX_SHUFFLE_TRADES = 5000
SWEEP_KEYS = ("chase_atr", "stretch_atr", "room_atr", "rr", "bearish_bos_bars")
P_RANDOM_OK = 0.05
DD_LIMIT_PCT = 20.0


def _vals(rows: Sequence[Dict[str, Any]], h: int) -> List[float]:
    return [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]


def random_entry(base: Sequence[Dict[str, Any]], cand: Sequence[Dict[str, Any]], h: int, perms: int, seed: int = 7) -> Dict[str, Any]:
    """Mean net return of the candidate vs `perms` random same-size, same-month-mix draws from the baseline. Pure, deterministic."""
    out: Dict[str, Any] = {"n_candidate": 0, "candidate_mean": None, "random_mean": None, "random_p5": None, "random_p95": None,
                           "p_random": None, "perms": perms}
    cv = _vals(cand, h)
    if not cv or perms < 1:
        return out
    pools: Dict[str, List[float]] = {}
    for r in base:
        if r.get(f"r{h}") is not None:
            pools.setdefault(r["month"], []).append(r[f"r{h}"])
    need: Dict[str, int] = {}
    for r in cand:
        if r.get(f"r{h}") is not None:
            need[r["month"]] = need.get(r["month"], 0) + 1
    rng = random.Random(seed)
    cm = sum(cv) / len(cv)
    means: List[float] = []
    for _ in range(perms):
        tot, cnt = 0.0, 0
        for m, k in need.items():
            pool = pools.get(m) or []
            if not pool:
                continue
            pick = rng.sample(pool, k) if k <= len(pool) else rng.choices(pool, k=k)
            tot += sum(pick)
            cnt += k
        if cnt:
            means.append(tot / cnt)
    if not means:
        return out
    means.sort()
    ge = sum(1 for x in means if x >= cm - 1e-12)
    out.update(n_candidate=len(cv), candidate_mean=round(cm, 3), random_mean=round(sum(means) / len(means), 3),
               random_p5=round(means[int(0.05 * (len(means) - 1))], 3), random_p95=round(means[int(0.95 * (len(means) - 1))], 3),
               p_random=round((ge + 1) / (len(means) + 1), 4))     # +1 smoothing: never reports an impossible p of exactly 0
    return out


def _max_dd(rets: Sequence[float], frac: float) -> float:
    eq = peak = 1.0
    dd = 0.0
    for r in rets:
        eq *= max(0.0, 1.0 + frac * r / 100.0)
        peak = max(peak, eq)
        dd = max(dd, 1.0 - eq / peak if peak > 0 else 0.0)
    return dd * 100.0


def trade_shuffle(cand: Sequence[Dict[str, Any]], h: int, perms: int, position_pct: float, seed: int = 7) -> Dict[str, Any]:
    """Compound the candidate's net returns at position_pct of equity per trade: real order vs shuffled orders (drawdown, not final equity, depends on order)."""
    out: Dict[str, Any] = {"n_trades": 0, "position_pct": position_pct, "final_return_pct": None, "real_max_dd_pct": None, "shuffle_dd_median_pct": None,
                           "shuffle_dd_p95_pct": None, "p_dd_over_limit": None, "dd_limit_pct": DD_LIMIT_PCT, "real_dd_percentile": None, "sampled": False}
    rows = sorted((r for r in cand if r.get(f"r{h}") is not None), key=lambda r: r["ts"])
    if not rows or perms < 1:
        return out
    rng = random.Random(seed)
    if len(rows) > MAX_SHUFFLE_TRADES:
        keep = sorted(rng.sample(range(len(rows)), MAX_SHUFFLE_TRADES))
        rows = [rows[i] for i in keep]
        out["sampled"] = True
    rets = [r[f"r{h}"] for r in rows]
    frac = max(0.0, min(1.0, position_pct / 100.0))
    eq = 1.0
    for r in rets:
        eq *= max(0.0, 1.0 + frac * r / 100.0)
    real = _max_dd(rets, frac)
    dds: List[float] = []
    buf = list(rets)
    for _ in range(perms):
        rng.shuffle(buf)
        dds.append(_max_dd(buf, frac))
    dds.sort()
    out.update(n_trades=len(rets), final_return_pct=round((eq - 1.0) * 100.0, 2), real_max_dd_pct=round(real, 2),
               shuffle_dd_median_pct=round(dds[len(dds) // 2], 2), shuffle_dd_p95_pct=round(dds[int(0.95 * (len(dds) - 1))], 2),
               p_dd_over_limit=round(sum(1 for d in dds if d > DD_LIMIT_PCT) / len(dds), 3),
               real_dd_percentile=round(100.0 * sum(1 for d in dds if d <= real) / len(dds), 1))
    return out


def _gate_edge(rows: Sequence[Dict[str, Any]], th: Dict[str, Any], h: int) -> Dict[str, Any]:
    """Edge (candidate mean - baseline mean, pp) when the gate is re-applied at thresholds `th` to the stored summaries."""
    base, cand = [], []
    for r in rows:
        v = r.get(f"r{h}")
        if v is None or not r.get("s"):
            continue
        base.append(v)
        if not any(rule_flags(r["s"], th).values()):
            cand.append(v)
    if not base or not cand:
        return {"n": len(cand), "edge_pp": None, "cand_mean": None}
    return {"n": len(cand), "edge_pp": round(sum(cand) / len(cand) - sum(base) / len(base), 3), "cand_mean": round(sum(cand) / len(cand), 3)}


def sweep(rows: Sequence[Dict[str, Any]], h: int, pct: float, th: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Nudge each numeric threshold by -pct / +pct (one at a time, then all tighter / all looser) and recompute the edge. See the module docstring."""
    t0 = dict(DEFAULT_THRESHOLDS)
    t0.update({k: v for k, v in (th or {}).items() if k in DEFAULT_THRESHOLDS})
    f = pct / 100.0
    keys = [k for k in SWEEP_KEYS if t0.get(k)]
    # tighter = rejects more: lower the ceilings (chase / stretch / bearish bars is a window, so higher = more rejections), raise the floors (room / rr)
    sign = {"chase_atr": -1, "stretch_atr": -1, "room_atr": +1, "rr": +1, "bearish_bos_bars": +1}

    def mk(changes: Dict[str, float]) -> Dict[str, Any]:
        t = dict(t0)
        t.update(changes)
        return t

    def val(k: str, m: float) -> float:
        v = t0[k] * (1.0 + m * f)
        return float(round(v)) if k == "bearish_bos_bars" else round(v, 4)

    base = _gate_edge(rows, mk({}), h)
    variants: List[Dict[str, Any]] = []
    for k in keys:
        for m in (-1.0, 1.0):
            r = _gate_edge(rows, mk({k: val(k, m)}), h)
            variants.append({"name": f"{k} {'+' if m > 0 else '-'}{pct:g}%", "value": val(k, m), **r})
    for label, direction in (("all tighter", 1.0), ("all looser", -1.0)):
        ch = {k: val(k, direction * sign[k]) for k in keys}
        variants.append({"name": f"{label} {pct:g}%", "value": None, **_gate_edge(rows, mk(ch), h)})
    e0 = base["edge_pp"]
    if e0 is None or base["n"] < HARD_MIN_N:
        status, why = "insufficient_sample", "baseline gate has fewer than %d signals or no edge value" % HARD_MIN_N
    elif e0 <= 0:
        status, why = "no_edge_to_test", "the untouched gate has no positive edge, so there is nothing for a nudge to break"
    else:
        bad = [v for v in variants if v["edge_pp"] is None or v["n"] < HARD_MIN_N or v["edge_pp"] <= 0 or v["edge_pp"] < 0.5 * e0]
        status = "fragile" if bad else "stable"
        why = ("fell below half the base edge, flipped sign or lost signals in: " + ", ".join(v["name"] for v in bad)) if bad else \
            "every nudge keeps a positive edge of at least half the base edge, with %d+ signals" % HARD_MIN_N
    return {"pct": pct, "base": base, "variants": variants, "status": status, "why": why,
            "min_edge_pp": min((v["edge_pp"] for v in variants if v["edge_pp"] is not None), default=None)}


def build(rows: List[Dict[str, Any]], horizon: int, perms: int, sweep_pct: float, position_pct: float, seed: int, th: Dict[str, Any]) -> Dict[str, Any]:
    base = rows
    cand = [r for r in rows if not r["rej"]]
    cs = ab._stats(cand, horizon)
    rep: Dict[str, Any] = {
        "horizon": horizon, "signals": {"baseline": len(base), "candidate": len(cand)}, "edge_pp": ab._edge(base, cand, horizon),
        "candidate_mean_net": cs.get("mean_net"), "baseline_mean_net": ab._stats(base, horizon).get("mean_net"),
        "random_entry": random_entry(base, cand, horizon, perms, seed), "trade_shuffle": trade_shuffle(cand, horizon, perms, position_pct, seed),
        "sweep": sweep(rows, horizon, sweep_pct, th), "settings": {"perms": perms, "sweep_pct": sweep_pct, "position_pct": position_pct, "seed": seed}}
    if not base:
        rep["verdict"] = "no_data"
    elif len(cand) < HARD_MIN_N or len(base) < HARD_MIN_N or rep["edge_pp"] is None:
        rep["verdict"] = "insufficient_sample"
    else:
        p = rep["random_entry"]["p_random"]
        ok = p is not None and p < P_RANDOM_OK and rep["sweep"]["status"] == "stable" and (cs.get("mean_net") or 0) > 0
        rep["verdict"] = "robust" if ok else "not_robust"
    rep["verdict_basis"] = {"p_random_below": P_RANDOM_OK, "sweep_status": rep["sweep"]["status"], "candidate_mean_net_positive": (cs.get("mean_net") or 0) > 0}
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    s, re_, ts, sw = rep["signals"], rep["random_entry"], rep["trade_shuffle"], rep["sweep"]
    L_: List[str] = [f"# Robustness report (T+{rep['horizon']}, net of costs)", "", f"**Verdict: `{rep['verdict']}`**", "",
                     f"- signals: baseline {s['baseline']}, candidate {s['candidate']}; edge {f(rep['edge_pp'])} pp; candidate mean net {f(rep['candidate_mean_net'])} %, baseline {f(rep['baseline_mean_net'])} %",
                     f"- robust needs: random-entry p < {P_RANDOM_OK}, sweep `stable`, candidate mean net > 0, and at least {HARD_MIN_N} candidate and baseline signals", "",
                     "## 1. Random-entry benchmark", "",
                     f"Candidate mean {f(re_['candidate_mean'])} % vs {re_['perms']} random same-size, same-month draws from the baseline: mean {f(re_['random_mean'])} % "
                     f"(5th-95th pct {f(re_['random_p5'])} to {f(re_['random_p95'])}). **p_random = {f(re_['p_random'])}** (share of random draws that did as well).", "",
                     "## 2. Trade-order shuffle", ""]
    if ts["n_trades"]:
        L_ += [f"{ts['n_trades']} candidate trades{' (seeded sample)' if ts['sampled'] else ''}, each sized at {ts['position_pct']:g} % of equity, compounded: final {f(ts['final_return_pct'])} %. "
               f"Real-order max drawdown {f(ts['real_max_dd_pct'])} % (a luckier-than-{f(ts['real_dd_percentile'])} % of shuffles). Shuffled orders: median {f(ts['shuffle_dd_median_pct'])} %, "
               f"95th percentile {f(ts['shuffle_dd_p95_pct'])} %; {f(ts['p_dd_over_limit'])} of shuffles draw down more than {ts['dd_limit_pct']:g} %. Plan for the 95th-percentile figure, not the real one."]
    else:
        L_ += ["No candidate trades."]
    L_ += ["", f"## 3. Parameter sweep (-/+{sw['pct']:g} %)", "", f"Status **`{sw['status']}`**: {sw['why']}.", "",
           "| variant | value | candidate n | edge pp | candidate mean % |", "|---|---|---|---|---|",
           f"| untouched | - | {sw['base']['n']} | {f(sw['base']['edge_pp'])} | {f(sw['base']['cand_mean'])} |"]
    for v in sw["variants"]:
        L_.append(f"| {v['name']} | {f(v['value'])} | {v['n']} | {f(v['edge_pp'])} | {f(v['cand_mean'])} |")
    L_ += ["", "Offline evidence on one history, with the baseline a technical-only proxy. `robust` is necessary, not sufficient; `insufficient_sample` means nothing may be concluded."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--perms", type=int, default=1000, help="random-entry draws and trade-order shuffles")
    ap.add_argument("--sweep-pct", type=float, default=20.0, help="nudge each threshold by -/+ this percent")
    ap.add_argument("--position-pct", type=float, default=10.0, help="percent of equity per trade in the shuffle (the live risk sizing is separate)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--sample", type=int, default=0, help="replay N symbols drawn at random (seeded); 0 = all")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    th = json.loads(a.thresholds)
    files = sorted(d.glob("*.json"))
    bench = None
    for f in files:
        if f.stem.upper() in ab.BENCH_NAMES:
            try:
                bb = ab.load_bars(f)
                if len(bb) >= 220:
                    bench = ab.bench_info(bb)
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {f.name} unreadable: {e}", file=sys.stderr)
            break
    stock_files = [f for f in files if f.stem.upper() not in ab.BENCH_NAMES]
    if a.sample > 0 and len(stock_files) > a.sample:
        stock_files = sorted(random.Random(a.seed).sample(stock_files, a.sample))
    run_th = dict(th)
    run_th["_keep_summary"] = True
    jobs = [(str(f), f.stem.upper(), a.step, run_th, a.cost_pct, a.slip_pct) for f in stock_files]
    from _progress import run_jobs
    results = run_jobs(ab.stock_signals, jobs, max(1, a.jobs), "robustness", chunksize=2, ckpt="", resume=False, stamp=lambda j: None,
                       initializer=ab._init_pool, initargs=(bench,))
    rows: List[Dict[str, Any]] = []
    for res in results:
        if res["err"]:
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    rep = build(rows, a.horizon, a.perms, a.sweep_pct, a.position_pct, a.seed, th)
    out = Path(a.out) if a.out else d / "robust_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "robustness.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "robustness.md").write_text(md)
    print(md)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
