"""
intraday_eligibility.py — 2026-09-11 fix.

User-reported bug: "if you plan to buy and sell intraday then pick those
stock only which allow for intraday — all stocks not allow to intraday."

Root cause (confirmed via live Dhan order-book evidence, session21e):
exit_engine already detects "this security can't use product_type=INTRADAY"
(T2T/ASM/GSM surveillance stocks) — but only reactively, when a same-day
SELL bounces off Dhan. Nothing upstream (candidate_engine, entry_engine,
manual_engine) ever checked eligibility BEFORE buying, so the same
restricted stock could keep getting picked and bought, then get stuck
unable to exit same-day every time.

There is no static "intraday eligible" flag anywhere in Dhan's own scrip
master (see execution/dhan_client.py's security-cache module note) — ASM/
GSM staging is an exchange-side status that changes over time and isn't
published in the instrument CSV. So this is deliberately NOT a lookup
table of "known good/bad stocks" seeded from nowhere — it's a learned list,
built entirely from real rejections this service has actually seen, and
consulted before future buys so today's lesson is used tomorrow.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger("real-trade-intraday-eligibility")


def _now():
    return datetime.now(timezone.utc)


def record_restriction(db: Session, symbol: str, detail: Optional[str] = None) -> None:
    """Upsert: call this wherever a live Dhan rejection is identified as
    dhan_client.is_security_intraday_restricted_error() (currently only
    exit_engine.py). Best-effort — a failure here must never block the
    actual exit-handling flow it's called from, so callers should wrap
    this in try/except and just log on failure."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return
    row = db.query(models.IntradayRestrictedSecurity).filter(
        models.IntradayRestrictedSecurity.symbol == sym
    ).first()
    if row:
        row.last_detected_at = _now()
        row.hit_count = (row.hit_count or 0) + 1
        if detail:
            row.last_detail = detail[:255]
    else:
        db.add(models.IntradayRestrictedSecurity(
            symbol=sym, first_detected_at=_now(), last_detected_at=_now(),
            hit_count=1, last_detail=(detail[:255] if detail else None),
        ))
    db.commit()
    logger.info("intraday_eligibility: recorded restriction for %s (%s)", sym, detail)


def get_restricted_symbols(db: Session) -> set:
    """Bulk fetch — used by candidate_engine to filter a whole cycle's
    candidate batch in one query instead of one lookup per symbol."""
    try:
        rows = db.query(models.IntradayRestrictedSecurity.symbol).all()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning("intraday_eligibility: bulk fetch failed (%s) — treating as empty", e)
        return set()


def is_restricted(db: Session, symbol: str) -> bool:
    """Single-symbol check — used by manual_engine.py for an explicit
    INTRADAY/MIS ticket, where fetching the whole set would be overkill."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return False
    try:
        return db.query(models.IntradayRestrictedSecurity).filter(
            models.IntradayRestrictedSecurity.symbol == sym
        ).first() is not None
    except Exception as e:
        logger.warning("intraday_eligibility: lookup failed for %s (%s) — treating as not restricted", sym, e)
        return False
