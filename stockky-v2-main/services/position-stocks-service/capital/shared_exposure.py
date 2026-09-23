"""
capital/shared_exposure.py — cross-service open-position market value.

WHY THIS EXISTS (2026-09-20 audit finding): real-trade-service and this
service share ONE real Dhan account, split 50/50 by config
(SCALP_POOL_CAPITAL_SHARE_PCT here, REAL_TRADE_CAPITAL_SHARE_PCT there).
real-trade-service's risk_engine "capital_share_cap" check (session52)
enforces its side of that split by comparing its own open-position value
plus a proposed BUY against the account's "total shared value" — but that
total only ever counted real-trade-service's OWN raw broker free cash plus
its OWN open positions, never this service's. Whenever this service holds a
real book, real-trade-service's computed total silently undercounted the
true account value by exactly that amount, over-restricting its own 50%
cap (the mirror-image of the original session52 incident — not money-unsafe
in this direction, since it errs toward blocking new BUYs rather than
overspending, but a real correctness bug in a check whose whole purpose is
a FAIR split).

This module is this service's half of the fix: publish_own_exposure()
writes this service's current open-position market value to the shared
`stockky_shared_service_exposure` table (models.py::SharedServiceExposure)
every ledger.sync_from_broker() cycle, so real-trade-service's own copy of
this module can read it back and complete its total.

This service does not currently need get_other_service_exposure() for its
own sizing — capital/ledger.py's pool allocation already self-corrects off
Dhan's live free cash, which already reflects whatever real-trade-service
has spent — but the reader is included for symmetry (same convention as
every other duplicated shared-table module in this codebase) and in case a
future check here needs it.

FAIL-OPEN, ALWAYS, both directions: this is visibility for a risk check,
not the ledger of record for either service's own capital. Any DB error on
publish or read is logged and swallowed — a broken publish/read must never
itself block or corrupt a real entry or exit. On a failed READ the caller
gets 0.0 (the same value this whole shared table exists to stop assuming),
which simply reverts the reading side to its old (undercounting, over-
restrictive-but-never-overspending) behavior — never a new way to get
stuck.

execution/shared_exposure.py in real-trade-service is the duplicated
counterpart — same logic, not imported (same isolation rationale as every
other duplicated module shared between these two services, e.g.
shared_order_budget.py / shared_symbol_lock.py).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from models import SharedServiceExposure

logger = logging.getLogger("position-stocks-shared-exposure")

SERVICE_NAME = "position-stocks-service"
OTHER_SERVICE_NAME = "real-trade-service"


def publish_own_exposure(db: Session, market_value: float) -> None:
    """Upsert this service's own open-position market value. Call once per
    ledger sync cycle (capital/ledger.py::sync_from_broker). Fail-open —
    never raises."""
    try:
        row = db.query(SharedServiceExposure).filter_by(service_name=SERVICE_NAME).first()
        if row is None:
            row = SharedServiceExposure(service_name=SERVICE_NAME, open_positions_market_value=0.0)
            db.add(row)
        row.open_positions_market_value = max(0.0, float(market_value or 0.0))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("shared-exposure: failed to publish own exposure: %s", e)


def get_other_service_exposure(db: Session) -> float:
    """Read real-trade-service's last-published open-position market
    value. Returns 0.0 (never raises) if the row doesn't exist yet or any
    DB error occurs — see module docstring for why that default is safe."""
    try:
        row = db.query(SharedServiceExposure).filter_by(service_name=OTHER_SERVICE_NAME).first()
        return float(row.open_positions_market_value) if row and row.open_positions_market_value else 0.0
    except Exception as e:
        logger.warning("shared-exposure: failed to read other service's exposure: %s", e)
        return 0.0
