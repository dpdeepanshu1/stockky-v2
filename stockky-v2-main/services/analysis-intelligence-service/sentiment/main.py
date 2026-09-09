"""
Market Sentiment Service
Responsibility: Fetch Indian index data (NIFTY 50, SENSEX) and compute a
normalized market sentiment score and classification.

v0.6.0 – more precise scoring: sensitive to small daily moves, uses momentum and volatility.
"""
import os
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, List
import asyncio

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Set User-Agent for yfinance requests
import requests
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})
try:
    yf.set_session(session)
except AttributeError:
    try:
        yf.shared._session = session
    except AttributeError:
        pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-sentiment-service")

# --- Configuration ---
INDEX_SYMBOLS: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
}

# Weights for the two indices
INDEX_WEIGHTS = {
    "NIFTY 50": 0.60,
    "SENSEX": 0.40,
}

# --- In-memory cache ---
_cache: Dict[str, Any] = {
    "data": None,
    "timestamp": None,
    "ttl_seconds": 300,  # 5 minutes
    "lock": asyncio.Lock(),
}

app = FastAPI(title="Stockky Market Sentiment Service", version="0.6.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class IndexData(BaseModel):
    symbol: str
    name: str
    current: Optional[float]
    previous_close: Optional[float]
    change: Optional[float]
    change_percent: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[int]
    timestamp: datetime

class MarketSentimentResponse(BaseModel):
    timestamp: datetime
    indices: Dict[str, IndexData]
    market_score: int  # 0-100
    classification: str
    trend: Optional[str]
    momentum: Optional[str]
    breadth: Optional[str]
    volatility: Optional[str]
    cached: bool = False
    stale: bool = False

# --- Helper Functions ---
def _safe_float(val):
    try:
        f = float(val)
        if np.isnan(f) or not np.isfinite(f):
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return None

def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def fetch_individual_ticker(symbol: str, name: str, max_retries=3) -> Optional[IndexData]:
    """Fetch a single ticker using Ticker.history as a fallback."""
    for attempt in range(max_retries):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")
            if len(hist) >= 2:
                current = _safe_float(hist['Close'].iloc[-1])
                prev_close = _safe_float(hist['Close'].iloc[-2])
                change = _safe_float(current - prev_close) if current and prev_close else None
                change_pct = _safe_float((change / prev_close) * 100) if change and prev_close else None
                high = _safe_float(hist['High'].iloc[-1])
                low = _safe_float(hist['Low'].iloc[-1])
                volume = _safe_int(hist['Volume'].iloc[-1])
                return IndexData(
                    symbol=symbol,
                    name=name,
                    current=current,
                    previous_close=prev_close,
                    change=change,
                    change_percent=change_pct,
                    high=high,
                    low=low,
                    volume=volume,
                    timestamp=datetime.now()
                )
            else:
                logger.warning(f"Insufficient history for {name} ({symbol})")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + 1
                logger.warning(f"Individual fetch for {name} attempt {attempt+1} failed: {e}, retrying in {wait}s")
                time.sleep(wait)
                continue
            else:
                logger.error(f"Individual fetch for {name} failed after {max_retries} retries: {e}")
                return None
    return None

def fetch_indices_batch(symbols: Dict[str, str]) -> Dict[str, IndexData]:
    """Fetch all indices in a single batch using yf.download; fallback to individual if batch fails."""
    result = {}
    if not symbols:
        return result

    yf_symbols = list(symbols.values())
    max_retries = 2
    data = None
    for attempt in range(max_retries):
        try:
            data = yf.download(
                tickers=yf_symbols,
                period="7d",  # need at least 5 days for momentum
                interval="1d",
                group_by='ticker',
                auto_adjust=True,
                threads=False,
                progress=False
            )
            if data is not None and not data.empty:
                logger.info(f"Batch download success: {len(data)} tickers")
                break
            else:
                logger.warning(f"Batch download returned empty data on attempt {attempt+1}")
        except Exception as e:
            logger.warning(f"Batch download attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + 1
                time.sleep(wait)
                continue
            else:
                logger.error("Batch download failed, falling back to individual fetches")
                for name, sym in symbols.items():
                    data = fetch_individual_ticker(sym, name)
                    if data:
                        result[name] = data
                    else:
                        logger.warning(f"Individual fetch also failed for {name}")
                return result

    if data is None or data.empty:
        for name, sym in symbols.items():
            data = fetch_individual_ticker(sym, name)
            if data:
                result[name] = data
            else:
                logger.warning(f"Individual fetch failed for {name}")
        return result

    # Process batch data – also compute 5-day change if available
    for name, sym in symbols.items():
        try:
            if sym not in data.columns or data[sym].empty:
                logger.warning(f"No data for {name} ({sym})")
                ind_data = fetch_individual_ticker(sym, name)
                if ind_data:
                    result[name] = ind_data
                continue
            df = data[sym]
            if len(df) >= 2:
                current = _safe_float(df['Close'].iloc[-1])
                prev_close = _safe_float(df['Close'].iloc[-2])
                change = _safe_float(current - prev_close) if current and prev_close else None
                change_pct = _safe_float((change / prev_close) * 100) if change and prev_close else None
                high = _safe_float(df['High'].iloc[-1])
                low = _safe_float(df['Low'].iloc[-1])
                volume = _safe_int(df['Volume'].iloc[-1])
                result[name] = IndexData(
                    symbol=sym,
                    name=name,
                    current=current,
                    previous_close=prev_close,
                    change=change,
                    change_percent=change_pct,
                    high=high,
                    low=low,
                    volume=volume,
                    timestamp=datetime.now()
                )
            else:
                logger.warning(f"Insufficient data for {name} ({sym})")
                ind_data = fetch_individual_ticker(sym, name)
                if ind_data:
                    result[name] = ind_data
        except Exception as e:
            logger.error(f"Error processing {name} ({sym}): {e}")
            ind_data = fetch_individual_ticker(sym, name)
            if ind_data:
                result[name] = ind_data

    return result

def compute_market_score(indices_data: Dict[str, IndexData]) -> int:
    """
    Compute a more precise market sentiment score (0-100) based on:
    - Weighted daily change of NIFTY 50 and SENSEX.
    - 5-day momentum adjustment.
    - Volatility normalisation (if available).
    """
    if not indices_data:
        return 50

    # 1. Weighted average of daily change percent
    weighted_change = 0.0
    total_weight = 0.0
    for name, data in indices_data.items():
        if name in INDEX_WEIGHTS and data.change_percent is not None:
            w = INDEX_WEIGHTS[name]
            weighted_change += w * data.change_percent
            total_weight += w
    if total_weight == 0:
        return 50
    avg_change = weighted_change / total_weight

    # 2. Map daily change to score: -0.2% -> 0, 0% -> 50, +0.2% -> 100
    sensitivity = 0.002  # 0.2%
    raw_score = 50 + (avg_change / sensitivity) * 50
    score = min(100, max(0, raw_score))

    # 3. Momentum adjustment (5-day change)
    try:
        nifty = yf.Ticker("^NSEI")
        hist = nifty.history(period="6d")
        if len(hist) >= 6:
            close_5d_ago = hist['Close'].iloc[-6]
            close_today = hist['Close'].iloc[-1]
            change_5d = (close_today - close_5d_ago) / close_5d_ago * 100
            momentum_adjust = max(-5, min(5, change_5d * 2))
            score = score + momentum_adjust
    except Exception as e:
        logger.debug(f"Could not compute 5-day momentum: {e}")

    # 4. Volatility normalisation
    try:
        nifty = yf.Ticker("^NSEI")
        hist = nifty.history(period="1mo")
        if len(hist) > 14:
            atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
            price = hist['Close'].iloc[-1]
            if price and atr:
                vol_ratio = atr / price * 100
                vol_adjust = max(-10, min(5, 10 - (vol_ratio - 0.5) * 5))
                score = score + vol_adjust
    except Exception as e:
        logger.debug(f"Could not compute volatility adjustment: {e}")

    final_score = int(round(max(0, min(100, score))))
    return final_score

def classify_sentiment(score: int) -> str:
    if score >= 75:
        return "STRONGLY BULLISH"
    elif score >= 55:
        return "BULLISH"
    elif score >= 45:
        return "NEUTRAL"
    elif score >= 25:
        return "BEARISH"
    else:
        return "STRONGLY BEARISH"

# --- API Endpoints ---
@app.get("/sentiment", response_model=MarketSentimentResponse)
async def get_market_sentiment(force_refresh: bool = False):
    now = datetime.now()

    if not force_refresh and _cache["data"] is not None:
        cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
        if cache_age < _cache["ttl_seconds"]:
            logger.info("Returning cached market sentiment")
            cached_response = _cache["data"].copy()
            cached_response["cached"] = True
            return MarketSentimentResponse(**cached_response)

    async with _cache["lock"]:
        if not force_refresh and _cache["data"] is not None:
            cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
            if cache_age < _cache["ttl_seconds"]:
                cached_response = _cache["data"].copy()
                cached_response["cached"] = True
                return MarketSentimentResponse(**cached_response)

        logger.info("Fetching fresh market sentiment data")
        indices_data = fetch_indices_batch(INDEX_SYMBOLS)

        if not indices_data:
            if _cache["data"] is not None:
                logger.warning("No fresh data, returning stale cache")
                stale_response = _cache["data"].copy()
                stale_response["cached"] = True
                stale_response["stale"] = True
                return MarketSentimentResponse(**stale_response)
            else:
                logger.error("No index data available, returning neutral fallback")
                fallback = {
                    "timestamp": now,
                    "indices": {},
                    "market_score": 50,
                    "classification": "NEUTRAL",
                    "trend": "Neutral",
                    "momentum": "Moderate",
                    "breadth": "Mixed",
                    "volatility": "Normal",
                    "cached": False,
                    "stale": True,
                }
                _cache["data"] = fallback
                _cache["timestamp"] = now
                return MarketSentimentResponse(**fallback)

        score = compute_market_score(indices_data)
        classification = classify_sentiment(score)

        trend = "Bullish" if score > 55 else "Bearish" if score < 45 else "Neutral"
        momentum = "Strong" if score > 65 else "Weak" if score < 35 else "Moderate"
        up_count = sum(1 for d in indices_data.values() if d.change and d.change > 0)
        breadth_pct = (up_count / len(indices_data)) * 100 if indices_data else 50
        breadth = "Positive" if breadth_pct > 60 else "Negative" if breadth_pct < 40 else "Mixed"
        volatility = "Normal" if 35 < score < 65 else "High"

        response_data = {
            "timestamp": now,
            "indices": indices_data,
            "market_score": score,
            "classification": classification,
            "trend": trend,
            "momentum": momentum,
            "breadth": breadth,
            "volatility": volatility,
            "cached": False,
            "stale": False,
        }

        _cache["data"] = response_data
        _cache["timestamp"] = now

        return MarketSentimentResponse(**response_data)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "market-sentiment-service"}

@app.get("/")
async def root():
    return {
        "service": "Stockky Market Sentiment Service",
        "version": "0.6.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/sentiment": "GET – current market sentiment (cached 300s)",
            "/sentiment?force_refresh=true": "GET – force refresh",
        },
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8009))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=True)