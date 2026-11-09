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


def save(root: str, name: str, result: Dict[str, Any]) -> str:
    d = os.path.join(root, "backtests")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, default=str)
    return path


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
    L += ["", f"VERDICT: {v.get('status')}"] + [f"  - {x}" for x in v.get("reasons", [])]
    if r.get("notes"):
        L += ["", "NOTES"] + [f"  - {x}" for x in r["notes"]]
    L += ["", "A backtest describes the past under its own assumptions. It is not a forecast, and it is not advice."]
    return "\n".join(L)
