"""DEMO vs REAL comparison (2026-10-19): are the new entry rules (pullback re-arm, VWAP-anchored chase limit) doing better in DEMO?

Closed real-trade-service positions of the last N days, grouped by mode: trades, win rate, average and total realized P&L, average
hold time, average initial stop distance (% of entry), and the share that were stopped out within 15 minutes. Read-only.

Run INSIDE the real-trade container. From ~/stockky-v2:

    docker compose exec -T real-trade-service python - --days 14 < scripts/mode_compare.py

Only compare modes over the same days and read it as evidence, not proof: with fewer than ~30 trades per mode the gap is noise.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=14)
args = ap.parse_args()

import db        # noqa: E402  (available inside the service container)
import models    # noqa: E402

S = db.get_session_factory()()
since = datetime.utcnow() - timedelta(days=args.days)
rows = (S.query(models.TradePosition)
        .filter(models.TradePosition.status == "CLOSED", models.TradePosition.closed_at >= since)
        .all())

by = defaultdict(list)
for p in rows:
    by[p.mode].append(p)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


print(f"\nDEMO vs REAL - closed positions, last {args.days} days (rows read: {len(rows)})\n")
print(f"{'mode':6} {'trades':>6} {'win%':>6} {'avg pnl':>9} {'total pnl':>10} {'avg hold min':>13} {'avg stop%':>10} {'stopped<15m%':>13}")
print("-" * 82)
for mode in sorted(by):
    ps = by[mode]
    n = len(ps)
    wins = sum(1 for p in ps if (p.realized_pnl or 0.0) > 0)
    holds = [((p.closed_at - p.opened_at).total_seconds() / 60.0) for p in ps if p.closed_at and p.opened_at]
    stops = [(p.initial_stop_distance / p.avg_entry_price * 100.0) for p in ps
             if p.initial_stop_distance and p.avg_entry_price]
    quick = sum(1 for p in ps if p.closed_at and p.opened_at and (p.realized_pnl or 0.0) < 0
                and (p.closed_at - p.opened_at).total_seconds() < 15 * 60)
    print(f"{mode:6} {n:6d} {100.0 * wins / n:6.1f} {_mean([p.realized_pnl for p in ps]):9.2f} "
          f"{sum((p.realized_pnl or 0.0) for p in ps):10.2f} {_mean(holds):13.1f} {_mean(stops):10.2f} {100.0 * quick / n:13.1f}")
print("\nReading it: compare DEMO with REAL on win%, avg pnl and stopped<15m% over the same days. n < 30 per mode = noise.")
