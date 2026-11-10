"""backtest/report.py - save a result as JSON and render it as plain text (the Calibration page offers the txt for download). Every result carries its reliability label."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

RELIABILITY = {
    "EOD": "REAL end-of-day option prices (NSE F&O bhavcopy). Coarse: one daily bar per contract, intraday order of events unknown (the stop is assumed first).",
    "SIM": "SIMULATED: option prices are Black-Scholes from index candles and VIX. NOT real prices: no true spread, IV skew or liquidity. Treat as an upper-bound sanity check only.",
    "ROLLING": "REAL premiums: Dhan's recorded expired-option candles (5-minute path), the strategies run on the index candles. Real prices, synthetic spread; limits are listed in the result's notes.",
    "REPLAY": "RECORDED chains: real premiums, bids and asks captured by this service, replayed through the live engine code. The most trustworthy, but only as long as the recording.",
}


def job_name(kind: str, underlyings: Any, strategy: str = "CORE") -> str:
    """Round 285: the file name stem of a backtest started from the service (/fno/backtests/run): kind + underlying(s) + strategy, the same style the command-line tools use
    (fno_sim_nifty, fno_sim_nifty_portfolio, fno_replay_nifty-sensex). Before this it was just fno_sim / fno_replay."""
    unds = [str(u).lower() for u in ([underlyings] if isinstance(underlyings, str) else list(underlyings or []))] or ["nifty"]
    tail = "" if str(kind) == "replay" or str(strategy).upper() == "CORE" else "_" + ("portfolio" if str(strategy).upper() == "ALL" else str(strategy).lower())
    return f"fno_{kind}_{'-'.join(unds)}{tail}"


def request_problem(kind: Any, underlyings: Any, strategy: Any = "CORE", days: Any = None, valid_strategies: Any = ()) -> Any:
    """Round 303: why a backtest request from the service's API must be refused, or None. Pure, so it is tested without the web framework. New: a SIMULATION runs ONE index per job (it used to take the first of
    several silently, and the result file was then named for all of them, e.g. fno_sim_nifty-sensex.json holding a NIFTY-only run)."""
    import re
    if kind not in ("replay", "sim"):
        return "kind must be replay or sim"
    if days is not None and any(not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d)) for d in days):
        return "days must be YYYY-MM-DD (they name folders under snapshots/)"
    unds = [str(u).upper() for u in (underlyings or [])]
    if not unds or any(u not in ("NIFTY", "SENSEX") for u in unds):
        return "underlyings must be NIFTY and/or SENSEX"
    if kind == "sim" and len(set(unds)) > 1:
        return "the simulation runs one index per job: pick NIFTY or SENSEX (run it twice for both)"
    if kind == "sim" and str(strategy).upper() != "ALL" and str(strategy).upper() not in list(valid_strategies):
        return f"strategy must be ALL or one of {list(valid_strategies)}"
    return None


def save(root: str, name: str, result: Dict[str, Any]) -> str:
    """Writes backtests/<name>_<YYYYmmdd_HHMMSS>.json and NEVER overwrites: round 285, two results with the same name finished in the same second (the stamp has no finer part) and the second silently
    replaced the first; now the second gets a _2, _3 ... suffix. The caller's .txt companion is path[:-5] + '.txt', so it stays unique too."""
    d = os.path.join(root, "backtests")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for n in range(1, 1000):
        path = os.path.join(d, f"{name}_{stamp}{'' if n == 1 else f'_{n}'}.json")
        try:
            fh = open(path, "x", encoding="utf-8")
        except FileExistsError:
            continue
        with fh:
            json.dump(result, fh, default=str)
        return path
    raise OSError("too many backtest results with the same name in one second")


def render_text(r: Dict[str, Any]) -> str:
    o = r.get("metrics", {}).get("overall", {})
    v = r.get("verdict", {})
    L = [f"F&O OPTIONS BACKTEST  -  {r.get('kind')}  -  {r.get('underlying', 'NIFTY+SENSEX')}" + (f"  -  strategy {r['strategy']}" if r.get("strategy") else ""), "=" * 78,
         "RELIABILITY: " + RELIABILITY.get(r.get("kind", ""), "?"), "",
         f"period: {r.get('from')} .. {r.get('to')}   days tested: {r.get('days')}   capital: {r.get('capital')}", "",
         f"trades {o.get('trades')}   win rate {o.get('win_rate_pct')}%   gross {o.get('gross')}   charges {o.get('charges')}   NET {o.get('net')}",
         f"avg win {o.get('avg_win')}   avg loss {o.get('avg_loss')}   profit factor {o.get('profit_factor')}   expectancy {o.get('expectancy')}",
         f"max drawdown {o.get('max_drawdown')} ({o.get('max_drawdown_pct')}%)   longest losing streak {o.get('longest_losing_streak')}", ""]
    if r.get("by_strategy"):
        L.append("BY STRATEGY (each simulated alone)")
        for b in r["by_strategy"]:
            L.append(f"  {b['strategy']:<15} trades {b['trades']:>4}  win {b['win_rate_pct']}%  net {b['net']:>12}  pf {b['profit_factor']}  maxDD {b['max_drawdown']}  {b['verdict']}")
        L.append("")
    for k in ("by_underlying", "by_side", "by_exit", "by_dte"):
        L.append(k.upper())
        for name, b in (r.get("metrics", {}).get(k) or {}).items():
            L.append(f"  {name:<12} trades {b['trades']:>4}  win {b['win_rate_pct']}%  net {b['net']:>12}  charges {b['charges']:>10}")
        L.append("")
    rb = r.get("metrics", {}).get("right_but_lost", {})
    L.append(f"direction right but the trade still lost after charges: {rb.get('gross_positive_net_negative')}   stopped out after trading up 10%+: {rb.get('stopped_after_trading_up')}")
    for f in r.get("metrics", {}).get("folds", []):
        L.append(f"  fold {f['fold']}: {f['from']}..{f['to']}  trades {f['trades']}  net {f['net']}  win {f['win_rate_pct']}%")
    dq = r.get("data_quality")
    if dq:
        L.append(f"data quality: strikes fetched {', '.join(dq.get('labels') or [])}   held bars without a candle {dq.get('unmanaged_bars')}/{dq.get('held_bars')} ({dq.get('unmanaged_pct')}%)   exits at last known price {dq.get('unpriced_exit_pct')}%   days without a usable VIX {dq.get('vix_unknown_days', '-')} ({dq.get('vix_unknown_pct', '-')}%)   {'OK' if dq.get('ok') else 'NOT OK'}")
    L += ["", f"VERDICT: {v.get('status')}"] + [f"  - {x}" for x in v.get("reasons", [])]
    if r.get("notes"):
        L += ["", "NOTES"] + [f"  - {x}" for x in r["notes"]]
    L += ["", "A backtest describes the past under its own assumptions. It is not a forecast, and it is not advice."]
    return "\n".join(L)
