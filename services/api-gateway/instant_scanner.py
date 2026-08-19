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
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("instant-scanner")

# Neutral-but-not-zero baselines so sparse feeds still produce differentiated
# scores once a live price is available (avoids every symbol = 40 HOLD).
DEFAULT_STATIC_INDICATORS: Dict[str, Any] = {
    "rsi": 52.0,
    "pe_ratio": 22.0,
    "roce": 15.0,
    "macd_hist": 0.05,
    "ema20": None,
    "market_cap": "MID",
}


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


def resolve_stock_features(
    symbol: str,
    feed_data: dict,
    live_quote: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Normalize feed + live quote into a feature bag with safe defaults.
    Used by compute_instant_scores so missing Neon rows do not force score=40.
    """
    data = feed_data if isinstance(feed_data, dict) else {}
    live = live_quote if isinstance(live_quote, dict) else {}
    m = _metrics(data)
    price = _extract_price(symbol, data, live)
    prev_close = _f(
        data.get("prev_close")
        or m.get("prev_close")
        or live.get("prev_close")
        or data.get("close"),
        0.0,
    )
    if prev_close <= 0 and price > 0:
        prev_close = price

    rsi = _f(data.get("rsi", m.get("rsi")), DEFAULT_STATIC_INDICATORS["rsi"])
    pe = _f(
        data.get("pe_ratio", data.get("pe", m.get("pe_ratio", m.get("pe")))),
        DEFAULT_STATIC_INDICATORS["pe_ratio"],
    )
    roce = _f(data.get("roce", m.get("roce")), DEFAULT_STATIC_INDICATORS["roce"])
    macd_hist = _f(
        data.get("macd_hist", data.get("macd", m.get("macd_hist"))),
        DEFAULT_STATIC_INDICATORS["macd_hist"],
    )
    ema20_raw = data.get("ema20", m.get("ema20", data.get("ema_20")))
    ema20 = _f(ema20_raw, prev_close if prev_close > 0 else price)
    if ema20 <= 0 and price > 0:
        ema20 = price

    return {
        "symbol": symbol,
        "price": float(price),
        "prev_close": float(prev_close),
        "rsi": float(rsi),
        "pe_ratio": float(pe),
        "roce": float(roce),
        "macd_hist": float(macd_hist),
        "ema20": float(ema20) if ema20 > 0 else None,
        "sentiment_score": float(_f(data.get("sentiment_score"), 0.0)),
        "has_explicit_tech": data.get("technical_score") is not None,
        "has_explicit_fund": data.get("fundamental_score") is not None
        or data.get("metrics") is not None
        or data.get("pe_ratio") is not None
        or data.get("roce") is not None,
        "stored_tech": data.get("technical_score"),
        "stored_fund": data.get("fundamental_score"),
        "sector": data.get("sector"),
        "industry": data.get("industry"),
        "valuation": data.get("valuation"),
        "metrics": m,
        "news_score": data.get("news_score"),
        "event_risk": data.get("event_risk"),
        "raw_feed": data,
    }


def compute_technical_score(feed: dict, price: float, prev_close: float) -> int:
    """0–100 technical score from cached indicators + price action."""
    feats = resolve_stock_features("", feed, {"price": price, "prev_close": prev_close})
    rsi = feats["rsi"]
    macd_hist = feats["macd_hist"]
    ema20 = feats["ema20"] or (prev_close if prev_close > 0 else price)

    stored = feed.get("technical_score")
    if stored is not None:
        try:
            ts = int(float(stored))
            if 0 < ts <= 100:
                bonus = 0
                if price > 0 and ema20 > 0 and price > ema20:
                    bonus += 5
                if price > 0 and prev_close > 0 and price >= prev_close:
                    bonus += 3
                return max(5, min(95, ts + bonus))
        except (TypeError, ValueError):
            pass

    score = 48
    if price > 0 and ema20 > 0:
        if price >= ema20 * 1.01:
            score += 18
        elif price >= ema20:
            score += 12
        elif price < ema20 * 0.97:
            score -= 12
        else:
            score -= 4
    if 45 <= rsi <= 65:
        score += 14
    elif 35 <= rsi < 45 or 65 < rsi <= 72:
        score += 6
    elif rsi < 30:
        score += 10  # oversold bounce potential
    elif rsi > 75:
        score -= 10
    if macd_hist > 0.05:
        score += 12
    elif macd_hist > 0:
        score += 6
    elif macd_hist < -0.05:
        score -= 8
    if price > 0 and prev_close > 0:
        chg = (price - prev_close) / prev_close * 100.0
        if chg >= 1.5:
            score += 10
        elif chg >= 0.4:
            score += 5
        elif chg <= -2.0:
            score -= 12
        elif chg <= -0.8:
            score -= 6
    return max(10, min(95, int(round(score))))


def compute_fundamental_score(feed: dict) -> int:
    """0–100 fundamental score from Neon metrics / stored score."""
    stored = feed.get("fundamental_score")
    if stored is not None:
        try:
            fs = int(float(stored))
            if 0 < fs <= 100:
                return max(5, min(95, fs))
        except (TypeError, ValueError):
            pass

    m = _metrics(feed)
    pe = _f(feed.get("pe_ratio", feed.get("pe", m.get("pe_ratio", m.get("pe")))), DEFAULT_STATIC_INDICATORS["pe_ratio"])
    roce = _f(feed.get("roce", m.get("roce")), DEFAULT_STATIC_INDICATORS["roce"])
    roe = _f(feed.get("roe", m.get("roe")), 0.0)

    score = 48
    if 8 <= pe <= 28:
        score += 22
    elif 28 < pe <= 40:
        score += 10
    elif 0 < pe < 8:
        score += 14
    elif pe > 50:
        score -= 10
    if roce >= 18:
        score += 20
    elif roce >= 14:
        score += 14
    elif roce >= 10:
        score += 6
    elif 0 < roce < 8:
        score -= 6
    if roe >= 15:
        score += 8
    elif roe >= 10:
        score += 4
    # quality / multi-quarter if present
    q = _f(feed.get("quality_score", m.get("quality_score")), 0.0)
    if q >= 70:
        score += 8
    elif q >= 50:
        score += 4
    return max(10, min(95, int(round(score))))


def derive_decision(combined: int, change_pct: float, tech: int, fund: int) -> Tuple[str, str]:
    """Map composite → decision label + confidence."""
    if combined >= 72 and change_pct >= 0.6 and tech >= 60:
        return "BUY NOW", "High"
    if combined >= 65 and change_pct >= 0.2:
        return "PREPARE TO BUY", "Medium"
    if combined >= 58 and change_pct >= -0.5:
        return "PREPARE TO BUY", "Medium"
    if change_pct <= -2.5 or (combined < 38 and change_pct < 0):
        return "AVOID", "Medium"
    if combined < 42:
        return "HOLD", "Low"
    return "HOLD", "Medium"


def compute_instant_scores(
    symbol: str,
    feed: Optional[dict] = None,
    tick: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Produce a Decision-shaped dict from Neon feed + optional live tick.
    Never blocks on downstream HTTP.
    """
    feed = feed if isinstance(feed, dict) else {}
    tick = tick if isinstance(tick, dict) else {}
    base = str(symbol or feed.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()

    feats = resolve_stock_features(base, feed, tick)
    price = feats["price"]
    prev_close = feats["prev_close"]
    if prev_close <= 0 and price > 0:
        prev_close = price

    change_pct = 0.0
    if price > 0 and prev_close > 0:
        change_pct = round(((price - prev_close) / prev_close) * 100.0, 2)

    tech = compute_technical_score(feed, price, prev_close)
    fund = compute_fundamental_score(feed)

    # Momentum tilt on combined (still bounded)
    mom = 0.0
    if abs(change_pct) >= 0.3:
        mom = max(-8.0, min(10.0, change_pct * 1.2))
    combined = int(round(0.55 * tech + 0.35 * fund + 0.10 * (50 + mom)))
    combined = max(12, min(95, combined))

    decision, confidence = derive_decision(combined, change_pct, tech, fund)

    target = round(price * 1.065, 2) if price > 0 else None
    stop = round(price * 0.968, 2) if price > 0 else None
    entry_low = round(price * 0.995, 2) if price > 0 else None
    entry_high = round(price * 1.008, 2) if price > 0 else None

    has_feed = bool(feed) and (
        feed.get("fundamental_score") is not None
        or feed.get("technical_score") is not None
        or feed.get("metrics")
        or feed.get("combined_score") is not None
        or feed.get("rsi") is not None
        or feed.get("prev_close") is not None
        or feed.get("pe_ratio") is not None
        or feed.get("roce") is not None
    )

    sparse = not has_feed and price <= 0
    if sparse:
        decision = "HOLD"
        confidence = "Low"
        combined = 40
        tech = 40
        fund = 40
        status = "SYNCING"
    else:
        # Price-only (no Neon row): still READY with default indicators + momentum
        status = "READY" if price > 0 else "SYNCING"
        if not has_feed and price > 0:
            # Tag so UI knows scores are provisional
            confidence = confidence if confidence else "Low"

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
        "rsi": feats["rsi"],
        "pe_ratio": feats["pe_ratio"],
        "roce": feats["roce"],
        "target": target,
        "stop_loss": stop,
        "entry_range": {"low": entry_low, "high": entry_high} if price > 0 else None,
        "holding_period": "3-7 Days",
        "sector": feats.get("sector") or feed.get("sector"),
        "industry": feats.get("industry") or feed.get("industry"),
        "valuation": feats.get("valuation") or feed.get("valuation"),
        "fundamental_metrics": feed.get("metrics"),
        "news_score": feed.get("news_score"),
        "event_risk": feed.get("event_risk"),
        "from_data_feed": has_feed,
        "data_insufficient": sparse,
        "provisional_defaults": (not has_feed and price > 0),
        "lite_fastpath": True,
        "instant_scanner": True,
        "status": status,
        "reasons": {
            "technical": [
                f"Tech score {tech}/100 from Neon indicators + price vs EMA/momentum",
            ],
            "fundamental": [
                f"Fund score {fund}/100 from Neon quarterly / valuation metrics"
                + (" (defaults)" if not has_feed else ""),
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
