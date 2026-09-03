"""
watchlist_engine/watchlist.py — Short-Term Trading Upgrade (2026-09-02)

Watchlist ingestion loop (Stage 1 of the two-stage entry flow):
  - refresh_watchlist:    fetch new catalyst candidates, deduplicate, write
                          trade_watchlist rows.
  - expire_stale_entries: mark rows whose expires_at has passed as "expired".

Called from cycle_runner.py at the top of every cycle, before the existing
candidate/entry/exit passes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import models
from watchlist_engine.decay import profile_for, expiry_from
from watchlist_engine.sources import fetch_watchlist_candidates

logger = logging.getLogger("real-trade-watchlist")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def refresh_watchlist(db: Session, mode: str) -> int:
    """
    Fetch candidates for `mode` via the tiered ladder (sources.py) and
    insert new WatchlistEntry rows for any symbol+catalyst_type combo
    not already actively tracked.

    Returns the number of new rows inserted.
    """
    try:
        candidates = await fetch_watchlist_candidates(db, mode)
    except Exception as exc:
        logger.error("refresh_watchlist[%s]: source fetch failed: %s", mode, exc)
        return 0

    added = 0
    for c in candidates:
        sym = (c.get("symbol") or "").upper()
        ctype = c.get("catalyst_type") or "volume_shock"
        if not sym:
            continue

        profile = profile_for(ctype)
        ts = c.get("catalyst_ts") or _now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # De-duplicate: skip if we already have an active entry for this
        # symbol + catalyst_type combo in this mode, OR a row (any status)
        # for the exact same catalyst event (same catalyst_ts).
        #
        # 2026-09-03 fix: the old check only looked at status="active".
        # For sources with a deterministic catalyst_ts (IPO's catalyst_ts
        # is derived from listing_date — see sources.py — so it's identical
        # on every poll of the same listing), a past-dated listing_date
        # produces expires_at (catalyst_ts + 3×half-life, see expiry_from())
        # that is already in the past at insert time. That row gets marked
        # "expired" on the very next expire_stale_entries() pass, at which
        # point the active-only check no longer finds it — so the next
        # refresh_watchlist cycle re-inserts an identical duplicate row,
        # which immediately expires again. Net effect: one duplicate row
        # per cycle, forever, for any already-past-dated IPO the upstream
        # feed keeps listing. Matching on catalyst_ts (regardless of
        # status) closes that loop: the same real-world event is only ever
        # inserted once. Tier 3 (volume_shock) is unaffected — its
        # catalyst_ts is always freshly set to _now() per cycle (sources.py
        # never supplies one), so this OR-clause practically never matches
        # for it and re-evaluation each cycle still works as designed.
        existing = (
            db.query(models.WatchlistEntry)
            .filter(
                models.WatchlistEntry.mode == mode,
                models.WatchlistEntry.symbol == sym,
                models.WatchlistEntry.catalyst_type == ctype,
            )
            .filter(
                (models.WatchlistEntry.status == "active")
                | (models.WatchlistEntry.catalyst_ts == ts)
            )
            .first()
        )
        if existing:
            continue

        catalyst_price = c.get("catalyst_price")
        if catalyst_price is None:
            catalyst_price = 0.0  # Tier 3 rows: set on first price sight

        row = models.WatchlistEntry(
            mode=mode,
            symbol=sym,
            catalyst_type=ctype,
            catalyst_price=float(catalyst_price),
            catalyst_ts=ts,
            horizon_class=profile["horizon_class"],
            decay_half_life_days=profile["decay_half_life_days"],
            entry_band_pct=profile["entry_band_pct"],
            source_tier=int(c.get("source_tier") or 3),
            conviction_score=c.get("conviction_score"),
            status="active",
            expires_at=expiry_from(ts, ctype),
        )
        db.add(row)
        added += 1

    if added:
        db.commit()
        logger.info("refresh_watchlist[%s]: added %d new entries", mode, added)

    return added


def expire_stale_entries(db: Session, mode: str) -> int:
    """
    Mark active entries whose expires_at is in the past as "expired".
    Returns the number of rows expired.
    """
    now = _now()
    stale = (
        db.query(models.WatchlistEntry)
        .filter(
            models.WatchlistEntry.mode == mode,
            models.WatchlistEntry.status == "active",
            models.WatchlistEntry.expires_at < now,
        )
        .all()
    )
    for row in stale:
        row.status = "expired"
    if stale:
        db.commit()
        logger.info(
            "expire_stale_entries[%s]: expired %d stale entries", mode, len(stale)
        )
    return len(stale)
