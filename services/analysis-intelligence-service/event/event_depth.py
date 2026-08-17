"""
Event depth upgrade: stronger Results / Bulk / Insider detection + clean summary.

Use from event/main.py:
    from event_depth import enrich_events, summarize_event_block
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("event_depth")

EVENT_KEYWORDS = {
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
    t = (text or "").lower()
    tags = []
    for tag, words in EVENT_KEYWORDS.items():
        if any(w in t for w in words):
            tags.append(tag)
    return tags


def summarize_event_block(events: Dict[str, Any], symbol: str = "") -> str:
    """Human-readable summary for the Event section."""
    parts: List[str] = []
    sym = (symbol or events.get("symbol") or "").upper()

    next_earn = events.get("next_earnings_date")
    if next_earn:
        parts.append(f"Next results/earnings date: {next_earn}")

    es = events.get("earnings_surprise") or {}
    if isinstance(es, dict) and es.get("surprise_pct") is not None:
        try:
            pct = float(es["surprise_pct"])
            direction = "beat" if pct > 0 else "missed"
            parts.append(f"Latest earnings {direction} estimates by {abs(pct):.1f}%")
        except Exception:
            pass

    ins = events.get("recent_insider_transactions") or events.get("insider_transactions") or []
    if ins:
        buys = [x for x in ins if str(x.get("side") or x.get("transaction") or "").lower() in ("buy", "purchase", "p")]
        sells = [x for x in ins if str(x.get("side") or x.get("transaction") or "").lower() in ("sell", "sale", "s")]
        if buys:
            parts.append(f"Recent insider/promoter buying ({len(buys)} txn)")
        if sells:
            parts.append(f"Recent insider/promoter selling ({len(sells)} txn)")
        if not buys and not sells:
            parts.append(f"{len(ins)} recent insider/promoter transaction(s)")

    bulk = events.get("bulk_deals") or events.get("block_deals") or []
    if bulk:
        parts.append(f"{len(bulk)} bulk/block deal(s) noted")

    upcoming = events.get("upcoming") or []
    recent = events.get("recent") or []
    if upcoming:
        parts.append(f"{len(upcoming)} upcoming event(s)")
    if recent and not parts:
        parts.append(f"{len(recent)} recent market event(s)")

    if not parts:
        return f"No major results, bulk, or insider events detected for {sym or 'this stock'} recently."

    prefix = f"{sym}: " if sym else ""
    return prefix + ". ".join(parts) + "."


def enrich_events(events: Dict[str, Any], symbol: str = "") -> Dict[str, Any]:
    """Attach summary + simple scores for results/bulk/insider."""
    out = dict(events or {})
    out["event_summary"] = summarize_event_block(out, symbol)

    score = 0.0
    if out.get("next_earnings_date"):
        score += 0.15
    es = out.get("earnings_surprise") or {}
    try:
        if float(es.get("surprise_pct") or 0) > 0:
            score += 0.35
        elif float(es.get("surprise_pct") or 0) < 0:
            score -= 0.2
    except Exception:
        pass
    ins = out.get("recent_insider_transactions") or out.get("insider_transactions") or []
    buys = [x for x in ins if "buy" in str(x.get("side") or x.get("transaction") or "").lower()]
    if buys:
        score += 0.35
    bulk = out.get("bulk_deals") or []
    if bulk:
        score += 0.25
    out["recent_event_score"] = round(max(0.0, min(1.0, score)), 3)
    out["has_positive_catalyst"] = score >= 0.35
    return out
