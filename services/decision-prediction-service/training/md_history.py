"""md_history.py (group270) - optional market-data-service source for TRAINING candles.

OFF by default (TRAINING_DATA_VIA_MARKET_DATA=0). market-data-service answers /history from Dhan, then AngelOne, then
yfinance, so turning this on moves training data off direct yfinance (which is IP-blocked from the VM).

Two things to settle BEFORE enabling it for a real training run:
  1. Adjusted prices. yfinance history is split/dividend adjusted (auto_adjust=True); Dhan's daily candles may not be.
     Mixing sources inside one dataset corrupts features. Compare a stock with a recent split/bonus first, and retrain
     from ONE source end to end.
  2. Depth. market-data-service caps /history at MAX_HISTORY_PERIOD (default 1y) and MAX_HISTORY_ROWS (260). Raise both
     there (e.g. MAX_HISTORY_PERIOD=5y MAX_HISTORY_ROWS=1300) or the frame returned here is shorter than the request.

Only the standard library is used. Returns a DataFrame shaped like yfinance's (DatetimeIndex; Open/High/Low/Close/Volume)
or an empty DataFrame on any failure, so callers fall straight through to their existing yfinance code.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

import pandas as pd


def enabled() -> bool:
    return ((os.getenv("TRAINING_DATA_VIA_MARKET_DATA") or "").strip() or "0").lower() in ("1", "true", "yes", "on")


def _base_url() -> str:
    return (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/")


def fetch_daily_df(symbol: str, period: str = "2y", timeout: float = 60.0) -> pd.DataFrame:
    """Daily candles for an NSE symbol from market-data-service as a yfinance-shaped frame; empty on any problem."""
    base = _base_url()
    if not base or not symbol:
        return pd.DataFrame()
    url = f"{base}/history/{urllib.parse.quote(symbol.upper(), safe='^')}?period={period}&interval=1d"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:     # noqa: S310 - fixed internal service URL
            if r.status != 200:
                return pd.DataFrame()
            body = json.loads(r.read().decode())
        candles = [c for c in (body.get("candles") or []) if isinstance(c, dict) and c.get("close")]
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        for col in ("Open", "High", "Low", "Close", "Volume"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    except Exception:  # noqa: BLE001 - never break training over a data-source hiccup
        return pd.DataFrame()
