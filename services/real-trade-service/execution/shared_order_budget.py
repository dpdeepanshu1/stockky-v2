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

FAIL-CLOSED FOR ENTRIES (group 448; was fail-open): if the DB cannot answer,
check_and_reserve() returns False and the new BUY is refused (CRITICAL log +
rate-limited Telegram). Exits are never gated. SHARED_GATES_FAIL_CLOSED=false
restores the old fail-open behavior. Wired into:
  - entry_engine/entry.py's automatic REAL BUY path (gated, checked right
    before the Dhan call)
  - manual_engine.py's manual REAL BUY path (gated, checked right before the
    Dhan call)
  - exit_engine.py's _send_real_sell (unconditional record after a
    successful SELL — covers both AUTO and manual sells, since that's the
    one function both paths already share; matches this codebase's existing
    "exits always allowed" convention)
"""
from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from models import SharedOrderBudget
from tz_utils import ist_today_str

logger = logging.getLogger("real-trade-shared-order-budget")
SVC = "real-trade-service"

# ── Fail-closed on a gate error (group 448) ──────────────────────────────────
# Before: any DB error in this gate meant "allow the order", so during a DB hiccup both services could buy the same symbol / overshoot the shared
# daily order budget. These gates only ever guard new BUY entries (exits are never gated), so refusing an entry while the gate cannot answer is safe:
# the entry is simply skipped and retried later. Set SHARED_GATES_FAIL_CLOSED=false to get the old fail-open behaviour back.
_ALERT_EVERY_S = 600.0
_last_alert = {"t": None}


def _fail_closed() -> bool:
    try:
        import config
        return bool(getattr(config, "SHARED_GATES_FAIL_CLOSED", True))
    except Exception:  # noqa: BLE001
        return True


def _gate_failed(gate: str, err) -> bool:
    """What the gate returns when it could not decide: True (allow) only if fail-closed is switched off. Loud: CRITICAL log + a rate-limited Telegram."""
    if not _fail_closed():
        logger.error("%s: %s could not decide (%s) - failing OPEN, order allowed (SHARED_GATES_FAIL_CLOSED=false)", SVC, gate, err)
        return True
    logger.critical("%s: %s could not decide (%s) - failing CLOSED, this entry is refused", SVC, gate, err)
    import time as _time
    now = _time.monotonic()
    if _last_alert["t"] is None or now - _last_alert["t"] >= _ALERT_EVERY_S:
        _last_alert["t"] = now
        try:
            from notifier import notify_fire_and_forget
            notify_fire_and_forget(f"{SVC}: the {gate} check could not reach the database ({type(err).__name__}). "
                                   "New BUY entries are being refused until it recovers. Exits are not affected.")
        except Exception:  # noqa: BLE001 - the alert must never break the gate
            pass
    return False



def _get_or_create_row(db: Session, today: str) -> SharedOrderBudget:
    row = db.query(SharedOrderBudget).filter_by(trade_date=today).first()
    if row is None:
        row = SharedOrderBudget(trade_date=today, orders_placed_today=0)
        db.add(row)
        db.flush()
    return row


def _ensure_row_exists(db: Session, today: str) -> None:
    """Best-effort row creation ahead of an atomic UPDATE. Race-safe: if two
    processes insert the same trade_date concurrently, the table's unique
    constraint on trade_date rejects whichever commits second; that
    IntegrityError is swallowed here since the caller only needs the row to
    exist by the time it runs its own atomic UPDATE below, not to have
    created it itself."""
    exists = db.query(SharedOrderBudget.id).filter_by(trade_date=today).first()
    if exists is not None:
        return
    try:
        db.add(SharedOrderBudget(trade_date=today, orders_placed_today=0))
        db.commit()
    except IntegrityError:
        db.rollback()  # another process created it first — fine, row exists now


def check_and_reserve(db: Session) -> bool:
    """Call BEFORE placing a real manual BUY order. Returns True if under
    budget (and increments the counter), False if the shared daily cap has
    been reached. On ANY error, logs and returns True (fail open).

    AUDIT FIX: the increment is now a single conditional UPDATE ("increment
    only if still under budget") instead of a separate read-then-increment.
    The old version could let two near-simultaneous callers both read
    "under budget" and both increment, overshooting the shared Dhan order
    cap — the UPDATE's WHERE clause is evaluated atomically by the DB, so
    only as many concurrent callers as there is remaining budget can ever
    succeed. Still fails open on any error, exactly as before — this closes
    the race, it doesn't change the soft-governor design."""
    try:
        today = ist_today_str()
        _ensure_row_exists(db, today)
        result = db.execute(
            update(SharedOrderBudget)
            .where(
                SharedOrderBudget.trade_date == today,
                SharedOrderBudget.orders_placed_today < config.SHARED_DAILY_ORDER_BUDGET,
            )
            .values(orders_placed_today=SharedOrderBudget.orders_placed_today + 1)
        )
        db.commit()
        if result.rowcount and result.rowcount > 0:
            return True
        row = db.query(SharedOrderBudget).filter_by(trade_date=today).first()
        logger.warning(
            "SHARED Dhan order budget exhausted (%d/%d today across both services)",
            row.orders_placed_today if row else config.SHARED_DAILY_ORDER_BUDGET,
            config.SHARED_DAILY_ORDER_BUDGET,
        )
        return False
    except Exception as e:
        logger.error("shared_order_budget.check_and_reserve failed: %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return _gate_failed("shared order budget", e)


def record_order_unconditional(db: Session) -> None:
    """Call after a successful SELL — tracked for visibility, never gates
    an exit.

    FIX (session70 audit): was read-then-increment (not atomic), inconsistent
    with the sibling check_and_reserve() fixed to use a conditional UPDATE.
    Now uses an atomic UPDATE so two near-simultaneous calls (this service +
    position-stocks-service) can't both read the same row and both ORM-
    increment, resulting in only +1 instead of +2 in the shared counter.
    Low severity (visibility-only, fails open) but inconsistent with the
    sibling. No WHERE guard — exits are unconditional, always increment."""
    try:
        today = ist_today_str()
        _ensure_row_exists(db, today)
        db.execute(
            update(SharedOrderBudget)
            .where(SharedOrderBudget.trade_date == today)
            .values(orders_placed_today=SharedOrderBudget.orders_placed_today + 1)
        )
        db.commit()
    except Exception as e:
        logger.error("shared_order_budget.record_order_unconditional failed (non-blocking): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
