"""
capital/shared_order_budget.py — cross-service Dhan account-wide order-rate
guard (tracking doc §3.8).

FOUND MISSING IN SESSION 7's AUDIT: TRACKING.md and STATUS.md have described
this feature as fully built since an earlier session ("Shared Dhan order-rate
guard — built this session, no longer deferred"), but no such file, table,
or call site existed anywhere in the repo until now — the documentation had
drifted ahead of the actual code. This is the real implementation, matching
the design already written up in TRACKING.md §3.8 as closely as possible.

WHY: Dhan's account-wide order cap (~5,000-7,000/day) is shared between this
service and real-trade-service (same Dhan account) — neither service's own
per-service budget knows about the other's order volume, so it's possible
for the two combined to hit Dhan's real limit even though each individually
looks fine. This table (`stockky_shared_order_budget`, models.py) is the one
thing both already unconditionally share: the same physical DB. Built as a
DB-backed counter rather than Redis because this codebase's Redis layer is
Upstash-based and optional/off-by-default — a real-money order-rate guard
shouldn't depend on optional infrastructure.

FAIL-OPEN, ALWAYS: this is a soft rate governor, not a financial ledger. One
read + one upsert per order attempt. Any DB error is logged and treated as
"allow the order" — a broken rate-governor must never itself block a real
entry or, especially, a real exit.

execution/shared_order_budget.py in real-trade-service is the duplicated
counterpart — same logic, not imported (same isolation rationale as every
other duplicated module in this service)."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import config
from models import SharedOrderBudget
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-shared-order-budget")


def _get_or_create_row(db: Session, today: str) -> SharedOrderBudget:
    row = db.query(SharedOrderBudget).filter_by(trade_date=today).first()
    if row is None:
        row = SharedOrderBudget(trade_date=today, orders_placed_today=0)
        db.add(row)
        db.flush()
    return row


def check_and_reserve(db: Session) -> bool:
    """Call BEFORE placing a real order that should count against the
    shared budget (new entries only — see orders/entry.py). Returns True if
    under budget (and increments the counter), False if the shared daily
    cap has been reached. On ANY error, logs and returns True (fail open) —
    this guard existing at all is a bonus, not something real money should
    ever be blocked by due to its own malfunction."""
    try:
        today = ist_today_str()
        row = _get_or_create_row(db, today)
        if row.orders_placed_today >= config.SHARED_DAILY_ORDER_BUDGET:
            logger.warning(
                "position-stocks: SHARED Dhan order budget exhausted (%d/%d today across both services)",
                row.orders_placed_today, config.SHARED_DAILY_ORDER_BUDGET,
            )
            return False
        row.orders_placed_today += 1
        db.commit()
        return True
    except Exception as e:
        logger.error("position-stocks: shared_order_budget.check_and_reserve failed (failing open): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return True


def record_order_unconditional(db: Session) -> None:
    """Call for forced/must-happen orders (EOD square-off, manual kill-switch
    closes, exits in general) — tracked for visibility into the shared
    budget's real usage, but NEVER gates the order. Matches this service's
    own tracking doc §3.7 "no exceptions" rule for exits, applied here too:
    an exit must never be blocked by a rate-governor, shared or not."""
    try:
        today = ist_today_str()
        row = _get_or_create_row(db, today)
        row.orders_placed_today += 1
        db.commit()
    except Exception as e:
        logger.error("position-stocks: shared_order_budget.record_order_unconditional failed (non-blocking): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


def status(db: Session) -> dict:
    """Read-only snapshot for /status. Never raises — worst case returns
    zeros, which is safer than crashing the status endpoint over this."""
    try:
        today = ist_today_str()
        row = db.query(SharedOrderBudget).filter_by(trade_date=today).first()
        used = row.orders_placed_today if row else 0
    except Exception as e:
        logger.error("position-stocks: shared_order_budget.status failed: %s", e, exc_info=True)
        used = 0
    return {
        "used_today": used,
        "budget": config.SHARED_DAILY_ORDER_BUDGET,
        "remaining": max(0, config.SHARED_DAILY_ORDER_BUDGET - used),
    }
