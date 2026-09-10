"""
scripts/tier_pnl_breakdown.py — 2026-09-10

Answers Option 4 from the session21e real-trade audit: before cutting
HIGH_CONVICTION / UPPER_CIRCUIT / base VOLUME_SHOCK candidates out of REAL
trading (Options 1/2), get the ACTUAL tier-by-tier P&L breakdown first —
it's possible losses aren't even concentrated where the audit's code-reading
predicted.

Why this can't just be a SQL one-liner: TradePosition has no direct FK to
the TradeCandidate that produced it (see models.py — position only carries
watchlist_entry_id, for the separate watchlist path). The link has to be
walked: TradePosition -> (matching BUY) TradeOrder -> TradeDecision ->
TradeCandidate.decision_label. TradeOrder.created_at is always at/just
before the position's opened_at (order created, then filled, then
portfolio.record_real_buy_fill opens the position in the same flow), so for
each position we take the newest same-mode/same-symbol BUY order with
created_at <= opened_at. Positions opened by manual_engine.py never carry a
decision_id (see models.py's TradeOrder.execution_source note) and are
reported separately as MANUAL rather than folded into a tier bucket.

Cost basis for %-return uses the actual TradeFill rows on that BUY order
(sum(qty*price)), not order.qty*avg_entry_price, so partial fills and any
slippage between limit_price and actual fill price don't skew the return.

This is READ-ONLY — no writes, no schema changes. Safe to run anytime,
including against the live REAL account, while the system is trading.

Usage (run inside the real-trade-service container/environment, where
DATABASE_URL/ORACLE_DSN is already set):
    python scripts/tier_pnl_breakdown.py --mode REAL
    python scripts/tier_pnl_breakdown.py --mode REAL --days 14
    python scripts/tier_pnl_breakdown.py --mode REAL --csv out.csv
"""
from __future__ import annotations

import argparse
import csv as csv_mod
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import db  # noqa: E402
import models  # noqa: E402
from sqlalchemy import and_  # noqa: E402

UNKNOWN_LABEL = "UNKNOWN (no matching BUY order/decision found)"
MANUAL_LABEL = "MANUAL (no decision_id — manual_engine trade)"


def _find_originating_order(session, position: "models.TradePosition"):
    """Newest same-mode/same-symbol BUY order created at/before this
    position's opened_at. See module docstring for why this join can't be
    a straight FK lookup."""
    return (
        session.query(models.TradeOrder)
        .filter(
            and_(
                models.TradeOrder.mode == position.mode,
                models.TradeOrder.symbol == position.symbol,
                models.TradeOrder.side == "BUY",
                models.TradeOrder.created_at <= position.opened_at,
            )
        )
        .order_by(models.TradeOrder.created_at.desc())
        .first()
    )


def _tier_for_order(session, order) -> str:
    if order is None:
        return UNKNOWN_LABEL
    if order.decision_id is None:
        return MANUAL_LABEL
    decision = session.get(models.TradeDecision, order.decision_id)
    if decision is None or decision.candidate_id is None:
        return MANUAL_LABEL
    candidate = session.get(models.TradeCandidate, decision.candidate_id)
    if candidate is None or not candidate.decision_label:
        return UNKNOWN_LABEL
    return candidate.decision_label


def _cost_basis(session, order) -> float | None:
    """sum(qty*price) over this BUY order's actual fills. None if no fills
    recorded (shouldn't happen for a position that exists, but don't crash
    the report over one bad row)."""
    if order is None:
        return None
    fills = (
        session.query(models.TradeFill)
        .filter(models.TradeFill.order_id == order.id)
        .all()
    )
    if not fills:
        return None
    return sum((f.qty or 0) * (f.price or 0.0) for f in fills)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="REAL", choices=["REAL", "DEMO"], help="Trading mode to report on (default REAL).")
    ap.add_argument("--days", type=int, default=None, help="Only include positions opened in the last N days (default: all history).")
    ap.add_argument("--include-open", action="store_true", help="Also include still-OPEN/PARTIALLY_CLOSED positions' unrealized P&L in the breakdown (default: closed-only, so numbers reflect money actually booked).")
    ap.add_argument("--csv", default=None, help="Also write the per-position detail rows to this CSV path.")
    args = ap.parse_args()

    Session = db.get_session_factory()
    session = Session()

    try:
        q = session.query(models.TradePosition).filter(models.TradePosition.mode == args.mode)
        if not args.include_open:
            q = q.filter(models.TradePosition.status == "CLOSED")
        if args.days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
            q = q.filter(models.TradePosition.opened_at >= cutoff)
        positions = q.order_by(models.TradePosition.opened_at.asc()).all()

        if not positions:
            print(f"No {args.mode} positions found matching the filters. Nothing to report.")
            return

        rows = []
        tier_stats = defaultdict(lambda: {
            "count": 0, "wins": 0, "losses": 0, "flat": 0,
            "total_pnl": 0.0, "total_cost_basis": 0.0, "pct_return_samples": [],
        })

        for pos in positions:
            if pos.broker_imported:
                # Not a Stockky-originated trade at all — skip, it can't be
                # attributed to any candidate tier and would just show up
                # as a false UNKNOWN.
                continue

            order = _find_originating_order(session, pos)
            tier = _tier_for_order(session, order)
            cost_basis = _cost_basis(session, order)

            # realized_pnl is what's actually booked; for still-open/partial
            # positions also fold in unrealized_pnl so --include-open numbers
            # mean something (otherwise every open position shows pnl=0).
            pnl = (pos.realized_pnl or 0.0) + (pos.unrealized_pnl or 0.0 if pos.status != "CLOSED" else 0.0)

            pct_return = None
            if cost_basis:
                pct_return = round((pnl / cost_basis) * 100, 3)

            stats = tier_stats[tier]
            stats["count"] += 1
            stats["total_pnl"] += pnl
            if cost_basis:
                stats["total_cost_basis"] += cost_basis
            if pct_return is not None:
                stats["pct_return_samples"].append(pct_return)
            if pnl > 0.005:
                stats["wins"] += 1
            elif pnl < -0.005:
                stats["losses"] += 1
            else:
                stats["flat"] += 1

            rows.append({
                "symbol": pos.symbol,
                "tier": tier,
                "status": pos.status,
                "opened_at": pos.opened_at.isoformat() if pos.opened_at else "",
                "closed_at": pos.closed_at.isoformat() if pos.closed_at else "",
                "avg_entry_price": pos.avg_entry_price,
                "cost_basis": round(cost_basis, 2) if cost_basis else "",
                "realized_pnl": pos.realized_pnl,
                "unrealized_pnl": pos.unrealized_pnl,
                "pnl_used": round(pnl, 2),
                "pct_return": pct_return if pct_return is not None else "",
            })

        # ---- Print summary table -------------------------------------------------
        header = f"{'TIER':<38}{'N':>5}{'WIN':>6}{'LOSS':>6}{'FLAT':>6}{'WIN%':>8}{'TOTAL P&L':>14}{'AVG P&L':>12}{'AVG %RET':>10}"
        print(f"\n{args.mode} positions — tier P&L breakdown"
              f"{' (last %d days)' % args.days if args.days else ' (all history)'}"
              f"{' incl. open' if args.include_open else ' — CLOSED only'}\n")
        print(header)
        print("-" * len(header))

        grand_total_pnl = 0.0
        grand_count = 0
        for tier, s in sorted(tier_stats.items(), key=lambda kv: kv[1]["total_pnl"]):
            decided = s["wins"] + s["losses"]
            win_pct = (s["wins"] / decided * 100) if decided else 0.0
            avg_pnl = s["total_pnl"] / s["count"] if s["count"] else 0.0
            avg_pct_ret = (sum(s["pct_return_samples"]) / len(s["pct_return_samples"])) if s["pct_return_samples"] else None
            avg_pct_ret_str = f"{avg_pct_ret:>9.2f}%" if avg_pct_ret is not None else f"{'n/a':>10}"
            print(
                f"{tier:<38}{s['count']:>5}{s['wins']:>6}{s['losses']:>6}{s['flat']:>6}"
                f"{win_pct:>7.1f}%{s['total_pnl']:>14,.2f}{avg_pnl:>12,.2f}"
                f"{avg_pct_ret_str}"
            )
            grand_total_pnl += s["total_pnl"]
            grand_count += s["count"]

        print("-" * len(header))
        print(f"{'TOTAL':<38}{grand_count:>5}{'':>6}{'':>6}{'':>6}{'':>8}{grand_total_pnl:>14,.2f}")

        if UNKNOWN_LABEL in tier_stats:
            print(f"\nNote: {tier_stats[UNKNOWN_LABEL]['count']} position(s) couldn't be matched to a BUY "
                  f"order/decision (UNKNOWN row above) — likely edge cases around the join heuristic; "
                  f"check the CSV detail if that count is non-trivial.")

        # ---- Optional CSV detail ---------------------------------------------
        if args.csv:
            fieldnames = list(rows[0].keys())
            with open(args.csv, "w", newline="") as f:
                writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(f"\nPer-position detail written to {args.csv} ({len(rows)} rows).")

    finally:
        session.close()


if __name__ == "__main__":
    main()
