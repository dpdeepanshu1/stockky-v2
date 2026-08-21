"""
Event depth upgrade: stronger Results / Bulk / Insider detection + clean summary
+ a proper nature-based Event Score (0-100, 50=neutral) with a transparent
breakdown so the frontend "event box" can show not just a summary sentence
but a real score and what drove it.

Use from event/main.py:
    from event_depth import enrich_events, summarize_event_block, compute_event_score
"""

from __future__ import annotations

import logging
import math
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


def _age_days(date_str: Any, now: Optional[datetime] = None) -> Optional[float]:
    if not date_str:
        return None
    try:
        now = now or datetime.utcnow()
        d = datetime.fromisoformat(str(date_str)[:10])
        return (now - d).days
    except Exception:
        return None


def _decay(age_days: Optional[float], half_life: float = 10.0) -> float:
    """Exponential recency decay. Unknown age = full weight (don't punish
    sources that don't carry a date); floors at 0.15 so very old events
    still register faintly rather than vanishing outright."""
    if age_days is None or age_days < 0:
        return 1.0
    try:
        return max(0.15, min(1.0, math.exp(-math.log(2) * age_days / max(0.5, half_life))))
    except Exception:
        return 1.0


def compute_event_score(events: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Proper nature-based Event Score: every event *type* gets an impact sized
    to what actually happened (earnings beat vs miss, bonus/buyback vs a
    dilutive rights issue, insider buying vs selling, rating upgrade vs
    downgrade, which side a bulk/block deal was on, regulatory action),
    not a flat "an event happened" bump. Recency-decayed (half-life varies
    by event type — a bulk deal fades faster than an earnings beat), then
    mapped onto the same 0-100 / 50=neutral scale as the other pillars
    (technical_score, fundamental_score, news_score) so it can be weighted
    directly in horizon scoring instead of only ever being an ad-hoc bonus.
    """
    events = events or {}
    now = now or datetime.utcnow()
    breakdown: List[Dict[str, Any]] = []
    raw = 0.0

    def _add(label: str, impact: float, age_days: Optional[float] = None,
             half_life: float = 10.0, meta: Optional[dict] = None) -> None:
        nonlocal raw
        w = _decay(age_days, half_life)
        decayed = impact * w
        raw += decayed
        row = {
            "type": label,
            "base_impact": round(impact, 1),
            "age_days": age_days,
            "decayed_impact": round(decayed, 1),
        }
        if meta:
            row.update(meta)
        breakdown.append(row)

    # 1) Earnings proximity (pre-results drift vs immediate blackout risk)
    earnings_days_out = None
    next_earn = events.get("next_earnings_date")
    if next_earn:
        try:
            edt = datetime.fromisoformat(str(next_earn)[:10])
            days_out = (edt - now).days
            earnings_days_out = days_out
            if 0 <= days_out <= 3:
                _add("earnings_imminent_risk", -5, age_days=0, meta={"days_out": days_out})
            elif 0 < days_out <= 10:
                _add("pre_results_momentum", 8, age_days=0, meta={"days_out": days_out})
        except Exception:
            pass

    # 2) Earnings surprise — the single strongest short-term catalyst
    es = events.get("earnings_surprise") or {}
    try:
        pct = float(es.get("surprise_pct")) if es.get("surprise_pct") is not None else None
    except (TypeError, ValueError):
        pct = None
    if pct is not None:
        age = _age_days(es.get("date") or es.get("report_date"), now)
        if pct > 5:
            _add("earnings_strong_beat", 25, age, half_life=15, meta={"surprise_pct": pct})
        elif pct > 0:
            _add("earnings_mild_beat", 12, age, half_life=15, meta={"surprise_pct": pct})
        elif pct < -5:
            _add("earnings_miss", -22, age, half_life=15, meta={"surprise_pct": pct})
        elif pct < 0:
            _add("earnings_mild_miss", -10, age, half_life=15, meta={"surprise_pct": pct})

    # 3) Corporate actions — direction matters (accretive vs dilutive)
    ca = events.get("corporate_actions") or events.get("recent_corporate_actions") or []
    for item in (ca if isinstance(ca, list) else [])[:5]:
        kind = str(item.get("type") or item.get("action") or "").lower()
        age = _age_days(item.get("date"), now)
        if any(k in kind for k in ("bonus", "split")):
            _add("bonus_or_split", 14, age, half_life=20, meta={"detail": kind})
        elif "buyback" in kind:
            _add("buyback", 16, age, half_life=20, meta={"detail": kind})
        elif "rights issue" in kind or "rights_issue" in kind:
            _add("rights_issue_dilutive", -6, age, half_life=20, meta={"detail": kind})
        elif any(k in kind for k in ("merger", "acquisition", "demerger", "takeover")):
            _add("ma_activity", 10, age, half_life=20, meta={"detail": kind})
        elif "delisting" in kind:
            _add("delisting_risk", -20, age, half_life=20, meta={"detail": kind})

    # 4) Dividend
    div = events.get("last_dividend") or {}
    if div and div.get("amount"):
        age = _age_days(div.get("date"), now)
        _add("dividend_announced", 6, age, half_life=15)

    # 5) Analyst rating actions
    for action in (events.get("recent_analyst_actions") or [])[:3]:
        act = str(action.get("action", "")).lower()
        grade = str(action.get("to_grade", "")).lower()
        age = _age_days(action.get("date"), now)
        if act in ("upgrade", "upgraded") or grade in ("buy", "strong buy", "outperform", "overweight"):
            _add("analyst_upgrade", 10, age, half_life=10, meta={"firm": action.get("firm")})
        elif act in ("downgrade", "downgraded") or grade in ("sell", "underperform", "underweight"):
            _add("analyst_downgrade", -10, age, half_life=10, meta={"firm": action.get("firm")})

    # 6) Insider / promoter transactions — size matters
    for txn in (events.get("recent_insider_transactions") or events.get("insider_transactions") or [])[:5]:
        kind = str(txn.get("transaction") or txn.get("side") or "").lower()
        shares = txn.get("shares") or 0
        age = _age_days(txn.get("date"), now)
        if "buy" in kind or "purchase" in kind or kind == "p":
            impact = 12 if shares and shares > 1000 else 7
            _add("insider_buying", impact, age, half_life=15, meta={"shares": shares})
        elif "sell" in kind or "sale" in kind or kind == "s":
            impact = -9 if shares and shares > 1000 else -5
            _add("insider_selling", impact, age, half_life=15, meta={"shares": shares})

    # 7) Bulk/block deals — direction, fast decay (short-lived signal)
    bulk = events.get("bulk_deals") or events.get("block_deals") or []
    for d in (bulk if isinstance(bulk, list) else [])[:5]:
        side = str(d.get("buy_sell") or d.get("side") or d.get("transaction") or "").lower()
        age = _age_days(d.get("date"), now)
        if "buy" in side or side in ("b", "purchase"):
            _add("bulk_block_buy", 9, age, half_life=7)
        elif "sell" in side or side in ("s",):
            _add("bulk_block_sell", -8, age, half_life=7)

    # 8) FII/DII net flow
    fii = events.get("fii_dii_net_flow") or {}
    net = fii.get("net")
    if net is not None:
        try:
            net = float(net)
            if net > 0:
                _add("fii_dii_inflow", 4, age_days=0)
            elif net < 0:
                _add("fii_dii_outflow", -4, age_days=0)
        except (TypeError, ValueError):
            pass

    # 9) Regulatory / SEBI action — strongest negative, slow decay
    reg = events.get("regulatory_actions") or events.get("sebi_actions") or []
    for r in (reg if isinstance(reg, list) else [])[:3]:
        age = _age_days(r.get("date"), now)
        _add("regulatory_action", -22, age, half_life=25, meta={"detail": r.get("description")})

    # 10) Board meeting scheduled with nothing else known yet — mild anticipation
    if events.get("board_meeting_date") and not ca:
        _add("board_meeting_upcoming", 3, age_days=0)

    # Diminishing returns once several events stack (avoid a wall of small
    # positives/negatives blowing the score to the rails), then map the
    # signed delta onto 0-100 with 50 = neutral, matching every other pillar.
    magnitude = abs(raw)
    capped = min(magnitude, 40) + max(0.0, magnitude - 40) * 0.3
    capped = math.copysign(capped, raw) if raw else 0.0
    event_score = max(0.0, min(100.0, 50.0 + capped * 1.25))

    risk_flag = any(
        b["type"] in ("earnings_imminent_risk", "regulatory_action", "delisting_risk")
        for b in breakdown
    )

    return {
        "event_score": round(event_score, 1),
        "event_score_raw_delta": round(raw, 1),
        "event_risk": risk_flag,
        "earnings_days_out": earnings_days_out,
        "event_score_breakdown": breakdown,
    }


def enrich_events(events: Dict[str, Any], symbol: str = "") -> Dict[str, Any]:
    """Attach summary + the full nature-based event score/breakdown.
    Keeps the older recent_event_score/has_positive_catalyst fields (0-1
    scale) for existing ML-feature consumers that already read them —
    just derives them from the same, better-grounded pipeline now instead
    of the old flat +0.15/+0.35/etc. scorer."""
    out = dict(events or {})
    out["event_summary"] = summarize_event_block(out, symbol)

    scored = compute_event_score(out)
    out["event_score"] = scored["event_score"]
    out["event_score_breakdown"] = scored["event_score_breakdown"]
    out["event_risk"] = scored["event_risk"]
    if scored.get("earnings_days_out") is not None:
        out["earnings_days_out"] = scored["earnings_days_out"]

    # Backward-compatible 0-1 fields, derived from the same score (50→0.0 mid,
    # 100→1.0, clamped) instead of a separate ad-hoc calculation.
    out["recent_event_score"] = round(max(0.0, min(1.0, (scored["event_score"] - 50.0) / 50.0)), 3)
    out["has_positive_catalyst"] = scored["event_score"] >= 62.0
    return out