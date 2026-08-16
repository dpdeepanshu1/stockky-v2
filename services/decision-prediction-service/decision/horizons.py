"""
Multi-horizon scoring for Stockky Decision Engine.
Short (3–21d) / Mid (1–6m) / Long (6–24m) with different weights.
Free-tier only — pure Python, no paid APIs.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple

# Decision labels (unchanged)
BUY_NOW = "BUY NOW"
PREPARE = "PREPARE TO BUY"
HOLD = "HOLD"
DO_NOT_BUY = "DO NOT BUY"
SELL = "SELL"

# Horizon weight profiles (must sum ~1.0 across core pillars)
HORIZON_WEIGHTS = {
    "short": {
        "technical": 0.38,
        "volume_rs": 0.18,
        "news_events": 0.14,
        "prediction": 0.12,
        "fundamental": 0.10,
        "quality_peers": 0.04,
        "regime": 0.04,
    },
    "mid": {
        "technical": 0.26,
        "volume_rs": 0.12,
        "news_events": 0.10,
        "prediction": 0.12,
        "fundamental": 0.22,
        "quality_peers": 0.10,
        "regime": 0.08,
    },
    "long": {
        "technical": 0.12,
        "volume_rs": 0.06,
        "news_events": 0.06,
        "prediction": 0.08,
        "fundamental": 0.34,
        "quality_peers": 0.22,
        "regime": 0.12,
    },
}

HORIZON_META = {
    "short": {"label": "Short-term", "days_min": 3, "days_max": 21, "holding": "3–21 trading days"},
    "mid": {"label": "Mid-term", "days_min": 21, "days_max": 126, "holding": "1–6 months"},
    "long": {"label": "Long-term", "days_min": 126, "days_max": 504, "holding": "6–24 months"},
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _n(v, default=50.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def regime_multipliers(market_score: float, classification: str = "") -> Dict[str, float]:
    """Dynamic weight tilt by market regime (Nifty-driven score 0–100)."""
    ms = _n(market_score, 50)
    # Bull: favour momentum/RS; Bear: favour quality/fundamentals; Neutral: balanced
    if ms >= 65 or "bull" in (classification or "").lower():
        return {"technical": 1.15, "volume_rs": 1.20, "fundamental": 0.90, "quality_peers": 0.95, "news_events": 1.05}
    if ms <= 40 or "bear" in (classification or "").lower():
        return {"technical": 0.85, "volume_rs": 0.80, "fundamental": 1.20, "quality_peers": 1.25, "news_events": 0.95}
    return {"technical": 1.0, "volume_rs": 1.0, "fundamental": 1.0, "quality_peers": 1.0, "news_events": 1.0}


def score_to_decision(
    score: float,
    already_owned: bool,
    event_risk: bool,
    extended: bool,
    thin_history: bool,
    low_liquidity: bool,
    horizon: str,
) -> str:
    """Soft score-driven rules (not hard AND gates). Prefer short-term aggressiveness."""
    if thin_history or low_liquidity:
        return DO_NOT_BUY
    if already_owned:
        if score >= 62:
            return HOLD
        if score < 40:
            return SELL
        return HOLD
    # Soft penalties — never a hard veto for a high overall score
    adj = score - (6 if extended and horizon != "short" else (3 if extended else 0))
    if event_risk and horizon == "short":
        adj -= 4
    # Short-term is primary focus: slightly lower bars so good setups surface
    if horizon == "short":
        if adj >= 66:
            return BUY_NOW
        if adj >= 54:
            return PREPARE
    else:
        if adj >= 70:
            return BUY_NOW
        if adj >= 58:
            return PREPARE
    if adj >= 45:
        return HOLD if already_owned else DO_NOT_BUY
    return DO_NOT_BUY


def compute_pillar_scores(
    technical: dict,
    fundamental: dict,
    news: Optional[dict],
    events: Optional[dict],
    prediction: Optional[dict],
    market: dict,
    extras: Optional[dict] = None,
) -> Dict[str, float]:
    extras = extras or {}
    tech = _n(technical.get("technical_score"), 50)
    # Relative strength vs Nifty (0–100 style)
    rs = _n(extras.get("rs_vs_nifty"), technical.get("rs_score"), 50)
    vol = 55.0
    if technical.get("volume_surge"):
        vol = 75.0
    delivery = _n(extras.get("delivery_pct"), technical.get("delivery_pct"), 50)
    volume_rs = _clamp(0.45 * rs + 0.35 * vol + 0.20 * (50 + (delivery - 50) * 0.5))

    fund = _n(fundamental.get("fundamental_score"), 50)
    quality = _n(extras.get("quality_score"), fundamental.get("quality_score"), fund)
    peer = _n(extras.get("peer_relative_score"), 50)
    quality_peers = _clamp(0.55 * quality + 0.45 * peer)

    news_s = _n((news or {}).get("news_score"), 50)
    event_penalty = 0.0
    if events and (events.get("event_risk") or events.get("next_earnings_date")):
        event_penalty = 8.0
    news_events = _clamp(news_s - event_penalty)

    pred = 50.0
    if prediction and prediction.get("model_loaded"):
        pred = _n(prediction.get("prediction_score"), 50)

    regime = _n(market.get("market_score"), 50)

    return {
        "technical": tech,
        "volume_rs": volume_rs,
        "news_events": news_events,
        "prediction": pred,
        "fundamental": fund,
        "quality_peers": quality_peers,
        "regime": regime,
    }


def score_horizon(
    horizon: str,
    pillars: Dict[str, float],
    market_score: float,
    classification: str,
    flags: dict,
) -> Dict[str, Any]:
    weights = HORIZON_WEIGHTS[horizon]
    mult = regime_multipliers(market_score, classification)
    total_w = 0.0
    weighted = 0.0
    for k, w in weights.items():
        m = mult.get(k, 1.0)
        ww = w * m
        weighted += pillars.get(k, 50.0) * ww
        total_w += ww
    score = _clamp(weighted / total_w if total_w else 50.0)

    # Live win-rate feedback (closed-loop): mild adjustment ±5
    live_wr = flags.get("live_win_rate")
    if live_wr is not None:
        # win rate 0–1 → adjust score toward empirical edge
        edge = (float(live_wr) - 0.5) * 10.0
        score = _clamp(score + edge)

    decision = score_to_decision(
        score,
        already_owned=bool(flags.get("already_owned")),
        event_risk=bool(flags.get("event_risk")),
        extended=bool(flags.get("extended")),
        thin_history=bool(flags.get("thin_history")),
        low_liquidity=bool(flags.get("low_liquidity")),
        horizon=horizon,
    )
    conf = "High" if score >= 75 else "Medium" if score >= 55 else "Low"
    meta = HORIZON_META[horizon]
    return {
        "horizon": horizon,
        "label": meta["label"],
        "holding_period": meta["holding"],
        "days_min": meta["days_min"],
        "days_max": meta["days_max"],
        "score": round(score, 1),
        "decision": decision,
        "confidence": conf,
        "weights_used": weights,
        "pillars": {k: round(v, 1) for k, v in pillars.items()},
    }


def multi_horizon_decide(
    technical: dict,
    fundamental: dict,
    news: Optional[dict],
    events: Optional[dict],
    prediction: Optional[dict],
    market: dict,
    extras: Optional[dict] = None,
    flags: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Returns short/mid/long horizon blocks + primary (short-first) verdict.
    """
    flags = flags or {}
    extras = extras or {}
    pillars = compute_pillar_scores(technical, fundamental, news, events, prediction, market, extras)
    market_score = _n(market.get("market_score"), 50)
    classification = str(market.get("classification") or market.get("trend") or "")

    horizons = {
        h: score_horizon(h, pillars, market_score, classification, flags)
        for h in ("short", "mid", "long")
    }

    # Primary focus = short-term (project requirement)
    primary = horizons["short"]
    # Final verdict: best across horizons with short bias
    ranked = sorted(
        horizons.values(),
        key=lambda x: (x["score"] + (6 if x["horizon"] == "short" else 2 if x["horizon"] == "mid" else 0)),
        reverse=True,
    )
    best = ranked[0]
    final_verdict = {
        "preferred_horizon": "short",
        "primary_decision": primary["decision"],
        "primary_score": primary["score"],
        "best_horizon": best["horizon"],
        "best_decision": best["decision"],
        "best_score": best["score"],
        "summary": (
            f"Primary (Short): {primary['decision']} ({primary['score']}). "
            f"Best overall: {best['label']} → {best['decision']} ({best['score']})."
        ),
    }
    return {
        "horizons": horizons,
        "final_verdict": final_verdict,
        "combined_score": primary["score"],  # backward compatible = short
        "decision": primary["decision"],
        "confidence": primary["confidence"],
        "holding_period": primary["holding_period"],
    }
