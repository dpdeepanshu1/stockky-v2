"""scripts/fno_sizing_whatif.py - read-only: what would the CLOSED F&O DEMO trades have made at other capital / risk / lot settings?

Run on the VM (nothing is written, no order is sent):
  docker compose exec -T fno-options-service python - < scripts/fno_sizing_whatif.py
  docker compose exec -T fno-options-service python - --mode DEMO --days 60 < scripts/fno_sizing_whatif.py

Each trade is re-sized with the service's own rule (fno/sizing.py): lots = min( floor(capital x risk% / rupees-at-stop per lot), floor(capital x 35% / premium paid per lot), max_lots );
a trade that does not fit ONE lot is not taken. Its result is the recorded net P&L PER LOT x the new lots. Charges are scaled with the lots too (a little too high: the flat brokerage does not
grow), and fills / slippage are the recorded ones. It is arithmetic on a small sample, not a forecast: read the trade count first."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SHARE = 0.35                                   # fno/sizing.lots_for max_premium_share


def load(db_path: str, mode: str, days: int) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=days)).date().isoformat()
    con = sqlite3.connect(db_path)
    out = []
    for day, opened, data in con.execute("select day, opened_ts, data from positions where mode=? and status='CLOSED' and day>=? order by opened_ts", (mode, since)):
        j = json.loads(data)
        try:
            prem, lot, lots = float(j["entry_premium"]), int(j["lot_size"]), max(1, int(j.get("lots") or 1))
            stop_rs = (prem - float(j["sl_price"])) * lot                      # rupees lost per lot if the stop is hit (the trade's own stop)
            out.append({"day": day, "ts": float(opened), "premium": prem, "lot": lot, "risk_per_lot": stop_rs,
                        "net_per_lot": float(j["net_pnl"]) / lots, "gross_per_lot": float(j.get("gross_pnl", j["net_pnl"])) / lots})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def lots_for(t: Dict[str, Any], capital: float, risk_pct: float, max_lots: int) -> int:
    if t["risk_per_lot"] <= 0 or t["premium"] <= 0:
        return 0
    by_risk = math.floor(capital * risk_pct / 100.0 / t["risk_per_lot"] + 1e-9)
    by_cash = math.floor(capital * SHARE / (t["premium"] * t["lot"]) + 1e-9)
    return max(0, min(by_risk, by_cash, max_lots))


def scenario(trades: List[Dict[str, Any]], capital: float, risk_pct: float, max_lots: int) -> Dict[str, Any]:
    taken, net, peak, cum, worst_dd, worst_trade, skipped = 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0
    for t in trades:
        n = lots_for(t, capital, risk_pct, max_lots)
        if n < 1:
            skipped += 1
            continue
        pnl = t["net_per_lot"] * n
        taken += 1
        net += pnl
        cum += pnl
        peak = max(peak, cum)
        worst_dd = min(worst_dd, cum - peak)
        worst_trade = min(worst_trade, pnl)
    return {"capital": capital, "risk_pct": risk_pct, "max_lots": max_lots, "taken": taken, "skipped": skipped, "net": net,
            "net_pct": 100.0 * net / capital if capital else 0.0, "max_drawdown": worst_dd, "dd_pct": 100.0 * worst_dd / capital if capital else 0.0,
            "worst_trade": worst_trade, "worst_trade_pct": 100.0 * worst_trade / capital if capital else 0.0}


def table(trades: List[Dict[str, Any]], capitals, risks, lot_caps) -> str:
    rows = [f"{'capital':>9} {'risk%':>6} {'maxlots':>7} {'taken':>5} {'skip':>4} {'net Rs':>9} {'net%':>6} {'maxDD Rs':>9} {'DD%':>6} {'worst Rs':>9} {'worst%':>7}"]
    for c in capitals:
        for r in risks:
            for m in lot_caps:
                s = scenario(trades, c, r, m)
                rows.append(f"{c:>9,.0f} {r:>6g} {m:>7d} {s['taken']:>5d} {s['skipped']:>4d} {s['net']:>9,.0f} {s['net_pct']:>6.1f} {s['max_drawdown']:>9,.0f} {s['dd_pct']:>6.1f} {s['worst_trade']:>9,.0f} {s['worst_trade_pct']:>7.1f}")
    return "\n".join(rows)


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/fno/fno.db")
    ap.add_argument("--mode", default="DEMO")
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args(argv)
    trades = load(a.db, a.mode.upper(), a.days)
    if not trades:
        print(f"no closed {a.mode} trades in the last {a.days} days in {a.db}")
        return
    wins = sum(1 for t in trades if t["net_per_lot"] > 0)
    print(f"F&O SIZING WHAT-IF  {len(trades)} closed {a.mode.upper()} trades, {wins} winners, net per ONE lot {sum(t['net_per_lot'] for t in trades):,.0f}  (days {trades[0]['day']} .. {trades[-1]['day']})\n")
    print(table(trades, (50000.0, 100000.0, 250000.0, 500000.0), (1.0, 2.0, 3.0, 5.0), (2, 5)))
    print("\nA row with taken < all trades did not fit one lot under that capital / risk: the trade was not taken. Same entries, same exits, same fills as recorded; 30 or more trades are needed before a row means anything.")


if __name__ == "__main__":
    main()
