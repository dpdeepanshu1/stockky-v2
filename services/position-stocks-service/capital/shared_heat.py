"""
capital/shared_heat.py - E276 round 50: the shared portfolio-heat figures, published by both engines.

real-trade-service and position-stocks-service trade ONE Dhan account. Each publishes its own open risk (sum of stop distance x qty), the gap allowance on
positions carried overnight, and its capital base in `stockky_shared_heat` (models.SharedHeat; a NEW table, so create_all() adds it with no ALTER). The check adds
the peer's open risk to its own and compares the total with the heat limit % of the combined pool (capital/portfolio_heat.py).

FAIL-OPEN, ALWAYS (same convention as shared_exposure.py / shared_daily_pnl.py): any DB error on publish or read is logged and swallowed; a peer row
that is missing, from an earlier IST day or older than max_age_s reads as (0, 0, 0), which is the old single-service cap - never a new way to get stuck.
The other service has the duplicated counterpart (execution/shared_heat.py in real-trade-service); same logic, not imported.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Tuple

from sqlalchemy.orm import Session

from models import SharedHeat, SharedHeatSectors
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-shared-heat")

SERVICE_NAME = "position-stocks-service"
OTHER_SERVICE_NAME = "real-trade-service"


def publish_own(db: Session, open_risk: float, gap_allowance: float, capital_base: float) -> None:
    """Upsert this service's open risk, gap allowance and capital base for today. Never raises."""
    try:
        row = db.query(SharedHeat).filter_by(service_name=SERVICE_NAME).first()
        if row is None:
            row = SharedHeat(service_name=SERVICE_NAME, trade_date=ist_today_str(), open_risk=0.0, gap_allowance=0.0, capital_base=0.0)
            db.add(row)
        row.trade_date = ist_today_str()
        row.open_risk = max(0.0, float(open_risk or 0.0))
        row.gap_allowance = max(0.0, float(gap_allowance or 0.0))
        row.capital_base = max(0.0, float(capital_base or 0.0))
        row.updated_at = datetime.now(timezone.utc)   # explicit heartbeat, like shared_exposure
        db.commit()
    except Exception as e:  # noqa: BLE001 - visibility for a risk check, must never block a trade
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("shared-heat: failed to publish own figures: %s", e)


def get_peer(db: Session, max_age_s: float = 900.0) -> Tuple[float, float, float]:
    """(peer open risk incl. its gap allowance, peer capital base, age seconds) for the OTHER service; (0.0, 0.0, 0.0) when the row is missing, from an
    earlier IST day, older than max_age_s (0 = no age limit) or on any DB error. Never raises."""
    try:
        row = db.query(SharedHeat).filter_by(service_name=OTHER_SERVICE_NAME).first()
        if row is None or row.trade_date != ist_today_str():
            return 0.0, 0.0, 0.0
        ts = row.updated_at
        age = 0.0
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
        if max_age_s and age > float(max_age_s):
            return 0.0, 0.0, age
        return float(row.open_risk or 0.0) + float(row.gap_allowance or 0.0), float(row.capital_base or 0.0), age
    except Exception as e:  # noqa: BLE001
        logger.warning("shared-heat: failed to read peer figures: %s", e)
        return 0.0, 0.0, 0.0


def publish_sectors(db: Session, sector_risk: dict) -> None:
    """E276 round 60: upsert this service's open risk split by "<sector>:<side>" for today. Never raises."""
    try:
        import json
        clean = {str(k): round(float(v), 2) for k, v in (sector_risk or {}).items() if float(v or 0.0) > 0}
        txt = json.dumps(clean, separators=(",", ":"))
        if len(txt) > 1900:   # column is 2000 wide: keep the biggest sectors rather than fail
            keep = sorted(clean.items(), key=lambda kv: kv[1], reverse=True)
            clean = {}
            for k, v in keep:
                trial = dict(clean, **{k: v})
                if len(json.dumps(trial, separators=(",", ":"))) > 1900:
                    break
                clean = trial
            txt = json.dumps(clean, separators=(",", ":"))
        row = db.query(SharedHeatSectors).filter_by(service_name=SERVICE_NAME).first()
        if row is None:
            row = SharedHeatSectors(service_name=SERVICE_NAME, trade_date=ist_today_str(), sector_json="{}")
            db.add(row)
        row.trade_date = ist_today_str()
        row.sector_json = txt
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("shared-heat: failed to publish sector split: %s", e)


def get_peer_sectors(db: Session, max_age_s: float = 900.0) -> dict:
    """{"<sector>:<side>": rupees} for the OTHER service; {} when the row is missing, from an earlier IST day, older than max_age_s or on any DB error. Never raises."""
    try:
        import json
        row = db.query(SharedHeatSectors).filter_by(service_name=OTHER_SERVICE_NAME).first()
        if row is None or row.trade_date != ist_today_str():
            return {}
        ts = row.updated_at
        if ts is not None and max_age_s:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() > float(max_age_s):
                return {}
        d = json.loads(row.sector_json or "{}")
        return {str(k): float(v) for k, v in d.items() if float(v or 0.0) > 0}
    except Exception as e:  # noqa: BLE001
        logger.warning("shared-heat: failed to read peer sector split: %s", e)
        return {}
