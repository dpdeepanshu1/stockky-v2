"""group279: one place for the gateway's "may I call yfinance myself?" decision and the market-data daily-history read.

market-data-service decides Dhan -> AngelOne -> yfinance. The gateway should only call yfinance itself as a LAST resort,
for what market-data could not price, and that last resort must be switchable. Importing this module costs nothing
(no yfinance, pandas or httpx import at load time) so every gateway file can use it.

  GATEWAY_DIRECT_YFINANCE_FALLBACK   default on. 0/false/no/off = never call yfinance directly from the gateway.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("gateway-yf-policy")

_OFF = ("0", "false", "no", "off")


def env_on(name: str, default: bool = True) -> bool:
    """True unless the env var is set to 0/false/no/off. An empty or missing var gives `default`."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in _OFF


def direct_yf_ok() -> bool:
    """May the gateway call yfinance itself as a last resort when market-data-service has nothing?
    GATEWAY_DIRECT_YFINANCE_FALLBACK=0 enforces 'prices come only through market-data'. Default on."""
    return env_on("GATEWAY_DIRECT_YFINANCE_FALLBACK", True)


def md_daily_frame(market_data_url: str, symbol: str, period: Optional[str] = None, days: Optional[int] = None,
                   timeout: float = 15.0):
    """Daily OHLCV for one symbol from market-data-service GET /history/{symbol}, as a DataFrame shaped like
    yfinance's Ticker history frame (DatetimeIndex, Open/High/Low/Close/Volume), or None when market-data has nothing.
    Pass `period` (1mo, 3mo, 1y ...) or `days` (exact window). Never raises."""
    try:
        import httpx
        import pandas as pd
        from urllib.parse import quote as _urlquote

        base = (market_data_url or "").rstrip("/")
        if not base or not symbol:
            return None
        params = {"interval": "1d"}
        if days is not None:
            params["days"] = max(1, int(days))
        else:
            params["period"] = period or "1y"
        resp = httpx.get(f"{base}/history/{_urlquote(str(symbol), safe='')}", params=params, timeout=timeout)
        if resp.status_code != 200:
            return None
        body = resp.json() or {}
        candles = body.get("candles") or body.get("data") or []
        if not candles:
            return None
        df = pd.DataFrame(candles)
        if "date" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.columns = [str(c).capitalize() for c in df.columns]
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "Close" not in df.columns:
            return None
        df = df.dropna(subset=["Close"]).sort_index()
        return None if df.empty else df
    except Exception as e:  # noqa: BLE001
        logger.debug("market-data daily history %s: %s", symbol, e)
        return None
