"""orders/catalyst_gate.py - a big move with no news behind it (group 336, 2026-10-12).

WHY: a stock already up several percent on the day with no result, order, bulk deal or other catalyst on record is usually a thin, speculative
run (operator-style move, low-float spike) - exactly the kind that reverses without warning. The quality gate already reads the event service's
`has_positive_catalyst` and the bulk-deal flag but only used them as labels on the log row.
RULE: pct_change >= CATALYST_MOVE_PCT (default 4.0) AND the event service answered that there is NO positive catalyst AND no bulk deal ->
  CATALYST_NO_NEWS_ACTION = "size" (default): position size x CATALYST_NO_NEWS_SIZE_MULT (0.6)
                            "block": skip the entry (CATALYST_NONE:...)
                            "off"  : nothing
HONEST LIMIT: the event service does not cover every small cap, so "no catalyst found" is not proof there is none - that is why the default only
SIZES DOWN instead of blocking. An unknown answer (service down / timeout / None) never counts as "no catalyst". Pure, never raises.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

import config

logger = logging.getLogger("position-stocks-catalyst-gate")


def decide(pct_change, has_catalyst: Optional[bool], bulk_deal: bool = False) -> Tuple[Optional[str], float]:
    """(block_reason | None, size_mult). size_mult is 1.0 unless the action is "size" and the rule fires."""
    try:
        action = str(getattr(config, "CATALYST_NO_NEWS_ACTION", "size") or "off").strip().lower()
        if action not in ("size", "block"):
            return None, 1.0
        thr = float(getattr(config, "CATALYST_MOVE_PCT", 4.0) or 0.0)
        move = float(pct_change)
        if thr <= 0 or not math.isfinite(move) or move < thr:
            return None, 1.0
        if has_catalyst is not False or bulk_deal:          # True = catalyst; None = unknown: neither counts as "no news"
            return None, 1.0
        if action == "block":
            return (f"CATALYST_NONE:up {move:.1f}% (>= {thr:.1f}%) with no news / catalyst on record - speculative move, not chasing"), 1.0
        return None, min(1.0, max(0.1, float(getattr(config, "CATALYST_NO_NEWS_SIZE_MULT", 0.6))))
    except Exception as e:  # noqa: BLE001
        logger.debug("catalyst gate failed (no opinion): %s", e)
        return None, 1.0
