"""
orders/reconcile.py — Super Order exit reconciliation monitor.

WHY THIS EXISTS (STATUS.md open item #4): a Super Order's TARGET_LEG or
STOP_LOSS_LEG fills entirely on Dhan's side — there is no webhook, and
nothing in this service was watching for it. Without this module, a
ScalpPosition stays "OPEN" in our DB forever after Dhan has already
closed it, capital never gets released back into ScalpCapitalLedger, and
`open_symbols` in main.py's trading loop keeps wrongly excluding a symbol
that's actually flat again.

This module polls `dhan_client.get_super_order_list()` (read-only, no
arm check) once per trading-loop tick and cross-references it against
every locally OPEN scalp position that has a `dhan_super_order_id`.

Dhan's /v2/super/orders response shape (per DhanHQ v2 API docs, confirmed
2026-09-12 — see STATUS.md for the source): each element is the ENTRY_LEG
order (top-level `orderId`, `orderStatus`, `legName="ENTRY_LEG"`,
`averageTradedPrice`, `filledQty`) plus a nested `legDetails` array holding
the STOP_LOSS_LEG and TARGET_LEG dicts (`orderId` same as parent,
`legName`, `orderStatus`, `price`, `remainingQuantity`,
`triggeredQuantity`). Dhan's fill status string is `"TRADED"` (same
vocabulary the frontend's groupDhanOrdersBySymbol already relies on for
plain orders — see components/RealAutoTrade.tsx).

ASSUMPTION FLAGGED FOR LIVE VERIFICATION: Dhan's public sample payloads
don't show an `averageTradedPrice` field on the nested leg dicts (only on
the top-level entry). `_extract_leg_price()` tries several plausible key
names before falling back to the leg's static `price` (its target/stop
trigger price, not necessarily the actual fill price — slightly wrong but
never crashes, and is your signal to compare against Dhan's own contract
note the first time a real exit fires). Recommended: eyeball the first
few real TARGET_HIT/STOP_HIT rows against Dhan's app before trusting the
booked P&L number for anything beyond a sanity check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from capital import ledger
from execution import dhan_client
from models import ScalpPosition

logger = logging.getLogger("position-stocks-reconcile")

_FILLED_STATUSES = {"TRADED", "FILLED", "EXECUTED", "COMPLETE"}
_DEAD_ENTRY_STATUSES = {"REJECTED", "CANCELLED"}


def _extract_leg_price(leg: dict, parent_row: dict) -> float:
    for key in ("averageTradedPrice", "tradedPrice", "avgPrice", "avgTradedPrice"):
        v = leg.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    v = leg.get("price")
    if v:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    # Last resort — better than crashing, but flagged in the log as a
    # low-confidence fill price.
    fallback = parent_row.get("averageTradedPrice") or 0.0
    try:
        return float(fallback)
    except (TypeError, ValueError):
        return 0.0


def run_exit_reconciliation(db: Session) -> int:
    """Check every locally-OPEN scalp position against Dhan's live super
    order book. Closes any position whose TARGET_LEG or STOP_LOSS_LEG has
    filled (or whose ENTRY_LEG was rejected/cancelled before ever filling),
    releases its reserved capital + realized P&L back into the ledger, and
    returns the count of positions closed this pass."""
    open_positions = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status == "OPEN",
            ScalpPosition.dhan_super_order_id.isnot(None),
        )
        .all()
    )
    if not open_positions:
        return 0

    try:
        super_orders = dhan_client.get_super_order_list(db)
    except Exception as e:
        logger.error("reconcile: failed to fetch super order list: %s", e)
        return 0

    by_id: dict[str, dict] = {}
    for row in super_orders:
        oid = str(row.get("orderId") or "")
        if oid:
            by_id[oid] = row

    closed = 0
    for pos in open_positions:
        row = by_id.get(str(pos.dhan_super_order_id))
        if row is None:
            # Not (yet) visible in today's order book — could be a brief
            # timing gap right after placement. Skip silently; next tick
            # will pick it up.
            continue

        leg_details = row.get("legDetails") or []
        target_leg = next((l for l in leg_details if l.get("legName") == "TARGET_LEG"), None)
        stop_leg = next((l for l in leg_details if l.get("legName") == "STOP_LOSS_LEG"), None)

        hit_leg: Optional[dict] = None
        hit_kind: Optional[str] = None
        if target_leg and str(target_leg.get("orderStatus", "")).upper() in _FILLED_STATUSES:
            hit_leg, hit_kind = target_leg, "TARGET_HIT"
        elif stop_leg and str(stop_leg.get("orderStatus", "")).upper() in _FILLED_STATUSES:
            hit_leg, hit_kind = stop_leg, "STOP_HIT"

        if hit_kind is None:
            # Entry itself never filled and is now dead (rejected/cancelled
            # on Dhan's side) — release capital, mark as error, move on.
            entry_status = str(row.get("orderStatus", "")).upper()
            if row.get("legName") == "ENTRY_LEG" and entry_status in _DEAD_ENTRY_STATUSES:
                pos.status = "ERROR"
                pos.error_message = f"Entry leg {entry_status} on Dhan (reconciled)"
                pos.closed_at = datetime.now(timezone.utc)
                db.commit()
                ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
                closed += 1
                logger.warning(
                    "reconcile: %s (id=%d) entry leg %s — capital released, no trade",
                    pos.symbol, pos.id, entry_status,
                )
            continue

        exit_price = _extract_leg_price(hit_leg, row)
        realized_pnl = (exit_price - pos.entry_price) * pos.quantity
        realized_pnl_pct = (
            (exit_price - pos.entry_price) / pos.entry_price * 100.0
            if pos.entry_price else 0.0
        )

        pos.status = hit_kind
        pos.exit_price = exit_price
        pos.realized_pnl = realized_pnl
        pos.realized_pnl_pct = realized_pnl_pct
        pos.dhan_exit_order_id = str(hit_leg.get("orderId") or pos.dhan_super_order_id)
        pos.closed_at = datetime.now(timezone.utc)
        db.commit()

        ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=realized_pnl)
        closed += 1
        logger.info(
            "reconcile: %s (id=%d) %s @ ₹%.2f — P&L ₹%.2f (%.2f%%)",
            pos.symbol, pos.id, hit_kind, exit_price, realized_pnl, realized_pnl_pct,
        )

    return closed
