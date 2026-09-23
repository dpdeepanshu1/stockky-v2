"""
Feature engineering for stock data.
"""
import pandas as pd
import numpy as np

FEATURE_COLUMNS = [
    'sma_10', 'sma_30', 'ema_10', 'rsi', 'volatility', 'volume_sma'
]

def compute_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def compute_feature_frame(df):
    """Compute technical indicators from OHLCV DataFrame."""
    df = df.copy()
    df['sma_10'] = df['close'].rolling(10).mean()
    df['sma_30'] = df['close'].rolling(30).mean()
    df['ema_10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['rsi'] = compute_rsi(df['close'], 14)
    df['volatility'] = df['close'].pct_change().rolling(10).std()
    df['volume_sma'] = df['volume'].rolling(10).mean()
    return df