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
from capital import ledger, shared_order_budget
from execution import dhan_client
from models import ScalpGateState, ScalpPosition
from screening import intraday_eligibility
from tz_utils import ist_today_str

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

    open_positions = db.query(ScalpPosition).filter_by(status="OPEN").all()
    if not open_positions:
        logger.info("EOD squareoff: no open scalp positions to close")
        gate.eod_squareoff_fired_date = today
        db.commit()
        return 0

    logger.warning(
        "EOD squareoff: closing %d open scalp position(s) at market price",
        len(open_positions),
    )
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
            elif dhan_client.is_insufficient_funds_error(err_str):
                logger.error(
                    "EOD squareoff: %s (id=%d) — INSUFFICIENT_FUNDS: Dhan RMS "
                    "margin shortfall on INTRA SELL (likely no matching MIS position "
                    "to net against — BUY may have been rejected but DB row persists). "
                    "Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INSUFFICIENT_FUNDS: {err_str}"
            else:
                logger.error(
                    "EOD squareoff: failed to close %s (id=%d): %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_FAILED: {err_str}"
            db.commit()

    gate.eod_squareoff_fired_date = today
    db.commit()
    logger.info("EOD squareoff: done — %d/%d closed", closed, len(open_positions))
    return closed
