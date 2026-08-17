"""
Feature engineering for Stockky Prediction Service.

Supports:
- Technical features (existing)
- Fundamental features (point-in-time)
- News / sentiment features (point-in-time)

All features are designed to avoid look-ahead bias when as_of_date is respected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import ta

# ---------------------------------------------------------------------------
# Feature columns the model expects (technical + fundamental + news)
# ---------------------------------------------------------------------------
TECHNICAL_COLUMNS = [
    "rsi_14",
    "macd_hist",
    "ema20_over_ema50",
    "ema50_over_ema200",
    "close_over_ema20",
    "adx_14",
    "bb_pct",
    "volume_ratio_20",
    "dist_from_20d_high_pct",
    "dist_from_20d_low_pct",
    "atr_pct",
    "golden_cross",
]

FUNDAMENTAL_COLUMNS = [
    "pe_ratio",
    "roe",
    "debt_to_equity",
    "revenue_growth_yoy",
    "profit_growth_yoy",
    "promoter_holding",
]

NEWS_COLUMNS = [
    "news_sentiment_7d",
    "news_sentiment_14d",
    "earnings_surprise_flag",
    "days_to_next_earnings",
    "recent_event_score",
]

# Peer + multi-quarter (from fundamental enrichment)
PEER_CONSISTENCY_COLUMNS = [
    "peer_score",
    "consistency_score",
]

FEATURE_COLUMNS = TECHNICAL_COLUMNS + FUNDAMENTAL_COLUMNS + NEWS_COLUMNS + PEER_CONSISTENCY_COLUMNS


def _safe(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and (np.isnan(val) or np.isinf(val))):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def compute_technical_features(df: pd.DataFrame) -> Dict[str, float]:
    """
    Compute technical features from OHLCV dataframe.
    Expects columns: Open, High, Low, Close, Volume (title case).
    Uses only data present in the dataframe (caller must already slice by as_of_date).
    """
    if df is None or len(df) < 50:
        return {c: 0.0 for c in TECHNICAL_COLUMNS}

    df = df.copy()
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # Indicators
    df["EMA_20"] = ta.trend.ema_indicator(close, window=20)
    df["EMA_50"] = ta.trend.ema_indicator(close, window=50)
    df["EMA_200"] = ta.trend.ema_indicator(close, window=200)
    df["RSI_14"] = ta.momentum.rsi(close, window=14)
    df["MACD_H"] = ta.trend.macd_diff(close)
    df["ADX_14"] = ta.trend.adx(high, low, close, window=14)
    df["ATR_14"] = ta.volatility.average_true_range(high, low, close, window=14)
    df["BB_PCT"] = ta.volatility.bollinger_pband(close, window=20)

    last = df.iloc[-1]
    prev_close = float(close.iloc[-1])

    # Ratios
    ema20 = _safe(last.get("EMA_20"), prev_close)
    ema50 = _safe(last.get("EMA_50"), prev_close)
    ema200 = _safe(last.get("EMA_200"), prev_close)

    vol_ma20 = volume.rolling(20).mean().iloc[-1]
    volume_ratio = _safe(volume.iloc[-1] / vol_ma20 if vol_ma20 and vol_ma20 > 0 else 1.0, 1.0)

    high_20 = high.rolling(20).max().iloc[-1]
    low_20 = low.rolling(20).min().iloc[-1]
    dist_high = _safe((prev_close - high_20) / high_20 * 100 if high_20 else 0.0)
    dist_low = _safe((prev_close - low_20) / low_20 * 100 if low_20 else 0.0)

    atr = _safe(last.get("ATR_14"), 0.0)
    atr_pct = _safe(atr / prev_close * 100 if prev_close else 0.0)

    # Golden cross: EMA50 crossed above EMA200 in last 5 bars
    ema50_s = df["EMA_50"]
    ema200_s = df["EMA_200"]
    cross = ((ema50_s > ema200_s) & (ema50_s.shift(1) <= ema200_s.shift(1))).astype(int)
    golden_cross = int(cross.rolling(5).max().iloc[-1] or 0)

    return {
        "rsi_14": _safe(last.get("RSI_14"), 50.0),
        "macd_hist": _safe(last.get("MACD_H"), 0.0),
        "ema20_over_ema50": _safe(ema20 / ema50 if ema50 else 1.0, 1.0),
        "ema50_over_ema200": _safe(ema50 / ema200 if ema200 else 1.0, 1.0),
        "close_over_ema20": _safe(prev_close / ema20 if ema20 else 1.0, 1.0),
        "adx_14": _safe(last.get("ADX_14"), 20.0),
        "bb_pct": _safe(last.get("BB_PCT"), 0.5),
        "volume_ratio_20": volume_ratio,
        "dist_from_20d_high_pct": dist_high,
        "dist_from_20d_low_pct": dist_low,
        "atr_pct": atr_pct,
        "golden_cross": float(golden_cross),
    }


def latest_feature_vector(df: pd.DataFrame) -> Dict[str, float]:
    """
    Backward-compatible helper used by existing prediction code.
    Returns technical features only (for old model compatibility).
    """
    return compute_technical_features(df)


def build_full_feature_vector(
    history_df: pd.DataFrame,
    fundamental_snapshot: Optional[Dict[str, Any]] = None,
    news_scores: Optional[Dict[str, Any]] = None,
    as_of_date: Optional[datetime] = None,
) -> Dict[str, float]:
    """
    Build the complete feature vector for training or live inference.

    Parameters
    ----------
    history_df : OHLCV dataframe (must already be sliced to <= as_of_date for training)
    fundamental_snapshot : dict of fundamental values known on/before as_of_date
    news_scores : dict of news/sentiment values calculated only from data <= as_of_date
    as_of_date : optional, used only for documentation / logging
    """
    fundamental_snapshot = fundamental_snapshot or {}
    news_scores = news_scores or {}

    # 1. Technical (point-in-time if caller already sliced the df)
    tech = compute_technical_features(history_df)

    # 2. Fundamental (point-in-time values supplied by caller)
    fund = {
        "pe_ratio": _safe(fundamental_snapshot.get("pe_ratio") or fundamental_snapshot.get("pe"), 0.0),
        "roe": _safe(fundamental_snapshot.get("roe"), 0.0),
        "debt_to_equity": _safe(
            fundamental_snapshot.get("debt_to_equity") or fundamental_snapshot.get("debtToEquity"), 0.0
        ),
        "revenue_growth_yoy": _safe(
            fundamental_snapshot.get("revenue_growth_yoy") or fundamental_snapshot.get("revenueGrowth"), 0.0
        ),
        "profit_growth_yoy": _safe(
            fundamental_snapshot.get("profit_growth_yoy") or fundamental_snapshot.get("earningsGrowth"), 0.0
        ),
        "promoter_holding": _safe(fundamental_snapshot.get("promoter_holding"), 0.0),
    }

    # 3. News / Event (point-in-time values supplied by caller)
    news = {
        "news_sentiment_7d": _safe(news_scores.get("news_sentiment_7d") or news_scores.get("sentiment_7d"), 0.0),
        "news_sentiment_14d": _safe(news_scores.get("news_sentiment_14d") or news_scores.get("sentiment_14d"), 0.0),
        "earnings_surprise_flag": _safe(
            news_scores.get("earnings_surprise_flag") or news_scores.get("surprise_flag"), 0.0
        ),
        "days_to_next_earnings": _safe(
            news_scores.get("days_to_next_earnings") or news_scores.get("days_to_earnings"), 30.0
        ),
        "recent_event_score": _safe(news_scores.get("recent_event_score") or news_scores.get("event_score"), 0.0),
    }

    peer = {
        "peer_score": _safe(
            fundamental_snapshot.get("peer_score")
            or (fundamental_snapshot.get("peer_relative") or {}).get("peer_score"),
            50.0,
        ),
        "consistency_score": _safe(
            fundamental_snapshot.get("consistency_score")
            or (fundamental_snapshot.get("multi_quarter") or {}).get("consistency_score"),
            50.0,
        ),
    }

    return {**tech, **fund, **news, **peer}


def get_feature_columns(include_fundamental: bool = True, include_news: bool = True) -> list:
    """Return the list of feature columns the model should use."""
    cols = list(TECHNICAL_COLUMNS)
    if include_fundamental:
        cols += FUNDAMENTAL_COLUMNS
    if include_news:
        cols += NEWS_COLUMNS
    return cols


def compute_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compatibility wrapper for pred_train.py / legacy OHLCV training.

    Builds a DataFrame of technical feature columns row-by-row where possible.
    For simplicity and stability, computes the latest technical vector and
    broadcasts as columns aligned to df index (walk-forward uses full series
    via compute_technical_features on expanding windows in advanced pipelines).
    """
    if df is None or len(df) < 50:
        return pd.DataFrame(columns=TECHNICAL_COLUMNS)

    work = df.copy()
    # Normalize column names
    colmap = {c: c.title() for c in work.columns}
    # Prefer standard OHLCV names
    rename = {}
    for c in work.columns:
        cl = c.lower()
        if cl == "open":
            rename[c] = "Open"
        elif cl == "high":
            rename[c] = "High"
        elif cl == "low":
            rename[c] = "Low"
        elif cl == "close":
            rename[c] = "Close"
        elif cl == "volume":
            rename[c] = "Volume"
    work = work.rename(columns=rename)

    if "Close" not in work.columns:
        # Last-resort: pick a close-like column
        for c in list(work.columns):
            if str(c).lower() in ("close", "adj close", "adj_close"):
                work = work.rename(columns={c: "Close"})
                break
    if "Close" not in work.columns:
        raise KeyError("OHLCV frame missing Close after normalize")

    # Rolling technicals as columns (vectorized subset)
    close = work["Close"]
    out = pd.DataFrame(index=work.index)
    # Keep Close for trainers that label from future price (pred_train)
    out["Close"] = close
    try:
        out["rsi_14"] = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd = ta.trend.MACD(close)
        out["macd_hist"] = macd.macd_diff()
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        out["ema20_over_ema50"] = ema20 / ema50.replace(0, np.nan)
        out["ema50_over_ema200"] = ema50 / ema200.replace(0, np.nan)
        out["close_over_ema20"] = close / ema20.replace(0, np.nan)
        out["adx_14"] = ta.trend.ADXIndicator(work["High"], work["Low"], close, window=14).adx()
        bb = ta.volatility.BollingerBands(close, window=20)
        out["bb_pct"] = bb.bollinger_pband()
        vol = work["Volume"]
        out["volume_ratio_20"] = vol / vol.rolling(20).mean().replace(0, np.nan)
        roll_high = close.rolling(20).max()
        roll_low = close.rolling(20).min()
        out["dist_from_20d_high_pct"] = (close - roll_high) / roll_high.replace(0, np.nan) * 100
        out["dist_from_20d_low_pct"] = (close - roll_low) / roll_low.replace(0, np.nan) * 100
        atr = ta.volatility.AverageTrueRange(work["High"], work["Low"], close, window=14).average_true_range()
        out["atr_pct"] = atr / close.replace(0, np.nan) * 100
        out["golden_cross"] = (ema50 > ema200).astype(float)
    except Exception:
        for col in TECHNICAL_COLUMNS:
            if col not in out.columns:
                out[col] = 0.0
    out = out.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return out
