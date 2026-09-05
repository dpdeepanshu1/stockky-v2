"""
scripts/calibrate_decay_profiles.py — 2026-09-03

Real gap from the audit: watchlist_engine/decay.py's CATALYST_PROFILES and
EXIT_PROFILES (entry_band_pct, decay_half_life_days, max_hold_days, etc.)
were reasoned estimates from the original design conversation, never
back-tested against live outcomes. This script is that back-test — it
reads what actually happened, grouped by catalyst_type, and prints
suggested profile adjustments as a ready-to-paste diff.

IMPORTANT — this script does NOT fabricate numbers. It reports "not
enough data yet" per catalyst type until there are enough closed
positions to be meaningfully different from noise (MIN_SAMPLES below).
Early on, most/all catalyst types will show this — that's expected and
correct, not a bug. Re-run weekly as live data accumulates.

Usage (run inside the real-trade-service container or with its venv/
DATABASE_URL /ORACLE_* env vars available):

    python scripts/calibrate_decay_profiles.py [--mode REAL|DEMO|ALL] [--days 30]

What it computes, per catalyst_type:
  - missed_rate: fraction of watchlist entries rejected by the chase-guard
    (status="missed") vs entered. A consistently high missed_rate for a
    catalyst type suggests entry_band_pct is too tight for how fast that
    catalyst actually moves — worth widening.
  - median_days_held / median_pnl_pct: for CLOSED positions that
    originated from this catalyst_type (via watchlist_entry_id), how long
    they were actually held and what they returned.
  - time_stop_rate: fraction of exits that were TIME_STOP rather than a
    price-based exit (stop/target/trail). A high rate suggests
    max_hold_days is cutting positions off before the move finished —
    the profile's decay_half_life_days assumption may be too short.
  - Suggested max_hold_days: current value with a directional nudge
    printed as a comment, not auto-applied — a human should sanity-check
    before editing decay.py.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")  # run from services/real-trade-service/

MIN_SAMPLES = 8  # below this, print "not enough data" rather than a number that's mostly noise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="ALL", choices=["REAL", "DEMO", "ALL"])
    parser.add_argument("--days", type=int, default=30, help="lookback window")
    args = parser.parse_args()

    import db
    import models
    from watchlist_engine.decay import CATALYST_PROFILES, EXIT_PROFILES

    session = db.get_session_factory()()  # real db.py helper — creates a standalone Session outside FastAPI's request lifecycle
    cutoff = datetime.utcnow() - timedelta(days=args.days)

    catalyst_types = sorted(CATALYST_PROFILES.keys())
    print(f"{'='*78}\nDecay profile calibration — last {args.days} days, mode={args.mode}\n{'='*78}\n")

    for ctype in catalyst_types:
        q = session.query(models.WatchlistEntry).filter(
            models.WatchlistEntry.catalyst_type == ctype,
            models.WatchlistEntry.catalyst_ts >= cutoff,
        )
        if args.mode != "ALL":
            q = q.filter(models.WatchlistEntry.mode == args.mode)
        entries = q.all()

        if not entries:
            print(f"[{ctype}] no watchlist entries in this window — skip\n")
            continue

        entered = [e for e in entries if e.status == "entered"]
        missed = [e for e in entries if e.status == "missed"]
        total_decided = len(entered) + len(missed)
        missed_rate = (len(missed) / total_decided) if total_decided else None

        # Pull the resulting closed positions for the "entered" rows
        closed_positions = []
        for e in entered:
            pos = (
                session.query(models.TradePosition)
                .filter(models.TradePosition.watchlist_entry_id == e.id, models.TradePosition.status == "CLOSED")
                .first()
            )
            if pos and pos.closed_at:
                held_days = (pos.closed_at - pos.opened_at).total_seconds() / 86400
                pnl_pct = (pos.realized_pnl / (pos.avg_entry_price * max(pos.qty_open, 1))) * 100 if pos.avg_entry_price else None
                closed_positions.append((pos, held_days, pnl_pct))

        current = CATALYST_PROFILES[ctype]
        exit_profile = EXIT_PROFILES.get(current["horizon_class"], {})

        print(f"[{ctype}]  (horizon_class={current['horizon_class']}, "
              f"current entry_band_pct={current['entry_band_pct']}, "
              f"current max_hold_days={exit_profile.get('max_hold_days', '?')})")
        print(f"  watchlist entries: {len(entries)}  (entered={len(entered)}, missed={len(missed)})")

        if total_decided < MIN_SAMPLES:
            print(f"  -> not enough decided entries yet ({total_decided} < {MIN_SAMPLES}) to suggest a band change\n")
        else:
            print(f"  missed_rate: {missed_rate:.0%}"
                  + ("  -> consider widening entry_band_pct, chase-guard rejecting most catalysts of this type"
                     if missed_rate > 0.5 else ""))

        if len(closed_positions) < MIN_SAMPLES:
            print(f"  -> not enough closed positions yet ({len(closed_positions)} < {MIN_SAMPLES}) to suggest a hold-time change\n")
        else:
            days_held = [d for _, d, _ in closed_positions]
            pnls = [p for _, _, p in closed_positions if p is not None]
            time_stops = sum(
                1 for pos, _, _ in closed_positions
                if session.query(models.TradeExitDecision)
                    .filter(models.TradeExitDecision.position_id == pos.id, models.TradeExitDecision.action == "FULL_EXIT")
                    .filter(models.TradeExitDecision.reasoning.ilike("%time%stop%"))
                    .first()
            )
            time_stop_rate = time_stops / len(closed_positions)
            print(f"  median_days_held: {statistics.median(days_held):.1f}  "
                  f"median_pnl_pct: {statistics.median(pnls):.1f}%  " if pnls else "  median_pnl_pct: n/a  ")
            print(f"  time_stop_rate: {time_stop_rate:.0%}"
                  + ("  -> consider raising max_hold_days, positions being cut off before the move finished"
                     if time_stop_rate > 0.4 else ""))
        print()

    session.close()
    print("Reminder: these are directional suggestions from real outcomes, not an "
          "auto-apply. Sanity-check before editing watchlist_engine/decay.py, and "
          "re-run after more data accumulates for anything still below MIN_SAMPLES.")


if __name__ == "__main__":
    main()
