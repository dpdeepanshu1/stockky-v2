"""
screening/intraday_eligibility.py — learned intraday-eligibility filter.

MIRRORS real-trade-service/intraday_eligibility.py exactly in design
(duplicated per this service's isolation contract — not imported from there).

WHY: screenshots from 2026-09-15 confirmed that SELL rejections with
"Order rejected as this stock is not allowed to be traded in Intraday."
(Medi Caps, confirmed across multiple sessions) ARE firing from this
service's eod_squareoff.py — meaning this service BUYs a stock that Dhan
then refuses to let us SELL intraday. Real-trade-service already learned
this lesson (2026-09-10, session21e) and built its own filter; this service
had no equivalent, so the same known-restricted symbols kept getting picked
as candidates every cycle.

DESIGN: no static list — Dhan's scrip master has no eligibility flag. The
table is seeded purely from live SELL rejections this service actually
observes (eod_squareoff.py and future exit paths), then consulted in
_run_cycle() / scan-candidate filtering before any new BUY so tomorrow's
cycle doesn't repeat today's lesson.

FAIL-SAFE: get_restricted_symbols() returns an empty set on any DB error —
the caller treats that as "no known restrictions" and proceeds normally
(buy-side gate), rather than blocking ALL candidates because the table was
unreachable. record_restriction() is always wrapped in try/except by its
callers — a recording failure must never block the actual exit flow.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import ScalpIntradayRestrictedSecurity

logger = logging.getLogger("position-stocks-intraday-eligibility")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 2026-09-17 fix: mirrors real-trade-service/intraday_eligibility.py's
# equivalent addition — see that module's docstring for the full
# reasoning. Both services share one Oracle schema, so
# real-trade-service's `trade_intraday_restricted` table is reachable
# from here via raw SQL, without importing that service's Python (the two
# are separate codebases/containers). Fails open — never raises, never
# blocks this service's own restriction logic.
_SISTER_TABLE = "trade_intraday_restricted"


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
    """Best-effort mirror of a newly-learned restriction into
    real-trade-service's table, so it benefits immediately from a
    rejection this service just observed."""
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
    """Upsert a restriction record.

    Call this whenever execution/dhan_client.is_security_intraday_restricted_error()
    fires on a SELL rejection (currently: orders/eod_squareoff.py).

    Best-effort — callers MUST wrap in try/except and just log on failure
    so a DB hiccup here can never block the exit-handling flow it's called
    from.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return
    row = db.query(ScalpIntradayRestrictedSecurity).filter(
        ScalpIntradayRestrictedSecurity.symbol == sym
    ).first()
    if row:
        row.last_detected_at = _now()
        row.hit_count = (row.hit_count or 0) + 1
        if detail:
            row.last_detail = detail[:255]
    else:
        db.add(ScalpIntradayRestrictedSecurity(
            symbol=sym,
            first_detected_at=_now(),
            last_detected_at=_now(),
            hit_count=1,
            last_detail=(detail[:255] if detail else None),
        ))
    db.commit()
    logger.info(
        "intraday_eligibility: recorded restriction for %s (%s)", sym, detail
    )
    _record_sister_restriction(db, sym, detail)


def get_restricted_symbols(db: Session) -> set:
    """Bulk fetch for the cycle screener — one query to filter the whole
    candidate batch rather than one lookup per symbol. Returns an empty
    set on any DB error (fail-open: an unreachable table must never block
    all candidates). Now also unions in real-trade-service's learned list
    (see module note above)."""
    try:
        rows = db.query(ScalpIntradayRestrictedSecurity.symbol).all()
        own = {r[0] for r in rows}
    except Exception as e:
        logger.warning(
            "intraday_eligibility: bulk fetch failed (%s) — treating as empty", e
        )
        own = set()
    return own | _sister_restricted_symbols(db)


def is_restricted(db: Session, symbol: str) -> bool:
    """Single-symbol check (used for one-off lookups, not the cycle batch).
    Also checks real-trade-service's table (see module note above)."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return False
    try:
        if (
            db.query(ScalpIntradayRestrictedSecurity)
            .filter(ScalpIntradayRestrictedSecurity.symbol == sym)
            .first()
        ) is not None:
            return True
    except Exception as e:
        logger.warning(
            "intraday_eligibility: lookup failed for %s (%s) — treating as not restricted",
            sym, e,
        )
    return _sister_has_restriction(db, sym)
