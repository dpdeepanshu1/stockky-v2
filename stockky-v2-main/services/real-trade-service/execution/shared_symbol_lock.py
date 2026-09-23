"""
execution/shared_symbol_lock.py — cross-service claim preventing this
service and position-stocks-service from both holding a live broker
position in the same symbol at once.

Duplicated from position-stocks-service/capital/shared_symbol_lock.py on
purpose (same isolation rationale as execution/shared_order_budget.py and
every other concept these two services share but don't share code for).

WHY: both services trade through the SAME Dhan account. Dhan holds one
consolidated position per symbol at the broker — it has no concept of
"these shares belong to real-trade-service" vs "these belong to
position-stocks-service". Confirmed in production: AEGISVOPAK was bought
by BOTH services on 17 Sept, each with its own local qty/entry/stop/target
row pointing at a share of one real, merged Dhan position. When either
side later sold on its own target/stop hit, its SELL was sized/priced
from only its own local record — Dhan's order book (reporting the true
merged position) didn't match what this service believed it had just
sent, which is exactly the "Broker order-type mismatch... investigate"
Telegram alert this was diagnosed from.

REAL-only: DEMO mode never touches the real broker, so it never claims or
releases this lock — only mode=="REAL" call sites check it (same
convention as shared_order_budget.check_and_reserve, which manual_engine
and entry.py only ever call on the REAL branch).

FAIL-OPEN, ALWAYS: this is a soft cross-service guard, not a financial
ledger. Any DB error is logged and treated as "allow the order" — a
broken lock must never itself block a real entry or, especially, a real
exit. Worst case on failure is a reversion to today's actual (buggy,
pre-fix) behavior, never a new way to get a position stuck.

Wired into:
  - entry_engine/entry.py's automatic REAL BUY path — claimed right before
    the Dhan LIMIT order is placed; released if that placement is
    REJECTED or if the order later EXPIRES unfilled (expire_stale_orders)
  - manual_engine.py's manual REAL BUY path — same claim/release shape
  - exit_engine/exit.py — released once a position's qty_open reaches 0
    (fully flat), not on a partial exit
  - portfolio.import_broker_holdings() (session62) — claimed before a
    pre-existing Dhan demat holding starts being actively managed by this
    service (stop/target evaluation, possible auto-SELL); the import is
    skipped for that symbol (not overridden) if position-stocks-service
    already holds the claim
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SharedSymbolLock

logger = logging.getLogger("real-trade-shared-symbol-lock")

_SERVICE_NAME = "real-trade-service"


def try_claim(db: Session, symbol: str, mode: str = "REAL") -> bool:
    """Call BEFORE a REAL BUY for `symbol` goes to Dhan. Returns True if
    the claim succeeded (no other service currently holds this symbol, or
    this service already holds it itself — e.g. an averaging-in add),
    False if position-stocks-service already holds it and the BUY should
    be rejected/skipped. On ANY error, logs and returns True (fail open)."""
    symbol = symbol.strip().upper()
    try:
        existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if existing is not None:
            if existing.held_by_service == _SERVICE_NAME:
                return True  # already ours — not a conflict
            logger.warning(
                "symbol lock BLOCKED buy of %s — already held by %s (mode=%s) since %s",
                symbol, existing.held_by_service, existing.held_by_mode, existing.claimed_at,
            )
            return False
        db.add(SharedSymbolLock(symbol=symbol, held_by_service=_SERVICE_NAME, held_by_mode=mode))
        db.commit()
        return True
    except IntegrityError:
        # Race: position-stocks-service (or a concurrent request here)
        # inserted the same symbol between our SELECT and our INSERT.
        db.rollback()
        try:
            existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
            if existing is not None and existing.held_by_service != _SERVICE_NAME:
                logger.warning(
                    "symbol lock BLOCKED buy of %s — lost race to %s",
                    symbol, existing.held_by_service,
                )
                return False
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error("shared_symbol_lock.try_claim(%s) failed (failing open): %s", symbol, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return True


def release(db: Session, symbol: str) -> None:
    """Call once this service's position in `symbol` is fully flat
    (qty_open reaches 0 — see exit_engine/exit.py) or once a PLACED order
    for it is REJECTED/EXPIRED without ever filling. Only releases a row
    this service itself holds; never touches a row position-stocks-service
    holds. Never raises."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol, held_by_service=_SERVICE_NAME).first()
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception as e:
        logger.error("shared_symbol_lock.release(%s) failed (non-blocking): %s", symbol, e, exc_info=True)
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
        logger.error("shared_symbol_lock.status failed: %s", e, exc_info=True)
        return []
