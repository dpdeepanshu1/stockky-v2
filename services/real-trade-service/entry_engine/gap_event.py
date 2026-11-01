"""entry_engine/gap_event.py - E273 round 20: the event-aware gap-up rule for Real Trade LONG entries (Position Stocks has had it since round 11).

A gap UP past its limit is refused twice until the morning settles (OPENING_GATE:EXTENDED above +3 % until 10:00, PREOPEN_GAP_UP above +6 % until 11:00), whatever the news.
Now a gap up with a fresh typed POSITIVE event of at least GAP_EVENT_MIN_STRENGTH is explained: both refusals are waived, but ONLY if S1_CATALYST_UP matches afterwards (price
holds the pre-open price and a completed bar closes above the opening-range high); otherwise the entry is refused as GAP_UP_NOT_CONFIRMED. A very large gap (at or above
PREOPEN_GAP_UP_BLOCK_PCT) keeps a 1.0 soft penalty (half size). No event / unknown polarity / weak event: unchanged. A gap DOWN against such an event (good news, the market says no) is SKIPPED as GAP_AGAINST_EVENT (E273 round 21): the candidate is consumed with that reason instead of being held and retried.
GAP_EVENT_RULE_ENABLED=false turns the rule off. Pure helpers; they fail to "no event" (the old behaviour). Never raises.
"""
from __future__ import annotations

from typing import Any, List, Optional

import config

S1_ID = "S1_CATALYST_UP"
LARGE_KEY = "gap_large"


def enabled() -> bool:
    return bool(getattr(config, "GAP_EVENT_RULE_ENABLED", True))


def explains_gap(event: Any) -> bool:
    """True when `event` ({"polarity": 1, "strength": ...} from intraday_news.positive_event) is positive and at least GAP_EVENT_MIN_STRENGTH strong (and the rule is on)."""
    try:
        if not enabled() or not isinstance(event, dict):
            return False
        return event.get("polarity") == 1 and float(event.get("strength") or 0.0) >= float(getattr(config, "GAP_EVENT_MIN_STRENGTH", 0.3))
    except (TypeError, ValueError):
        return False


def gap_kind(reason: Optional[str]) -> Optional[str]:
    """"up" for an opening-gate EXTENDED / pre-open GAP_UP refusal, "down" for BELOW_PREV_CLOSE / GAP_DOWN, else None."""
    r = str(reason or "")
    if r.startswith(("OPENING_GATE:EXTENDED", "PREOPEN_GAP_UP")):
        return "up"
    if r.startswith(("OPENING_GATE:BELOW_PREV_CLOSE", "PREOPEN_GAP_DOWN")):
        return "down"
    return None


def large_gap_penalty(gap_pct: Optional[float]) -> Optional[float]:
    """1.0 (= half size) when an explained gap-up is at or above PREOPEN_GAP_UP_BLOCK_PCT, else None."""
    try:
        up = float(getattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", 0.0) or 0.0)
        return 1.0 if up > 0 and gap_pct is not None and float(gap_pct) >= up else None
    except (TypeError, ValueError):
        return None


def unconfirmed_reason(waived: List[str], setup_id: Optional[str]) -> Optional[str]:
    """GAP_UP_NOT_CONFIRMED (carrying the first waived gap reason) when a gap-up refusal was waived for an event but S1 did not match, else None."""
    if waived and setup_id != S1_ID:
        return f"GAP_UP_NOT_CONFIRMED:event-backed gap needs the S1 closed-bar trigger (setup {setup_id or 'none'}) - {waived[0]}"
    return None


def against_reason(reason: str, event: Any) -> str:
    """The skip reason for a gap down against a positive event."""
    et = (event or {}).get("type") if isinstance(event, dict) else None
    try:
        st = float((event or {}).get("strength") or 0.0) if isinstance(event, dict) else 0.0
    except (TypeError, ValueError):
        st = 0.0
    return f"GAP_AGAINST_EVENT:positive {et or 'event'} (strength {st:.2f}) but the market gaps down - {reason}"
