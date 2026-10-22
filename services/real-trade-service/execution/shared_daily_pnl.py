"""
execution/shared_daily_pnl.py - A11 (2026-10-22): account-wide daily loss cap, shared by both engines.

WHY: real-trade-service and position-stocks-service trade ONE Dhan account, split ~50/50 by config. Each already
stops new BUYs off its OWN realized loss against its OWN capital (real-trade: DEFAULT_MAX_DAILY_LOSS_PCT of its
equity; position-stocks: MAX_DAILY_LOSS_PCT_OF_POOL of its pool). Neither looked at the other, so a bad day on BOTH
sides could lose more of the account than either cap intended while each engine still read "within my limit".
This adds one extra layer: sum both engines' realized P&L for the IST day, divide by the sum of both capital
bases, and block new BUYs in BOTH engines once the combined net loss reaches ACCOUNT_MAX_DAILY_LOSS_PCT
(default 3.0; 0 turns the layer off). It does NOT replace or change the per-service caps. Exits are never blocked.

HOW: each service upserts its own row in `stockky_shared_daily_pnl` (models.SharedDailyPnl: realized P&L for
today, capital base, IST trade_date). The checking side passes its OWN live figures (never a lagging copy) and
reads only the PEER's row. A peer row from an earlier IST day is ignored, so yesterday's loss can never freeze
today. If the peer row is missing or unreadable the sum falls back to this service's own figures alone, which is
the same as its own per-service cap - never a new way to get stuck.

FAIL-OPEN, ALWAYS: any DB error on publish or read is logged and swallowed (same convention as shared_exposure.py).
capital/shared_daily_pnl.py in position-stocks-service is the duplicated counterpart (same logic, not
imported - same isolation rationale as shared_exposure.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from models import SharedDailyPnl
from tz_utils import ist_today_str

logger = logging.getLogger("real-trade-shared-daily-pnl")

SERVICE_NAME = "real-trade-service"
OTHER_SERVICE_NAME = "position-stocks-service"


def combined_loss_pct(own_pnl: float, own_base: float, peer_pnl: float, peer_base: float) -> float:
    """Net combined loss today as a % of the combined capital base. Profit on one side offsets loss on the other
    (it is an ACCOUNT-level number). 0.0 when the combined base is not positive or the account is net up."""
    base = max(float(own_base or 0.0), 0.0) + max(float(peer_base or 0.0), 0.0)
    if base <= 0:
        return 0.0
    net = float(own_pnl or 0.0) + float(peer_pnl or 0.0)
    return max(0.0, -net) / base * 100.0


def publish_own(db: Session, realized_pnl_today: float, capital_base: float) -> None:
    """Upsert this service's realized P&L for today and its capital base. Never raises."""
    try:
        row = db.query(SharedDailyPnl).filter_by(service_name=SERVICE_NAME).first()
        if row is None:
            row = SharedDailyPnl(service_name=SERVICE_NAME, trade_date=ist_today_str(),
                                 realized_pnl_today=0.0, capital_base=0.0)
            db.add(row)
        row.trade_date = ist_today_str()
        row.realized_pnl_today = float(realized_pnl_today or 0.0)
        row.capital_base = max(0.0, float(capital_base or 0.0))
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:  # noqa: BLE001 - visibility for a risk check, must never block a trade
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("shared-daily-pnl: failed to publish own figures: %s", e)


def get_peer(db: Session) -> Tuple[float, float, Optional[float]]:
    """(peer_realized_pnl_today, peer_capital_base, age_seconds) for the OTHER service. (0.0, 0.0, None) when the
    row is missing, belongs to an earlier IST day, or on any DB error. Never raises."""
    try:
        row = db.query(SharedDailyPnl).filter_by(service_name=OTHER_SERVICE_NAME).first()
        if row is None or row.trade_date != ist_today_str():
            return 0.0, 0.0, None
        ts = row.updated_at
        age = None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
        return float(row.realized_pnl_today or 0.0), float(row.capital_base or 0.0), age
    except Exception as e:  # noqa: BLE001
        logger.warning("shared-daily-pnl: failed to read peer figures: %s", e)
        return 0.0, 0.0, None


def account_wide_loss_pct(db: Session, own_pnl: float, own_base: float) -> float:
    """Combined net loss today (% of combined base), using this service's own LIVE figures plus the peer's
    last-published row. Never raises; any failure degrades to this service's own figures alone."""
    try:
        peer_pnl, peer_base, _age = get_peer(db)
        return combined_loss_pct(own_pnl, own_base, peer_pnl, peer_base)
    except Exception as e:  # noqa: BLE001 - a bad figure must never become a new way to block (or crash) an entry
        logger.warning("shared-daily-pnl: account-wide loss unavailable, treating as 0: %s", e)
        return 0.0
