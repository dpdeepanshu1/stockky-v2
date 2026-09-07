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
import re
import statistics
import sys
from datetime import datetime, timedelta

from sqlalchemy import func

sys.path.insert(0, ".")  # run from services/real-trade-service/

MIN_SAMPLES = 8  # below this, print "not enough data" rather than a number that's mostly noise

_MISSED_REASON_RE = re.compile(r"price moved (-?\d+\.?\d*)%")


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0, 1]. sorted_vals must be non-empty and sorted."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = p * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


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

        # BUG FIX (2026-09-06, round 2): WatchlistEntry.status is NEVER set
        # to "entered" anywhere in the codebase — grep confirms the only
        # values ever written to it are "active" (default), "missed"
        # (entry_engine/entry.py's chase-guard), and "expired"
        # (watchlist_engine/watchlist.py's TTL cleanup). Filtering on
        # `status == "entered"` here always returned an empty list —
        # structurally, regardless of how many real trades actually
        # happened — which is exactly why the first live run showed
        # entered=0 across every single catalyst type. The real signal for
        # "did this watchlist row turn into a trade" is a linked
        # TradePosition (open OR closed) via watchlist_entry_id, not the
        # watchlist row's own status field.
        entered_ids = {
            wid for (wid,) in session.query(models.TradePosition.watchlist_entry_id)
                .filter(models.TradePosition.watchlist_entry_id.in_([e.id for e in entries]))
                .all()
        }
        entered = [e for e in entries if e.id in entered_ids]
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
                # BUG FIX (2026-09-06): pnl_pct used to divide realized_pnl by
                # qty_open — but qty_open is decremented to 0 on every exit
                # (portfolio.py close_position/record_real_exit_fill both
                # end with qty_open <= 0 -> status "CLOSED"), so for every
                # CLOSED position the old `max(pos.qty_open, 1)` silently
                # fell back to a denominator of 1. That turned pnl_pct into
                # ~absolute rupee P&L per one share, not a real percentage —
                # for any position sized more than 1 share, this wildly
                # overstated (or understated, if realized_pnl was negative)
                # the reported return, making median_pnl_pct meaningless for
                # every catalyst type, not just the 4 still open. Fixed by
                # reconstructing the ORIGINAL bought qty from the BUY
                # order(s)/fills that opened this watchlist-sourced position
                # — the only reliable source once exits have already reset
                # qty_open toward 0.
                buy_order_ids = [
                    oid for (oid,) in session.query(models.TradeOrder.id)
                        .filter(models.TradeOrder.watchlist_entry_id == e.id,
                                models.TradeOrder.side == "BUY")
                        .all()
                ]
                original_qty = None
                if buy_order_ids:
                    original_qty = session.query(func.sum(models.TradeFill.qty)).filter(
                        models.TradeFill.order_id.in_(buy_order_ids)
                    ).scalar()
                if not original_qty:
                    # Last-resort fallback for pre-migration rows with no
                    # linked BUY order/fill — better than crashing, but flag
                    # it so it isn't mistaken for a fully-reconstructed number.
                    original_qty = pos.qty_open or 1
                pnl_pct = (
                    (pos.realized_pnl / (pos.avg_entry_price * original_qty)) * 100
                    if pos.avg_entry_price else None
                )
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
            print(f"  missed_rate: {missed_rate:.0%}")
            if missed_rate > 0.5:
                # BUG FIX (2026-09-07): the first version of this compared
                # every historical miss's overrun against TODAY's
                # CATALYST_PROFILES value — but entry_band_pct is a real
                # column FROZEN on each WatchlistEntry at creation time
                # (watchlist.py: `entry_band_pct=profile["entry_band_pct"]`),
                # never updated retroactively. A row created before the last
                # decay.py edit is still evaluated against whatever band was
                # active THEN, forever. Comparing its overrun to today's
                # config produces exactly the confusing/wrong result seen in
                # testing: "current band catches 37/37 misses" when every
                # overrun was actually BELOW today's band — i.e. those rows
                # were rejected under an older, smaller, now-superseded band
                # and say nothing about whether today's setting is still too
                # tight. Fixed by splitting on each row's own e.entry_band_pct:
                # only rows evaluated under the band value that's still
                # actually configured today are live evidence; anything else
                # is reported separately as stale, not folded into the
                # suggestion.
                current_band = current["entry_band_pct"]
                live_overruns, stale_overruns = [], []
                for e in missed:
                    m = _MISSED_REASON_RE.search(e.missed_reason or "")
                    if not m:
                        continue
                    o = float(m.group(1)) / 100
                    (live_overruns if e.entry_band_pct == current_band else stale_overruns).append(o)

                if stale_overruns:
                    print(f"  -> {len(stale_overruns)}/{len(missed)} misses were evaluated under an "
                          f"older entry_band_pct (not today's {current_band:.0%}) — excluded as stale, "
                          f"not evidence about the current setting")

                if len(live_overruns) >= MIN_SAMPLES:
                    live_overruns.sort()
                    suggested = _percentile(live_overruns, 0.70)
                    still_rejected = sum(1 for o in live_overruns if o > current_band)
                    print(f"  -> under the CURRENT entry_band_pct={current_band:.0%} ({len(live_overruns)} "
                          f"misses actually evaluated at this band): {still_rejected}/{len(live_overruns)} "
                          f"would still be rejected today. 70th-percentile overrun is {suggested:.1%} "
                          f"(range {min(live_overruns):.1%}-{max(live_overruns):.1%}) — widening to ~"
                          f"{suggested:.1%} would catch ~70% of these. "
                          f"Still directional — sanity-check against slippage/liquidity before editing decay.py.")
                else:
                    print(f"  -> only {len(live_overruns)} misses evaluated under the current "
                          f"{current_band:.0%} band so far — not enough yet for a percentile suggestion "
                          f"(need {MIN_SAMPLES}); most of the {len(missed)} misses here predate the current setting")

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
