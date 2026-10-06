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
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from models import SharedServiceExposure

logger = logging.getLogger("real-trade-shared-exposure")

SERVICE_NAME = "real-trade-service"
OTHER_SERVICE_NAME = "position-stocks-service"

# GROUP 193: warn (at most every _STALE_WARN_EVERY_S) when position-stocks-service's
# published exposure is missing or older than this, because a missing/stale
# figure silently skews the share-cap total.
_PEER_STALE_AFTER_S = 900.0
_STALE_WARN_EVERY_S = 600.0
_last_stale_warn_ts: Optional[float] = None


def _warn_peer_stale(age: Optional[float]) -> None:
    global _last_stale_warn_ts
    if age is not None and age <= _PEER_STALE_AFTER_S:
        return
    now = time.monotonic()
    if _last_stale_warn_ts is not None and now - _last_stale_warn_ts < _STALE_WARN_EVERY_S:
        return
    _last_stale_warn_ts = now
    logger.warning(
        "shared-exposure: position-stocks-service exposure is %s — the 50%% share-cap total may be "
        "undercounted or overcounted until it publishes again.",
        "missing" if age is None else f"{age:,.0f}s old",
    )


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
        # GROUP 193: explicit heartbeat. SQLAlchemy skips the UPDATE (and so the
        # onupdate timestamp) when the value is unchanged, which made updated_at
        # mean 'last time the figure changed' and a quiet service look stale.
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:
        # BUG FIX (session112 round 8): this handler's own rollback() was
        # unguarded, so a dead connection (commit fails, then the cleanup
        # rollback fails too) would raise straight out of a function that
        # documents "never raises" — into equity_sync.py and from there
        # whatever triggered the sync cycle. Every sibling shared-table
        # module (shared_order_budget / shared_symbol_lock) already guards
        # this. Mirrors the identical fix in position-stocks-service's copy
        # of this module (session112 round 7).
        try:
            db.rollback()
        except Exception:
            pass
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


def get_other_service_exposure_age(db: Session) -> Optional[float]:
    """Seconds since position-stocks-service last published its exposure, or None when no row
    exists / on any DB error. Never raises.

    GROUP 193: the share-cap reject message used to show only a total, so a missing or stale
    peer figure (the cause of an undercounted, shrinking total) could not be told apart from a
    genuinely full account. Also logs a throttled warning when the figure is missing or old."""
    try:
        row = db.query(SharedServiceExposure).filter_by(service_name=OTHER_SERVICE_NAME).first()
        if row is None:
            _warn_peer_stale(None)
            return None
        ts = row.updated_at
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
        _warn_peer_stale(age)
        return age
    except Exception as e:
        logger.warning("shared-exposure: failed to read other service's exposure age: %s", e)
        return None


def _in_flight_max_age_minutes() -> float:
    raw = (os.getenv("SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES") or "").strip()
    try:
        v = float(raw) if raw else 120.0
    except ValueError:
        return 120.0
    return v if v >= 0 else 120.0


def get_in_flight_buy_value(db: Session, mode: str) -> float:
    """Rupee value of this service's REAL BUY orders that Dhan has not finished
    filling and that are not yet booked into a position.

    GROUP 193 (share-cap total kept shrinking): a BUY becomes a TradePosition
    only when reconcile books its fill. Between placement and that booking Dhan
    already blocks the cash, so broker_cash_available (part of the share-cap
    total) fell while the order was counted nowhere, and exposure was
    undercounted at the same time. Value = remaining qty (qty minus
    filled_qty_so_far, which reconcile has already booked into positions) times
    limit_price. Only PENDING/PLACED/PARTIAL orders created within
    SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES (default 120; 0 = disabled) count, so an
    order stuck in a non-terminal state cannot inflate exposure forever. Orders
    with no price are skipped. Fail-open: any error returns 0.0 (the old
    behaviour). Never raises."""
    if mode != "REAL":
        return 0.0
    max_age = _in_flight_max_age_minutes()
    if max_age <= 0:
        return 0.0
    try:
        import models
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age)
        rows = (
            db.query(models.TradeOrder)
            .filter(
                models.TradeOrder.mode == mode,
                models.TradeOrder.side == "BUY",
                models.TradeOrder.status.in_(("PENDING", "PLACED", "PARTIAL")),
                models.TradeOrder.created_at >= cutoff,
            )
            .all()
        )
        total = 0.0
        for o in rows:
            price = float(o.limit_price or 0.0)
            remaining = int(o.qty or 0) - int(o.filled_qty_so_far or 0)
            if price > 0 and remaining > 0:
                total += price * remaining
        return round(total, 2)
    except Exception as e:
        logger.warning("shared-exposure: failed to compute in-flight BUY value: %s", e)
        return 0.0
