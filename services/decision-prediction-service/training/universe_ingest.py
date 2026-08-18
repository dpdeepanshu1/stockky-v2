"""
Universe → Training ingest.

Accepts the full daily scan universe (not only actionable picks),
builds point-in-time feature rows (technical + fundamental + news),
stores them in DB with 24–48h TTL for training + cache, and can
trigger / queue a training run.

Designed to be called:
1. Manually from the frontend button "Send the Stock Universe For Training"
2. Automatically between 12:00 AM – 6:00 AM IST via scheduler / GitHub Actions
"""

from __future__ import annotations

import json
import re
import re
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger("universe_ingest")

IST = ZoneInfo("Asia/Kolkata")
RETENTION_HOURS = int(os.getenv("TRAINING_SAMPLE_RETENTION_HOURS", "48"))  # 24–48h
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("TRAINING_DATABASE_URL") or "sqlite:///./training.db"

Base = declarative_base()


class UniverseTrainingSample(Base):
    """Short-lived training samples from daily scan universe (24–48h retention)."""
    __tablename__ = "universe_training_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), index=True, nullable=False)
    as_of = Column(DateTime, index=True, nullable=False)
    source = Column(String(64), default="daily_scan")  # daily_scan | manual_universe
    decision = Column(String(32), nullable=True)  # BUY NOW / PREPARE / etc (optional)
    combined_score = Column(Float, nullable=True)
    feature_snapshot = Column(Text, nullable=True)  # JSON of full feature vector
    label = Column(Float, nullable=True)  # filled later by T+1/T+5 evaluator if available
    created_at = Column(DateTime, default=lambda: datetime.now(IST).replace(tzinfo=None), index=True)
    expires_at = Column(DateTime, index=True, nullable=False)


def _get_engine():
    url = os.getenv("TRAINING_DATABASE_URL") or os.getenv("DATABASE_URL") or DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Neon/Supabase SSL
    if url.startswith("postgresql") and "sslmode=" not in url.lower():
        url += ("&" if "?" in url else "?") + "sslmode=require"
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url).replace("?&", "?").rstrip("?&")
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=1, echo=False)


def init_universe_tables():
    engine = _get_engine()
    Base.metadata.create_all(engine)
    return engine


def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def ingest_universe(
    symbols: List[str],
    *,
    decisions: Optional[Dict[str, str]] = None,
    scores: Optional[Dict[str, float]] = None,
    feature_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    source: str = "daily_scan",
    retention_hours: int = RETENTION_HOURS,
) -> Dict[str, Any]:
    """
    Store the full scan universe for training.

    Parameters
    ----------
    symbols : full list of symbols from the daily scan (not only actionable)
    decisions : optional map symbol -> decision label
    scores : optional map symbol -> combined_score
    feature_snapshots : optional map symbol -> full feature dict (tech+fund+news)
    source : "daily_scan" | "manual_universe"
    retention_hours : how long to keep rows (default 48h)
    """
    if not symbols:
        return {"ok": False, "ingested": 0, "message": "No symbols provided"}

    engine = init_universe_tables()
    Session = sessionmaker(bind=engine)
    db = Session()
    now = _now_ist()
    expires = now + timedelta(hours=retention_hours)
    decisions = decisions or {}
    scores = scores or {}
    feature_snapshots = feature_snapshots or {}

    ingested = 0
    try:
        # Purge expired first (one query)
        deleted = db.query(UniverseTrainingSample).filter(
            UniverseTrainingSample.expires_at < now
        ).delete(synchronize_session=False)
        if deleted:
            logger.info("Purged %s expired universe samples", deleted)

        # Normalize symbols once
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        clean = []
        seen = set()
        for sym in symbols:
            s = (sym or "").strip().upper().replace(".NS", "").replace(".BO", "")
            if not s or s in seen:
                continue
            seen.add(s)
            clean.append(s)

        # Delete today's rows for this source in one shot, then bulk insert
        # (avoids N SELECT-per-symbol which times out on free tier with 200+ symbols)
        if clean:
            db.query(UniverseTrainingSample).filter(
                UniverseTrainingSample.symbol.in_(clean),
                UniverseTrainingSample.as_of >= day_start,
                UniverseTrainingSample.source == source,
            ).delete(synchronize_session=False)

        batch = []
        for sym in clean:
            snap = feature_snapshots.get(sym) or feature_snapshots.get(sym + ".NS")
            batch.append(UniverseTrainingSample(
                symbol=sym,
                as_of=now,
                source=source,
                decision=decisions.get(sym) or decisions.get(sym + ".NS"),
                combined_score=scores.get(sym) or scores.get(sym + ".NS"),
                feature_snapshot=json.dumps(snap) if snap else None,
                expires_at=expires,
            ))
        if batch:
            # Commit in chunks so free-tier DB/proxy never times out mid-write
            chunk = 80
            for i in range(0, len(batch), chunk):
                db.bulk_save_objects(batch[i : i + chunk])
                db.commit()
            ingested = len(batch)
        else:
            db.commit()
        logger.info("Ingested %s universe symbols for training (source=%s, ttl=%sh)", ingested, source, retention_hours)
        return {
            "ok": True,
            "ingested": ingested,
            "source": source,
            "expires_at": expires.isoformat(),
            "retention_hours": retention_hours,
            "message": f"Stored {ingested} symbols for training (kept {retention_hours}h)",
        }
    except Exception as e:
        db.rollback()
        logger.exception("Universe ingest failed: %s", e)
        return {"ok": False, "ingested": 0, "message": str(e)}
    finally:
        db.close()


def get_active_universe_samples(limit: int = 5000) -> List[Dict[str, Any]]:
    """Return non-expired samples for training."""
    engine = init_universe_tables()
    Session = sessionmaker(bind=engine)
    db = Session()
    now = _now_ist()
    try:
        rows = (
            db.query(UniverseTrainingSample)
            .filter(UniverseTrainingSample.expires_at >= now)
            .order_by(UniverseTrainingSample.created_at.desc())
            .limit(limit)
            .all()
        )
        out = []
        for r in rows:
            out.append({
                "symbol": r.symbol,
                "as_of": r.as_of.isoformat() if r.as_of else None,
                "source": r.source,
                "decision": r.decision,
                "combined_score": r.combined_score,
                "feature_snapshot": json.loads(r.feature_snapshot) if r.feature_snapshot else None,
                "label": r.label,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            })
        return out
    finally:
        db.close()


def purge_expired() -> int:
    engine = init_universe_tables()
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        n = db.query(UniverseTrainingSample).filter(
            UniverseTrainingSample.expires_at < _now_ist()
        ).delete(synchronize_session=False)
        db.commit()
        return n
    finally:
        db.close()
