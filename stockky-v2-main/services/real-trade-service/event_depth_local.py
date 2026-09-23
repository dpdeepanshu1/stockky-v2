"""
event_depth_local.py — Short-Term Trading Upgrade (2026-09-02)

Thin local copy of the classify_text keyword matcher from
analysis-intelligence-service/event/event_depth.py.

Purpose: watchlist_engine/sources.py's Tier 2 fallback must classify
raw event headlines without importing across service boundaries — a
cross-service import would defeat the durability goal (if
analysis-intelligence-service's process/deploy is unhealthy, importing
its module would fail too, not just its HTTP endpoint).

This file contains ONLY the keyword matcher (~15 lines, zero external
dependencies), copied verbatim from EVENT_KEYWORDS / classify_text in
analysis-intelligence-service/event/event_depth.py. If that source is
ever updated, update this copy to match.
"""
from __future__ import annotations

from typing import List

EVENT_KEYWORDS: dict[str, list[str]] = {
    "results": [
        "result", "results", "earnings", "q1", "q2", "q3", "q4", "quarterly",
        "annual report", "profit after tax", "pat", "revenue", "top line",
        "financial results", "earnings release", "quarterly numbers", "net profit",
    ],
    "bulk_block": [
        "bulk deal", "block deal", "bulk buys", "block trade", "large deal",
        "institutional buy", "institutional sell", "bulk purchase", "block buy",
    ],
    "insider": [
        "insider", "promoter buying", "promoter selling", "promoter stake",
        "insider trading", "management buy", "key personnel", "stake increase",
        "promoter holding", "insider buy", "insider purchase",
    ],
    "board": [
        "board meeting", "board approves", "dividend", "bonus", "split",
        "buyback", "agm", "egm", "rights issue",
    ],
}


def classify_text(text: str) -> List[str]:
    """Return a list of matching catalyst-type tags for the given headline."""
    t = (text or "").lower()
    tags: List[str] = []
    for tag, words in EVENT_KEYWORDS.items():
        if any(w in t for w in words):
            tags.append(tag)
    return tags
