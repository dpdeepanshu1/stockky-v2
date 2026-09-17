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

from sqlalchemy import text
from sqlalchemy.orm import Session

import models

logger = logging.getLogger("real-trade-intraday-eligibility")


def _now():
    return datetime.now(timezone.utc)


# 2026-09-17 fix: real-trade-service and position-stocks-service each keep
# their OWN restricted-symbol table (trade_intraday_restricted here,
# scalp_intraday_restricted there) even though both place orders against
# the exact same Dhan account. A symbol Dhan rejects as intraday-restricted
# for one service is just as restricted for the other — but until this fix
# a rejection learned by one service never protected the other, so the
# same stock could get bought and stuck again on whichever service hadn't
# personally seen it reject yet. Both services share one Oracle schema
# (same ORACLE_DSN/USER/PASSWORD — see docker-compose.yml), so the sister
# table is reachable from this service's own DB session; no cross-service
# HTTP call needed. Read via raw SQL rather than importing
# position-stocks-service's model class, since the two services are
# separate codebases/containers and neither imports the other's Python.
# Fails open (never raises) — an unreachable/missing sister table must
# never block this service's own restriction logic.
_SISTER_TABLE = "scalp_intraday_restricted"


def _sister_restricted_symbols(db: Session) -> set:
    try:
        rows = db.execute(text(f"SELECT symbol FROM {_SISTER_TABLE}")).fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        logger.info(
            "intraday_eligibility: sister-table (%s) bulk fetch unavailable (%s) — "
            "continuing with this service's own list only", _SISTER_TABLE, e,
        )
        return set()


def _sister_has_restriction(db: Session, sym: str) -> bool:
    try:
        row = db.execute(
            text(f"SELECT 1 FROM {_SISTER_TABLE} WHERE symbol = :sym"), {"sym": sym}
        ).first()
        return row is not None
    except Exception as e:
        logger.info(
            "intraday_eligibility: sister-table (%s) lookup unavailable for %s (%s)",
            _SISTER_TABLE, sym, e,
        )
        return False


def _record_sister_restriction(db: Session, sym: str, detail: Optional[str]) -> None:
    """Best-effort mirror of a newly-learned restriction into the sister
    service's table, so position-stocks-service benefits immediately from
    a rejection real-trade-service just observed, without waiting for it
    to independently hit the same rejection itself."""
    try:
        exists = db.execute(
            text(f"SELECT 1 FROM {_SISTER_TABLE} WHERE symbol = :sym"), {"sym": sym}
        ).first()
        now = _now()
        if exists:
            db.execute(
                text(
                    f"UPDATE {_SISTER_TABLE} SET last_detected_at = :now, "
                    f"hit_count = hit_count + 1"
                    + (", last_detail = :detail" if detail else "")
                    + " WHERE symbol = :sym"
                ),
                {"now": now, "sym": sym, **({"detail": detail[:255]} if detail else {})},
            )
        else:
            db.execute(
                text(
                    f"INSERT INTO {_SISTER_TABLE} "
                    f"(symbol, first_detected_at, last_detected_at, hit_count, last_detail) "
                    f"VALUES (:sym, :now, :now, 1, :detail)"
                ),
                {"sym": sym, "now": now, "detail": (detail[:255] if detail else None)},
            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.info(
            "intraday_eligibility: could not mirror restriction for %s into sister "
            "table (%s) — non-fatal, this service's own record still saved", sym, e,
        )


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
    _record_sister_restriction(db, sym, detail)


def get_restricted_symbols(db: Session) -> set:
    """Bulk fetch — used by candidate_engine to filter a whole cycle's
    candidate batch in one query instead of one lookup per symbol. Now
    also unions in position-stocks-service's learned list (see module
    note above) so a restriction either service has seen protects both."""
    try:
        rows = db.query(models.IntradayRestrictedSecurity.symbol).all()
        own = {r[0] for r in rows}
    except Exception as e:
        logger.warning("intraday_eligibility: bulk fetch failed (%s) — treating as empty", e)
        own = set()
    return own | _sister_restricted_symbols(db)


def is_restricted(db: Session, symbol: str) -> bool:
    """Single-symbol check — used by manual_engine.py for an explicit
    INTRADAY/MIS ticket, where fetching the whole set would be overkill.
    Also checks position-stocks-service's table (see module note above)."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return False
    try:
        if db.query(models.IntradayRestrictedSecurity).filter(
            models.IntradayRestrictedSecurity.symbol == sym
        ).first() is not None:
            return True
    except Exception as e:
        logger.warning("intraday_eligibility: lookup failed for %s (%s) — treating as not restricted", sym, e)
    return _sister_has_restriction(db, sym)
