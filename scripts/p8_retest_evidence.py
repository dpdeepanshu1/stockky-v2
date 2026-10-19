#!/usr/bin/env python3
"""p8_retest_evidence.py - group 441: is the P8 retest / CHOCH branch (STRUCTURE_POLICY retest_entry=1) worth switching on?

  python3 scripts/p8_retest_evidence.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10] [--min-n 100]
                                        [--boot 500] [--jobs 1] [--sample 400 --seed 7] [--thresholds '{}']

The retest branch lets an OLD but intact bullish BOS (older than bos_max_bars) pass P8 when the price is retesting a live bullish zone (FVG / order
block / golden pocket) AND a bullish CHOCH confirmed it within retest_confirm_bars. It is OFF by default. Nothing here switches it on; this reports what
it would have changed on history, replayed with no lookahead (same engine, same policy_flags() the live policy is tested against).

Every replayed day that passes the gate rules and the baseline filter (0 <= stretch <= 3 ATR, like ms_ab_report) is put in exactly one group,
by running the policy rules twice (retest_entry 0 and 1, same other thresholds):
  clean    no policy rule trips with the retest branch OFF
  rescued  p8_old_bos is the ONLY rule tripped with the branch OFF and nothing trips with it ON   = the days the branch would add
  blocked  p8_old_bos is the ONLY rule tripped and it still trips with the branch ON             = old BOS without a confirmed retest
  other    any other rule trips (kept out of every comparison)
Entry = OPEN of the next session (+ slippage); T+h counts the entry session as 1; round-trip cost subtracted; a bar touching both stop and target
counts as a STOP. Edge = mean(rescued) - mean(blocked) in percentage points, cluster bootstrap (symbol x month blocks, seed 7).

Verdict: no_data | insufficient_sample (rescued or blocked < --min-n) | retest_ok (edge vs blocked > 0, bootstrap 5th pct > 0, rescued mean net > 0,
both halves positive) | retest_hurts (95th pct of rescued-blocked < 0, or rescued mean net < 0 with 95th pct of rescued-clean < 0) | no_edge.
`retest_ok` is evidence on this history, not a switch: turn the branch on only with the evidence gates, record-only first.
Output: p8_retest.json + p8_retest.md in --out (default HIST_DIR/p8_out). Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ms_ab_report as ab  # noqa: E402
from market_structure.backtest import HORIZON_MAX, HORIZONS, forward_outcome, policy_flags, rule_flags  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from market_structure.ms_types import O, T, finite  # noqa: E402
from market_structure.replay import replay  # noqa: E402

GROUPS = ("clean", "rescued", "blocked", "other")
_BENCH: Optional[Dict[str, Any]] = None


def classify(summary: Dict[str, Any], th: Optional[Dict[str, Any]] = None) -> str:
    """clean | rescued | blocked | other for one flat ms_* summary. Pure."""
    th = dict(th or {})
    off = {k: v for k, v in policy_flags(summary, {**th, "retest_entry": 0.0}).items() if v}
    on = {k: v for k, v in policy_flags(summary, {**th, "retest_entry": 1.0}).items() if v}
    if not off:
        return "clean"
    if set(off) == {"p8_old_bos"}:
        return "blocked" if on else "rescued"
    return "other"


def zones_matched(summary: Dict[str, Any]) -> List[str]:
    """Which live bullish zone the price sat in (same 0.5 % tolerance as the policy)."""
    s = summary
    px = s.get("ms_price")
    out: List[str] = []
    if not finite(px):
        return out
    px = float(px)
    for name, side, top, bot, st in (("fvg", s.get("ms_fvg_side"), s.get("ms_fvg_top"), s.get("ms_fvg_bottom"), s.get("ms_fvg_status")),
                                     ("ob", s.get("ms_ob_side"), s.get("ms_ob_top"), s.get("ms_ob_bottom"), s.get("ms_ob_status"))):
        if side == "bullish" and finite(top) and finite(bot) and st not in ("filled", "broken", "expired") and float(bot) * 0.995 <= px <= float(top) * 1.005:
            out.append(name)
    if s.get("ms_in_golden"):
        out.append("golden")
    return out


def _rows(bars: Sequence[Sequence[float]], sym: str, step: int, th: Dict[str, Any], cost: float, slip: float,
          bench: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(bars) < ab.MIN_BARS + HORIZON_MAX + 2:
        return []
    rows: List[Dict[str, Any]] = []
    bk = bench["keys"] if bench else None
    for r in replay(bars, step=step):
        i = r["i"]
        if r["quality"] not in ("ok", "limited") or i + 1 >= len(bars):
            continue
        s = r["summary"]
        st = s.get("ms_stretch_atr")
        if not finite(st) or not (0.0 <= float(st) <= 3.0):
            continue
        if any(rule_flags(s, th).values()):          # the entry gates come first
            continue
        entry_open = float(bars[i + 1][O])
        if not finite(entry_open) or entry_open <= 0:
            continue
        entry = entry_open * (1.0 + slip / 100.0)
        oc = forward_outcome(entry, s.get("ms_stop"), s.get("ms_target"), bars[i + 1:])
        if oc["ret_20d"] is None:
            continue
        g = classify(s, th)
        row: Dict[str, Any] = {"sym": sym, "ts": bars[i][T], "month": ist_date(bars[i][T])[:7], "g": g,
                               "zones": zones_matched(s) if g == "rescued" else [], "stop_first": oc["first_touch"] == "stop", "bull": None}
        for h in HORIZONS:
            v = oc[f"ret_{h}d"]
            row[f"r{h}"] = round(v - cost, 3) if v is not None else None
        if bench and bk is not None:
            j = ab._pos_le(bk, ab.day_key(bars[i][T]))
            if j >= 0:
                row["bull"] = bench["bull"][j]
        rows.append(row)
    return rows


def _job(args: Tuple[str, str, int, Dict[str, Any], float, float]) -> Dict[str, Any]:
    path, sym, step, th, cost, slip = args
    try:
        bars = ab.load_bars(Path(path))
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "rows": [], "err": str(e)[:120]}
    return {"sym": sym, "rows": _rows(bars, sym, step, th, cost, slip, _BENCH), "err": None}


def _init(bench: Optional[Dict[str, Any]]) -> None:
    global _BENCH
    _BENCH = bench


def _stats(rows: Sequence[Dict[str, Any]], h: int) -> Dict[str, Any]:
    rets = [r[f"r{h}"] for r in rows if r.get(f"r{h}") is not None]
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "mean_net": round(mean(rets), 3), "win_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1),
            "stop_first_pct": round(100.0 * sum(1 for r in rows if r["stop_first"]) / len(rows), 1)}


def _diff(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]], h: int) -> Optional[float]:
    x = [r[f"r{h}"] for r in a if r.get(f"r{h}") is not None]
    y = [r[f"r{h}"] for r in b if r.get(f"r{h}") is not None]
    return round(mean(x) - mean(y), 3) if x and y else None


def boot_diff(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]], h: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """Cluster bootstrap (symbol x month) of mean(a) - mean(b). A resample that leaves either side empty is dropped."""
    cl: Dict[Tuple[str, str], List[float]] = {}
    for idx, grp in ((0, a), (2, b)):
        for r in grp:
            v = r.get(f"r{h}")
            if v is None:
                continue
            c = cl.setdefault((r["sym"], r["month"]), [0, 0.0, 0, 0.0])
            c[idx] += 1
            c[idx + 1] += v
    arr = list(cl.values())
    out: Dict[str, Any] = {"clusters": len(arr), "resamples": n_boot, "ci90": None, "p_positive": None}
    if len(arr) < 20 or n_boot < 50:
        return out
    rng = random.Random(seed)
    m = len(arr)
    es: List[float] = []
    for _ in range(n_boot):
        na = sa = nb = sb = 0
        for k in rng.choices(range(m), k=m):
            c = arr[k]
            na, sa, nb, sb = na + c[0], sa + c[1], nb + c[2], sb + c[3]
        if na and nb:
            es.append(sa / na - sb / nb)
    if len(es) < n_boot // 2:
        return out
    es.sort()
    out["ci90"] = [round(es[int(0.05 * (len(es) - 1))], 3), round(es[int(0.95 * (len(es) - 1))], 3)]
    out["p_positive"] = round(sum(1 for e in es if e > 0) / len(es), 3)
    return out


def verdict(n_resc: int, n_block: int, n_all: int, mean_resc: Optional[float], vs_block_ci: Optional[List[float]], vs_clean_ci: Optional[List[float]],
            vs_block: Optional[float], halves: Sequence[Optional[float]], min_n: int) -> str:
    if n_all == 0:
        return "no_data"
    if n_resc < min_n or n_block < min_n or vs_block is None:
        return "insufficient_sample"
    if vs_block_ci is not None and vs_block_ci[1] < 0:
        return "retest_hurts"
    if mean_resc is not None and mean_resc < 0 and vs_clean_ci is not None and vs_clean_ci[1] < 0:
        return "retest_hurts"
    halves_ok = len(halves) == 2 and all(e is not None and e > 0 for e in halves)
    if vs_block > 0 and vs_block_ci is not None and vs_block_ci[0] > 0 and halves_ok and mean_resc is not None and mean_resc > 0:
        return "retest_ok"
    return "no_edge"


def build_report(rows: List[Dict[str, Any]], horizon: int, min_n: int, n_boot: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    by = {g: [r for r in rows if r["g"] == g] for g in GROUPS}
    resc, blk, cln = by["rescued"], by["blocked"], by["clean"]
    both = resc + blk
    cut = sorted(r["ts"] for r in both)[len(both) // 2] if both else 0
    halves = [_diff([r for r in resc if r["ts"] < cut], [r for r in blk if r["ts"] < cut], horizon),
              _diff([r for r in resc if r["ts"] >= cut], [r for r in blk if r["ts"] >= cut], horizon)]
    b_blk = boot_diff(resc, blk, horizon, n_boot)
    b_cln = boot_diff(resc, cln, horizon, n_boot)
    vs_block = _diff(resc, blk, horizon)
    zone_split = {z: _stats([r for r in resc if z in r["zones"]], horizon) for z in ("fvg", "ob", "golden")}
    regime = {nm: _stats([r for r in resc if r["bull"] is flag], horizon) for nm, flag in (("bull", True), ("bear", False))}
    rep: Dict[str, Any] = {
        "meta": meta, "horizon": horizon, "counts": {g: len(by[g]) for g in GROUPS},
        "groups": {g: _stats(by[g], horizon) for g in ("clean", "rescued", "blocked")},
        "by_horizon": {str(h): {"rescued": _stats(resc, h), "blocked": _stats(blk, h), "clean": _stats(cln, h),
                                "rescued_minus_blocked": _diff(resc, blk, h), "rescued_minus_clean": _diff(resc, cln, h)} for h in HORIZONS},
        "rescued_minus_blocked_pp": vs_block, "rescued_minus_clean_pp": _diff(resc, cln, horizon),
        "bootstrap_vs_blocked": b_blk, "bootstrap_vs_clean": b_cln,
        "halves_vs_blocked": {"first": halves[0], "second": halves[1], "cut": ist_date(cut) if both else None},
        "rescued_by_zone": zone_split, "rescued_by_regime": regime,
        "added_share_of_buy_now_pct": round(100.0 * len(resc) / (len(resc) + len(cln)), 1) if (resc or cln) else None}
    rep["verdict"] = verdict(len(resc), len(blk), len(rows), rep["groups"]["rescued"].get("mean_net"), b_blk.get("ci90"), b_cln.get("ci90"),
                             vs_block, halves, min_n)
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    m = rep["meta"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    c = rep["counts"]
    L: List[str] = [f"# P8 retest branch evidence (T+{rep['horizon']}, net of costs)", "", f"**Verdict: `{rep['verdict']}`**", "",
                    f"- symbols: {m['symbols']} (skipped {m['skipped']}), benchmark: {m['benchmark']}, step {m['step']}, cost {m['cost_pct']}% + {m['slip_pct']}% slippage",
                    f"- days: clean {c['clean']}, rescued {c['rescued']}, blocked {c['blocked']}, other {c['other']}",
                    f"- the branch would add {f(rep['added_share_of_buy_now_pct'])}% to today's BUY NOW days",
                    f"- rescued - blocked: **{f(rep['rescued_minus_blocked_pp'])} pp**, 90% CI {f(rep['bootstrap_vs_blocked']['ci90'])}, P(>0) {f(rep['bootstrap_vs_blocked']['p_positive'])}",
                    f"- rescued - clean: {f(rep['rescued_minus_clean_pp'])} pp, 90% CI {f(rep['bootstrap_vs_clean']['ci90'])}",
                    f"- halves (cut {f(rep['halves_vs_blocked']['cut'])}): first {f(rep['halves_vs_blocked']['first'])} pp, second {f(rep['halves_vs_blocked']['second'])} pp", "",
                    "| group | n | mean net % | win % | stop first % |", "|---|---|---|---|---|"]
    for g, x in rep["groups"].items():
        L.append(f"| {g} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('win_pct'))} | {f(x.get('stop_first_pct'))} |")
    L += ["", "## By horizon", "", "| T+ | rescued | blocked | clean | rescued-blocked | rescued-clean |", "|---|---|---|---|---|---|"]
    for h, x in rep["by_horizon"].items():
        L.append(f"| {h} | {f(x['rescued'].get('mean_net'))} | {f(x['blocked'].get('mean_net'))} | {f(x['clean'].get('mean_net'))} | "
                 f"{f(x['rescued_minus_blocked'])} | {f(x['rescued_minus_clean'])} |")
    L += ["", "## Rescued days by zone (a day can sit in several)", "", "| zone | n | mean net % | win % |", "|---|---|---|---|"]
    for z, x in rep["rescued_by_zone"].items():
        L.append(f"| {z} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('win_pct'))} |")
    L += ["", "## Rescued days by market regime", "", "| regime | n | mean net % | win % |", "|---|---|---|---|"]
    for z, x in rep["rescued_by_regime"].items():
        L.append(f"| {z} | {x.get('n', 0)} | {f(x.get('mean_net'))} | {f(x.get('win_pct'))} |")
    L += ["", "Thresholds are the live policy defaults (nothing tuned). `retest_ok` is evidence on this history only; the branch stays OFF until the evidence "
              "gates agree, and goes live record-only first. `insufficient_sample` means nothing may be concluded."]
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--thresholds", default="{}")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--checkpoint", default="", help="folder where each finished symbol is saved as the run goes (group 466); emptied at the start unless --resume")
    ap.add_argument("--resume", action="store_true", help="reuse the symbols already saved in --checkpoint (same file and settings), compute only the rest")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    th = json.loads(a.thresholds)
    files = sorted(d.glob("*.json"))
    bench, bname = None, None
    for f in files:
        if f.stem.upper() in ab.BENCH_NAMES:
            try:
                bb = ab.load_bars(f)
                if len(bb) >= 220:
                    bench, bname = ab.bench_info(bb), f.stem
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {f.name} unreadable: {e}", file=sys.stderr)
            break
    stock_files = [f for f in files if f.stem.upper() not in ab.BENCH_NAMES]
    pool = len(stock_files)
    if a.sample > 0 and pool > a.sample:
        stock_files = sorted(random.Random(a.seed).sample(stock_files, a.sample))
    jobs = [(str(f), f.stem.upper(), a.step, th, a.cost_pct, a.slip_pct) for f in stock_files]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _progress import file_stamp, run_jobs          # group466: progress lines + per-symbol checkpoint / resume
    bstamp = file_stamp(str(next((f for f in files if f.stem.upper() in ab.BENCH_NAMES), "")))
    settings = json.dumps([a.step, th, a.cost_pct, a.slip_pct, bstamp], sort_keys=True, default=str)
    results = run_jobs(_job, jobs, max(1, a.jobs), "p8_retest", chunksize=2, ckpt=a.checkpoint, resume=a.resume,
                       stamp=lambda j: file_stamp(j[0], settings), initializer=_init, initargs=(bench,))
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "benchmark": bname or "none", "step": a.step, "cost_pct": a.cost_pct,
            "slip_pct": a.slip_pct, "thresholds": th, "sample": {"requested": a.sample, "pool": pool, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep = build_report(rows, a.horizon, a.min_n, a.boot, meta)
    out = Path(a.out) if a.out else d / "p8_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "p8_retest.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "p8_retest.md").write_text(md)
    print(md)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
