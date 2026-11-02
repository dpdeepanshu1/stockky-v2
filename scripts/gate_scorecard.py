"""Gate scorecard (2026-10-19; E285 batch 6 rewrite): did the position-stocks gates block winners or losers?

For every SKIPPED / SOFT row in scalp_candidate_log, look up the recorded bars (/data/bars/<date>/<quote(SYM)>.json) and measure what the stock did AFTER the skip: +30 min, +60 min,
close of day, and whether a +TARGET% move came before a -STOP% move inside 60 minutes. Results are grouped by gate (RANGE_GATE:NEAR_DAY_HIGH, OPENING_GATE:EXTENDED_ABOVE_OR, ...).
The logic lives in services/position-stocks-service/orders/scorecard.py (the same samples and the same LOOSEN / KEEP rule as the nightly adaptive-policy job); this file only reads the DB and prints.

Run it INSIDE the position-stocks container (it needs the DB models and /data/bars). From ~/stockky-v2:

    docker compose exec -T position-stocks-service python - --days 15 < scripts/gate_scorecard.py

Options (after the lone "-"): --days N (default 15)   --target 1.5   --stop 1.0   --cost 0.15   --min-n 30 (per-day lines only; the verdict uses GATE_POLICY_MIN_N)
  --direction long|short|auto   --by-day (a per-day line under every gate with enough samples)   --baseline-k 3 (random entries per sample for rand60 / edge; 0 = off)   --bars-dir /data/bars
  --direction long (default): every row is measured as a long, as before. SHORT_* rows then read INVERTED (a negative number means the short would have worked).
  --direction short: every row is measured as a short (the move is flipped: positive = the short would have gained; target / stop are on the short's side).
  --direction auto: rows whose reason starts with SHORT_ are measured as shorts, all others as longs; a gate with both sides gets one row per side (never averaged).
Read-only. Rows are de-duplicated to one per (symbol, gate, side, day, 30-minute bucket) so a stock re-skipped every 10 s does not count as 200 samples.
"""
import argparse
import calendar
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=15)
ap.add_argument("--target", type=float, default=1.5)
ap.add_argument("--stop", type=float, default=1.0)
ap.add_argument("--cost", type=float, default=0.15, help="round-trip cost %% assumed")
ap.add_argument("--min-n", type=int, default=30)
ap.add_argument("--bars-dir", default="/data/bars")
ap.add_argument("--direction", choices=("long", "short", "auto"), default="long", help="E272: measure rows as longs (default), as shorts, or auto (SHORT_* rows as shorts)")
ap.add_argument("--by-day", action="store_true", help="E285 batch 6: a per-day line under every gate with at least --min-n samples")
ap.add_argument("--baseline-k", type=int, default=3, help="E285 batch 6: random entries per sample at the same time (rand60 / edge); 0 = off")
args = ap.parse_args()

import db        # noqa: E402  (available inside the service container)
import models    # noqa: E402
from orders import candidate_outcomes as co, gate_policy as gp, scorecard as sc  # noqa: E402

S = db.get_session_factory()()
since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
q = (S.query(models.ScalpCandidateLog).filter(models.ScalpCandidateLog.created_at >= since).order_by(models.ScalpCandidateLog.id).all())
rows = [{"symbol": r.symbol, "decision": r.decision, "reason": r.reason, "created_ts": calendar.timegm(r.created_at.timetuple())} for r in q]

states = {}
try:        # the state in force for the four adaptable gates: a row the job / an operator MOVED, else what the gate's own config gives
    moved = {(p.gate): p.state for p in S.query(models.ScalpGatePolicy).all() if p.last_change_day}
    states = {g: str(moved.get(g) or gp.static_state(g)).upper() for g in gp.GATES}
except Exception:
    S.rollback()
    states = {g: gp.static_state(g) for g in gp.GATES}

res = sc.build(rows, co.file_loader(args.bars_dir), co.file_symbols(args.bars_dir), cost=args.cost, target=args.target, stop=args.stop,
               direction=args.direction, baseline_k=args.baseline_k, policy_states=states)
print(sc.render(res, args.days, args.min_n, by_day=args.by_day))
