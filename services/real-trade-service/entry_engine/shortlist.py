"""entry_engine/shortlist.py - group332: the "best 3 to 5" step inside Gate 6, plus the Market Board nudge that feeds it.

Gate 6 ranks every risk-approved candidate of a cycle by composite score and enters the top ENTRY_MAX_NEW_PER_CYCLE. Missing
before: (1) the Market Board's verdict on the symbol (conviction, rank, zone, chase guard) never reached that ranking, and
(2) only the 20 OLDEST queued candidates reached it at all. This module adds
  * board_nudge(row)  - small CAPPED +/- composite change from the board row (ranking nudge only, never a gate),
  * build(approved)   - the shortlist: the best ENTRY_SHORTLIST_MAX (default 5) approved candidates that clear the composite
                        floor (UC bypasses it), best first; entries are drawn from its first ENTRY_MAX_NEW_PER_CYCLE.
Pure functions, no I/O. ENTRY_SHORTLIST_ENABLED=false / ENTRY_BOARD_NUDGE_ENABLED=false restore the old behaviour."""
from __future__ import annotations

from typing import Dict, List, Optional

import config


def board_nudge(row: Optional[dict]) -> float:
    """Composite points for one board row, capped to +/-ENTRY_BOARD_MAX_NUDGE.
    + bull side, conviction >= 70 and rank <= 5 (the board's own PICK rule) -> up to the cap, scaled by conviction
    + bull side, conviction >= 60                                           -> half of that
    - bear side (the entry engine is long-only)                              -> -cap
    - extended past its order-block zone (chase guard)                       -> -cap/2
    0 no row / no data."""
    try:
        if not row or not getattr(config, "ENTRY_BOARD_NUDGE_ENABLED", True):
            return 0.0
        cap = float(config.ENTRY_BOARD_MAX_NUDGE)
        if cap <= 0:
            return 0.0
        side = row.get("side")
        if side == "bear":
            return -cap
        pts = 0.0
        conv = row.get("conviction")
        conv = float(conv) if conv is not None else None
        rank = row.get("rank")
        if side == "bull" and conv is not None:
            scale = max(0.0, min(1.0, (conv - 50.0) / 50.0))
            if conv >= 70 and rank is not None and int(rank) <= 5:
                pts = cap * scale
            elif conv >= 60:
                pts = cap * 0.5 * scale
        if row.get("extended"):
            pts -= cap / 2.0
        return max(-cap, min(cap, pts))
    except Exception:  # noqa: BLE001 - a nudge must never break a cycle
        return 0.0


def build(approved: List[dict]) -> Dict[str, List[dict]]:
    """approved: staged entries ({"composite_score", "is_upper_circuit", ...}). Returns
    {"shortlist": best ENTRY_SHORTLIST_MAX that clear the floor (UC bypasses), best first,
     "selected": its first ENTRY_MAX_NEW_PER_CYCLE, "skipped": every other approved entry}."""
    ranked = sorted(approved, key=lambda e: e["composite_score"], reverse=True)   # stable: ties keep processing order
    eligible = [e for e in ranked
                if e["composite_score"] >= config.ENTRY_MIN_COMPOSITE_SCORE or e.get("is_upper_circuit")]
    if getattr(config, "ENTRY_SHORTLIST_ENABLED", True):
        shortlist = eligible[: max(1, int(config.ENTRY_SHORTLIST_MAX))]
    else:
        shortlist = eligible[: config.ENTRY_MAX_NEW_PER_CYCLE]
    selected = shortlist[: config.ENTRY_MAX_NEW_PER_CYCLE]
    ids = {id(e) for e in selected}
    return {"shortlist": shortlist, "selected": selected, "skipped": [e for e in ranked if id(e) not in ids]}
