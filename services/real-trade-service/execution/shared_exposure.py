"""
execution/shared_exposure.py — cross-service open-position market value.

WHY THIS EXISTS (2026-09-20 audit finding): this service and
position-stocks-service share ONE real Dhan account, split 50/50 by config
(CAPITAL_SHARE_PCT here, SCALP_POOL_CAPITAL_SHARE_PCT there).
risk_engine/engine.py's "capital_share_cap" check (session52) enforces this
service's side of that split by comparing this service's own open-position
value plus a proposed BUY against the account's "total shared value" — but
that total only ever counted this service's OWN raw broker free cash
(AccountState.broker_cash_available) plus this service's OWN open positions
(AccountState.open_positions_market_value), never position-stocks-service's.
Whenever position-stocks-service holds a real book, the computed total
silently undercounted the true account value by exactly that amount,
over-restricting this service's own 50% cap — the mirror-image of the
original session52 incident (this service was found holding ~93.5% of the
account). Not money-unsafe in this new direction, since undercounting the
total only makes the cap SMALLER (errs toward blocking new BUYs, never
toward allowing overspend), but it's a real correctness bug in a check
whose entire purpose is a FAIR split, and it was silent — nothing logged
that the total was an undercount.

get_other_service_exposure() closes that gap: it reads
position-stocks-service's last-published open-position market value from
the shared `stockky_shared_service_exposure` table
(models.py::SharedServiceExposure), so entry_engine/entry.py,
manual_engine.py, and main.py's dry-run risk-check endpoint can add it into
AccountState.other_service_open_positions_market_value before calling
risk_engine.evaluate().

publish_own_exposure() is this service's other half of the same shared
mechanism — writes this service's own open-position value so
position-stocks-service's copy of this module could read it back too, if a
future check there ever needs it (it doesn't today — that service's pool
sizing already self-corrects off Dhan's live free cash, which already
reflects whatever this service has spent). Call it once per equity sync
cycle (execution/equity_sync.py).

FAIL-OPEN, ALWAYS, both directions — same convention as
execution/shared_order_budget.py / shared_symbol_lock.py: any DB error on
publish or read is logged and swallowed. On a failed READ the caller gets
0.0, which simply reverts to the old (undercounting, over-restrictive-but-
never-overspending) behavior — never a new way to get stuck.

capital/shared_exposure.py in position-stocks-service is the duplicated
counterpart — same logic, not imported (same isolation rationale as every
other duplicated module shared between these two services).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from models import SharedServiceExposure

logger = logging.getLogger("real-trade-shared-exposure")

SERVICE_NAME = "real-trade-service"
OTHER_SERVICE_NAME = "position-stocks-service"


def publish_own_exposure(db: Session, market_value: float) -> None:
    """Upsert this service's own open-position market value. Call once per
    equity sync cycle (execution/equity_sync.py). Fail-open — never
    raises."""
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
    """Read position-stocks-service's last-published open-position market
    value. Returns 0.0 (never raises) if the row doesn't exist yet or any
    DB error occurs — see module docstring for why that default is safe."""
    try:
        row = db.query(SharedServiceExposure).filter_by(service_name=OTHER_SERVICE_NAME).first()
        return float(row.open_positions_market_value) if row and row.open_positions_market_value else 0.0
    except Exception as e:
        logger.warning("shared-exposure: failed to read other service's exposure: %s", e)
        return 0.0
