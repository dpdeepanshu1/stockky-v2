"""execution/reconcile.py — the only place a REAL fill becomes "real" in
this service's own DB.

Placing an order (entry_engine) or sending a SELL (exit_engine) only ever
proves Dhan ACCEPTED the request — never that it filled. This module is
what actually asks Dhan "did it fill", via the read-only, non-arming-gated
dhan_client.get_order_list(), and only then calls
portfolio.record_real_fill / record_real_exit_fill. Runs on the same
manual Run Cycle trigger as everything else in this phase (see main.py's
module note on why there's no background scheduler yet) — call it once
per cycle for REAL, same as check_pending_fills is for DEMO.

Defensive key lookup below: the dhanhq SDK response mirrors Dhan's raw v2
JSON keys, but this hasn't been run against a live sandbox in this session
(see the SDK-version caveat in dhan_client.py's docstring) — prefer
several plausible key names over a hard KeyError, and treat "can't tell"
as "leave it PLACED/PENDING_EXIT for next cycle", never as a fill.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import models
from execution import dhan_client
from notifier import notify_async
from portfolio.portfolio import (
    record_real_fill, record_real_exit_fill, import_broker_holdings, holdings_sync_reconcile,
)
from resilience.local_cache import load_snapshot, save_snapshot

logger = logging.getLogger("real-trade-reconcile")

_FILLED_STATUSES = {"TRADED", "COMPLETE", "FILLED", "EXECUTED"}
# Dhan's actual v2 orderStatus for a partial fill is "PART_TRADED"
# (confirmed against the same DhanHQ v2 docs/release notes used for the
# filledQty fix above — the field names on the row are identical to a
# fully-TRADED row: filledQty/averageTradedPrice/remainingQuantity are
# all populated on a PART_TRADED row too). PARTIALLY_FILLED is kept as a
# defensive alternate spelling, same harmless-fallback idiom as
# tradedQuantity below. Unlike _FILLED_STATUSES, a status in this set is
# NOT terminal — the order needs to be checked again next cycle, so it
# must stay out of _DEAD_STATUSES and keep the order eligible in the
# PLACED/PARTIAL query below.
_PARTIAL_STATUSES = {"PART_TRADED", "PARTIALLY_FILLED"}
_DEAD_STATUSES = {"REJECTED", "CANCELLED", "CANCELED"}


def _get(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


async def _book_fill_delta(
    db: Session, order: models.TradeOrder, fill_price: float, delta_qty: int, is_partial: bool,
) -> None:
    """Book delta_qty NEWLY-confirmed shares for this order into the
    position/account and notify. `delta_qty` must already be the
    incremental amount (fill_qty_cumulative - order.filled_qty_so_far) —
    this function trusts the caller to have done that subtraction; it
    only ever adds, never re-derives, the total.

    Updates order.filled_qty_so_far itself so every caller (the normal
    partial/complete path AND the dead-status-after-partial-fill path)
    gets this bookkeeping for free instead of repeating it."""
    order.filled_qty_so_far = (order.filled_qty_so_far or 0) + delta_qty

    if order.side == "BUY":
        decision = db.query(models.TradeDecision).filter_by(id=order.decision_id).first()
        # 2026-09-01 fix: previously fell back to a hardcoded 3%/3% stop/
        # target when the decision row couldn't be found (or lacked these
        # fields) — inconsistent with entry_engine's actual flat fallback
        # (FLAT_STOP_PCT/FLAT_TARGET_PCT = 3.2%/6.5%) used everywhere else
        # a stop/target has to be invented without a live ATR read. Reusing
        # those constants here means a rare broken-decision-link case still
        # gets the same risk/reward shape as a normal entry, instead of a
        # tighter, inconsistent one. This should be rare — log it so it's
        # visible rather than a silent, unexplained stop/target on the
        # dashboard.
        from entry_engine.entry import FLAT_STOP_PCT, FLAT_TARGET_PCT
        if decision is not None and decision.proposed_stop is not None:
            stop_price = decision.proposed_stop
        else:
            stop_price = round(fill_price * (1 - FLAT_STOP_PCT / 100.0), 2)
            logger.warning(
                "reconcile: no decision/proposed_stop for order %s (%s) — "
                "using flat %.1f%% fallback stop ₹%.2f instead.",
                order.id, order.symbol, FLAT_STOP_PCT, stop_price,
            )
        if decision is not None and decision.proposed_target is not None:
            target_price = decision.proposed_target
        else:
            target_price = round(fill_price * (1 + FLAT_TARGET_PCT / 100.0), 2)
            logger.warning(
                "reconcile: no decision/proposed_target for order %s (%s) — "
                "using flat %.1f%% fallback target ₹%.2f instead.",
                order.id, order.symbol, FLAT_TARGET_PCT, target_price,
            )
        record_real_fill(db, order, fill_price, delta_qty, stop_price, target_price, is_partial=is_partial)
        if is_partial:
            await notify_async(
                f"🟡 *BUY partial fill* — {order.symbol}\n"
                f"{delta_qty} shares @ ₹{fill_price:.2f} (₹{fill_price * delta_qty:,.2f}) so far\n"
                f"Order {order.filled_qty_so_far}/{order.qty} filled — watching for the rest"
            )
        else:
            await notify_async(
                f"✅ *BUY filled* — {order.symbol}\n"
                f"{delta_qty} shares @ ₹{fill_price:.2f} (₹{fill_price * delta_qty:,.2f})\n"
                f"Stop ₹{stop_price:.2f} · Target ₹{target_price:.2f}"
            )
        return

    # SELL
    position = db.query(models.TradePosition).filter_by(
        mode="REAL", symbol=order.symbol, status="PENDING_EXIT"
    ).first()
    if position is None:
        # Partial exits never set PENDING_EXIT (see
        # record_real_exit_sent's full=False docstring) — the remainder
        # stays OPEN/PARTIALLY_CLOSED, so look it up by symbol among live
        # positions instead.
        position = db.query(models.TradePosition).filter(
            models.TradePosition.mode == "REAL",
            models.TradePosition.symbol == order.symbol,
            models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
        ).first()
    if position is None:
        logger.warning(
            "reconcile: SELL order %s broker-confirmed %s new shares for %s but no matching REAL "
            "position (OPEN/PARTIALLY_CLOSED/PENDING_EXIT) — fill NOT booked, order status will still "
            "advance so this doesn't get retried forever against a position that isn't there.",
            order.dhan_order_id, delta_qty, order.symbol,
        )
        order.status = "PARTIAL" if is_partial else "FILLED"
        db.commit()
        return

    # 2026-09-15 fix (session40 — see models.py TradePosition.
    # consecutive_exit_failures docstring): a real fill (full or partial)
    # for this position's SELL proves the exit backoff/escalation this
    # counter exists for is no longer needed — reset it the moment any
    # fill is booked, not just on a full CLOSED, so a position that
    # finally gets a partial fill after a rejection streak immediately
    # goes back to normal-cadence retries for its remainder instead of
    # staying in backoff.
    if position.consecutive_exit_failures or position.last_exit_failure_at:
        position.consecutive_exit_failures = 0
        position.last_exit_failure_at = None

    # BUG FIX (2026-08-27): this used to read Dhan's own `remarks` field as
    # "the reason" — broker text, not our trading logic's reason
    # (stop_hit/target_hit_partial/time_stop). Use what exit_engine
    # actually sent instead.
    reason = order.exit_reason or "exit"
    pnl = record_real_exit_fill(db, position, fill_price, delta_qty, str(reason))
    order.status = "PARTIAL" if is_partial else "FILLED"
    db.commit()
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    if is_partial:
        await notify_async(
            f"{pnl_emoji} *SELL partial fill* — {order.symbol}\n"
            f"{delta_qty} shares @ ₹{fill_price:.2f}\n"
            f"P&L so far: ₹{pnl:+,.2f} · order {order.filled_qty_so_far}/{order.qty} filled"
        )
    else:
        await notify_async(
            f"{pnl_emoji} *SELL filled* — {order.symbol}\n"
            f"{delta_qty} shares @ ₹{fill_price:.2f}\n"
            f"P&L: ₹{pnl:+,.2f}"
        )


async def _track_exit_failure_and_maybe_alert(
    db: Session, position: models.TradePosition, order: models.TradeOrder, status: str,
    rejection_reason: str | None = None, rejection_code: str | None = None,
) -> None:
    """2026-09-15 fix (session40 — DATAMATICS position 81, 89 consecutive
    REJECTED zero-fill SELL attempts over ~4.5h with no backoff and no
    operator alert — see models.py TradePosition.consecutive_exit_failures
    docstring for the full incident). Called once per dead (REJECTED/
    CANCELLED, zero-fill) SELL. Increments the position's failure streak
    and, once it crosses EXIT_RETRY_ALERT_THRESHOLD (and again on every
    further multiple of it, so a very long stuck streak doesn't go silent
    after the first alert), sends a loud operator notification — repeated
    identical rejections this many times in a row is a signal something
    structural is wrong (wrong order type/price/product reaching the
    broker, a genuinely unsellable position, etc.), not routine broker
    flakiness. exit_engine/exit.py._send_real_sell reads these same two
    fields to back off how often it will even try again.

    2026-09-16 (additive diagnostic): rejection_reason/rejection_code are
    Dhan's own omsErrorDescription/omsErrorCode for this rejection, when the
    broker's response included them (see execution/reconcile.py's capture
    point). Purely informational — included in the alert text so a stuck
    streak's operator alert says WHY, not just HOW MANY, the next time this
    fires live. Both default to None (unchanged behavior) for any caller
    that doesn't have them."""
    position.consecutive_exit_failures = (position.consecutive_exit_failures or 0) + 1
    position.last_exit_failure_at = datetime.now(timezone.utc)
    db.commit()

    n = position.consecutive_exit_failures
    threshold = config.EXIT_RETRY_ALERT_THRESHOLD
    if threshold > 0 and n >= threshold and n % threshold == 0:
        cooldown_seconds = min(
            config.EXIT_RETRY_MAX_COOLDOWN_SECONDS,
            config.EXIT_RETRY_BASE_COOLDOWN_SECONDS * (2 ** (n - 1)),
        )
        reason_line = (
            f"Broker reason: {rejection_reason}{f' [{rejection_code}]' if rejection_code else ''}\n"
            if rejection_reason else ""
        )
        await notify_async(
            f"🚨 *Exit SELL stuck* — {position.symbol}\n"
            f"{n} consecutive broker rejections with ZERO fill (latest: {status}, "
            f"order {order.dhan_order_id}).\n"
            f"{reason_line}"
            f"Backing off to a {cooldown_seconds:.0f}s cooldown before the next retry. "
            f"This many identical rejections in a row usually means something structural "
            f"(wrong order type/price/product reaching the broker, or the position is "
            f"genuinely unsellable right now) — please check manually rather than assume "
            f"it will eventually clear on its own."
        )
        logger.critical(
            "reconcile: position %s (%s) has %d consecutive dead-zero-fill SELL attempts "
            "(latest status=%s, order=%s, broker_reason=%s [%s]) — alerted operator, cooldown=%.0fs",
            position.id, position.symbol, n, status, order.dhan_order_id,
            rejection_reason, rejection_code, cooldown_seconds,
        )


def _restore_orphaned_position(db: Session, position: models.TradePosition, note: str) -> None:
    """Revert a PENDING_EXIT position back to a live status once we know
    its SELL definitively died with zero fill.

    There's no separate "status before PENDING_EXIT" column (see
    record_real_exit_sent — it overwrites status unconditionally when the
    SELL is sent, and full=True is only ever used for the WHOLE open qty,
    never a partial), so the pre-exit status is gone by the time we get
    here. realized_pnl is only ever nonzero after a PRIOR partial-exit fill
    was actually booked for this position (record_real_exit_fill is the
    only writer of it), so its presence is a reliable, already-available
    signal for whether this position was OPEN or PARTIALLY_CLOSED right
    before the now-dead SELL was sent — no schema change needed."""
    restored = "PARTIALLY_CLOSED" if position.realized_pnl else "OPEN"
    position.status = restored
    db.add(models.TradePositionEvent(
        position_id=position.id, event_type="EXIT_ORDER_DEAD",
        detail=f"{note} — restored to {restored} for re-evaluation next cycle",
    ))
    logger.warning(
        "reconcile: position %s (%s) restored from PENDING_EXIT to %s — %s",
        position.id, position.symbol, restored, note,
    )


def _repair_orphaned_pending_exits(db: Session, tally: dict) -> None:
    """Self-heal pass for positions ALREADY stuck at PENDING_EXIT with no
    active SELL order left to reconcile them.

    BUG (found 2026-09-04, dashboard showed ASHOKLEY/ADANIPOWER stuck
    PENDING_EXIT with the reconcile banner reading "checked 0"): the
    per-order loop below only ever revisits an order that's still PLACED/
    PARTIAL. A SELL that died (REJECTED/CANCELLED) at the broker with
    ZERO fill, in a cycle BEFORE this fix existed, already advanced past
    that query — its order.status is permanently REJECTED/CANCELLED now,
    so `pending_orders` never finds it again, and the dead-status branch
    below (which now reverts the position itself the moment an order
    dies) never got the chance to run for it. Its position is left at
    PENDING_EXIT forever: open_positions() excludes that status, so
    exit_engine never re-evaluates it, and nothing ever sells it again —
    a true orphan, invisible to every other part of this pipeline.

    Runs BEFORE the pending_orders early-return so it fires even when
    there is nothing left to check (exactly this bug's symptom), the same
    way _self_heal_orders already self-heals stale PLACED orders on every
    positions/orders read, not just on a manual Run Cycle."""
    orphans = (
        db.query(models.TradePosition)
        .filter(models.TradePosition.mode == "REAL", models.TradePosition.status == "PENDING_EXIT")
        .all()
    )
    fixed = 0
    for pos in orphans:
        still_in_flight = db.query(models.TradeOrder).filter(
            models.TradeOrder.mode == "REAL",
            models.TradeOrder.symbol == pos.symbol,
            models.TradeOrder.side == "SELL",
            models.TradeOrder.status.in_(("PLACED", "PARTIAL")),
        ).first()
        if still_in_flight is not None:
            continue  # genuinely still awaiting the broker — leave it for the loop below
        _restore_orphaned_position(
            db, pos, "no active SELL order found for this PENDING_EXIT position"
        )
        fixed += 1
    if fixed:
        db.commit()
    tally["positions_unstuck"] = fixed


async def reconcile_real_orders(db: Session) -> dict:
    """One pass over every REAL order still PLACED or PARTIAL, and every
    REAL position PENDING_EXIT. Returns a tally for the cycle summary.

    PARTIAL means the broker has confirmed SOME fill for this order
    (order.filled_qty_so_far > 0, tracked via db.py's additive migration)
    but the order itself hasn't reached a terminal broker status yet —
    unlike FILLED/REJECTED/CANCELLED, it must stay in this query so the
    rest of the fill (or a cancel of the remainder) gets picked up on a
    later cycle instead of silently stopping after the first partial."""
    tally = {"checked": 0, "entries_filled": 0, "partial_fills": 0, "exits_confirmed": 0,
             "dead_orders": 0, "errors": 0, "positions_unstuck": 0, "holdings_imported": 0,
             "ghost_positions_closed": 0}
    # ADDED (this session — item 5 from the follow-up: "REAL_RECONCILE_MIN_
    # INTERVAL_SECONDS (20s) may need tuning, but I don't have your actual
    # Dhan API latency — worth checking logs for how long reconcile_real_
    # orders calls actually take"). No timing was ever logged, so there was
    # nothing to check. _t0/_log_duration below log every call's wall-clock
    # duration at INFO, from every return path, so a quick log grep
    # ("reconcile_real_orders took") tells you directly whether 20s is
    # enough headroom or needs raising — no more guessing.
    _t0 = time.perf_counter()

    def _log_duration(label: str) -> None:
        logger.info("reconcile_real_orders took %.2fs (%s)", time.perf_counter() - _t0, label)

    _repair_orphaned_pending_exits(db, tally)

    # 2026-09-04 fix: pre-existing/manually-bought Dhan holdings (confirmed
    # against a user's Portfolio.csv export — Devyani International,
    # Paradeep Phosphates, Suzlon Energy, Vodafone Idea had no row here at
    # all) were invisible to /positions, /orders, and exit_engine because
    # nothing ever imported them. Runs every cycle, before the
    # pending_orders early-return, same self-heal treatment as the orphan
    # repair above — idempotent (see import_broker_holdings' own
    # already-tracked check), so this is a no-op once a holding is in.
    try:
        tally["holdings_imported"] = import_broker_holdings(db)
    except Exception as e:  # noqa: BLE001 — must never block the rest of the cycle
        logger.warning("reconcile: import_broker_holdings failed: %s", e)

    # 2026-09-08 fix (see portfolio.holdings_sync_reconcile's own docstring
    # for the full incident writeup — Stockky's DB showing 19 OPEN REAL
    # positions against only 4 real Dhan holdings): the mirror-image of
    # import_broker_holdings above. Runs every cycle, same self-heal
    # treatment and same "must never block the cycle" contract.
    try:
        ghost_result = holdings_sync_reconcile(db)
        tally["ghost_positions_closed"] = ghost_result["closed"]
        if ghost_result["closed"]:
            await notify_async(
                f"🧹 *Holdings sync* — force-closed {ghost_result['closed']} ghost "
                f"position(s) not found at Dhan (holdings or live positions):\n"
                f"{', '.join(ghost_result['symbols'])}\n"
                f"These were tracked as OPEN here but Dhan's own account doesn't hold "
                f"them — cash refunded, no P&L booked."
            )
    except Exception as e:  # noqa: BLE001 — must never block the rest of the cycle
        logger.warning("reconcile: holdings_sync_reconcile failed: %s", e)

    pending_orders = db.query(models.TradeOrder).filter(
        models.TradeOrder.mode == "REAL", models.TradeOrder.status.in_(("PLACED", "PARTIAL"))
    ).all()
    if not pending_orders:
        _log_duration("no pending orders")
        return tally

    try:
        broker_orders = dhan_client.get_order_list(db)
    except Exception as e:  # noqa: BLE001 — a reconcile failure must never crash the cycle
        logger.warning("reconcile: could not fetch Dhan order list: %s", e)
        tally["errors"] += 1
        _log_duration("order-list fetch failed")
        return tally

    by_id: dict[str, dict] = {}
    for row in broker_orders or []:
        oid = str(_get(row, "orderId", "order_id", default=""))
        if oid:
            by_id[oid] = row

    for order in pending_orders:
        tally["checked"] += 1
        if not order.dhan_order_id:
            continue
        broker_row = by_id.get(str(order.dhan_order_id))
        if broker_row is None:
            continue  # not visible yet — check again next cycle, never assume

        # 2026-09-15 fix (session40 — see dhan_client.place_order's
        # docstring for the full DATAMATICS incident this complements).
        # dhan_client.place_order already checks this once, right after
        # placement — this is the durable, every-cycle version: for as
        # long as an order stays PLACED/PARTIAL, re-verify on every
        # reconcile pass that Dhan's own order_type/price for it still
        # matches what this service's own record says it sent. Alerted
        # once per order (dedup via the snapshot cache — a mismatch that
        # persists across cycles must not re-alert every single pass).
        broker_order_type = str(
            _get(broker_row, "orderType", "order_type", default="")
        ).upper()
        broker_order_price = _get(broker_row, "price", default=None)
        if broker_order_type and broker_order_type != (order.order_type or "").upper():
            _mismatch_key = f"ordertype_mismatch_alerted_order_{order.id}"
            if not load_snapshot(db, _mismatch_key):
                save_snapshot(db, _mismatch_key, {"alerted_at": str(datetime.now(timezone.utc))})
                logger.critical(
                    "reconcile: BROKER ORDER TYPE MISMATCH — order %s (%s %s, our record "
                    "says order_type=%s) but Dhan's order book reports orderType=%s "
                    "(price=%s). This order was placed differently than this service "
                    "believes — investigate immediately.",
                    order.dhan_order_id, order.side, order.symbol, order.order_type,
                    broker_order_type, broker_order_price,
                )
                await notify_async(
                    f"🚨 *Broker order-type mismatch* — {order.symbol} {order.side}\n"
                    f"order {order.dhan_order_id}: we sent order_type={order.order_type}, "
                    f"but Dhan's own order book reports orderType={broker_order_type} "
                    f"(price={broker_order_price}).\n"
                    f"This means an order is being placed differently than this service "
                    f"believes — please investigate."
                )

        status = str(_get(broker_row, "orderStatus", "order_status", default="")).upper()

        # DIAGNOSTIC (2026-09-16, additive, no behavior change): capture Dhan's
        # own rejection-reason text when the broker's order-book response
        # includes it, so a REJECTED/CANCELLED order stops being a bare status
        # string with no "why". Field names per Dhan's documented Postback
        # payload schema (https://dhanhq.co/docs/v2/postback/), which uses the
        # same order-object shape as the orderbook GET this module already
        # polls: `omsErrorCode` / `omsErrorDescription`. Not independently
        # reconfirmed that GET /orders echoes these two fields identically to
        # the Postback payload — if they're absent here, `_get` just returns
        # None like it does for any other missing field, so this is purely
        # additive: it can only ADD information to the alert/log/event detail
        # below, never change what already gets logged. Exactly the kind of
        # data point needed to confirm/refute the DATAMATICS SDK MARKET→LIMIT
        # theory and the emergency_gap_down retry-cause question the next
        # time either recurs live.
        rejection_reason = _get(broker_row, "omsErrorDescription", "omsErrorMessage", default=None)
        rejection_code = _get(broker_row, "omsErrorCode", default=None)

        # filledQty is Dhan v2 GET /orders' actual field name (confirmed against
        # DhanHQ v2 docs/release notes: "filledQty, remainingQuantity and
        # averageTradedPrice is available as part of all GET Order APIs").
        # tradedQuantity/traded_quantity — the ONLY keys previously checked here
        # — belong to a different endpoint (v2/trades tradebook, and the old v1
        # orders API), so they never appear on this orderbook response and
        # fill_qty was always None. See this module's docstring update for the
        # full filledQty incident writeup.
        #
        # NOTE on precision: fill_price below is the broker's CUMULATIVE
        # average price for the whole order to date, not the average price
        # of just this delta — Dhan's v2 orderbook doesn't expose a
        # per-poll incremental price, only the tradebook (v2/trades) does,
        # and pulling that in is a bigger change than this fix (flagged as
        # out of scope in the filledQty incident entry). Using the
        # cumulative average as a stand-in for the increment's price is a
        # small, known approximation — it converges to the exact figure
        # once the order finishes, and is materially closer than not
        # booking the partial fill at all.
        fill_price = _get(broker_row, "averageTradedPrice", "average_traded_price", "tradedPrice", default=None)
        fill_qty_cumulative = _get(broker_row, "filledQty", "filled_qty", "tradedQuantity", "traded_quantity", default=None)
        if fill_qty_cumulative is None:
            # Cross-check: quantity - remainingQuantity gives the same number
            # via a second, independent field pair, in case a future SDK/API
            # version ever renames filledQty again.
            qty_total = _get(broker_row, "quantity", "qty", default=None)
            qty_remaining = _get(broker_row, "remainingQuantity", "remaining_quantity", default=None)
            if qty_total is not None and qty_remaining is not None:
                try:
                    fill_qty_cumulative = int(qty_total) - int(qty_remaining)
                except (TypeError, ValueError):
                    fill_qty_cumulative = None

        already_booked = order.filled_qty_so_far or 0
        delta_qty = None
        if fill_qty_cumulative is not None:
            try:
                delta_qty = int(fill_qty_cumulative) - already_booked
            except (TypeError, ValueError):
                delta_qty = None

        if status in _DEAD_STATUSES:
            # A partial fill can be followed by the REMAINDER being
            # rejected/cancelled (e.g. a day order Dhan auto-cancels at
            # close after only some of it traded) — those already-executed
            # shares are real fills that happened at the broker and must
            # be booked before the order is marked dead, or they'd
            # silently disappear from this system's own books while still
            # sitting in the actual Dhan account/position.
            if delta_qty and delta_qty > 0 and fill_price is not None:
                await _book_fill_delta(db, order, float(fill_price), delta_qty, is_partial=True)
                tally["partial_fills"] += 1
            elif order.side == "SELL":
                # BUG FIX (2026-09-04, see _repair_orphaned_pending_exits'
                # docstring for the full incident): a SELL that dies with
                # ZERO fill must not leave its position stuck at
                # PENDING_EXIT — fix it the moment we learn the order is
                # dead, in this same cycle, rather than waiting for the
                # repair pass to catch it as an orphan next time.
                dead_position = db.query(models.TradePosition).filter_by(
                    mode="REAL", symbol=order.symbol, status="PENDING_EXIT"
                ).first()
                if dead_position is None:
                    # Partial exits never set PENDING_EXIT — same
                    # OPEN/PARTIALLY_CLOSED lookup as _book_fill_delta's
                    # SELL branch above, so a dead zero-fill SELL against
                    # the remainder of an already-partially-exited
                    # position still gets its failure streak tracked below.
                    dead_position = db.query(models.TradePosition).filter(
                        models.TradePosition.mode == "REAL",
                        models.TradePosition.symbol == order.symbol,
                        models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
                    ).first()
                if dead_position is not None:
                    _restore_orphaned_position(
                        db, dead_position,
                        f"SELL order {order.dhan_order_id} came back {status.lower()} with zero fill",
                    )
                    tally["positions_unstuck"] += 1
                    await _track_exit_failure_and_maybe_alert(
                        db, dead_position, order, status,
                        rejection_reason=rejection_reason, rejection_code=rejection_code,
                    )
            dead_status = "REJECTED" if status == "REJECTED" else "CANCELLED"
            order.status = dead_status
            note = f" (after {order.filled_qty_so_far} of {order.qty} already filled)" if order.filled_qty_so_far else ""
            # DIAGNOSTIC (2026-09-16): append Dhan's own rejection reason/code
            # when present, additive only — see the capture point above.
            reason_note = f" — {rejection_reason}" if rejection_reason else ""
            code_note = f" [{rejection_code}]" if rejection_code else ""
            db.add(models.TradeOrderEvent(order_id=order.id, event_type=dead_status,
                                           detail=f"Broker reported {status}{note}{reason_note}{code_note}"))
            db.commit()
            tally["dead_orders"] += 1
            continue

        is_partial_status = status in _PARTIAL_STATUSES
        is_complete_status = status in _FILLED_STATUSES
        if not (is_partial_status or is_complete_status):
            continue  # still pending at the broker, nothing confirmed yet

        if fill_price is None or fill_qty_cumulative is None:
            logger.warning("reconcile: order %s reports %s but no fill price/qty — leaving as-is.",
                            order.dhan_order_id, status)
            continue

        if delta_qty is None or delta_qty <= 0:
            # Nothing NEW to book this pass (e.g. still PART_TRADED at the
            # same cumulative qty as last cycle) — but if the broker has
            # since moved it to a terminal filled status, finalize the
            # order status even though there's no new qty to add. Also
            # guards against a broker-side qty going backwards somehow
            # (delta_qty < 0) being booked as a negative fill.
            if is_complete_status and order.status != "FILLED":
                order.status = "FILLED"
                db.commit()
            continue

        await _book_fill_delta(db, order, float(fill_price), delta_qty, is_partial=is_partial_status)
        if is_partial_status:
            tally["partial_fills"] += 1
        elif order.side == "BUY":
            tally["entries_filled"] += 1
        else:
            tally["exits_confirmed"] += 1

    _log_duration("completed")
    return tally
