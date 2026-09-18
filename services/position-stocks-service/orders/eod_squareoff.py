"""
orders/eod_squareoff.py — 3:00 PM IST hard flat sweep.

Force-closes ALL open scalp positions at market price, no exceptions.
Called once per trading day by the main loop at EOD_SQUAREOFF_TIME_IST.
Uses a once-per-day guard (ScalpGateState.eod_squareoff_fired_date) so
accidental repeated calls do nothing.

Mirrors real-trade-service's own EOD squareoff convention (15:00 IST,
same shape) but is completely independent — if real-trade-service's EOD
sweep hangs, this one still runs, and vice versa.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import notifier
from capital import ledger, shared_order_budget, shared_symbol_lock
from execution import dhan_client
from models import ScalpGateState, ScalpPosition
from screening import intraday_eligibility
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("position-stocks-eod")


def _fire_flat_sell(db: Session, pos: ScalpPosition) -> dict:
    """2026-09-15 fix (session40): places the flat-SELL for one position,
    retrying up to config.EOD_SELL_RETRY_ATTEMPTS times ONLY when the
    failure doesn't match one of the three classified permanent-for-today
    rejection types below (those can never succeed on an immediate retry,
    so retrying them would just add pointless broker calls — same
    reasoning as real-trade-service's intraday-cutoff short-circuit).
    Everything else (a transient network blip, a momentary RMS hiccup) is
    the kind of failure a same-second retry can plausibly clear, and
    previously got exactly one attempt before the position was left OPEN
    until tomorrow.

    Returns the raw place_order() result dict (has ``orderId``) on
    success. Raises the last exception once attempts are exhausted, same
    as an unretried call would have — callers keep their existing
    try/except classification logic unchanged."""
    last_exc: Exception | None = None
    attempts = max(1, config.EOD_SELL_RETRY_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            return dhan_client.place_order(
                db,
                is_armed=True,
                security_id=pos.dhan_security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="SELL",
                quantity=pos.quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="EOD_SQUAREOFF",
            )
        except Exception as e:  # noqa: BLE001 — classified by the caller
            last_exc = e
            err_str = str(e)
            if (
                dhan_client.is_intraday_cutoff_error(err_str)
                or dhan_client.is_security_intraday_restricted_error(err_str)
                or dhan_client.is_insufficient_funds_error(err_str)
                or dhan_client.is_circuit_limit_error(err_str)
            ):
                # Permanent for today (or permanent, period) — no point
                # retrying, fail fast so the caller's classification/
                # logging runs immediately instead of after a pointless wait.
                raise
            if attempt < attempts:
                logger.warning(
                    "EOD squareoff: %s (id=%d) flat SELL attempt %d/%d failed "
                    "with an unclassified (possibly transient) error — retrying "
                    "in %.1fs: %s",
                    pos.symbol, pos.id, attempt, attempts,
                    config.EOD_SELL_RETRY_DELAY_SECONDS, err_str,
                )
                time.sleep(config.EOD_SELL_RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    # BUG FIX (session13, found via live testing): lazy reset of
    # gate.daily_loss_kill_switch_tripped — see main.py's
    # _maybe_lazy_reset_gate_kill_switch() for the full story.
    if row.daily_loss_kill_switch_tripped and row.daily_loss_kill_switch_tripped_date != ist_today_str():
        row.daily_loss_kill_switch_tripped = False
        row.daily_loss_kill_switch_tripped_date = None
        db.commit()
        logger.info("gate: lazy daily reset applied to gate.daily_loss_kill_switch_tripped")
    return row


def run_eod_squareoff(db: Session) -> int:
    """Close all OPEN scalp positions. Returns number of positions closed."""
    gate = _get_gate_state(db)
    today = ist_today_str()

    if gate.eod_squareoff_fired_date == today:
        logger.info("EOD squareoff already fired today (%s) — skipping", today)
        return 0

    # 2026-09-15 fix (session41b): also pick up EXIT_LEGS_REJECTED positions
    # (super-order exit legs were rejected — circuit-limit / surveillance).
    # Those positions cannot self-exit via their super order.  We attempt
    # ONE plain MARKET SELL here; if it also fails (e.g. stock is frozen at
    # lower circuit) we log and mark ERROR — we do NOT loop.  The once-per-
    # day guard (eod_squareoff_fired_date) prevents this from re-running.
    open_positions = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .all()
    )
    if not open_positions:
        logger.info("EOD squareoff: no open scalp positions to close")
        gate.eod_squareoff_fired_date = today
        db.commit()
        return 0

    logger.warning(
        "EOD squareoff: closing %d open/exit-rejected scalp position(s) at market price",
        len(open_positions),
    )

    # ── Overnight carry filter (2026-09-18, session67) ───────────────────────
    # When OVERNIGHT_HOLD_ENABLED, skip force-close for positions that pass
    # ALL four overnight quality conditions (in profit + stricter fund/tech/
    # mcap scores). Uses the quality scores recorded on ScalpCandidateLog at
    # entry time — no new network call at EOD. Only EXIT_LEGS_REJECTED
    # positions are always squaredoff regardless (their bracket is dead and
    # cannot protect them overnight).
    carry_positions: list = []
    if config.OVERNIGHT_HOLD_ENABLED:
        from models import ScalpCandidateLog
        from feed import ws_client as _ws
        squareoff_only: list = []
        for pos in open_positions:
            if pos.status == "EXIT_LEGS_REJECTED":
                squareoff_only.append(pos)
                continue
            # Get live price for unrealized P&L check
            try:
                buf = _ws.get_tick_buffer(pos.symbol)
                ltp = buf[-1][1] if buf else None
            except Exception:
                ltp = None
            if ltp is None or ltp <= pos.entry_price:
                # Not in profit or no live price — squareoff
                squareoff_only.append(pos)
                continue
            # Check quality scores from entry-time candidate log
            log_row = (
                db.query(ScalpCandidateLog)
                .filter_by(symbol=pos.symbol, decision="ENTERED")
                .order_by(ScalpCandidateLog.created_at.desc())
                .first()
            )
            # Any None quality field = unknown = don't carry overnight
            if (
                log_row is None
                or log_row.fundamental_score is None
                or log_row.technical_score is None
                or log_row.market_cap_cr is None
                or log_row.fundamental_score < config.OVERNIGHT_MIN_FUNDAMENTAL_SCORE
                or log_row.technical_score < config.OVERNIGHT_MIN_TECHNICAL_SCORE
                or log_row.market_cap_cr < config.OVERNIGHT_MIN_MARKET_CAP_CR
            ):
                squareoff_only.append(pos)
                continue
            # All checks passed — carry overnight
            carry_positions.append(pos)
            logger.info(
                "EOD overnight carry: %s (id=%d) qualifies — ltp=₹%.2f > entry=₹%.2f, "
                "fund=%.0f tech=%.0f mcap=₹%.0fcr — skipping squareoff",
                pos.symbol, pos.id, ltp, pos.entry_price,
                log_row.fundamental_score, log_row.technical_score, log_row.market_cap_cr,
            )
        if carry_positions:
            from notifier import notify_sync
            carry_lines = [f"🌙 *EOD overnight carry — {len(carry_positions)} position(s) held:*"]
            for p in carry_positions:
                carry_lines.append(f"  • {p.symbol} entry=₹{p.entry_price:.2f}")
            notify_sync("\n".join(carry_lines))
        open_positions = squareoff_only

    closed = 0
    for pos in open_positions:
        try:
            if config.USE_SUPER_ORDER and pos.dhan_super_order_id:
                # Cancel all legs of the super order first, then close at market
                for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
                    try:
                        dhan_client.cancel_super_order(
                            db, order_id=pos.dhan_super_order_id, order_leg=leg
                        )
                    except Exception:
                        pass  # leg may already be filled/cancelled — not fatal

            # Always place a plain MARKET SELL to guarantee flat.
            #
            # AUDIT FIX (this session): this previously passed
            # is_armed=gate.is_armed. dhan_client.place_order() raises
            # DhanNotArmedError whenever is_armed is False, with NO
            # exemption for SELL — unlike real-trade-service's risk engine
            # and manual_engine.py, which explicitly exempt SELL/exit
            # orders from the armed gate (see risk_engine/engine.py's
            # evaluate() docstring: "Exits must never be blocked..."). So a
            # disarmed service (e.g. after /disarm or /kill, or simply
            # never re-armed that morning) with open positions would hit
            # this exact EOD sweep, silently fail every closing SELL with
            # DhanNotArmedError (caught below, logged, position left OPEN),
            # and leave real-money positions unflattened past 3pm — exactly
            # the "no exceptions" scenario tracking doc §3.7 exists to rule
            # out. Forcing True here: flattening at EOD must never be
            # gated by the arm switch, same as cancel_order/
            # cancel_super_order already are unconditionally allowed above.
            #
            # 2026-09-15 fix (session40): routed through _fire_flat_sell()
            # for a small bounded retry on transient failures (see its
            # docstring), and the returned order id is now captured onto
            # pos.dhan_exit_order_id — previously nothing recorded which
            # broker order this SELL actually was, so reconcile.py's EOD
            # pending-reconcile pass had no way to look up its real fill
            # and was stuck using the entry_price placeholder forever.
            sell_result = _fire_flat_sell(db, pos)
            pos.dhan_exit_order_id = str(
                sell_result.get("orderId") or sell_result.get("order_id") or ""
            ) or None
            # Unconditional — tracked for visibility into the shared budget's
            # real usage, but never gates a forced exit (tracking doc §3.7
            # "no exceptions", applied to the shared guard too).
            shared_order_budget.record_order_unconditional(db)

            # Mark closed — exit price is unknown here (the MARKET SELL
            # just fired; Dhan has not yet confirmed a fill price). We record
            # entry_price as a placeholder so the row is immediately visible
            # in /positions as CLOSED, with a clear note that the final P&L
            # will be zero until reconciled. The next reconcile() pass that
            # reads Dhan's order book will overwrite exit_price / realized_pnl
            # with the real fill price once the MARKET SELL shows as TRADED.
            pos.status = "EOD_SQUAREOFF"
            pos.closed_at = datetime.now(timezone.utc)
            pos.exit_price = pos.entry_price   # placeholder — reconcile() will update
            pos.realized_pnl = 0.0             # placeholder — reconcile() will update
            pos.realized_pnl_pct = 0.0         # placeholder — reconcile() will update
            # AUDIT FIX: record that this is a placeholder so operators
            # reading /positions or /trades/history before the next
            # reconcile() pass don't mistake 0.0 P&L for a real break-even
            # exit. The field is overwritten to None by reconcile() on a
            # real fill (it only sets error_message on REJECTED/CANCELLED).
            pos.error_message = "EOD_SQUAREOFF_PENDING_RECONCILE: exit_price=entry_price placeholder until next reconcile pass fills in the real fill price."
            db.commit()

            ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
            # AUDIT FIX (session60): flat SELL fired — release the symbol
            # lock now, not only on TARGET_HIT/STOP_HIT reconcile. The flat
            # SELL is a forced, unconditional exit (same as the shared
            # order-budget's "no exceptions" note above); leaving the lock
            # held until the next reconcile pass confirms the fill would
            # needlessly block a re-entry (by either service) for longer
            # than the position is actually still open.
            shared_symbol_lock.release(db, pos.symbol)
            closed += 1
            logger.info("EOD squareoff: closed %s (id=%d)", pos.symbol, pos.id)
        except Exception as e:
            err_str = str(e)
            # AUDIT FIX (2026-09-15): previously all SELL failures landed in
            # one generic catch — retried identically next cycle with no
            # diagnosis. Screenshots confirmed three distinct rejection types
            # firing from this service's own EOD sweep. Classify them:
            #
            # 1. INTRADAY_CUTOFF: "Intraday orders cannot be placed at this
            #    time" — the exchange window has closed for today. The position
            #    is not permanently stuck; tomorrow it's a delivery holding and
            #    CNC will work. Don't count against a permanent restriction.
            #
            # 2. SECURITY_INTRADAY_RESTRICTED: "not allowed to be traded in
            #    Intraday" — T2T/ASM/GSM surveillance stock. This SELL will
            #    NEVER succeed as INTRA on any future attempt. Record it so
            #    screening/_run_cycle() excludes it from future BUY candidates.
            #    The position is stranded until manual intervention or T+1
            #    settlement lets a CNC SELL clear.
            #
            # 3. INSUFFICIENT_FUNDS: Dhan's RMS treating an INTRA SELL as a
            #    new naked short because there is no matching MIS position to
            #    net against (common when the BUY itself was rejected but the
            #    EOD sweep still finds an OPEN DB row). Log clearly.
            #
            # 4. Everything else: generic failure — same log as before.
            if dhan_client.is_intraday_cutoff_error(err_str):
                logger.warning(
                    "EOD squareoff: %s (id=%d) — INTRADAY_CUTOFF: exchange window "
                    "closed for today. Position left OPEN; a CNC sell will be possible "
                    "tomorrow once settlement clears. Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INTRADAY_CUTOFF: {err_str}"
                # FIX (session70 audit): all SELL-failure branches now alert via
                # notify_critical — previously none of them did. A position that
                # fails to flatten at 3 PM has real capital at risk overnight with
                # its bracket already cancelled; silent log-only was the gap.
                try:
                    notifier.notify_critical(
                        f"⚠️ <b>EOD SQUAREOFF FAILED — INTRADAY CUTOFF</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Exchange window closed. CNC sell possible tomorrow.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_security_intraday_restricted_error(err_str):
                logger.error(
                    "EOD squareoff: %s (id=%d) — SECURITY_INTRADAY_RESTRICTED: "
                    "this symbol cannot be traded INTRA ever. Recording restriction "
                    "so future cycles skip it as a BUY candidate. Position left OPEN "
                    "— requires manual CNC sell or T+1 delivery settlement. Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INTRADAY_RESTRICTED: {err_str}"
                try:
                    intraday_eligibility.record_restriction(
                        db, pos.symbol,
                        detail=f"EOD SELL rejection: {err_str[:200]}",
                    )
                except Exception as rec_e:
                    logger.warning(
                        "EOD squareoff: could not record restriction for %s: %s",
                        pos.symbol, rec_e,
                    )
                try:
                    notifier.notify_critical(
                        f"🚫 <b>EOD SQUAREOFF FAILED — SURVEILLANCE RESTRICTED</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"T2T/ASM/GSM — cannot sell INTRA ever. Manual CNC sell required.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_insufficient_funds_error(err_str):
                logger.error(
                    "EOD squareoff: %s (id=%d) — INSUFFICIENT_FUNDS: Dhan RMS "
                    "margin shortfall on INTRA SELL (likely no matching MIS position "
                    "to net against — BUY may have been rejected but DB row persists). "
                    "Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INSUFFICIENT_FUNDS: {err_str}"
                try:
                    notifier.notify_critical(
                        f"💸 <b>EOD SQUAREOFF FAILED — INSUFFICIENT FUNDS</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Dhan RMS margin shortfall on INTRA SELL (no matching MIS position?).\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_circuit_limit_error(err_str):
                # 2026-09-15 fix (session41b): stock is at lower circuit —
                # the SELL price is outside the allowed band.  Cannot fill
                # today.  Record it; do NOT retry.
                logger.error(
                    "EOD squareoff: %s (id=%d) — CIRCUIT_LIMIT: stock may be at "
                    "lower circuit; SELL price outside band.  Position left OPEN; "
                    "try a CNC sell tomorrow.  Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_CIRCUIT_LIMIT: {err_str}"
                try:
                    notifier.notify_critical(
                        f"🔴 <b>EOD SQUAREOFF FAILED — CIRCUIT LIMIT</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Stock at lower circuit; SELL price outside band. Try CNC sell tomorrow.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            else:
                logger.error(
                    "EOD squareoff: failed to close %s (id=%d): %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_FAILED: {err_str}"
                try:
                    notifier.notify_critical(
                        f"❌ <b>EOD SQUAREOFF FAILED</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Unclassified error — check logs immediately.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            db.commit()

    gate.eod_squareoff_fired_date = today
    db.commit()
    logger.info("EOD squareoff: done — %d/%d closed", closed, len(open_positions))
    return closed


class ManualCloseRejected(Exception):
    """Raised by close_position_now() for a rejection the caller (a live
    admin request) should surface as an explicit error — same reasoning
    as entry.py's ManualEntryRejected."""


def close_position_now(db: Session, pos: ScalpPosition, exit_reason: str = "MANUAL_EXIT") -> dict:
    """Manual override — flatten ONE open scalp position immediately, any
    time of day, independent of whether its own bracket target/stop would
    currently trigger. This is the "manual exit if needed" feature (no
    such path existed before: the only ways a position ever closed were
    its own Super Order target/stop leg filling, or the once-a-day EOD
    sweep — there was no way for an admin to just get out of one position
    right now).

    Deliberately mirrors run_eod_squareoff()'s per-position body above
    (cancel every Super Order leg first — the entry side may still be
    resting for a MARKET-priced order that briefly hasn't filled, so this
    is a no-op-if-already-filled defensive cancel, same as EOD does — then
    a plain MARKET SELL to guarantee flat) rather than introducing a
    second, different close mechanism: same proven retry/error-
    classification path (_fire_flat_sell), same PENDING_RECONCILE
    placeholder convention that orders/reconcile.py already knows how to
    resolve once the real fill price is known (see that module's
    _reconcile_eod_pending — generalized this session to also handle
    status="MANUAL_EXIT", not just "EOD_SQUAREOFF").

    Always allowed regardless of the armed switch — exiting a position
    must never be gated by "not armed", same policy eod_squareoff.py's own
    forced-True is_armed already documents above, and the same policy
    real-trade-service's manual close route uses.

    Raises ManualCloseRejected with a human-readable reason if the
    position isn't in a closeable state or Dhan rejects the flat SELL.
    Returns a small status dict on success (the real fill/P&L is not yet
    known — same placeholder-then-reconcile flow as EOD squareoff).

    `exit_reason` (session68, made a REAL status this session): labels the
    error_message placeholder, the Telegram notification, AND pos.status
    itself — e.g. run_stagnation_exit() below passes "STAGNATION_EXIT" and
    the position is actually stored/shown as STAGNATION_EXIT, not
    MANUAL_EXIT. reconcile.py's _FLAT_SELL_PENDING_STATUSES tuple now
    includes "STAGNATION_EXIT" alongside "EOD_SQUAREOFF"/"MANUAL_EXIT" so
    the same pending-fill resolution path covers it — see that module.
    Previously this was cosmetic-only (error_message/notification said
    STAGNATION_EXIT but pos.status was hardcoded to the literal
    "MANUAL_EXIT"), which is exactly the mislabeling the user flagged:
    every stagnation early-exit showed up in Positions/Trade History
    indistinguishable from a real manual exit."""
    if pos.status not in ("OPEN", "EXIT_LEGS_REJECTED"):
        raise ManualCloseRejected(f"Position is {pos.status} — nothing to close.")

    if config.USE_SUPER_ORDER and pos.dhan_super_order_id:
        for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
            try:
                dhan_client.cancel_super_order(
                    db, order_id=pos.dhan_super_order_id, order_leg=leg
                )
            except Exception:
                pass  # leg may already be filled/cancelled — not fatal
        # 2026-09-18 fix (session67): give Dhan a moment to acknowledge the
        # cancellations before firing the SELL. Without this pause, the plain
        # MARKET SELL can race against a still-live TARGET_LEG or
        # STOP_LOSS_LEG on Dhan's side, resulting in a rejected/double-fill
        # or a position that appears closed in our DB but still has a live
        # exit leg sitting at the broker. Even 0.3–0.5s is enough for the
        # cancel to propagate over the RMS. Configurable via
        # MANUAL_EXIT_CANCEL_WAIT_S; default 0.5s; set to 0 to restore the
        # original no-wait behaviour.
        wait_s = getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5)
        if wait_s > 0:
            time.sleep(wait_s)

    try:
        sell_result = _fire_flat_sell(db, pos)
    except Exception as e:
        err_str = str(e)
        if dhan_client.is_intraday_cutoff_error(err_str):
            raise ManualCloseRejected(
                f"Dhan rejected — intraday order window has closed for today: {err_str}"
            )
        if dhan_client.is_security_intraday_restricted_error(err_str):
            try:
                intraday_eligibility.record_restriction(
                    db, pos.symbol, detail=f"Manual exit SELL rejection: {err_str[:200]}",
                )
            except Exception:
                pass
            raise ManualCloseRejected(
                f"Dhan rejected — {pos.symbol} is not tradeable intraday (T2T/ASM/GSM "
                f"surveillance): {err_str}"
            )
        if dhan_client.is_insufficient_funds_error(err_str):
            raise ManualCloseRejected(f"Dhan rejected — insufficient margin for the exit SELL: {err_str}")
        if dhan_client.is_circuit_limit_error(err_str):
            raise ManualCloseRejected(f"Dhan rejected — stock is at circuit limit: {err_str}")
        raise ManualCloseRejected(f"Dhan rejected the manual exit: {err_str}")

    pos.dhan_exit_order_id = str(
        sell_result.get("orderId") or sell_result.get("order_id") or ""
    ) or None
    shared_order_budget.record_order_unconditional(db)

    pos.status = exit_reason  # real status now, not a hardcoded literal — see exit_reason note above
    pos.closed_at = datetime.now(timezone.utc)
    pos.exit_price = pos.entry_price   # placeholder — reconcile() will update, same as EOD squareoff
    pos.realized_pnl = 0.0
    pos.realized_pnl_pct = 0.0
    pos.error_message = f"{exit_reason}_PENDING_RECONCILE: exit_price=entry_price placeholder until next reconcile pass fills in the real fill price."
    db.commit()

    ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
    # AUDIT FIX (session60): same optimistic release as the EOD sweep above
    # — reconcile.py's dead-status branch re-claims it if this SELL turns
    # out to have died with zero fill.
    shared_symbol_lock.release(db, pos.symbol)
    logger.info("%s: closed %s (id=%d) — awaiting broker fill confirmation", exit_reason, pos.symbol, pos.id)

    from notifier import notify_sync
    _label = "Manual EXIT sent" if exit_reason == "MANUAL_EXIT" else f"{exit_reason.replace('_', ' ').title()} sent"
    notify_sync(
        f"📤 <b>{_label}</b> — {pos.symbol} x{pos.quantity}\n"
        f"Entry ₹{pos.entry_price:.2f} | Order {pos.dhan_exit_order_id or 'N/A'}\n"
        f"Awaiting broker fill confirmation."
    )

    return {"id": pos.id, "symbol": pos.symbol, "status": "pending_broker_confirmation"}


def run_stagnation_exit(db: Session) -> int:
    """session68/69. Toggle is DB-backed (ScalpGateState.stagnation_exit_
    enabled — POST /stagnation-exit/enable|disable, frontend button on the
    Pipeline tab), not config/env — see config.py's tuning-knobs comment
    for why. Closes an OPEN position early if it has moved less than
    config.STAGNATION_EXIT_BAND_PCT (either direction) from its entry
    price after config.STAGNATION_EXIT_MINUTES — i.e. neither its target
    nor its stop is anywhere close, and it isn't going to be. Frees that
    capital and its MAX_CONCURRENT_SCALP_POSITIONS slot for a better
    candidate the SAME session instead of parking it dead until the 15:00
    EOD sweep.

    Root cause this targets — 2026-09-17's trade history: MANGALAM,
    GEEKAYWIRE and JISLJALEQS all sat inside a near-zero P&L band the
    entire session and only ever closed via EOD_SQUAREOFF, while TREL
    (fund=49, tech=78 — a real candidate) repeatedly hit
    INSUFFICIENT_CAPITAL. Reusing close_position_now()'s proven cancel-
    wait-sell + PENDING_RECONCILE path rather than a second exit
    mechanism; tagged exit_reason="STAGNATION_EXIT", which close_position_
    now() now stores as the real pos.status (this session — it used to
    fall back to the hardcoded "MANUAL_EXIT" literal), so it's
    distinguishable from a true manual exit everywhere: dashboard, Trade
    History, notifications. Intentionally does NOT touch EXIT_LEGS_REJECTED
    positions — those are eod_squareoff's job, not this one's.
    """
    gate = _get_gate_state(db)
    if not gate.stagnation_exit_enabled:
        return 0

    now = datetime.now(timezone.utc)
    open_positions = db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all()
    closed = 0
    for pos in open_positions:
        try:
            opened_at = as_aware(pos.opened_at)
        except Exception:
            continue
        age_min = (now - opened_at).total_seconds() / 60.0
        if age_min < config.STAGNATION_EXIT_MINUTES:
            continue

        try:
            from feed import ws_client
            buf = ws_client.get_tick_buffer(pos.symbol)
            ltp = buf[-1][1] if buf else None
        except Exception:
            ltp = None
        if ltp is None or ltp <= 0 or not pos.entry_price:
            continue  # no live price to judge stagnation by — leave it to EOD/target/stop

        pct_move = abs(ltp - pos.entry_price) / pos.entry_price * 100.0
        if pct_move >= config.STAGNATION_EXIT_BAND_PCT:
            continue  # moved meaningfully — target/stop logic already owns this case

        try:
            close_position_now(db, pos, exit_reason="STAGNATION_EXIT")
            closed += 1
            logger.info(
                "STAGNATION_EXIT: %s (id=%d) closed after %.0fm flat within ±%.2f%% "
                "(ltp=₹%.2f entry=₹%.2f) — freeing capital/slot",
                pos.symbol, pos.id, age_min, config.STAGNATION_EXIT_BAND_PCT, ltp, pos.entry_price,
            )
        except ManualCloseRejected as e:
            logger.info("STAGNATION_EXIT: %s (id=%d) skipped — %s", pos.symbol, pos.id, e)
        except Exception as e:
            logger.error(
                "STAGNATION_EXIT: %s (id=%d) unexpected error: %s", pos.symbol, pos.id, e, exc_info=True,
            )
    return closed