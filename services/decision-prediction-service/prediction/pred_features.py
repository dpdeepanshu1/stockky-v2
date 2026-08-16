"""
Shared feature engineering for the Prediction Service.

Used by both `train.py` (building the training set from historical candles)
and `main.py` (computing the same features live at inference time). Keeping
this in one module guarantees train/serve feature parity.
"""
import pandas as pd
import ta
import numpy as np


FEATURE_COLUMNS = [
    "rsi_14", "macd_hist", "ema20_over_ema50", "ema50_over_ema200",
    "close_over_ema20", "adx_14", "bb_pct", "volume_ratio_20",
    "dist_from_20d_high_pct", "dist_from_20d_low_pct", "atr_pct",
    "golden_cross",  # NEW: Golden cross signal
]


def compute_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Given a DataFrame with Open/High/Low/Close/Volume columns (any length,
    but needs >= 200 rows for stable EMA200), return a DataFrame indexed the
    same way with the engineered feature columns added."""
    df = df.copy()
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    df["EMA_20"]  = ta.trend.ema_indicator(close, window=20)
    df["EMA_50"]  = ta.trend.ema_indicator(close, window=50)
    df["EMA_200"] = ta.trend.ema_indicator(close, window=200)
    df["RSI_14"]  = ta.momentum.rsi(close, window=14)
    df["MACD_H"]  = ta.trend.macd_diff(close)
    df["ADX_14"]  = ta.trend.adx(high, low, close, window=14)
    df["ATR_14"]  = ta.volatility.average_true_range(high, low, close, window=14)
    df["BB_PCT"]  = ta.volatility.bollinger_pband(close, window=20)

    df["rsi_14"]               = df["RSI_14"]
    df["macd_hist"]            = df["MACD_H"]
    df["ema20_over_ema50"]     = df["EMA_20"] / df["EMA_50"]
    df["ema50_over_ema200"]    = df["EMA_50"] / df["EMA_200"]
    df["close_over_ema20"]     = close / df["EMA_20"]
    df["adx_14"]               = df["ADX_14"]
    df["bb_pct"]               = df["BB_PCT"]
    df["volume_ratio_20"]      = volume / volume.rolling(20).mean()
    df["dist_from_20d_high_pct"] = (high.rolling(20).max() - close) / close * 100
    df["dist_from_20d_low_pct"]  = (close - low.rolling(20).min()) / close * 100
    df["atr_pct"]              = df["ATR_14"] / close * 100

    # --- NEW: Golden Cross – 1 if EMA50 crossed above EMA200 within last 5 days ---
    ema50 = df["EMA_50"]
    ema200 = df["EMA_200"]
    cross = ((ema50 > ema200) & (ema50.shift(1) <= ema200.shift(1))).astype(int)
    df["golden_cross"] = cross.rolling(5).max().fillna(0).astype(int)

    return df


def latest_feature_vector(df: pd.DataFrame) -> dict:
    """Feature dict for the most recent row — used at inference time."""
    feat_df = compute_feature_frame(df)
    latest = feat_df.iloc[-1]
    return {col: float(latest[col]) for col in FEATURE_COLUMNS if pd.notna(latest[col])}