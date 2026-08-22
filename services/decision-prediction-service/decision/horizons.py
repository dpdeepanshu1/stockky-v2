"""
Multi-horizon scoring for Stockky Decision Engine.
Short (3–21d) / Mid (1–6m) / Long (6–24m) with different weights.
Free-tier only — pure Python, no paid APIs.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, List, Tuple

# Decision labels (unchanged)
BUY_NOW = "BUY NOW"
PREPARE = "PREPARE TO BUY"
HOLD = "HOLD"
DO_NOT_BUY = "DO NOT BUY"
SELL = "SELL"

# Horizon weight profiles (must sum ~1.0 across core pillars)
HORIZON_WEIGHTS = {
    "short": {
        # News/Event fix: split the old combined "news_events" pillar into
        # two independently-weighted pillars per your spec — News 25%,
        # Event 25% for short-term (catalyst-heavy horizon), with the
        # remaining 50% spread across technical/volume/prediction/
        # fundamental/quality/regime in the same relative proportions used
        # before.
        "technical": 0.21,
        "volume_rs": 0.14,
        "news": 0.25,
        "event": 0.25,
        "prediction": 0.06,
        "fundamental": 0.04,
        "quality_peers": 0.03,
        "regime": 0.02,
    },
    "mid": {
        # Mid-term: catalysts still matter (earnings/events shape the next
        # 1-6 months) but less than short-term momentum — News/Event scaled
        # down from short's 25/25 while fundamentals stay the dominant pillar.
        "technical": 0.23,
        "volume_rs": 0.11,
        "news": 0.10,
        "event": 0.10,
        "prediction": 0.11,
        "fundamental": 0.20,
        "quality_peers": 0.09,
        "regime": 0.06,
    },
    "long": {
        # Long-term: catalysts matter least, fundamentals/quality dominate.
        "technical": 0.12,
        "volume_rs": 0.06,
        "news": 0.05,
        "event": 0.05,
        "prediction": 0.07,
        "fundamental": 0.32,
        "quality_peers": 0.21,
        "regime": 0.12,
    },
}

HORIZON_META = {
    "short": {"label": "Short-term", "days_min": 3, "days_max": 21, "holding": "3–21 trading days"},
    "mid": {"label": "Mid-term", "days_min": 21, "days_max": 126, "holding": "1–6 months"},
    "long": {"label": "Long-term", "days_min": 126, "days_max": 504, "holding": "6–24 months"},
}



def _age_hours(ts) -> Optional[float]:
    """Hours since ts (datetime / iso str). None if unknown."""
    if ts is None:
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.replace(tzinfo=None)
        return max(0.0, (datetime.utcnow() - ts).total_seconds() / 3600.0)
    except Exception:
        return None


def time_decay_weight(age_hours: Optional[float], half_life_hours: float = 4.0) -> float:
    """Exponential decay: full weight at age 0, ~0.5 at half_life, floors at 0.15."""
    if age_hours is None:
        return 1.0
    try:
        import math
        w = math.exp(-math.log(2) * float(age_hours) / max(0.5, half_life_hours))
        return max(0.15, min(1.0, w))
    except Exception:
        return 1.0

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
        return {"technical": 1.15, "volume_rs": 1.20, "fundamental": 0.90, "quality_peers": 0.95, "news": 1.05, "event": 1.05}
    if ms <= 40 or "bear" in (classification or "").lower():
        return {"technical": 0.85, "volume_rs": 0.80, "fundamental": 1.20, "quality_peers": 1.25, "news": 0.95, "event": 0.95}
    return {"technical": 1.0, "volume_rs": 1.0, "fundamental": 1.0, "quality_peers": 1.0, "news": 1.0, "event": 1.0}


def _threshold_offsets(live_win_rate, live_n: int = 0) -> tuple:
    """Closed-loop: shift BUY_NOW / PREPARE thresholds from live win-rate.

    live_win_rate may be 0–1 or 0–100. High empirical edge → slightly lower
    bars (system has been right). Low edge → raise bars (be more selective).
    Full strength from ~25 evaluated samples; partial from 8+.
    """
    if live_win_rate is None:
        return 0.0, 0.0, "neutral"
    try:
        wr = float(live_win_rate)
        n = int(live_n or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, "neutral"
    if n < 8:
        return 0.0, 0.0, "neutral_sparse"
    if wr > 1.5:  # passed as percentage
        wr = wr / 100.0
    wr = max(0.0, min(1.0, wr))
    delta = (0.55 - wr) * 20.0  # positive delta = harder (higher bar)
    conf = min(1.0, max(0.4, (n - 8) / 17.0 + 0.4))  # 8→0.4, 25→1.0
    delta = max(-6.0, min(8.0, delta * conf))
    label = "raise_bar" if delta > 1 else "ease_bar" if delta < -1 else "neutral"
    return delta, -delta * 0.5, label  # (buy_now_offset, prepare_offset_extra, label)


def score_to_decision(
    score: float,
    already_owned: bool,
    event_risk: bool,
    extended: bool,
    thin_history: bool,
    low_liquidity: bool,
    horizon: str,
    live_win_rate=None,
    live_win_rate_n: int = 0,
) -> str:
    """Soft score-driven rules (not hard AND gates). Prefer short-term aggressiveness.

    Closed-loop: live_win_rate shifts BUY_NOW / PREPARE thresholds so the
    engine becomes more selective when recent outcomes are weak and slightly
    more aggressive when the live edge is strong.
    """
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

    buy_off, prep_extra, _ = _threshold_offsets(live_win_rate, live_win_rate_n)

    if horizon == "short":
        buy_bar = 66.0 + buy_off
        prep_bar = 54.0 + buy_off * 0.7 + prep_extra
        if adj >= buy_bar:
            return BUY_NOW
        if adj >= prep_bar:
            return PREPARE
    else:
        buy_bar = 70.0 + buy_off
        prep_bar = 58.0 + buy_off * 0.7 + prep_extra
        if adj >= buy_bar:
            return BUY_NOW
        if adj >= prep_bar:
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
    rs = _n(extras.get("rs_vs_nifty") or technical.get("rs_score"), 50)
    vol = 55.0
    if technical.get("volume_surge"):
        vol = 75.0
    delivery = _n(extras.get("delivery_pct") or technical.get("delivery_pct"), 50)
    volume_rs = _clamp(0.45 * rs + 0.35 * vol + 0.20 * (50 + (delivery - 50) * 0.5))

    fund = _n(fundamental.get("fundamental_score"), 50)
    quality = _n(extras.get("quality_score") or fundamental.get("quality_score"), fund)
    peer = _n(extras.get("peer_relative_score"), 50)
    quality_peers = _clamp(0.55 * quality + 0.45 * peer)

    # News and Event are now independent pillars (previously combined into
    # one "news_events" pillar with an ad-hoc -8 event penalty baked in).
    # News = pure news-sentiment score from the news service.
    news_pillar = _n((news or {}).get("news_score"), 50)

    # Event = the proper nature-based 0-100 score from
    # event_depth.compute_event_score (analysis-intelligence-service),
    # passed through via extras["event_score"]. Falls back to a neutral 50
    # if the event pipeline hasn't scored this symbol yet.
    event_pillar = _n(extras.get("event_score") or (events or {}).get("event_score"), 50)

    pred = 50.0
    if prediction and prediction.get("model_loaded"):
        pred = _n(prediction.get("prediction_score"), 50)

    regime = _n(market.get("market_score"), 50)

    return {
        "technical": tech,
        "volume_rs": volume_rs,
        "news": news_pillar,
        "event": event_pillar,
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

    # Closed-loop score nudge from live win-rate (±8 max)
    # Time-decay: stale news/sentiment (e.g. 4h old) contributes less vs fresh technicals.
    # Only applied to the "news" pillar — "event" already carries its own
    # per-item recency decay from event_depth.compute_event_score, so
    # decaying it again here would double-penalize old events.
    news_age = _age_hours(flags.get("news_as_of") or flags.get("sentiment_as_of") or flags.get("as_of"))
    news_w = time_decay_weight(news_age, half_life_hours=4.0)
    if "news" in pillars and news_w < 0.999:
        # Pull news pillar toward neutral 50 as it ages
        pillars["news"] = pillars["news"] * news_w + 50.0 * (1.0 - news_w)

    live_wr = flags.get("live_win_rate")
    live_n = int(flags.get("live_win_rate_n") or 0)
    if live_wr is not None and live_n >= 8:
        try:
            wr = float(live_wr)
            if wr > 1.5:
                wr = wr / 100.0
            edge = (wr - 0.50) * 16.0
            conf = min(1.0, max(0.4, (live_n - 8) / 17.0 + 0.4))
            score = _clamp(score + max(-8.0, min(8.0, edge * conf)))
        except (TypeError, ValueError):
            pass

    decision = score_to_decision(
        score,
        already_owned=bool(flags.get("already_owned")),
        event_risk=bool(flags.get("event_risk")),
        extended=bool(flags.get("extended")),
        thin_history=bool(flags.get("thin_history")),
        low_liquidity=bool(flags.get("low_liquidity")),
        horizon=horizon,
        live_win_rate=live_wr,
        live_win_rate_n=live_n,
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
