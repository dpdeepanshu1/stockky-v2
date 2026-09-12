"""
capital/ledger.py — ScalpCapitalLedger management.

Enforces the software 50/50 capital split between this service and
real-trade-service. Dhan itself has no concept of sub-pools — this table
IS the split.

On entry:
  1. Sync total_allocated_capital from Dhan's real available balance
     * SCALP_POOL_CAPITAL_SHARE_PCT (once per session or on demand).
  2. Check available_capital >= position_value before allowing a new entry.
  3. Decrement available_capital by position_value atomically in the DB.

On exit (TARGET_HIT / STOP_HIT / EOD_SQUAREOFF):
  4. Increment available_capital by realized_pnl + returned_capital.
  5. Accumulate realized_pnl_today / realized_pnl_total.

Daily loss kill switch:
  6. If realized_pnl_today drops below -(total_allocated_capital *
     MAX_DAILY_LOSS_PCT_OF_POOL / 100), trip the kill switch and refuse
     new entries for the rest of the day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpCapitalLedger
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-ledger")


def _get_or_create(db: Session) -> ScalpCapitalLedger:
    row = db.query(ScalpCapitalLedger).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpCapitalLedger(
            mode="REAL",
            total_allocated_capital=0.0,
            available_capital=0.0,
            realized_pnl_today=0.0,
            realized_pnl_total=0.0,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def sync_from_broker(db: Session) -> float:
    """Fetch live fund balance from Dhan, compute 50% scalp allocation,
    store in DB. Returns new total_allocated_capital."""
    try:
        funds = dhan_client.get_funds(db)
    except Exception as e:
        logger.error("ledger.sync_from_broker: failed to get funds: %s", e)
        return 0.0

    available_balance = float(funds.get("availabelBalance") or funds.get("availableBalance") or 0.0)
    if available_balance <= 0:
        logger.warning("ledger.sync_from_broker: Dhan returned zero/negative available balance")
        return 0.0

    scalp_alloc = available_balance * (config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0)
    row = _get_or_create(db)
    row.total_allocated_capital = scalp_alloc
    if row.available_capital <= 0:
        row.available_capital = scalp_alloc
    row.last_synced_from_broker_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "ledger: synced from broker — total Dhan balance ₹%.2f, scalp pool ₹%.2f",
        available_balance, scalp_alloc,
    )
    return scalp_alloc


def compute_position_value(adaptive_stop_pct: float) -> float:
    """Adaptive position sizing (tracking doc §3.5):
      position_value = (RISK_PER_TRADE_PCT% of scalp pool) / adaptive_stop_pct
    A wider stop → smaller position; a tighter stop → larger.
    Returns position_value in rupees."""
    if adaptive_stop_pct <= 0:
        return 0.0
    # NOTE: we use total_allocated_capital as the pool size.
    # The in-memory computation uses the row's stored value — no live
    # Dhan call on the hot path.
    return 0.0  # placeholder; resolved inside reserve_capital()


def reserve_capital(
    db: Session,
    *,
    adaptive_stop_pct: float,
) -> Optional[float]:
    """Gate check + decrement. Returns position_value (₹) if OK, None if
    insufficient capital or kill-switch tripped."""
    row = _get_or_create(db)

    if row.daily_loss_kill_switch_tripped:
        logger.warning("ledger.reserve_capital: daily loss kill switch tripped — refusing entry")
        return None

    if row.total_allocated_capital <= 0:
        logger.warning("ledger.reserve_capital: total_allocated_capital=0 — run sync_from_broker first")
        return None

    risk_rupees = row.total_allocated_capital * (config.RISK_PER_TRADE_PCT / 100.0)
    position_value = risk_rupees / (adaptive_stop_pct / 100.0)

    if position_value > row.available_capital:
        logger.info(
            "ledger.reserve_capital: insufficient capital (need ₹%.2f, have ₹%.2f)",
            position_value, row.available_capital,
        )
        return None

    row.available_capital -= position_value
    db.commit()
    logger.info(
        "ledger.reserve_capital: reserved ₹%.2f (risk ₹%.2f, stop %.2f%%), remaining ₹%.2f",
        position_value, risk_rupees, adaptive_stop_pct, row.available_capital,
    )
    return position_value


def release_capital(
    db: Session,
    *,
    position_value: float,
    realized_pnl: float,
) -> None:
    """Return capital + P&L on exit. Checks daily-loss kill switch."""
    row = _get_or_create(db)
    row.available_capital += position_value + realized_pnl
    row.realized_pnl_today += realized_pnl
    row.realized_pnl_total += realized_pnl

    # Daily loss kill switch
    if row.total_allocated_capital > 0:
        loss_pct = abs(min(row.realized_pnl_today, 0)) / row.total_allocated_capital * 100
        if loss_pct >= config.MAX_DAILY_LOSS_PCT_OF_POOL and not row.daily_loss_kill_switch_tripped:
            row.daily_loss_kill_switch_tripped = True
            row.daily_loss_kill_switch_tripped_date = ist_today_str()
            logger.warning(
                "DAILY LOSS KILL SWITCH TRIPPED: realized_pnl_today=₹%.2f "
                "(%.1f%% of pool ₹%.2f). No new entries today.",
                row.realized_pnl_today, loss_pct, row.total_allocated_capital,
            )

    db.commit()
    logger.info(
        "ledger.release_capital: returned ₹%.2f + P&L ₹%.2f, available=₹%.2f, "
        "pnl_today=₹%.2f",
        position_value, realized_pnl, row.available_capital, row.realized_pnl_today,
    )


def reset_daily(db: Session) -> None:
    """Called at EOD / next-day startup to reset daily P&L and kill switch.
    Does NOT reset available_capital (that carries over)."""
    row = _get_or_create(db)
    row.realized_pnl_today = 0.0
    row.daily_loss_kill_switch_tripped = False
    row.daily_loss_kill_switch_tripped_date = None
    db.commit()
    logger.info("ledger.reset_daily: daily P&L and kill switch reset")


def get_state(db: Session) -> dict:
    row = _get_or_create(db)
    return {
        "total_allocated_capital": row.total_allocated_capital,
        "available_capital": row.available_capital,
        "realized_pnl_today": row.realized_pnl_today,
        "realized_pnl_total": row.realized_pnl_total,
        "last_synced_from_broker_at": row.last_synced_from_broker_at,
        "daily_loss_kill_switch_tripped": row.daily_loss_kill_switch_tripped,
    }
