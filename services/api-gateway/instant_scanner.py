"""
Instant score composite for market scan (lite + full-stream fallback).

Reads pre-calculated Neon data-feed rows (canonical stockky:data_feed:sym: and
alias feed:) + optional live tick, and produces Technical / Fundamental /
combined scores, decision, targets — without calling downstream microservices.

Used by:
  - _lite_evaluate_from_feed (main.py)
  - /scan/stream when lite=true
  - full stream fallback when decision-engine times out / is cold
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("instant-scanner")


def _f(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _metrics(feed: dict) -> dict:
    m = feed.get("metrics") if isinstance(feed.get("metrics"), dict) else {}
    return m if isinstance(m, dict) else {}


def _extract_price(sym: str, feed: dict, tick: Optional[dict]) -> float:
    try:
        from price_resolver import extract_safe_price
        return float(extract_safe_price(sym, tick=tick or {}, feed=feed, decision=None) or 0.0)
    except Exception:
        for src in (tick or {}, feed):
            for k in ("price", "cmp", "last_price", "ltp", "close", "current_price", "prev_close"):
                px = _f(src.get(k))
                if px > 0:
                    return px
        return 0.0


def compute_technical_score(feed: dict, price: float, prev_close: float) -> int:
    """0–100 technical score from cached indicators + price action."""
    m = _metrics(feed)
    rsi = _f(feed.get("rsi", m.get("rsi")), 50.0)
    macd_hist = _f(feed.get("macd_hist", m.get("macd_hist")), 0.0)
    ema20 = _f(feed.get("ema20", m.get("ema20")), prev_close if prev_close > 0 else price)
    # Prefer explicit technical_score from feed when present and sensible
    stored = feed.get("technical_score")
    if stored is not None:
        try:
            ts = int(float(stored))
            if 0 < ts <= 100:
                # Blend lightly with live momentum so score is not stale-only
                bonus = 0
                if price > 0 and ema20 > 0 and price > ema20:
                    bonus += 5
                if price > 0 and prev_close > 0 and price >= prev_close:
                    bonus += 3
                return max(5, min(95, ts + bonus))
        except (TypeError, ValueError):
            pass

    score = 50
    if price > 0 and ema20 > 0 and price > ema20:
        score += 20
    elif price > 0 and ema20 > 0 and price < ema20 * 0.98:
        score -= 10
    if 45 <= rsi <= 65:
        score += 15
    elif 35 <= rsi < 45 or 65 < rsi <= 72:
        score += 5
    elif rsi < 30:
        score += 8  # oversold bounce potential
    elif rsi > 75:
        score -= 10
    if macd_hist > 0:
        score += 15
    elif macd_hist < 0:
        score -= 5
    # Momentum from change
    if prev_close > 0 and price > 0:
        chg = ((price - prev_close) / prev_close) * 100.0
        if chg >= 1.5:
            score += 8
        elif chg <= -2.0:
            score -= 8
    return max(5, min(95, int(round(score))))


def compute_fundamental_score(feed: dict) -> int:
    """0–100 fundamental score from Neon feed metrics / stored score."""
    stored = feed.get("fundamental_score")
    if stored is not None:
        try:
            fs = int(float(stored))
            if 0 < fs <= 100:
                return max(5, min(95, fs))
        except (TypeError, ValueError):
            pass

    m = _metrics(feed)
    pe = _f(feed.get("pe_ratio", m.get("pe_ratio", m.get("trailingPE"))), 20.0)
    roce = _f(feed.get("roce", m.get("roce", m.get("returnOnCapitalEmployed"))), 15.0)
    roe = _f(feed.get("roe", m.get("roe", m.get("returnOnEquity"))), 12.0)
    quality = _f(feed.get("quality_score"), 0.0)
    multi_q = _f(feed.get("multi_quarter_score"), 0.0)

    score = 50
    if 10 <= pe <= 35:
        score += 20
    elif 5 <= pe < 10 or 35 < pe <= 50:
        score += 8
    elif pe > 60:
        score -= 10
    if roce >= 18:
        score += 15
    elif roce >= 12:
        score += 8
    if roe >= 15:
        score += 10
    elif roe >= 10:
        score += 5
    if quality >= 60:
        score += 5
    if multi_q >= 60:
        score += 5
    return max(5, min(95, int(round(score))))


def compute_instant_scores(
    sym: str,
    feed: Optional[dict] = None,
    tick: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Sub-ms score composite from Neon feed + optional live tick.
    Returns a Decision-shaped dict the FE already understands.
    """
    base = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
    feed = feed if isinstance(feed, dict) else {}
    tick = tick if isinstance(tick, dict) else {}

    price = _extract_price(base, feed, tick)
    prev_close = 0.0
    for k in ("prev_close", "close", "price"):
        prev_close = _f(feed.get(k))
        if prev_close > 0:
            break
    if prev_close <= 0:
        prev_close = price if price > 0 else 1.0

    change_pct = 0.0
    if price > 0 and prev_close > 0:
        change_pct = round(((price - prev_close) / prev_close) * 100.0, 2)

    tech = compute_technical_score(feed, price, prev_close)
    fund = compute_fundamental_score(feed)

    # Composite conviction / combined
    momentum_boost = min(abs(change_pct) * 4.0, 12.0) if change_pct > 0 else max(change_pct * 2.0, -10.0)
    combined = int(round(tech * 0.50 + fund * 0.35 + (50 + momentum_boost) * 0.15))
    combined = max(10, min(95, combined))

    # Prefer stored decision when present and still sensible
    decision = feed.get("decision")
    if not decision:
        if combined >= 72 and change_pct >= 0.4:
            decision = "BUY NOW"
        elif combined >= 58 or (combined >= 52 and change_pct >= 0.8):
            decision = "PREPARE TO BUY"
        elif change_pct <= -2.5 or combined <= 35:
            decision = "SELL" if change_pct <= -3.5 else "DO NOT BUY"
        else:
            decision = "HOLD"

    confidence = "High" if combined >= 75 else ("Medium" if combined >= 55 else "Low")

    target = round(price * 1.06, 2) if price > 0 else None
    stop = round(price * 0.97, 2) if price > 0 else None
    entry_low = round(price * 0.995, 2) if price > 0 else None
    entry_high = round(price * 1.008, 2) if price > 0 else None

    has_feed = bool(feed) and (
        feed.get("fundamental_score") is not None
        or feed.get("technical_score") is not None
        or feed.get("metrics")
        or feed.get("combined_score") is not None
        or feed.get("rsi") is not None
        or feed.get("prev_close") is not None
    )
    # Synthetic momentum from previous candle slice when feed is sparse
    if not has_feed and price > 0 and prev_close > 0 and abs(change_pct) < 0.01:
        # Use tiny synthetic noise-free estimate from price alone (flat session)
        change_pct = 0.0
    sparse = not has_feed and price <= 0
    if sparse:
        decision = "HOLD"
        confidence = "Low"
        combined = 40
        tech = 40
        fund = 40
        status = "SYNCING"
    else:
        status = "READY" if (has_feed or price > 0) else "SYNCING"

    out: Dict[str, Any] = {
        "symbol": base,
        "decision": decision,
        "confidence": confidence,
        "combined_score": combined,
        "technical_score": tech,
        "fundamental_score": fund,
        "conviction": combined,
        "change_pct": change_pct,
        "close": price if price > 0 else None,
        "price": price if price > 0 else None,
        "cmp": price if price > 0 else None,
        "current_price": price if price > 0 else None,
        "ltp": price if price > 0 else None,
        "last_price": price if price > 0 else None,
        "prev_close": round(prev_close, 2) if prev_close > 0 else None,
        "target": target,
        "stop_loss": stop,
        "entry_range": {"low": entry_low, "high": entry_high} if price > 0 else None,
        "holding_period": "3-7 Days",
        "sector": feed.get("sector"),
        "industry": feed.get("industry"),
        "valuation": feed.get("valuation"),
        "fundamental_metrics": feed.get("metrics"),
        "news_score": feed.get("news_score"),
        "event_risk": feed.get("event_risk"),
        "from_data_feed": has_feed,
        "data_insufficient": sparse or (not has_feed and price <= 0),
        "lite_fastpath": True,
        "instant_scanner": True,
        "status": status,
        "reasons": {
            "technical": [
                f"Tech score {tech}/100 from Neon indicators + price vs EMA/momentum",
            ],
            "fundamental": [
                f"Fund score {fund}/100 from Neon quarterly / valuation metrics",
            ],
            "lite": [
                "Instant scanner: Neon data-feed + live quote (no downstream HTTP)",
            ],
        },
        "natural_language_summary": (
            f"{base}: {'SYNCING — awaiting feed/quote' if status == 'SYNCING' else 'instant'} — "
            f"{decision} · tech {tech} · fund {fund} · combined {combined} · Δ {change_pct:+.2f}%"
        ),
    }

    try:
        from price_resolver import apply_price_aliases
        if price > 0:
            out = apply_price_aliases(out, price)
    except Exception:
        pass

    return out
