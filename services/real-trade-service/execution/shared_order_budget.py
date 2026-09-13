"""
execution/shared_order_budget.py — cross-service Dhan account-wide
order-rate guard.

Duplicated from position-stocks-service/capital/shared_order_budget.py on
purpose (same isolation rationale as everywhere else these two services
share a concept but not code) — found missing entirely during a session 7
audit of position-stocks-service, where TRACKING.md/STATUS.md had described
this as built on both sides for several sessions with no actual file, table,
or call site anywhere in the repo. This is the real implementation for this
service's side.

WHY: Dhan's account-wide order cap (~5,000-7,000/day) is shared between this
service and position-stocks-service (same Dhan account) — neither service's
own per-service limits know about the other's order volume. This table
(`stockky_shared_order_budget`, models.py::SharedOrderBudget) is the one
thing both already unconditionally share: the same physical DB. DB-backed
rather than Redis because this codebase's Redis layer is optional/off-by-
default — a real-money order-rate guard shouldn't depend on that.

FAIL-OPEN, ALWAYS: this is a soft rate governor, not a financial ledger. Any
DB error is logged and treated as "allow the order" — a broken rate-governor
must never itself block a real entry or, especially, a real exit. Wired into:
  - manual_engine.py's manual REAL BUY path (gated, checked right before the
    Dhan call)
  - exit_engine.py's _send_real_sell (unconditional record after a
    successful SELL — covers both AUTO and manual sells, since that's the
    one function both paths already share; matches this codebase's existing
    "exits always allowed" convention)
The AUTOMATIC entry path (entry_engine/entry.py) is intentionally NOT gated
by this — only the manual BUY ticket is, matching the original design in
position-stocks-service's tracking doc, which never mentioned entry_engine.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import config
from models import SharedOrderBudget
from tz_utils import ist_today_str

logger = logging.getLogger("real-trade-shared-order-budget")


def _get_or_create_row(db: Session, today: str) -> SharedOrderBudget:
    row = db.query(SharedOrderBudget).filter_by(trade_date=today).first()
    if row is None:
        row = SharedOrderBudget(trade_date=today, orders_placed_today=0)
        db.add(row)
        db.flush()
    return row


def check_and_reserve(db: Session) -> bool:
    """Call BEFORE placing a real manual BUY order. Returns True if under
    budget (and increments the counter), False if the shared daily cap has
    been reached. On ANY error, logs and returns True (fail open)."""
    try:
        today = ist_today_str()
        row = _get_or_create_row(db, today)
        if row.orders_placed_today >= config.SHARED_DAILY_ORDER_BUDGET:
            logger.warning(
                "SHARED Dhan order budget exhausted (%d/%d today across both services)",
                row.orders_placed_today, config.SHARED_DAILY_ORDER_BUDGET,
            )
            return False
        row.orders_placed_today += 1
        db.commit()
        return True
    except Exception as e:
        logger.error("shared_order_budget.check_and_reserve failed (failing open): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return True


def record_order_unconditional(db: Session) -> None:
    """Call after a successful SELL — tracked for visibility, never gates
    an exit."""
    try:
        today = ist_today_str()
        row = _get_or_create_row(db, today)
        row.orders_placed_today += 1
        db.commit()
    except Exception as e:
        logger.error("shared_order_budget.record_order_unconditional failed (non-blocking): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
