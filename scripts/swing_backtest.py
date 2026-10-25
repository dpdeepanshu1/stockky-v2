#!/usr/bin/env python3
"""swing_backtest.py - E131 (swing-hold plan step S2): would holding strong-momentum stocks for up to 5 sessions have paid, AFTER delivery costs?

  python3 scripts/swing_backtest.py HIST_DIR [--out DIR] [--step 3] [--hold 5] [--sizes 3000,10000,20000,50000] [--primary-size 20000]
                                    [--top-pct 80] [--cost-slip-pct 0.10] [--stop-atr 1.5] [--min-n 200] [--boot 500] [--sample 0 --seed 7] [--jobs 1]

Same HIST_DIR of per-symbol JSON candles as scripts/entry_rules_backtest.py (a ^NSEI.json benchmark gives the exchange calendar and the bull / bear split; it is not traded).
It scores every stock with the SAME code the live ranker uses (services/real-trade-service/swing/momentum.py: 12-1 and 6-1 return adjusted for volatility,
200-day trend, ADX), ranks the whole folder on each signal day, and replays the entries a swing lane would take:

  signal day i (only bars up to the close of i are used) -> entry = OPEN of session i+1 (+ slippage) -> held up to --hold sessions (entry session counts as 1)
  exit variants:  hold = close of the last session | stop = ATR stop only (stop = ATR14 x --stop-atr, clamped 2-6 %, stop first when one bar holds both,
                  a gap through the stop exits at the open) | trail = that stop ratcheted up to (close - stop %) after every close
  costs: the real Dhan delivery round trip at each trade size (STT both legs, stamp, exchange, SEBI, GST, flat DP charge on the sale), read from
         services/real-trade-service/config.py when it is in the image; a small trade pays the flat DP fee as a big share of its value, so SIZE MATTERS.

Setups (SCREEN is the ONE pre-declared primary; the rest are exploratory and are NOT adopted on their own):
  ALL         every ranked stock-day (the control)
  TOP         top --top-pct percent of the day's ranking
  SCREEN      TOP and above its 200-day average            <- PRIMARY (the live `screen` flag)
  SCREEN_DIP  SCREEN and down 1 % or more over the last 3 sessions (a dip inside an uptrend)
  SCREEN_BRK  SCREEN and a close at a new 20-day high on 1.5x average volume

VERDICT (primary setup, `trail` exit, --primary-size), same tests as the other replays (day-cluster bootstrap, 90 % interval, median and clipped views):
  USEFUL            the net mean per trade has its 5th percentile above zero AND the edge over the rest of the ranked stocks is `helps` in the robust test AND
                    at most one calendar year with 30+ entries has a negative net mean
  NO_EDGE           anything else with enough entries (the report says which test failed; a gross edge eaten by costs is called out)
  INSUFFICIENT      fewer than --min-n primary entries, or fewer than --min-n others
READ THIS BEFORE TRUSTING IT: daily bars cannot show intraday stop-outs inside a session; signals are every --step-th session, so neighbouring days overlap;
survivorship (the folder holds today's symbols) flatters any momentum result; the ranking universe is the folder, not the whole exchange; one primary setup is
tested on purpose, so the exploratory rows must not be read as findings. Stdlib only. Output: swing_backtest_report.json + swing_backtest_report.md in --out.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "services" / "real-trade-service"))

from market_structure.ms_types import C, H, L, O, T, V  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from ms_ab_report import BENCH_NAMES, load_bars  # noqa: E402
from entry_rules_backtest import atr_pct, load_calendar, stop_target, walk  # noqa: E402
from legacy_gate_backtest import _bootstrap  # noqa: E402
from _robust import jump_in_window, robust_stats, robust_verdict  # noqa: E402
from swing import momentum  # noqa: E402  (the SAME scoring code the live /swing/rank uses)

WINDOW = 260                       # bars the live ranker sees (market-data keeps at most 260 daily rows)
EXITS = ("hold", "stop", "trail")
SETUPS = ("ALL", "TOP", "SCREEN", "SCREEN_DIP", "SCREEN_BRK")
PRIMARY_SETUP, PRIMARY_EXIT = "SCREEN", "trail"
DIP3_PCT = -1.0
BRK_VOLX = 1.5
MIN_SPLIT_N = 30

_COST_KEYS = ("BROKERAGE_PER_ORDER", "STT_DELIVERY_PCT_PER_LEG", "EXCHANGE_TXN_PCT", "IPFT_PCT", "SEBI_TURNOVER_PCT", "GST_PCT",
              "STAMP_DUTY_BUY_PCT_DELIVERY", "DP_CHARGE_FLAT")
_COST_DEFAULTS = {"BROKERAGE_PER_ORDER": 0.0, "STT_DELIVERY_PCT_PER_LEG": 0.10, "EXCHANGE_TXN_PCT": 0.00297, "IPFT_PCT": 0.0001,
                  "SEBI_TURNOVER_PCT": 0.0001, "GST_PCT": 18.0, "STAMP_DUTY_BUY_PCT_DELIVERY": 0.015, "DP_CHARGE_FLAT": 12.5}


def cost_constants() -> Tuple[Dict[str, float], str]:
    """The delivery charge constants: from real-trade config.py when it can be loaded, else the same defaults config.py ships with."""
    p = ROOT / "services" / "real-trade-service" / "config.py"
    try:
        spec = importlib.util.spec_from_file_location("_swing_bt_rt_config", str(p))
        mod = importlib.util.module_from_spec(spec)      # type: ignore[arg-type]
        spec.loader.exec_module(mod)                      # type: ignore[union-attr]
        return {k: float(getattr(mod, k)) for k in _COST_KEYS}, "real-trade config.py"
    except Exception:  # noqa: BLE001
        return dict(_COST_DEFAULTS), "built-in defaults (the values config.py ships with)"


def delivery_round_trip_pct(value: float, c: Optional[Dict[str, float]] = None) -> float:
    """Round-trip cost of a delivery (CNC) buy sold on a LATER day, as % of the trade value. Mirrors cost_model.estimate_round_trip_cost (is_delivery_sell=True)."""
    c = c or _COST_DEFAULTS
    v = float(value)
    if v <= 0:
        return 0.0
    brokerage = c["BROKERAGE_PER_ORDER"] * 2
    stt = 2 * v * c["STT_DELIVERY_PCT_PER_LEG"] / 100.0
    stamp = v * c["STAMP_DUTY_BUY_PCT_DELIVERY"] / 100.0
    exch = 2 * v * (c["EXCHANGE_TXN_PCT"] + c["IPFT_PCT"]) / 100.0
    sebi = 2 * v * c["SEBI_TURNOVER_PCT"] / 100.0
    gst = (brokerage + exch + sebi) * c["GST_PCT"] / 100.0
    dp = c["DP_CHARGE_FLAT"] * (1.0 + c["GST_PCT"] / 100.0)
    return (brokerage + stt + stamp + exch + sebi + gst + dp) / v * 100.0


def walk_trail(bars: Sequence[Sequence[float]], start: int, entry: float, stop_pct: float, h: int) -> Tuple[float, str]:
    """Gross % of a long entered at `entry` in session `start`, held up to h sessions, stop ratcheted to (close - stop %) after every close. Stop first; a gap exits at the open."""
    stop = entry * (1 - stop_pct / 100.0)
    for k in range(start, start + h):
        o, lo, c = float(bars[k][O]), float(bars[k][L]), float(bars[k][C])
        if o <= stop:
            return (o / entry - 1) * 100.0, "stop"
        if lo <= stop:
            return (stop / entry - 1) * 100.0, "stop"
        stop = max(stop, c * (1 - stop_pct / 100.0))
    return (float(bars[start + h - 1][C]) / entry - 1) * 100.0, "time"


def rows_for(bars: Sequence[Sequence[float]], sym: str, sig_days: Set[str], hold: int, slip: float, stop_atr: float,
             jump_pct: float = 35.0) -> List[Dict[str, Any]]:
    """One row per signal day of this symbol: its momentum scores (bars up to that close only), entry-timing flags and the gross % of every exit variant."""
    rows: List[Dict[str, Any]] = []
    n = len(bars)
    if n < 130 + hold + 2:
        return rows
    closes = [float(b[C]) for b in bars]
    for i in range(125, n - hold - 1):
        day = ist_date(bars[i][T])
        if day not in sig_days:
            continue
        sc = momentum.score_symbol(sym, bars[max(0, i - WINDOW + 1): i + 1])
        if sc["status"] not in ("ok", "limited"):
            continue
        entry_open = float(bars[i + 1][O])
        if entry_open <= 0 or closes[i] <= 0:
            continue
        entry = entry_open * (1.0 + slip / 100.0)
        sp, _ = stop_target(atr_pct(bars, i), stop_atr, 1.0)
        g_stop, _ = walk(bars, i + 1, entry, sp, 1e9, hold)
        g_trail, how = walk_trail(bars, i + 1, entry, sp, hold)
        vols = [float(b[V]) for b in bars[max(0, i - 20): i]]
        avgv = (sum(vols) / len(vols)) if vols else 0.0
        prior_hi = max(float(b[H]) for b in bars[max(0, i - 20): i]) if i >= 1 else 0.0
        sc.update({
            "sym": sym, "day": day, "month": day[:7], "entry_slip_pct": slip, "stop_pct": round(sp, 3),
            "g_hold": (closes[i + hold] / entry - 1.0) * 100.0, "g_stop": g_stop, "g_trail": g_trail, "stopped": how == "stop",
            "dip3": bool(i >= 3 and (closes[i] / closes[i - 3] - 1.0) * 100.0 <= DIP3_PCT),
            "brk20": bool(prior_hi > 0 and closes[i] >= prior_hi and avgv > 0 and float(bars[i][V]) >= BRK_VOLX * avgv),
            "jump": jump_in_window(closes, i - 20, i + hold + 1, jump_pct) if jump_pct > 0 else False,
        })
        for k in ("g_hold", "g_stop", "g_trail"):
            sc[k] = round(sc[k], 4)
        rows.append(sc)
    return rows


def _job(a: Tuple[Any, ...]) -> Dict[str, Any]:
    path, sym, sig_days, hold, slip, stop_atr, jump_pct = a
    try:
        return {"sym": sym, "err": None, "rows": rows_for(load_bars(Path(path)), sym, set(sig_days), hold, slip, stop_atr, jump_pct)}
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "err": f"{type(e).__name__}: {e}", "rows": []}


# ───────────────────────────── cross-section and statistics ─────────────────────────────

def rank_days(rows: Sequence[Dict[str, Any]], top_pct: float) -> List[Dict[str, Any]]:
    """Rank every signal day's stocks together (the live rank_scores), keep the ranked rows, attach the setup flags."""
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    out: List[Dict[str, Any]] = []
    for day in sorted(by_day):
        for r in momentum.rank_scores(by_day[day], min_pct_rank=top_pct):
            if r.get("rank") is None:
                continue
            r["top"] = bool(r["pct_rank"] >= top_pct)
            out.append(r)
    return out


SETUP_FN = {
    "ALL": lambda r: True,
    "TOP": lambda r: bool(r["top"]),
    "SCREEN": lambda r: bool(r["screen"]),
    "SCREEN_DIP": lambda r: bool(r["screen"] and r["dip3"]),
    "SCREEN_BRK": lambda r: bool(r["screen"] and r["brk20"]),
}


def day_mean_ci(rows: Sequence[Dict[str, Any]], key: str, n_boot: int, seed: int = 7) -> Optional[List[float]]:
    """90 % interval of the mean, resampling whole signal days (the stocks of one day move together). None under 20 days."""
    cl: Dict[str, List[float]] = {}
    for r in rows:
        a = cl.setdefault(r["day"], [0.0, 0.0])
        a[0] += 1
        a[1] += r[key]
    arr = list(cl.values())
    if len(arr) < 20 or n_boot < 50:
        return None
    rng = random.Random(seed)
    m = len(arr)
    means: List[float] = []
    for _ in range(n_boot):
        n = s = 0.0
        for k in rng.choices(range(m), k=m):
            n += arr[k][0]
            s += arr[k][1]
        if n:
            means.append(s / n)
    if len(means) < n_boot // 2:
        return None
    means.sort()
    return [round(means[int(0.05 * (len(means) - 1))], 3), round(means[int(0.95 * (len(means) - 1))], 3)]


def summarize(rows: Sequence[Dict[str, Any]], key: str, n_boot: int) -> Dict[str, Any]:
    vals = [r[key] for r in rows]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "win_pct": None, "ci90": None}
    return {"n": len(vals), "mean": round(mean(vals), 3), "median": round(median(vals), 3),
            "win_pct": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1), "ci90": day_mean_ci(rows, key, n_boot)}


def edge_report(pop: Sequence[Dict[str, Any]], pred, key: str, min_n: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """Selected minus the rest of the population, same cluster tests and verdict words as the other replays."""
    rej = lambda r: not pred(r)  # noqa: E731
    ps = [r for r in pop if pred(r)]
    rj = [r for r in pop if rej(r)]
    mp, mr = (mean(r[key] for r in ps) if ps else None), (mean(r[key] for r in rj) if rj else None)
    edge = round(mp - mr, 3) if (mp is not None and mr is not None) else None
    ci = _bootstrap(pop, rej, key, n_boot, seed) if edge is not None else None
    if len(ps) < min_n or len(rj) < min_n:
        v = "insufficient_sample"
    elif ci and ci[0] > 0:
        v = "helps"
    elif ci and ci[1] < 0:
        v = "hurts"
    else:
        v = "no_edge"
    out: Dict[str, Any] = {"selected": len(ps), "rest": len(rj), "edge_pp": edge, "ci90": ci, "verdict": v}
    rs = robust_stats(pop, rej, key, n_boot, seed)
    if rs:
        out.update(rs)
    out["verdict_robust"] = robust_verdict(v, rs)
    return out


def bench_regime(folder: Path) -> Dict[str, bool]:
    """day -> True when the benchmark closed above its 200-day average (bull), False when below. Empty without a benchmark file."""
    for f in sorted(folder.glob("*.json")):
        if f.stem.upper() in BENCH_NAMES:
            try:
                b = load_bars(f)
            except Exception:  # noqa: BLE001
                return {}
            cl = [float(x[C]) for x in b]
            return {ist_date(b[i][T]): bool(cl[i] > sum(cl[i - 199: i + 1]) / 200.0) for i in range(199, len(b))}
    return {}


def build_report(ranked: Sequence[Dict[str, Any]], sizes: Sequence[float], primary_size: float, hold: int, min_n: int, n_boot: int,
                 cost_c: Dict[str, float], bull: Dict[str, bool]) -> Dict[str, Any]:
    costs = {str(int(s)): round(delivery_round_trip_pct(s, cost_c), 3) for s in sizes}
    rep: Dict[str, Any] = {"cost_pct_by_size": costs, "setups": {}, "primary": {}}
    pop = list(ranked)
    for name in SETUPS:
        pred = SETUP_FN[name]
        sel = [r for r in pop if pred(r)]
        block: Dict[str, Any] = {"n": len(sel), "days": len({r["day"] for r in sel}), "exits": {}}
        for ex in EXITS:
            g = f"g_{ex}"
            gross = summarize(sel, g, n_boot)
            per_size: Dict[str, Any] = {}
            for s in sizes:
                cpct = delivery_round_trip_pct(s, cost_c)
                for r in sel:
                    r["net"] = r[g] - cpct
                per_size[str(int(s))] = summarize(sel, "net", n_boot)
            block["exits"][ex] = {"gross": gross, "net_by_size": per_size,
                                  "edge_vs_rest_gross": edge_report(pop, pred, g, min_n, n_boot) if name != "ALL" else None}
        block["stopped_pct"] = round(100.0 * sum(1 for r in sel if r["stopped"]) / len(sel), 1) if sel else None
        rep["setups"][name] = block
    # the verdict: ONE pre-declared primary
    pred = SETUP_FN[PRIMARY_SETUP]
    sel = [r for r in pop if pred(r)]
    cpct = delivery_round_trip_pct(primary_size, cost_c)
    g = f"g_{PRIMARY_EXIT}"
    for r in pop:
        r["net"] = r[g] - cpct
    net = summarize(sel, "net", n_boot)
    edge = rep["setups"][PRIMARY_SETUP]["exits"][PRIMARY_EXIT]["edge_vs_rest_gross"]
    splits: Dict[str, Any] = {"year": {}, "regime": {}}
    for r in sel:
        splits["year"].setdefault(r["day"][:4], []).append(r)
        splits["regime"].setdefault({True: "bull (benchmark above 200d)", False: "bear (benchmark below 200d)"}.get(bull.get(r["day"]), "unknown"), []).append(r)
    split_out = {k: {b: summarize(v, "net", 0) for b, v in sorted(d.items())} for k, d in splits.items()}
    bad_years = [y for y, s in split_out["year"].items() if s["n"] >= MIN_SPLIT_N and s["mean"] is not None and s["mean"] < 0]
    reasons: List[str] = []
    if net["n"] < min_n or (edge and edge["rest"] < min_n):
        verdict = "INSUFFICIENT"
        reasons.append(f"fewer than {min_n} primary entries or others (selected {net['n']}, rest {edge['rest'] if edge else 0})")
    else:
        ci = net["ci90"]
        ok_net = bool(ci and ci[0] > 0)
        ok_edge = bool(edge and edge["verdict_robust"] == "helps")
        ok_years = len(bad_years) <= 1
        if not ok_net:
            reasons.append("net mean per trade: 90% interval does not stay above zero" if ci else "net mean: too few signal days for an interval")
        if not ok_edge:
            reasons.append(f"edge over the rest of the ranked stocks is not proven (robust verdict: {edge['verdict_robust'] if edge else 'n/a'})")
        if not ok_years:
            reasons.append("negative net mean in " + ", ".join(bad_years))
        verdict = "USEFUL" if (ok_net and ok_edge and ok_years) else "NO_EDGE"
        gross_sel = summarize(sel, g, 0)
        if verdict == "NO_EDGE" and gross_sel["mean"] is not None and gross_sel["mean"] > 0 and (net["mean"] is None or net["mean"] <= 0):
            reasons.append(f"gross mean {gross_sel['mean']}% is positive but costs ({round(cpct, 2)}% at Rs {int(primary_size)}) eat it: a larger trade size lowers the cost")
    rep["primary"] = {"setup": PRIMARY_SETUP, "exit": PRIMARY_EXIT, "size": primary_size, "cost_pct": round(cpct, 3), "hold": hold, "net": net,
                      "edge_vs_rest_gross": edge, "splits": split_out, "negative_years": bad_years, "verdict": verdict, "reasons": reasons}
    return rep


def to_markdown(rep: Dict[str, Any], meta: Dict[str, Any]) -> str:
    p = rep["primary"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L_ = [f"VERDICT: {p['verdict']}", "",
          f"# Swing momentum replay: top-ranked stocks held up to {p['hold']} sessions, net of delivery costs", "",
          f"Primary (declared before looking): setup **{p['setup']}**, exit **{p['exit']}**, trade size **Rs {int(p['size'])}** (round-trip cost {p['cost_pct']}%).", ""]
    n = p["net"]
    L_ += [f"Primary net result: {n['n']} entries, mean {f(n['mean'])}%, median {f(n['median'])}%, win {f(n['win_pct'])}%, 90% interval (resampling days) {f(n['ci90'])}.", ""]
    e = p["edge_vs_rest_gross"] or {}
    L_ += [f"Edge over the other ranked stocks (gross, same exit): {f(e.get('edge_pp'))} pp, interval {f(e.get('ci90'))}, day-cluster interval {f(e.get('day_ci'))}, robust verdict **{f(e.get('verdict_robust'))}**.", ""]
    if p["reasons"]:
        L_ += ["Why not USEFUL:" if p["verdict"] != "USEFUL" else "Notes:"] + [f"- {x}" for x in p["reasons"]] + [""]
    L_ += ["## Cost of a round trip by trade size (delivery, sold on a later day)", "", "| Rs per trade | cost % |", "| --- | --- |"]
    L_ += [f"| {k} | {v} |" for k, v in rep["cost_pct_by_size"].items()] + [""]
    sizes = list(rep["cost_pct_by_size"])
    L_ += ["## Net mean % per trade by setup, exit and size (n entries; exploratory except the primary)", "",
           "| setup | exit | n | gross mean % | " + " | ".join(f"net @ Rs {s}" for s in sizes) + " | stopped % |", "| --- | --- | --- | --- | " + " | ".join("---" for _ in sizes) + " | --- |"]
    for name in SETUPS:
        b = rep["setups"][name]
        for ex in EXITS:
            x = b["exits"][ex]
            L_.append(f"| {name} | {ex} | {b['n']} | {f(x['gross']['mean'])} | " + " | ".join(f(x["net_by_size"][s]["mean"]) for s in sizes) + f" | {f(b['stopped_pct']) if ex != 'hold' else '-'} |")
    L_ += ["", "## Primary by calendar year and benchmark regime (net, primary size)", "", "| bucket | n | mean % | median % | win % |", "| --- | --- | --- | --- | --- |"]
    for kind in ("year", "regime"):
        for b, s in p["splits"][kind].items():
            L_.append(f"| {b} | {s['n']} | {f(s['mean'])} | {f(s['median'])} | {f(s['win_pct'])} |")
    L_ += ["", "## Run", "", f"- {meta['symbols']} symbols ({meta['skipped']} skipped), signal every {meta['step']} sessions, {meta['days']} signal days, {meta['rows']} ranked stock-days, {meta['jump_rows']} dropped as split/bad-candle suspects",
           f"- slippage on entry {meta['slip_pct']}%, stop = ATR14 x {meta['stop_atr']} (clamped 2-6%), calendar: {meta['calendar']}, charges from: {meta['cost_source']}",
           "- exploratory setups are NOT findings: only the primary carries the verdict. A positive row elsewhere needs its own re-test on new data before it is trusted.",
           "- survivorship: the folder holds today's symbols, which flatters momentum. A USEFUL here is a reason to run the shadow lane, not to trade real money."]
    return "\n".join(L_)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--sizes", default="3000,10000,20000,50000")
    ap.add_argument("--primary-size", type=float, default=20000.0)
    ap.add_argument("--top-pct", type=float, default=80.0)
    ap.add_argument("--cost-slip-pct", type=float, default=0.10, dest="slip")
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--min-n", type=int, default=200)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--jump-pct", type=float, default=35.0)
    ap.add_argument("--checkpoint", default="")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    sizes = [float(x) for x in a.sizes.split(",") if x.strip()]
    if a.primary_size not in sizes:
        sizes.append(a.primary_size)
    sizes = sorted(set(sizes))
    files = [f for f in sorted(d.glob("*.json")) if f.stem.upper() not in BENCH_NAMES]
    pool = len(files)
    if a.sample > 0 and pool > a.sample:
        files = sorted(random.Random(a.seed).sample(files, a.sample))
    cal = load_calendar(d)
    if cal is None:
        print("WARNING: no ^NSEI.json in the folder - signal days are the union of the symbols' own dates and the bull / bear split is unavailable", file=sys.stderr)
        cal = set()
        for f in files:
            try:
                cal |= {ist_date(b[T]) for b in load_bars(f)}
            except Exception:  # noqa: BLE001
                continue
    days = sorted(cal)
    sig = frozenset(days[:: max(1, a.step)])
    cost_c, cost_src = cost_constants()
    jobs = [(str(f), f.stem.upper(), sig, a.hold, a.slip, a.stop_atr, a.jump_pct) for f in files]
    from _progress import file_stamp, run_jobs, settings_stamp
    st = settings_stamp(a, str(d))
    results = run_jobs(_job, jobs, a.jobs, "swing", ckpt=a.checkpoint, resume=a.resume, stamp=lambda j: file_stamp(j[0], st))
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    clean = [r for r in rows if not r.get("jump")]
    ranked = rank_days(clean, a.top_pct)
    out = Path(a.out) if a.out else d / "swing_out"
    out.mkdir(parents=True, exist_ok=True)
    if not ranked:
        msg = "VERDICT: INSUFFICIENT\n\nNO RANKED DAYS: the history folder needs 5+ symbol files with 250+ daily candles on the same dates."
        (out / "swing_backtest_report.md").write_text(msg)
        (out / "swing_backtest_report.json").write_text(json.dumps({"primary": {"verdict": "INSUFFICIENT"}, "reasons": ["nothing ranked"]}, indent=1))
        print(msg)
        return 1
    rep = build_report(ranked, sizes, a.primary_size, a.hold, a.min_n, a.boot, cost_c, bench_regime(d))
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "step": a.step, "days": len({r["day"] for r in ranked}), "rows": len(ranked), "jump_rows": len(rows) - len(clean),
            "slip_pct": a.slip, "stop_atr": a.stop_atr, "calendar": "^NSEI benchmark dates" if load_calendar(d) is not None else "union of the symbols' own dates", "cost_source": cost_src,
            "sample": {"requested": a.sample, "pool": pool, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep["meta"] = meta
    (out / "swing_backtest_report.json").write_text(json.dumps(rep, indent=1, default=str))
    md = to_markdown(rep, meta)
    (out / "swing_backtest_report.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
