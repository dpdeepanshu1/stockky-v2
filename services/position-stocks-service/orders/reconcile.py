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


def _extract_leg_price(leg: dict, parent_row: dict, own_fallback_price: float = 0.0) -> float:
    """own_fallback_price: this position's OWN target_price or stop_price
    (whichever leg actually fired — passed in by the caller, which already
    knows hit_kind). A Dhan Super Order's TARGET_LEG/STOP_LOSS_LEG fills
    at (or essentially at) that pre-set trigger price by construction, so
    it is a realistic last-resort estimate — unlike the hard 0.0 this
    function used to fall through to.

    BUG FIX (this session): every price field this function tries
    (averageTradedPrice/tradedPrice/avgPrice/avgTradedPrice on the leg,
    the leg's own `price`, then the parent row's averageTradedPrice) can
    plausibly be absent from Dhan's real response — the module docstring
    already flags this as unverified against live payloads. The previous
    fallback was a hard 0.0, which run_exit_reconciliation() then used
    directly as `exit_price` with no sanity check: realized_pnl =
    (0 - entry_price) * quantity records a phantom 100%-loss exit as
    real, permanent data — corrupting the trade ledger, wrongly returning
    far too little capital via release_capital(), and potentially
    tripping the daily-loss kill switch over a data-shape gap that has
    nothing to do with an actual trading loss. own_fallback_price (this
    position's own known, never-null target/stop price) is used instead
    of 0.0 whenever every real field comes back empty."""
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
    parent_price = parent_row.get("averageTradedPrice")
    if parent_price:
        try:
            return float(parent_price)
        except (TypeError, ValueError):
            pass
    # Last resort: this position's own target/stop trigger price rather
    # than a hard 0.0 — see docstring above. Both columns are NOT NULL on
    # ScalpPosition, so own_fallback_price is only 0.0 here if the caller
    # explicitly passed 0.0 (it never should).
    if own_fallback_price:
        logger.warning(
            "reconcile: no real fill price found in Dhan's response for this "
            "leg — using this position's own trigger price ₹%.2f as a "
            "low-confidence estimate instead of recording a phantom loss.",
            own_fallback_price,
        )
        return float(own_fallback_price)
    return 0.0


def run_exit_reconciliation(db: Session) -> int:
    """Check every locally-OPEN scalp position against Dhan's live super
    order book. Closes any position whose TARGET_LEG or STOP_LOSS_LEG has
    filled (or whose ENTRY_LEG was rejected/cancelled before ever filling),
    releases its reserved capital + realized P&L back into the ledger, and
    returns the count of positions closed this pass."""
    # AUDIT FIX: also pick up EOD_SQUAREOFF positions whose exit_price is
    # still the entry_price placeholder (recorded by eod_squareoff.py's
    # `pos.error_message = "EOD_SQUAREOFF_PENDING_RECONCILE..."` comment).
    # Without this, EOD-squared-off positions would show P&L=₹0.0 forever
    # in /positions and /trades/history even after the MARKET SELL filled on
    # Dhan's side — the real fill price and P&L would never be populated.
    # We detect these by the status (EOD_SQUAREOFF) and the placeholder
    # sentinel in error_message rather than a separate column, so no schema
    # change is needed.
    open_positions = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status == "OPEN",
            ScalpPosition.dhan_super_order_id.isnot(None),
        )
        .all()
    )
    eod_pending = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status == "EOD_SQUAREOFF",
            ScalpPosition.dhan_super_order_id.isnot(None),
            ScalpPosition.error_message.like("EOD_SQUAREOFF_PENDING_RECONCILE%"),
        )
        .all()
    )
    all_positions = open_positions + eod_pending
    if not all_positions:
        return 0
    # Alias for the rest of the function (which iterates `open_positions`)
    open_positions = all_positions

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

        # AUDIT FIX (this session — "check buy/sell for every scenario,
        # consider very high and frequent price change"): entry_price was
        # set exactly once, in orders/entry.py, to the pre-order LTP
        # sampled at scan/decision time — and never corrected afterwards.
        # Every realized_pnl / realized_pnl_pct this function computes
        # below is (exit_price - pos.entry_price) * quantity, so a stale
        # entry reference silently mis-states booked P&L. On a genuinely
        # calm stock this barely matters; on the fast-moving, volatile
        # names this scalp strategy specifically targets, the real
        # ENTRY_LEG's average traded price (`row["averageTradedPrice"]`,
        # already being read a few lines below via `_extract_leg_price`'s
        # parent-row fallback for the EXIT side — the entry side just
        # never used it) can differ meaningfully from that LTP snapshot:
        # queueing/network latency between the scan tick and Dhan
        # receiving the MARKET order, plus the order's own market impact
        # on a thin/fast-moving name. Correcting it here, as soon as
        # Dhan's response confirms a real fill, before it's ever used in a
        # P&L calc — idempotent (only writes when the value actually
        # changed) and applies to both still-OPEN positions and
        # EOD_SQUAREOFF-pending ones (whose exit_price is a placeholder
        # copy of entry_price — see eod_squareoff.py — so it's bumped in
        # lockstep to keep that placeholder's phantom P&L at exactly zero,
        # same as before, just anchored to the real fill instead of the
        # estimate). Does NOT touch capital_risked/the ledger — that's a
        # separate, already-reserved software allocation and out of this
        # fix's scope.
        if pos.status in ("OPEN", "EOD_SQUAREOFF"):
            entry_status_now = str(row.get("orderStatus", "")).upper()
            if entry_status_now in _FILLED_STATUSES:
                raw_fill = row.get("averageTradedPrice")
                real_entry_price: Optional[float] = None
                if raw_fill:
                    try:
                        real_entry_price = float(raw_fill)
                    except (TypeError, ValueError):
                        real_entry_price = None
                if real_entry_price and abs(real_entry_price - pos.entry_price) > 1e-6:
                    old_entry_price = pos.entry_price
                    pos.entry_price = real_entry_price
                    if pos.status == "EOD_SQUAREOFF" and pos.exit_price == old_entry_price:
                        pos.exit_price = real_entry_price

                    # AUDIT FIX (this session): the previously-flagged
                    # follow-up — capital_risked was reserved off the same
                    # stale pre-order LTP estimate as entry_price, and had
                    # the same "never corrected" gap. Now that the real
                    # fill price is known, recompute the real cost and
                    # push the delta through the ledger so
                    # available_capital stays consistent with what this
                    # position will actually return at exit (see
                    # capital/ledger.py::reconcile_position_cost's
                    # docstring for the full reasoning, including why a
                    # positive delta is allowed to push available_capital
                    # negative rather than being silently clamped).
                    old_capital_risked = pos.capital_risked
                    real_capital_cost = pos.quantity * real_entry_price
                    delta = real_capital_cost - old_capital_risked
                    pos.capital_risked = real_capital_cost
                    db.commit()
                    if delta != 0:
                        ledger.reconcile_position_cost(db, delta=delta)

                    logger.info(
                        "reconcile: %s (id=%d) entry_price corrected ₹%.2f -> ₹%.2f, "
                        "capital_risked ₹%.2f -> ₹%.2f (Dhan's real avg fill vs "
                        "pre-order LTP estimate)",
                        pos.symbol, pos.id, old_entry_price, real_entry_price,
                        old_capital_risked, real_capital_cost,
                    )

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
            # AUDIT FIX (EOD reconcile path): EOD_SQUAREOFF positions used
            # a plain dhan_client.place_order (MARKET SELL), not a super
            # order — so they will never have a TARGET_LEG or STOP_LOSS_LEG
            # in Dhan's super-order book. Their plain sell order won't even
            # appear in get_super_order_list() (super order list only shows
            # super orders, not plain orders). So for EOD_SQUAREOFF-pending
            # positions we check the top-level ENTRY_LEG of the ORIGINAL
            # super order to get the entry fill, then record the exit at the
            # known entry_price placeholder — the best we can do without
            # a separate plain-order list call. A future improvement would
            # call get_order_list() to find the actual EOD SELL fill price.
            # For now, if we cannot find the real fill, leave error_message
            # as-is (still marked PENDING_RECONCILE) for the next pass.
            if pos.status == "EOD_SQUAREOFF":
                entry_status = str(row.get("orderStatus", "")).upper()
                if entry_status in _FILLED_STATUSES:
                    # Original entry traded — exit was a plain MARKET SELL
                    # whose fill we can't directly read from super_orders.
                    # Use entry_price as exit_price placeholder (already set
                    # by eod_squareoff.py); clear the pending-reconcile flag.
                    pos.error_message = None
                    db.commit()
                    logger.info(
                        "reconcile: %s (id=%d) EOD_SQUAREOFF — entry leg confirmed traded; "
                        "exit price remains entry_price placeholder (plain SELL fill not in super-order list)",
                        pos.symbol, pos.id,
                    )
                continue

            # Entry itself never filled and is now dead (rejected/cancelled
            # on Dhan's side) — release capital, mark as error, move on.
            entry_status = str(row.get("orderStatus", "")).upper()
            # AUDIT FIX: Dhan's /v2/super/orders response omits the
            # `legName` key on the parent (top-level) row in some SDK
            # versions — the module docstring already flags this payload
            # shape as "confirmed via docs, not live-tested". Guarding
            # against a missing `legName` here: if Dhan doesn't include
            # it, we still treat the parent row as the ENTRY_LEG (it's
            # the only row at the top level by definition) and apply the
            # same REJECTED/CANCELLED logic, which is correct. Without
            # this guard, a Dhan response with no `legName` on the parent
            # would silently skip the dead-entry cleanup path entirely,
            # leaving the position stuck as OPEN and capital locked.
            leg_name = row.get("legName", "ENTRY_LEG")
            if leg_name in ("ENTRY_LEG", "") and entry_status in _DEAD_ENTRY_STATUSES:
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

        exit_price = _extract_leg_price(
            hit_leg, row,
            own_fallback_price=(pos.target_price if hit_kind == "TARGET_HIT" else pos.stop_price),
        )
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
