"""
audit/logger.py — one call, one row, in trade_audit_log. Every module that
takes a consequential action (admin login, arm/disarm, order placed, risk
rejection, reconciliation mismatch) calls log_action() rather than writing
to the table directly, so the schema/shape stays consistent everywhere it's
read back (dashboard "Recent Activity" panel, future debugging).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger("real-trade-audit")


def log_action(db: Session, *, actor: str, action: str, detail: str = "", mode: Optional[str] = None) -> None:
    try:
        row = models.TradeAuditLog(mode=mode, actor=actor, action=action, detail=detail)
        db.add(row)
        db.commit()
    except Exception as e:
        # Audit logging must never be the reason a real request fails — log
        # the failure to stderr and let the caller's actual work proceed.
        logger.error("audit log_action failed (action=%s): %s", action, e)
        db.rollback()


def log_action_behind(db: Session, *, actor: str, action: str, detail: str = "", mode: Optional[str] = None) -> None:
    """E285 round 131: log_action for a summary / housekeeping row written from inside a trading cycle (ENTRY_CYCLE, ORDERS_EXPIRED, EXIT_ORDERS_EXPIRED).
    log_action commits the CALLER'S session, so the 2 Nov VM log showed single trade_audit_log INSERTs of 2.3-4.6 s waiting inside the entry and exit ticks.
    Here the row is queued for the write-behind worker (its own session, backed off after a timeout) and the cycle moves on. Falls back to the old
    synchronous log_action when CYCLE_AUDIT_WRITE_BEHIND is off, or the queue is off / unavailable. Rows that decide something (ORDER_PLACED, ARMED, login, ...)
    keep using log_action. Never raises."""
    try:
        import config
        if getattr(config, "CYCLE_AUDIT_WRITE_BEHIND", True):
            import write_behind
            if write_behind.submit("audit", {"mode": mode, "actor": actor, "action": action, "detail": detail}):
                return
    except Exception as e:  # noqa: BLE001
        logger.debug("audit write-behind unavailable, writing synchronously: %s", e)
    log_action(db, actor=actor, action=action, detail=detail, mode=mode)
