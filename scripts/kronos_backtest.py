#!/usr/bin/env python3
"""kronos_backtest.py - group 425: would the Kronos gate have helped? Replays decision days over daily candles with NO look-ahead, asks the
kronos-forecast-service worker for a forecast on each, applies the SAME veto rules as entry_engine/kronos_gate.py, and compares what really
happened to vetoed vs passed candidates, net of round-trip cost.

  python3 scripts/kronos_backtest.py DIR [--worker http://localhost:8010] [--per-symbol 5] [--seed 7] [--horizon 5] [--paths 10] [--cost-pct 0.35]
        [--min-edge-x-cost 1.0] [--min-p-target 0.10] [--out results.jsonl] [--max-forecasts 0] [--min-bars 250] [--timeout 120]

DIR holds one JSON per symbol (the market-data /history reply or a bare candle list) - the folder scripts/fetch_history_for_backtest.py writes
(e.g. --sample 400 --seed 7). The worker must be running (docker compose up kronos-forecast-service, port-forwarded, or run it locally).
Per symbol, --per-symbol decision days are drawn at random (seeded). On decision day t the worker sees ONLY bars before t; the entry is the open of
day t; stop / target are the entry engine's ATR rules (1.5 x ATR stop clamped 2-6 %, target 2 x stop); the real outcome walks bars t .. t+H-1 (stop first
when one bar holds both; a gap through the stop exits at the open). Results are appended to --out (JSON lines) so an interrupted run resumes.

READ THE RESULT WITH CARE: decision days are drawn from the whole sample, not from the live scanner's candidates; 10 paths per forecast is a noisy
estimate; and a few hundred vetoes cannot prove a small edge (the report prints a bootstrap interval for the difference). Enable the gate only if
vetoed rows are clearly worse than passed rows (interval below zero) AND passed rows are positive net of cost. Otherwise leave it off.
Stdlib only (the worker does the modelling).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
ATR_STOP_MULT, ATR_TARGET_MULT, MIN_STOP_PCT, MAX_STOP_PCT = 1.5, 3.0, 2.0, 6.0      # mirror entry_engine/entry.py
ATR_LEN = 14
SEND_BARS = 200


def load_symbol(path: Path) -> List[Dict[str, Any]]:
    doc = json.loads(path.read_text())
    c = doc.get("candles") if isinstance(doc, dict) else doc
    out = []
    for r in c or []:
        try:
            lo = {str(k).lower(): v for k, v in r.items()}
            out.append({"date": str(lo["date"])[:10], "open": float(lo["open"]), "high": float(lo["high"]), "low": float(lo["low"]),
                        "close": float(lo["close"]), "volume": float(lo.get("volume") or 0.0)})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: r["date"])
    return out


def atr_pct(bars: List[Dict[str, Any]], n: int = ATR_LEN) -> Optional[float]:
    """Simple ATR(n) as a % of the last close, from the bars given. None when there are not enough."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    last = bars[-1]["close"]
    return (sum(trs) / n) / last * 100.0 if last > 0 else None


def stop_target_pct(atr: Optional[float]) -> Tuple[float, float]:
    if atr is None or atr <= 0:
        return 3.0, 6.0
    stop = max(MIN_STOP_PCT, min(atr * ATR_STOP_MULT, MAX_STOP_PCT))
    return round(stop, 2), round(stop * (ATR_TARGET_MULT / ATR_STOP_MULT), 2)


def realized_outcome(future: List[Dict[str, Any]], entry: float, stop_pct: float, target_pct: float) -> Tuple[float, str]:
    """(gross % return, 'target'|'stop'|'time') walking real bars; stop first when one bar holds both; a gap through the stop exits at the open."""
    stop, target = entry * (1 - stop_pct / 100.0), entry * (1 + target_pct / 100.0)
    for j, b in enumerate(future):
        if j > 0 and b["open"] <= stop:
            return (b["open"] / entry - 1) * 100.0, "stop"
        if b["low"] <= stop:
            return -stop_pct, "stop"
        if b["high"] >= target:
            return target_pct, "target"
    return (future[-1]["close"] / entry - 1) * 100.0, "time"


def draw_days(n_bars: int, per_symbol: int, horizon: int, min_bars: int, rng: random.Random) -> List[int]:
    """Decision-day indices t: at least `min_bars` bars before it, `horizon` bars after it (the outcome must be fully known)."""
    lo, hi = min_bars, n_bars - horizon                              # bars[t:t+horizon] must exist
    if hi < lo or per_symbol <= 0:
        return []
    pool = list(range(lo, hi + 1))
    return sorted(rng.sample(pool, min(per_symbol, len(pool))))


def post_forecast(worker: str, symbol: str, bars: List[Dict[str, Any]], ref_price: float, stop_pct: float, target_pct: float,
                  horizon: int, paths: int, timeout: float) -> Optional[Dict[str, Any]]:
    body = json.dumps({"symbol": symbol, "ref_price": ref_price, "stop_pct": stop_pct, "target_pct": target_pct, "horizon": horizon,
                       "n_paths": paths, "candles": bars[-SEND_BARS:], "as_of_today": False}).encode()
    req = urllib.request.Request(worker.rstrip("/") + "/forecast", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def verdict_for(forecast: Optional[Dict[str, Any]], cost_pct: float, min_edge_x_cost: float, min_p_target: float) -> Dict[str, Any]:
    """The live gate's own evaluate(), with the thresholds from the command line."""
    _svc = str(ROOT / "services" / "real-trade-service")
    if _svc not in sys.path:
        sys.path.insert(0, _svc)
    import config
    from entry_engine import kronos_gate
    old = (config.KRONOS_MIN_EDGE_X_COST, config.KRONOS_MIN_P_TARGET)
    config.KRONOS_MIN_EDGE_X_COST, config.KRONOS_MIN_P_TARGET = min_edge_x_cost, min_p_target
    try:
        return kronos_gate.evaluate(forecast, cost_pct)
    finally:
        config.KRONOS_MIN_EDGE_X_COST, config.KRONOS_MIN_P_TARGET = old


def mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def bootstrap_diff(a: List[float], b: List[float], iters: int = 2000, seed: int = 1) -> Optional[Tuple[float, float]]:
    """90 % bootstrap interval for mean(a) - mean(b). None when either side has fewer than 5 rows."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        ma = sum(rng.choice(a) for _ in a) / len(a)
        mb = sum(rng.choice(b) for _ in b) / len(b)
        diffs.append(ma - mb)
    diffs.sort()
    return diffs[int(0.05 * iters)], diffs[int(0.95 * iters) - 1]


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: {symbol, date, opinion, would_reject, net_pct, exit}. 'passed' = every row that was not vetoed (no opinion counts as passed, as live)."""
    ok = [r for r in rows if r.get("opinion") == "ok"]
    vetoed = [r["net_pct"] for r in rows if r.get("would_reject")]
    passed = [r["net_pct"] for r in rows if not r.get("would_reject")]
    allr = [r["net_pct"] for r in rows]
    ci = bootstrap_diff(vetoed, passed)
    mv, mp = mean(vetoed), mean(passed)
    verdict = "NO DATA"
    if ci is not None and mv is not None and mp is not None:
        if ci[1] < 0 and mp > 0:
            verdict = "VETO HELPS: vetoed rows are clearly worse and passed rows are positive net of cost"
        elif ci[1] < 0:
            verdict = "VETO SEPARATES but passed rows are not positive net of cost - the gate alone is not an edge"
        else:
            verdict = "NOT PROVEN: the vetoed-vs-passed difference is not clearly below zero - leave the gate off"
    return {"n": len(rows), "n_opinion": len(ok), "n_vetoed": len(vetoed), "n_passed": len(passed),
            "mean_net_all": mean(allr), "mean_net_vetoed": mv, "mean_net_passed": mp,
            "win_vetoed": (sum(1 for x in vetoed if x > 0) / len(vetoed)) if vetoed else None,
            "win_passed": (sum(1 for x in passed if x > 0) / len(passed)) if passed else None,
            "diff_ci90": ci, "verdict": verdict}


def _fmt(x: Any, pct: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.0f}%" if pct else f"{x:+.2f}%"


def print_report(s: Dict[str, Any]) -> None:
    print(f"rows {s['n']}  with a forecast opinion {s['n_opinion']}  vetoed {s['n_vetoed']}  passed {s['n_passed']}")
    print(f"mean net return   all {_fmt(s['mean_net_all'])}   vetoed {_fmt(s['mean_net_vetoed'])}   passed {_fmt(s['mean_net_passed'])}")
    print(f"win rate          vetoed {_fmt(s['win_vetoed'], True)}   passed {_fmt(s['win_passed'], True)}")
    ci = s["diff_ci90"]
    print("vetoed - passed   90% interval: " + (f"[{ci[0]:+.2f}, {ci[1]:+.2f}] points" if ci else "n/a (fewer than 5 rows on a side)"))
    print("VERDICT: " + s["verdict"])


def run(args: argparse.Namespace, forecast_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None) -> int:
    forecast_fn = forecast_fn or post_forecast
    out_path = Path(args.out)
    done = set()
    rows: List[Dict[str, Any]] = []
    if out_path.exists():
        for ln in out_path.read_text().splitlines():
            try:
                r = json.loads(ln)
                rows.append(r)
                done.add((r["symbol"], r["date"]))
            except (ValueError, KeyError):
                pass
    files = sorted(p for p in Path(args.dir).glob("*.json") if not p.name.startswith("^"))
    fresh = 0
    with out_path.open("a") as fh:
        for p in files:
            bars = load_symbol(p)
            sym = p.stem.upper()
            rng = random.Random(f"{args.seed}:{sym}")                    # per-symbol stream: a symbol's days do not depend on the others
            for t in draw_days(len(bars), args.per_symbol, args.horizon, args.min_bars, rng):
                if (sym, bars[t]["date"]) in done:
                    continue
                if args.max_forecasts and fresh >= args.max_forecasts:
                    break
                history = bars[:t]                                           # bars strictly BEFORE the decision day
                entry = bars[t]["open"]
                sp, tp = stop_target_pct(atr_pct(history))
                fc = forecast_fn(args.worker, sym, history, entry, sp, tp, args.horizon, args.paths, args.timeout)
                v = verdict_for(fc, args.cost_pct, args.min_edge_x_cost, args.min_p_target)
                gross, how = realized_outcome(bars[t:t + args.horizon], entry, sp, tp)
                r = {"symbol": sym, "date": bars[t]["date"], "opinion": v["opinion"], "would_reject": bool(v["would_reject"]),
                     "ev_pct": v.get("ev_pct"), "p_target_first": v.get("p_target_first"), "stop_pct": sp, "target_pct": tp,
                     "gross_pct": round(gross, 4), "net_pct": round(gross - args.cost_pct, 4), "exit": how}
                fh.write(json.dumps(r) + "\n")
                fh.flush()
                rows.append(r)
                fresh += 1
            if args.max_forecasts and fresh >= args.max_forecasts:
                break
    print(f"{fresh} new forecasts, {len(rows)} rows in {out_path}")
    print_report(summarize(rows))
    return 0 if rows else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dir")
    ap.add_argument("--worker", default=os.getenv("KRONOS_URL") or "http://localhost:8010")
    ap.add_argument("--per-symbol", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--paths", type=int, default=10)
    ap.add_argument("--cost-pct", type=float, default=0.35, help="round-trip cost as %% of trade value (default 0.35)")
    ap.add_argument("--min-edge-x-cost", type=float, default=1.0)
    ap.add_argument("--min-p-target", type=float, default=0.10)
    ap.add_argument("--min-bars", type=int, default=250)
    ap.add_argument("--max-forecasts", type=int, default=0, help="stop after this many NEW forecasts (0 = no limit)")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", default="kronos_backtest_results.jsonl")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
