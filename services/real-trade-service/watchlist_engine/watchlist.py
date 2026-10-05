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
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import models
from watchlist_engine.decay import profile_for, expiry_from
from watchlist_engine.sources import fetch_watchlist_candidates
from market_feed.feed import _clean_sym

logger = logging.getLogger("real-trade-watchlist")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _drop_cooldown_hours() -> float:
    """WATCHLIST_DROP_COOLDOWN_HOURS (default 24). Blank/bad/negative values fall back to 24; 0 disables."""
    raw = (os.getenv("WATCHLIST_DROP_COOLDOWN_HOURS") or "").strip()
    if not raw:
        return 24.0
    try:
        v = float(raw)
    except ValueError:
        return 24.0
    return v if v == v and v >= 0 else 24.0


def _recently_retired_for_drop(db: Session, mode: str, sym: str, ctype: str, hours: float) -> bool:
    """True when this symbol+catalyst was retired by the group158 deep-drop check within `hours`."""
    if hours <= 0:
        return False
    return (
        db.query(models.WatchlistEntry.id)
        .filter(
            models.WatchlistEntry.mode == mode,
            models.WatchlistEntry.symbol.in_([sym, f"{sym}.NS", f"{sym}.BO"]),
            models.WatchlistEntry.catalyst_type == ctype,
            models.WatchlistEntry.status == "expired",
            models.WatchlistEntry.missed_reason.like("adverse:%"),
            models.WatchlistEntry.updated_at >= _now() - timedelta(hours=hours),
        )
        .first()
        is not None
    )


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
    cooldown_h = _drop_cooldown_hours()
    for c in candidates:
        # group159 (item 4): one spelling per stock. Tier 1/2 sources can send "KOTAKBANK.NS" while Tier 3
        # sends "KOTAKBANK"; stored raw they became two rows and two price lookups. Rows written before this
        # change may still carry a suffix, so the duplicate check below matches either spelling.
        sym = _clean_sym(c.get("symbol") or "")
        ctype = c.get("catalyst_type") or "volume_shock"
        if not sym:
            continue
        spellings = [sym, f"{sym}.NS", f"{sym}.BO"]

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
                models.WatchlistEntry.symbol.in_(spellings),
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

        # group158: retired a moment ago for falling far below its catalyst: do not re-add it with a
        # fresh (lower) baseline, which would reset the drop check.
        if _recently_retired_for_drop(db, mode, sym, ctype, cooldown_h):
            continue

        catalyst_price = c.get("catalyst_price")
        if catalyst_price is None:
            catalyst_price = 0.0  # Tier 3 rows: set on first price sight

        row = models.WatchlistEntry(
            mode=mode,
            symbol=sym,
            catalyst_type=ctype,
            catalyst_price=float(catalyst_price),
            catalyst_price_source=c.get("catalyst_price_source"),
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
