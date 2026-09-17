"""
capital/shared_symbol_lock.py — cross-service claim preventing this service
and real-trade-service from both holding a live broker position in the
same symbol at once (session60 fix).

WHY: both services trade through the SAME Dhan account. Dhan holds one
consolidated position per symbol at the broker — it has no concept of
"these shares belong to position-stocks-service" vs "these belong to
real-trade-service". Confirmed in production: AEGISVOPAK was bought by
BOTH services on 17 Sept, each with its own local qty/entry/stop/target
row pointing at a share of one real, merged Dhan position. When either
side later sold on its own target/stop hit, its SELL was sized/priced
from only its own local record — Dhan's order book (reporting the true
merged position) didn't match what this service believed it had just
sent, which is exactly the "Broker order-type mismatch... investigate"
Telegram alert this was diagnosed from.

FAIL-OPEN, ALWAYS, same as capital/shared_order_budget.py: this is a
soft cross-service guard, not a financial ledger. Any DB error is logged
and treated as "allow the order" — a broken lock must never itself block
a real entry or, especially, a real exit. Worst case on failure is a
reversion to today's actual (buggy, pre-fix) behavior, never a new way
to get a position stuck.

execution/shared_symbol_lock.py in real-trade-service is the duplicated
counterpart — same logic, not imported (same isolation rationale as
every other duplicated module shared between these two services)."""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SharedSymbolLock

logger = logging.getLogger("position-stocks-shared-symbol-lock")

_SERVICE_NAME = "position-stocks-service"


def try_claim(db: Session, symbol: str) -> bool:
    """Call BEFORE a BUY for `symbol` goes to Dhan. Returns True if the
    claim succeeded (no other service currently holds this symbol, or
    this service already holds it itself — e.g. an averaging-in add),
    False if real-trade-service already holds it and the BUY should be
    skipped this cycle. On ANY error, logs and returns True (fail open) —
    same rationale as shared_order_budget.check_and_reserve."""
    symbol = symbol.strip().upper()
    try:
        existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if existing is not None:
            if existing.held_by_service == _SERVICE_NAME:
                return True  # already ours (e.g. re-entry attempt) — not a conflict
            logger.warning(
                "position-stocks: symbol lock BLOCKED buy of %s — already held by %s (mode=%s) since %s",
                symbol, existing.held_by_service, existing.held_by_mode, existing.claimed_at,
            )
            return False
        db.add(SharedSymbolLock(symbol=symbol, held_by_service=_SERVICE_NAME, held_by_mode=None))
        db.commit()
        return True
    except IntegrityError:
        # Race: real-trade-service (or a concurrent request here) inserted
        # the same symbol between our SELECT and our INSERT. The unique
        # constraint on `symbol` caught it — treat exactly like the
        # "existing row found" branch above.
        db.rollback()
        try:
            existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
            if existing is not None and existing.held_by_service != _SERVICE_NAME:
                logger.warning(
                    "position-stocks: symbol lock BLOCKED buy of %s — lost race to %s",
                    symbol, existing.held_by_service,
                )
                return False
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error("position-stocks: shared_symbol_lock.try_claim(%s) failed (failing open): %s", symbol, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return True


def release(db: Session, symbol: str) -> None:
    """Call once this service's position in `symbol` is fully flat
    (closed/target hit/stop hit/EOD squareoff/manual exit — any terminal
    state). Only releases a row this service itself holds; never touches
    a row real-trade-service holds. Never raises — a release failure must
    not block the exit that triggered it; worst case the row is left
    stale and simply blocks this service's own re-entry into the same
    symbol until manually cleared, which is safe (if a little annoying)
    rather than dangerous."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol, held_by_service=_SERVICE_NAME).first()
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception as e:
        logger.error("position-stocks: shared_symbol_lock.release(%s) failed (non-blocking): %s", symbol, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


def status(db: Session) -> list[dict]:
    """Read-only snapshot for /status — every symbol currently locked by
    either service. Never raises."""
    try:
        rows = db.query(SharedSymbolLock).all()
        return [
            {
                "symbol": r.symbol,
                "held_by_service": r.held_by_service,
                "held_by_mode": r.held_by_mode,
                "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("position-stocks: shared_symbol_lock.status failed: %s", e, exc_info=True)
        return []


def force_release(db: Session, symbol: str) -> bool:
    """Admin-only: unconditionally delete the lock row for `symbol`
    regardless of which service holds it. Used to clear stuck locks
    (e.g. TREL held_by_mode=null after a dead-entry error that didn't
    call release()). Returns True if a row was deleted, False if none
    existed. Never raises."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        logger.warning(
            "position-stocks: shared_symbol_lock.force_release(%s) — "
            "lock held by %s (mode=%s) cleared by admin",
            symbol, row.held_by_service, row.held_by_mode,
        )
        return True
    except Exception as e:
        logger.error(
            "position-stocks: shared_symbol_lock.force_release(%s) failed: %s",
            symbol, e, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False


def cleanup_stale(db: Session) -> list[str]:
    """BUG FIX (Issue #4): on startup (and optionally on demand), sweep
    SharedSymbolLock rows held by THIS service and release any whose symbol
    has no corresponding OPEN or EXIT_LEGS_REJECTED ScalpPosition — meaning
    the position was closed/errored but release() was never called (exactly
    the dead-entry bug fixed in reconcile.py). Safe to run at startup
    because a genuinely open position will always have its status row; a
    stale lock by definition has none. Returns list of released symbols."""
    released = []
    try:
        from models import ScalpPosition  # local to avoid circular import
        our_locks = db.query(SharedSymbolLock).filter_by(
            held_by_service=_SERVICE_NAME
        ).all()
        for lock in our_locks:
            has_open = db.query(ScalpPosition).filter(
                ScalpPosition.symbol == lock.symbol,
                ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")),
            ).first()
            if has_open is None:
                db.delete(lock)
                released.append(lock.symbol)
                logger.warning(
                    "position-stocks: shared_symbol_lock.cleanup_stale: "
                    "released stale lock for %s (no open position found)",
                    lock.symbol,
                )
        if released:
            db.commit()
    except Exception as e:
        logger.error(
            "position-stocks: shared_symbol_lock.cleanup_stale failed: %s",
            e, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
    return released
