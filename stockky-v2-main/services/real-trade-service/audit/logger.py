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
