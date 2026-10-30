"""screening/gate_prefilter.py - E259: keep names the opening gate will refuse out of the shortlist's SHORTLIST_MAX slots.

WHY: on 30 Oct 09:40-09:46 IST every cycle's shortlist (5 names) was TRENT / ALIVUS / SANGHVIMOV / KOTYARK / BOROLTD ... and every one was refused by
screening/opening_gate.py (gap up +3.2 to +7 % against a +3 % limit, gap down, under the open, weak previous day). The scan score likes gappers, the gate
refuses them by design, so the 5 slots were full of names that could not enter and nothing else was tried.

WHAT: partition(passers) asks opening_gate.reject_reason (the same call orders/entry.py makes, same price) for each (candidate, quality) pair and returns
(kept, held). `held` are the names the gate refuses now, with the reason. Not held: a name whose refusal is only missing data (NO_PREV_DAY_DATA,
NO_DAY_STATS, NO_PRICE, NO_OPEN_OR_PREV_CLOSE, NO_OPENING_RANGE_DATA, NO_ATR_DATA, ERROR) - entry decides those, and the call warms the prev-day cache.
It never lets anything through that the gate would refuse and never changes the gate. Outside the gate window reject_reason is None, so nothing is held.
Never raises: any error keeps every name. SHORTLIST_GATE_PREFILTER=0 turns it off.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import config

logger = logging.getLogger("position-stocks-gate-prefilter")

# a refusal that only says "the data is not here yet" is not a verdict on the stock
_DATA_NOT_READY = ("NO_PREV_DAY_DATA", "NO_DAY_STATS", "NO_PRICE", "NO_OPEN_OR_PREV_CLOSE", "NO_OPENING_RANGE_DATA", "NO_ATR_DATA", "ERROR", "TOO_EARLY")


def is_verdict(reason: Optional[str]) -> bool:
    """True when `reason` (an opening_gate skip string) is a real refusal of the stock, not a data-not-ready hold."""
    if not reason or not str(reason).startswith("OPENING_GATE"):
        return False
    parts = str(reason).split(":")
    code = parts[1] if len(parts) > 1 else ""
    return code not in _DATA_NOT_READY


def partition(passers: Sequence[Tuple[object, object]], now=None) -> Tuple[List[Tuple[object, object]], List[dict]]:
    """passers: [(candidate, quality)]. Returns (kept, held) with held = [{"symbol", "reason"}]. Order of `kept` is unchanged."""
    if not config.SHORTLIST_GATE_PREFILTER or not passers:
        return list(passers), []
    try:
        from screening import opening_gate
        if not opening_gate.is_active(now):
            return list(passers), []
        kept: List[Tuple[object, object]] = []
        held: List[dict] = []
        for pair in passers:
            cand = pair[0]
            try:
                why = opening_gate.reject_reason(cand.symbol, cand.current_ltp, now)
            except Exception:  # noqa: BLE001 - one bad name must not drop the others
                why = None
            if is_verdict(why):
                held.append({"symbol": cand.symbol, "reason": str(why)[:140]})
            else:
                kept.append(pair)
        return kept, held
    except Exception as e:  # noqa: BLE001 - fail open: keep everyone
        logger.debug("gate prefilter skipped (%s)", e)
        return list(passers), []
