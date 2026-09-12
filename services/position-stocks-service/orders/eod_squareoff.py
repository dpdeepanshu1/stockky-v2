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
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
from capital import ledger
from execution import dhan_client
from models import ScalpGateState, ScalpPosition
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-eod")


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
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

            # Always place a plain MARKET SELL to guarantee flat
            dhan_client.place_order(
                db,
                is_armed=gate.is_armed,   # cancel is allowed disarmed, but sell needs arm
                security_id=pos.dhan_security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="SELL",
                quantity=pos.quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="EOD_SQUAREOFF",
            )

            # Mark closed — exit price unknown here (will be reconciled from
            # Dhan positions on next sync), use entry_price as placeholder
            pos.status = "EOD_SQUAREOFF"
            pos.closed_at = datetime.now(timezone.utc)
            pos.exit_price = pos.entry_price   # placeholder until reconciled
            pos.realized_pnl = 0.0
            pos.realized_pnl_pct = 0.0
            db.commit()

            ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
            closed += 1
            logger.info("EOD squareoff: closed %s (id=%d)", pos.symbol, pos.id)
        except Exception as e:
            logger.error("EOD squareoff: failed to close %s (id=%d): %s", pos.symbol, pos.id, e)
            pos.error_message = f"EOD_SQUAREOFF_FAILED: {e}"
            db.commit()

    gate.eod_squareoff_fired_date = today
    db.commit()
    logger.info("EOD squareoff: done — %d/%d closed", closed, len(open_positions))
    return closed
