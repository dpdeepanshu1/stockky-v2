"""orders/gap_event.py - E273 round 11: the event-aware gap rule for LONG entries (plan: "Why the opening gate and the gap block stop cancelling each other").

A gap past its limit used to be refused twice (OPENING_GATE:GAP_UP above +3 %, PREOPEN_GAP_UP above its own ATR limit / 6 %) until 10:00, whatever the news. Now:
  * a gap UP with a matching POSITIVE typed event (strength >= GAP_EVENT_MIN_STRENGTH) is explained: the refusal is waived, but ONLY if S1_CATALYST_UP (the closed-bar
    trigger: price holds the pre-open price and a completed bar closes above the opening-range high) matches afterwards, else the original gap reason refuses
    (GAP_UP_NOT_CONFIRMED). A very large gap (at or above PREOPEN_GAP_UP_BLOCK_PCT, the stock's fixed outer limit) keeps a 1.0 size penalty (half size).
  * a gap DOWN past its limit against a positive event (good news, market says no) is skipped: GAP_AGAINST_EVENT.
  * no event / unknown polarity / weak event: unchanged (the gap penalty or the opening-gate refusal).
Pure helpers, fail to "no event" (the old behaviour) on anything unreadable. GAP_EVENT_RULE_ENABLED=0 turns the rule off. Never raises.
"""
from __future__ import annotations

from typing import Any, List, Optional

import config

S1_ID = "S1_CATALYST_UP"
LARGE_KEY = "gap_large"


def enabled() -> bool:
    return bool(getattr(config, "GAP_EVENT_RULE_ENABLED", True))


def positive_event(quality: Any) -> bool:
    """True when the quality check carries a positive typed event of at least GAP_EVENT_MIN_STRENGTH (and the rule is on)."""
    try:
        if not enabled() or quality is None:
            return False
        pol, strength = getattr(quality, "event_polarity", None), getattr(quality, "event_strength", None)
        return pol == 1 and float(strength or 0.0) >= float(getattr(config, "GAP_EVENT_MIN_STRENGTH", 0.3))
    except (TypeError, ValueError):
        return False


def gap_kind(reason: Optional[str]) -> Optional[str]:
    """"up" / "down" for a pre-open or opening-gate GAP refusal / penalty reason, else None."""
    r = str(reason or "")
    if r.startswith(("OPENING_GATE:GAP_UP", "PREOPEN_GAP_UP")):
        return "up"
    if r.startswith(("OPENING_GATE:GAP_DOWN", "PREOPEN_GAP_DOWN")):
        return "down"
    return None


def against_reason(reason: str, quality: Any) -> str:
    """The skip reason for a gap down against a positive event."""
    et = getattr(quality, "event_type", None) or "event"
    return f"GAP_AGAINST_EVENT:positive {et} (strength {float(getattr(quality, 'event_strength', 0.0) or 0.0):.2f}) but the market gaps down - {reason}"


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
