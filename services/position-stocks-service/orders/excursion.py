"""orders/excursion.py — record each open trade's highest / lowest price (2026-10-02).

Why: several scalps ended near zero (stop moved to entry after a rise, price
fell back). Whether the 3-4% target is simply too far for this style can only
be answered with the peak each trade actually reached, which was never stored.
This keeps ScalpPosition.max_price_seen / min_price_seen current while a
position is OPEN, so max favourable / adverse excursion can be read straight
off /positions and /trades/history.

Runs from main.py's fast-reconcile loop. Read-only with respect to Dhan (no
orders), independent of the breakeven toggle, fail-open per position.
Ticks are taken from the in-memory buffer since the position opened, so a
spike between two polls is still captured (as long as it is in the buffer).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from models import ScalpPosition
from tz_utils import as_aware

logger = logging.getLogger("position-stocks-excursion")


def _extremes_since(symbol: str, since_ts: float) -> Optional[Tuple[float, float]]:
    """(high, low) of buffered ticks at/after since_ts, or None if none."""
    from feed import ws_client
    hi = lo = None
    for ts, px in ws_client.get_tick_buffer(symbol):
        if px is None or px <= 0 or ts < since_ts:
            continue
        hi = px if hi is None or px > hi else hi
        lo = px if lo is None or px < lo else lo
    return None if hi is None else (hi, lo)


def run_excursion_tracking(db: Session) -> int:
    """Update max/min price seen for every OPEN position. Returns the number of
    positions whose stored high/low changed."""
    changed = 0
    for pos in db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all():
        try:
            if not pos.entry_price or pos.entry_price <= 0:
                continue
            opened_ts = as_aware(pos.opened_at).timestamp() if pos.opened_at else 0.0
            ext = _extremes_since(pos.symbol, opened_ts)
            if ext is None:
                continue
            hi, lo = ext
            # the entry fill itself is a price this trade has been at
            hi = max(hi, pos.entry_price)
            lo = min(lo, pos.entry_price)
            dirty = False
            if pos.max_price_seen is None or hi > pos.max_price_seen:
                pos.max_price_seen = hi
                dirty = True
            if pos.min_price_seen is None or lo < pos.min_price_seen:
                pos.min_price_seen = lo
                dirty = True
            if dirty:
                db.commit()
                changed += 1
        except Exception as e:
            db.rollback()
            logger.debug("excursion: %s (id=%s) skipped: %s", pos.symbol, pos.id, e)
    return changed


def excursion_pcts(pos) -> dict:
    """Max gain / max drawdown % vs entry for API output (None when unknown)."""
    e, hi, lo = pos.entry_price, pos.max_price_seen, pos.min_price_seen
    if not e or e <= 0 or hi is None or lo is None:
        return {"max_gain_pct": None, "max_drawdown_pct": None}
    return {
        "max_gain_pct": round((hi - e) / e * 100.0, 2),
        "max_drawdown_pct": round((lo - e) / e * 100.0, 2),
    }
