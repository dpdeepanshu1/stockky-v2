"""backtest/portfolio.py - run every strategy (fno/strategies.py) through the SAME simulated intraday backtest and put the results side by side. Reliability: SIM (Black-Scholes prices).

This is the honest version of "backtest the portfolio": each strategy is run ALONE on the same candles, then listed in one table. The combined row simply adds the trades up (NAIVE): it does
not apply the live limits shared by all strategies (max open positions, max trades per day, one position per side), so the real engine would take fewer trades than the sum. A strategy that looks
good here has only passed a simulation on the days you gave it."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from fno import strategies
from fno.paper import Position
from fno.params import Params
from . import metrics, sim


def run(underlying: str, daily: Sequence[dict], intraday: Sequence[dict], vix_by_day: Optional[Dict[str, float]] = None, p: Optional[Params] = None,
        keys: Optional[Sequence[str]] = None, **kw: Any) -> Dict[str, Any]:
    p = p or Params()
    keys = list(keys or strategies.ORDER)
    runs = {k: sim.run(underlying, daily, intraday, vix_by_day, p, strategy=k, **kw) for k in keys}
    table = []
    trades = []
    for k, r in runs.items():
        o = r["metrics"]["overall"]
        spec = strategies.STRATEGIES[k]
        table.append({"strategy": k, "label": spec["label"], "ad": spec["ad"], "sides": spec["sides"], "days": r["days"], "trades": o["trades"], "win_rate_pct": o["win_rate_pct"], "net": o["net"],
                      "profit_factor": o["profit_factor"], "max_drawdown": o["max_drawdown"], "verdict": r["verdict"]["status"]})
        trades += [Position.from_json(t) for t in r["trades"]]
    m = metrics.summarize(trades, p.capital)
    first = next((r for r in runs.values()), {})
    return {"kind": "SIM", "strategy": "PORTFOLIO", "underlying": underlying.upper(), "from": first.get("from"), "to": first.get("to"), "days": first.get("days"), "capital": p.capital,
            "params": p.to_json(), "metrics": m, "verdict": metrics.verdict(m), "by_strategy": table, "trades": [t.to_json() for t in trades],
            "notes": ["each strategy was simulated ALONE; the overall block below is the NAIVE sum of all of them (live limits shared by the strategies are not applied)",
                      "premiums are Black-Scholes at the day's India VIX (or the default IV); spread is synthetic",
                      "the strategy RULES are this project's own, inspired by the names of an ad's algos: a SIM result says nothing about that ad's system"] + (first.get("notes") or [])[:2]}
