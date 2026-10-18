"""kronos-forecast-service - FastAPI worker. POST /forecast -> path statistics from Kronos-small for one symbol (one request at a time).

The model loads lazily on the first forecast (or at start when KRONOS_PRELOAD=true) so /health answers immediately.
Body: {symbol, ref_price, stop_pct, target_pct, [horizon=5], [n_paths=10], [candles]}. Without `candles` the worker fetches daily history from
market-data-service GET /history/{symbol}. With `candles` (the backtest does this) no network is used, and the candles are cut at today's
date only when `as_of_today` is true (live use); a backtest passes candles that already end on the decision day and leaves it false.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import engine
import forecast_stats as fs

logger = logging.getLogger("kronos-forecast-service")
logging.basicConfig(level=logging.INFO)

MARKET_DATA_URL = (os.getenv("MARKET_DATA_URL") or "http://market-data-service:8001").strip().rstrip("/")
LOOKBACK = int(float(os.getenv("KRONOS_LOOKBACK") or 128))
CACHE_TTL_S = float(os.getenv("KRONOS_WORKER_CACHE_TTL_S") or 900)
HISTORY_TIMEOUT_S = float(os.getenv("KRONOS_HISTORY_TIMEOUT_S") or 20)

app = FastAPI(title="kronos-forecast-service")
_lock = threading.Lock()                       # one forecast at a time: the CPU budget is small
_forecaster: Any = None
_cache: Dict[str, tuple] = {}


class ForecastRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    ref_price: float = Field(gt=0)
    stop_pct: float = Field(gt=0, le=50)
    target_pct: float = Field(gt=0, le=100)
    horizon: int = Field(default=5, ge=1, le=20)
    n_paths: int = Field(default=10, ge=2, le=50)
    candles: Optional[List[Dict[str, Any]]] = None
    as_of_today: bool = True


def _get_forecaster():
    global _forecaster
    if _forecaster is None:
        from forecaster import KronosForecaster      # imported late: torch is heavy and /health must not wait for it
        _forecaster = KronosForecaster()
    return _forecaster


def _ist_today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _fetch_candles(symbol: str) -> list:
    sym = symbol.strip().upper().replace(".NS", "")
    r = httpx.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": "1y", "interval": "1d"}, timeout=HISTORY_TIMEOUT_S)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="no history for symbol")
    r.raise_for_status()
    body = r.json()
    return (body.get("candles") or body.get("data") or []) if isinstance(body, dict) else (body if isinstance(body, list) else [])


@app.on_event("startup")
def _startup() -> None:
    if (os.getenv("KRONOS_PRELOAD") or "").strip().lower() in ("1", "true", "yes"):
        threading.Thread(target=lambda: (_lock.acquire(), _get_forecaster(), _lock.release()), daemon=True).start()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "model_loaded": _forecaster is not None}


@app.post("/forecast")
def forecast(req: ForecastRequest) -> Dict[str, Any]:
    key = f"{req.symbol.upper()}|{round(req.ref_price, 1)}|{req.stop_pct}|{req.target_pct}|{req.horizon}|{req.n_paths}"
    now = time.monotonic()
    if req.candles is None:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return {**hit[1], "cached": True}
    try:
        candles = req.candles if req.candles is not None else _fetch_candles(req.symbol)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"history fetch failed: {str(e)[:120]}")
    with _lock:
        try:
            out = engine.run_forecast(_get_forecaster(), candles, req.ref_price, req.stop_pct, req.target_pct, horizon=req.horizon,
                                      n_paths=req.n_paths, lookback=LOOKBACK, today=_ist_today() if req.as_of_today else None, symbol=req.symbol)
        except fs.BadHistory as e:
            raise HTTPException(status_code=422, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if req.candles is None:
        _cache[key] = (now + CACHE_TTL_S, out)
        if len(_cache) > 500:
            for k in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(k, None)
    return {**out, "cached": False}
