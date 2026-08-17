"""
Point-in-time feature builder for training the prediction model.

This module builds one training row per (symbol, date) using ONLY information
that was known on or before that date:

- Technical features  → calculated from price history up to that date
- Fundamental features → latest reported snapshot available on/before that date
- News / event features → calculated only from news/events published on/before that date

This prevents look-ahead bias.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Import the shared feature engineering (same file used by live prediction)
import sys
import os

# Allow importing from sibling prediction package when running inside the service
_PRED_DIR = os.path.join(os.path.dirname(__file__), "..", "prediction")
if _PRED_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_PRED_DIR))

from pred_features import (
    FEATURE_COLUMNS,
    TECHNICAL_COLUMNS,
    FUNDAMENTAL_COLUMNS,
    NEWS_COLUMNS,
    build_full_feature_vector,
    compute_technical_features,
    _safe,
)

logger = logging.getLogger("feature_builder")


def _normalize_fundamentals(raw: Dict[str, Any]) -> Dict[str, float]:
    """Map various API key names to the canonical fundamental feature names."""
    if not raw:
        return {c: 0.0 for c in FUNDAMENTAL_COLUMNS}
    return {
        "pe_ratio": _safe(raw.get("pe_ratio") or raw.get("pe") or raw.get("trailingPE")),
        "roe": _safe(raw.get("roe") or raw.get("returnOnEquity")),
        "debt_to_equity": _safe(raw.get("debt_to_equity") or raw.get("debtToEquity")),
        "revenue_growth_yoy": _safe(raw.get("revenue_growth_yoy") or raw.get("revenueGrowth")),
        "profit_growth_yoy": _safe(raw.get("profit_growth_yoy") or raw.get("earningsGrowth")),
        "promoter_holding": _safe(raw.get("promoter_holding") or raw.get("promoterHolding")),
    }


def _get_fundamental_as_of(
    fundamental_history: List[Dict[str, Any]],
    as_of: datetime,
) -> Dict[str, float]:
    """
    fundamental_history: list of snapshots, each with at least an 'as_of' or 'date' field.
    Returns the latest snapshot whose date <= as_of.
    """
    if not fundamental_history:
        return {c: 0.0 for c in FUNDAMENTAL_COLUMNS}

    best = None
    best_date = None
    for snap in fundamental_history:
        d = snap.get("as_of") or snap.get("date") or snap.get("reported_date")
        if d is None:
            continue
        try:
            if isinstance(d, str):
                d = datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
            elif hasattr(d, "to_pydatetime"):
                d = d.to_pydatetime().replace(tzinfo=None)
        except Exception:
            continue
        if d <= as_of and (best_date is None or d > best_date):
            best = snap
            best_date = d

    if best is None:
        # Fall back to oldest available (still better than future data)
        return _normalize_fundamentals(fundamental_history[0] if fundamental_history else {})
    return _normalize_fundamentals(best)


def _compute_news_scores_as_of(
    news_items: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    as_of: datetime,
) -> Dict[str, float]:
    """
    news_items: list of {date, sentiment_score, ...}
    events: list of {date, type, surprise_pct, ...}
    Only uses items with date <= as_of.
    """
    cutoff = as_of

    def _parse_date(item: Dict) -> Optional[datetime]:
        d = item.get("date") or item.get("published_at") or item.get("as_of")
        if d is None:
            return None
        try:
            if isinstance(d, str):
                return datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
            if hasattr(d, "to_pydatetime"):
                return d.to_pydatetime().replace(tzinfo=None)
            return d
        except Exception:
            return None

    # Sentiment windows
    s7, s14 = [], []
    for item in news_items or []:
        d = _parse_date(item)
        if d is None or d > cutoff:
            continue
        score = item.get("sentiment_score") or item.get("score")
        try:
            score = float(score)
            # normalize 0-100 → -1..+1 if needed
            if score > 1.5:
                score = (score - 50.0) / 50.0
        except Exception:
            continue
        delta = (cutoff - d).days
        if delta <= 7:
            s7.append(score)
        if delta <= 14:
            s14.append(score)

    sentiment_7d = float(np.mean(s7)) if s7 else 0.0
    sentiment_14d = float(np.mean(s14)) if s14 else 0.0

    # Events
    surprise_flag = 0.0
    event_score = 0.0
    days_to_earnings = 30.0
    next_earn = None

    for ev in events or []:
        d = _parse_date(ev)
        if d is None:
            continue
        etype = (ev.get("type") or ev.get("event_type") or "").lower()
        if d <= cutoff:
            # past events
            if "earn" in etype or "result" in etype:
                surprise = ev.get("surprise_pct") or ev.get("surprise")
                try:
                    if surprise is not None and float(surprise) > 0:
                        surprise_flag = 1.0
                except Exception:
                    pass
            if "bulk" in etype or "block" in etype:
                event_score += 0.25
            if "insider" in etype and ("buy" in etype or ev.get("side") == "buy"):
                event_score += 0.35
        else:
            # future earnings
            if "earn" in etype or "result" in etype:
                if next_earn is None or d < next_earn:
                    next_earn = d

    if next_earn is not None:
        days_to_earnings = float(max(0, min(90, (next_earn - cutoff).days)))

    event_score = min(1.0, event_score + (0.3 if surprise_flag else 0.0))

    return {
        "news_sentiment_7d": _safe(sentiment_7d),
        "news_sentiment_14d": _safe(sentiment_14d),
        "earnings_surprise_flag": surprise_flag,
        "days_to_next_earnings": days_to_earnings,
        "recent_event_score": _safe(event_score),
    }


def build_training_row(
    symbol: str,
    as_of: datetime,
    history_df: pd.DataFrame,
    fundamental_history: Optional[List[Dict[str, Any]]] = None,
    news_items: Optional[List[Dict[str, Any]]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    label: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build one training sample for (symbol, as_of).

    history_df must contain OHLCV up to (and including) as_of.
    Returns a flat dict with all FEATURE_COLUMNS + optional 'label' + meta.
    """
    if history_df is None or len(history_df) < 50:
        return None

    # Ensure we only use prices on or before as_of
    if not isinstance(history_df.index, pd.DatetimeIndex):
        history_df = history_df.copy()
        if "date" in history_df.columns:
            history_df["date"] = pd.to_datetime(history_df["date"])
            history_df = history_df.set_index("date")
    hist = history_df[history_df.index <= as_of].copy()
    if len(hist) < 50:
        return None

    fund = _get_fundamental_as_of(fundamental_history or [], as_of)
    news = _compute_news_scores_as_of(news_items or [], events or [], as_of)

    features = build_full_feature_vector(
        history_df=hist,
        fundamental_snapshot=fund,
        news_scores=news,
        as_of_date=as_of,
    )

    row = {
        "symbol": symbol,
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        **features,
    }
    if label is not None:
        row["label"] = float(label)
    return row


def build_training_dataset(
    samples: List[Dict[str, Any]],
) -> Tuple[pd.DataFrame, Optional[pd.Series]]:
    """
    samples: list of dicts produced by build_training_row (must contain FEATURE_COLUMNS + optional label)

    Returns X (DataFrame of features) and y (Series of labels) if labels present.
    """
    if not samples:
        return pd.DataFrame(columns=FEATURE_COLUMNS), None

    df = pd.DataFrame(samples)
    # Ensure all feature columns exist
    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    X = df[FEATURE_COLUMNS].astype(float).fillna(0.0)

    y = None
    if "label" in df.columns:
        y = df["label"].astype(float)

    return X, y


def make_label_from_forward_return(
    history_df: pd.DataFrame,
    as_of: datetime,
    horizon_days: int = 10,
    threshold_pct: float = 5.0,
) -> Optional[float]:
    """
    Classic label: 1 if close rises >= threshold_pct within the next horizon_days trading days.
    Uses only future prices relative to as_of (this is the target, not a feature).
    """
    if history_df is None or len(history_df) < 20:
        return None
    if not isinstance(history_df.index, pd.DatetimeIndex):
        return None

    hist = history_df.sort_index()
    past = hist[hist.index <= as_of]
    if past.empty:
        return None
    entry_price = float(past["Close"].iloc[-1])
    future = hist[hist.index > as_of].head(horizon_days)
    if future.empty:
        return None
    max_future = float(future["Close"].max())
    ret_pct = (max_future - entry_price) / entry_price * 100.0
    return 1.0 if ret_pct >= threshold_pct else 0.0
