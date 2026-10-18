#!/usr/bin/env python3
"""entry_rules_backtest.py - did the entry-path rules and the stop/target width help, after costs?

  python3 scripts/entry_rules_backtest.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10] [--min-n 200]
                                          [--boot 500] [--sample 400 --seed 7] [--expiry-weekday 1] [--expiry-switch 2025-09-01]
                                          [--grid-horizon 5] [--jobs 1]

Companion to scripts/legacy_gate_backtest.py (candidate gates: downtrend, 52-week range, resistance, volume health) and scripts/structure_backtest.py
(market-structure rules). This one covers what those two do not, on the same HIST_DIR of per-symbol JSON candles
(scripts/fetch_history_for_backtest.py ... --sample 400 --seed 7; a ^NSEI.json benchmark file, if present, gives the exchange calendar and is not traded).

PART 1 - day-type and floor rules (same scoring and cluster bootstrap as legacy_gate_backtest.py)
  Day i is the signal session, the entry is the OPEN of session i+1 (+ slippage), exit = close of session i+h (entry session counts as 1), round-trip cost
  subtracted. "Rejected" = the rule would act on that entry:
    cal_expiry_any / cal_expiry_monthly / cal_pre_holiday / cal_friday   the ENTRY session is that kind of day (entry_engine/calendar_gate.py). Live those
                                    rules cut off late entries and shrink size rather than veto, so read them as "is this kind of day worse to enter on?"
    thin_turnover_1cr / _3cr / _10cr  mean(volume x close) over the last 20 sessions below the floor (entry_engine/liquidity_gate.py; live floor = 3 crore)
    penny_price_20 / _50            close below Rs 20 (live floor CANDIDATE_MIN_STOCK_PRICE) / below Rs 50
  Edge = mean net return of the entries the rule leaves alone - mean of the entries it acts on (percentage points). Positive = the rule acted on the worse days.
  Verdict: insufficient_sample (fewer than --min-n on either side), helps (5th percentile of edge > 0), hurts (95th percentile < 0), no_edge (anything else).

PART 2 - stop / target width (the entry engine uses stop = 1.5 x ATR14 clamped 2-6 %, target = 2 x stop)
  Every entry day is replayed under a grid of ATR multipliers and reward:risk ratios over --grid-horizon sessions: stop checked first when one bar holds both,
  a gap through the stop exits at the open, a gap through the target exits at the open, otherwise the close of the last session. For each cell:
  mean net %, win %, stopped %, target hit %, and the paired difference to the live setting (1.5 x ATR, 2R) with a 90% cluster-bootstrap interval.
  A cell is only worth adopting when that interval is entirely above zero (flagged BETTER); entirely below zero = WORSE; anything else = same.

READ THIS BEFORE TRUSTING IT: every sampled day of every sampled stock is replayed, not only the days the live scanner would have proposed, and daily bars
cannot show intraday stop-outs inside a session (the stop is tested against the bar's low, the target against its high, stop first). Without a ^NSEI.json
the pre-holiday tag is guessed from gaps in each stock's own data and is unreliable - the report says which was used. Weekly index expiry is Tuesday from
--expiry-switch (default 2025-09-01) and Thursday before it: CHECK both against the NSE circulars. Stdlib only.
Output: entry_rules_report.json + entry_rules_report.md in --out (default HIST_DIR/entry_out).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_structure.ms_types import C, H, L, O, T, V  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from ms_ab_report import BENCH_NAMES, load_bars  # noqa: E402
from legacy_gate_backtest import _bootstrap  # noqa: E402  (same cluster bootstrap: symbol x month, seed 7)

HORIZONS = (1, 5, 10, 20)
MIN_BARS = 120
ATR_LEN = 14
TURNOVER_WIN = 20
CRORE = 1e7
MIN_STOP_PCT, MAX_STOP_PCT = 2.0, 6.0
LIVE_CELL = (1.5, 2.0)                       # entry_engine: 1.5 x ATR stop, target 2 x stop
ATR_MULTS = (1.0, 1.5, 2.0, 2.5, 3.0)
RRS = (1.0, 1.5, 2.0, 3.0)

# name -> (population, rejected) over the row dicts built by rows_for()
GATES: List[Tuple[str, Any, Any]] = [
    ("cal_expiry_any", lambda r: True, lambda r: r["exp_any"]),
    ("cal_expiry_monthly", lambda r: True, lambda r: r["exp_month"]),
    ("cal_pre_holiday", lambda r: True, lambda r: r["pre_hol"]),
    ("cal_friday", lambda r: True, lambda r: r["friday"]),
    ("thin_turnover_1cr", lambda r: r["turn"] is not None, lambda r: r["turn"] < 1.0),
    ("thin_turnover_3cr", lambda r: r["turn"] is not None, lambda r: r["turn"] < 3.0),
    ("thin_turnover_10cr", lambda r: r["turn"] is not None, lambda r: r["turn"] < 10.0),
    ("penny_price_20", lambda r: True, lambda r: r["px"] < 20.0),
    ("penny_price_50", lambda r: True, lambda r: r["px"] < 50.0),
]


# ───────────────────────────── calendar ─────────────────────────────

def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def expiry_weekday_for(d: date, weekday: int, switch: Optional[date]) -> int:
    """Weekly index expiry weekday for date d: `weekday` from `switch` on, Thursday (3) before it. switch=None -> always `weekday`."""
    return weekday if (switch is None or d >= switch) else 3


def _is_day(d: date, market_days: Optional[Set[str]]) -> bool:
    return d.weekday() < 5 and (market_days is None or d.isoformat() in market_days)


def _roll_back(d: date, market_days: Optional[Set[str]]) -> date:
    for _ in range(14):
        if _is_day(d, market_days):
            return d
        d -= timedelta(days=1)
    return d


def expiry_kind(d: date, weekday: int, switch: Optional[date], market_days: Optional[Set[str]]) -> Optional[str]:
    """'monthly' / 'weekly' when d is an index-derivatives expiry session (same maths as calendar_gate.is_expiry_day), else None."""
    if not _is_day(d, market_days):
        return None
    wd = expiry_weekday_for(d, weekday, switch)
    if _roll_back(d + timedelta(days=wd - d.weekday()), market_days) != d:
        return None
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = nxt - timedelta(days=1)
    last_wd = last - timedelta(days=(last.weekday() - wd) % 7)
    return "monthly" if _roll_back(last_wd, market_days) == d else "weekly"


def pre_holiday(d: date, next_session: date, market_days: Optional[Set[str]]) -> bool:
    """A weekday that is NOT a session lies between d and the next session (an exchange holiday, not just the weekend)."""
    n = d + timedelta(days=1)
    while n < next_session:
        if n.weekday() < 5 and (market_days is None or n.isoformat() not in market_days):
            return True
        n += timedelta(days=1)
    return False


def load_calendar(folder: Path) -> Optional[Set[str]]:
    for f in sorted(folder.glob("*.json")):
        if f.stem.upper() in BENCH_NAMES:
            try:
                b = load_bars(f)
                if len(b) >= 220:
                    return {ist_date(x[T]) for x in b}
            except Exception as e:  # noqa: BLE001
                print(f"benchmark {f.name} unreadable: {e}", file=sys.stderr)
            break
    return None


# ───────────────────────────── scoring helpers ─────────────────────────────

def atr_pct(bars: Sequence[Sequence[float]], i: int, n: int = ATR_LEN) -> Optional[float]:
    """Simple ATR(n) as % of close, from bars[:i+1] only."""
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l, pc = float(bars[k][H]), float(bars[k][L]), float(bars[k - 1][C])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    c = float(bars[i][C])
    return (sum(trs) / n) / c * 100.0 if c > 0 else None


def stop_target(atr: Optional[float], mult: float, rr: float) -> Tuple[float, float]:
    """Stop % (ATR x mult clamped 2-6) and target % (stop x rr). No ATR -> 3 % / 3 % x rr, like the entry engine's fallback."""
    stop = 3.0 if (atr is None or atr <= 0) else max(MIN_STOP_PCT, min(atr * mult, MAX_STOP_PCT))
    return stop, stop * rr


def walk(bars: Sequence[Sequence[float]], start: int, entry: float, stop_pct: float, tgt_pct: float, h: int) -> Tuple[float, str]:
    """Gross % of a long entered at `entry` in session `start`, held up to h sessions. Stop first when one bar holds both."""
    stop_px, tgt_px = entry * (1 - stop_pct / 100.0), entry * (1 + tgt_pct / 100.0)
    for k in range(start, start + h):
        o, hi, lo = float(bars[k][O]), float(bars[k][H]), float(bars[k][L])
        if o <= stop_px:
            return (o / entry - 1) * 100.0, "stop"
        if lo <= stop_px:
            return -stop_pct, "stop"
        if o >= tgt_px:
            return (o / entry - 1) * 100.0, "target"
        if hi >= tgt_px:
            return tgt_pct, "target"
    return (float(bars[start + h - 1][C]) / entry - 1) * 100.0, "time"


def rows_for(bars: Sequence[Sequence[float]], sym: str, step: int, cost: float, slip: float, weekday: int, switch: Optional[date],
             market_days: Optional[Set[str]], grid_h: int) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], List[List[float]]]]:
    """(rows for PART 1, per-(symbol, month) grid sums for PART 2). Signal on bars[:i+1]; entry = open of i+1."""
    rows: List[Dict[str, Any]] = []
    grid: Dict[Tuple[str, str], List[List[float]]] = {}
    ncell = len(ATR_MULTS) * len(RRS)
    base = ATR_MULTS.index(LIVE_CELL[0]) * len(RRS) + RRS.index(LIVE_CELL[1])
    top = max(max(HORIZONS), grid_h)
    if len(bars) < MIN_BARS + top + 3:
        return rows, grid
    for i in range(MIN_BARS - 1, len(bars) - top - 2, max(1, step)):
        entry_open = float(bars[i + 1][O])
        px = float(bars[i][C])
        if entry_open <= 0 or px <= 0:
            continue
        entry = entry_open * (1.0 + slip / 100.0)
        d_in, d_next = _d(ist_date(bars[i + 1][T])), _d(ist_date(bars[i + 2][T]))
        ek = expiry_kind(d_in, weekday, switch, market_days)
        tv = [float(b[V]) * float(b[C]) for b in bars[i - TURNOVER_WIN + 1:i + 1]]
        turn = (sum(tv) / len(tv) / CRORE) if all(x > 0 for x in tv) else None
        month = ist_date(bars[i][T])[:7]
        row: Dict[str, Any] = {"sym": sym, "month": month, "px": px, "turn": turn, "exp_any": ek is not None, "exp_month": ek == "monthly",
                               "pre_hol": pre_holiday(d_in, d_next, market_days), "friday": d_in.weekday() == 4}
        for h in HORIZONS:
            row[f"r{h}"] = round((float(bars[i + h][C]) / entry - 1.0) * 100.0 - cost, 3)
        rows.append(row)
        atr = atr_pct(bars, i)
        cells = []
        for m in ATR_MULTS:
            for rr in RRS:
                sp, tp = stop_target(atr, m, rr)
                g, how = walk(bars, i + 1, entry, sp, tp, grid_h)
                cells.append((g - cost, how))
        a = grid.setdefault((sym, month), [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(ncell)])   # n, sum, wins, stops, targets, sum diff vs live
        for k, (net, how) in enumerate(cells):
            x = a[k]
            x[0] += 1
            x[1] += net
            x[2] += 1.0 if net > 0 else 0.0
            x[3] += 1.0 if how == "stop" else 0.0
            x[4] += 1.0 if how == "target" else 0.0
            x[5] += net - cells[base][0]
    return rows, grid


def _job(a: Tuple[Any, ...]) -> Dict[str, Any]:
    path, sym, step, cost, slip, weekday, switch, mdays, grid_h = a
    try:
        rows, grid = rows_for(load_bars(Path(path)), sym, step, cost, slip, weekday, switch, mdays, grid_h)
        return {"sym": sym, "err": None, "rows": rows, "grid": grid}
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "err": f"{type(e).__name__}: {e}", "rows": [], "grid": {}}


# ───────────────────────────── reports ─────────────────────────────

def gate_report(rows: Sequence[Dict[str, Any]], h: int, min_n: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    key = f"r{h}"
    for name, pop, rej in GATES:
        pr = [r for r in rows if pop(r)]
        rj = [r for r in pr if rej(r)]
        ps = [r for r in pr if not rej(r)]
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


def grid_report(grids: Sequence[Dict[Tuple[str, str], List[List[float]]]], n_boot: int, min_n: int, seed: int = 7) -> Dict[str, Any]:
    clusters: Dict[Tuple[str, str], List[List[float]]] = {}
    for g in grids:
        for k, v in g.items():
            clusters[k] = v
    arr = list(clusters.values())
    ncell = len(ATR_MULTS) * len(RRS)
    cells: List[Dict[str, Any]] = []
    rng = random.Random(seed)
    m = len(arr)
    boots: List[List[float]] = [[] for _ in range(ncell)]
    if m >= 20 and n_boot >= 50:
        for _ in range(n_boot):
            pick = [arr[j] for j in rng.choices(range(m), k=m)]
            for c in range(ncell):
                n = sum(p[c][0] for p in pick)
                if n:
                    boots[c].append(sum(p[c][5] for p in pick) / n)
    for c in range(ncell):
        n = sum(p[c][0] for p in arr)
        mult, rr = ATR_MULTS[c // len(RRS)], RRS[c % len(RRS)]
        if not n:
            continue
        ci = None
        if len(boots[c]) >= n_boot // 2 and boots[c]:
            b = sorted(boots[c])
            ci = [round(b[int(0.05 * (len(b) - 1))], 3), round(b[int(0.95 * (len(b) - 1))], 3)]
        diff = sum(p[c][5] for p in arr) / n
        live = (mult, rr) == LIVE_CELL
        if live:
            v = "live"
        elif n < min_n or ci is None:
            v = "insufficient_sample"
        elif ci[0] > 0:
            v = "BETTER"
        elif ci[1] < 0:
            v = "WORSE"
        else:
            v = "same"
        cells.append({"atr_mult": mult, "rr": rr, "n": int(n), "mean_net": round(sum(p[c][1] for p in arr) / n, 3),
                      "win_pct": round(100.0 * sum(p[c][2] for p in arr) / n, 1), "stop_pct": round(100.0 * sum(p[c][3] for p in arr) / n, 1),
                      "target_pct": round(100.0 * sum(p[c][4] for p in arr) / n, 1), "diff_vs_live": round(diff, 3), "ci90": ci, "verdict": v})
    return {"clusters": m, "cells": cells}


def to_markdown(rep: Dict[str, Any]) -> str:
    m = rep["meta"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L_ = [f"# Entry rules and stop/target width, replayed on daily history (net of costs)", "",
          f"- symbols: {m['symbols']} (skipped {m['skipped']}), step {m['step']}, cost {m['cost_pct']}% round trip + {m['slip_pct']}% slippage, entries replayed {rep['days']}",
          f"- calendar source: {m['calendar']}; weekly expiry weekday {m['expiry_weekday']} from {m['expiry_switch']} (Thursday before)",
          *([f"- sample: {m['sample']['drawn']} of {m['sample']['pool']} symbols, random seed {m['sample']['seed']}"] if m["sample"]["seed"] is not None else []), "",
          f"## Part 1 - rules (T+{rep['horizon']})", "",
          "| rule | entries | acted on % | left alone mean % | acted-on mean % | edge pp | 90% CI | verdict |", "|---|---|---|---|---|---|---|---|"]
    for k, x in rep["gates"].items():
        L_.append(f"| {k} | {x['population']} | {f(x['rejected_pct'])} | {f(x['mean_net_passed'])} | {f(x['mean_net_rejected'])} | {f(x['edge_pp'])} | {f(x['ci90'])} | {x['verdict']} |")
    L_ += ["", f"## Part 2 - stop / target grid ({rep['grid_horizon']} sessions, live setting = {LIVE_CELL[0]} x ATR stop, {LIVE_CELL[1]}R target)", "",
           "| ATR x | R:R | entries | mean net % | win % | stopped % | target % | vs live pp | 90% CI | verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
    for c in rep["grid"]["cells"]:
        L_.append(f"| {c['atr_mult']} | {c['rr']} | {c['n']} | {c['mean_net']} | {c['win_pct']} | {c['stop_pct']} | {c['target_pct']} | {c['diff_vs_live']} | {f(c['ci90'])} | {c['verdict']} |")
    L_ += ["", "Part 1 edge = mean of the entries a rule leaves alone minus mean of the entries it acts on; positive = the rule acted on the worse days. Part 2 `BETTER` / `WORSE` "
           "need the whole 90% interval on one side of zero; `same` and `insufficient_sample` mean nothing may be concluded. Every sampled day is replayed, not only the "
           "days the live scanner would have proposed, and daily bars cannot see stop-outs inside a session."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--grid-horizon", type=int, default=5)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--min-n", type=int, default=200)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--expiry-weekday", type=int, default=1, choices=range(5))
    ap.add_argument("--expiry-switch", default="2025-09-01", help="date the weekly expiry moved to --expiry-weekday ('' = never; Thursday before)")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    switch = _d(a.expiry_switch) if a.expiry_switch else None
    mdays = load_calendar(d)
    if mdays is None:
        print("WARNING: no ^NSEI.json in the folder - pre-holiday and expiry roll-back are guessed from each stock's own data (unreliable)", file=sys.stderr)
    files = [f for f in sorted(d.glob("*.json")) if f.stem.upper() not in BENCH_NAMES]
    pool = len(files)
    if a.sample > 0 and pool > a.sample:
        files = sorted(random.Random(a.seed).sample(files, a.sample))
    jobs = [(str(f), f.stem.upper(), a.step, a.cost_pct, a.slip_pct, a.expiry_weekday, switch, mdays, a.grid_horizon) for f in files]
    from _progress import run_jobs                       # group427: a progress line so a long run does not look hung
    results = run_jobs(_job, jobs, a.jobs, "entry_rules")
    rows: List[Dict[str, Any]] = []
    grids: List[Dict[Tuple[str, str], List[List[float]]]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
        grids.append(res["grid"])
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "step": a.step, "cost_pct": a.cost_pct, "slip_pct": a.slip_pct,
            "calendar": "^NSEI benchmark dates" if mdays is not None else "guessed from each stock's own data",
            "expiry_weekday": a.expiry_weekday, "expiry_switch": a.expiry_switch or "never",
            "sample": {"requested": a.sample, "pool": pool, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep = {"meta": meta, "horizon": a.horizon, "grid_horizon": a.grid_horizon, "days": len(rows),
           "gates": gate_report(rows, a.horizon, a.min_n, a.boot), "grid": grid_report(grids, a.boot, a.min_n)}
    out = Path(a.out) if a.out else d / "entry_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "entry_rules_report.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "entry_rules_report.md").write_text(md)
    print(md)
    if not rows:
        print("NO DAYS: the history folder needs symbol files with 130+ daily candles", file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
