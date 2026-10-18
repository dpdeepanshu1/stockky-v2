"""timesfm-forecast-service (group 434): nightly TimesFM-style forecasts turned into trading PARAMETERS (range, volatility ratio, volume pace,
stop-touch probability). Never a buy / sell direction. The entry path only reads cached results (GET /features/{symbol}?cached_only=true), so
no model call happens in the hot path; POST /batch (scripts/tsfm_nightly.py) fills the cache after the end-of-day bars arrive.

Env: TSFM_BACKEND (timesfm25 | bigquery | research-3.0 | fake), TSFM_TRADE_MODE (REAL | DEMO; research-3.0 refuses REAL), TSFM_CACHE_PATH,
TSFM_HORIZON (5), TSFM_MAX_AGE_DAYS (5), TSFM_BATCH_MAX (60), MARKET_DATA_URL, TSFM_HISTORY_TIMEOUT_S, TSFM_PRELOAD.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import backends
import engine
import tsfm_features as tf

logger = logging.getLogger("timesfm-forecast-service")

MARKET_DATA_URL = (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/") or "http://market-data-service:8001"
CACHE_PATH = (os.getenv("TSFM_CACHE_PATH") or "").strip() or "/app/cache/tsfm_cache.json"
HISTORY_TIMEOUT_S = float(os.getenv("TSFM_HISTORY_TIMEOUT_S") or 20)


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


HORIZON = max(1, min(_int_env("TSFM_HORIZON", 5), 20))
MAX_AGE_DAYS = max(1, _int_env("TSFM_MAX_AGE_DAYS", 5))        # same reasoning as STRUCTURE_MAX_AGE_DAYS: Friday's bar is ~3.4 days old on Monday 09:15
BATCH_MAX = max(1, _int_env("TSFM_BATCH_MAX", 60))

app = FastAPI(title="timesfm-forecast-service")
_lock = threading.Lock()                 # one model call at a time: the CPU budget is small
_backend: Any = None
_backend_error: Optional[str] = None
_records: Dict[str, Dict[str, Any]] = {}


def _trade_mode() -> str:
    return ((os.getenv("TSFM_TRADE_MODE") or "").strip().upper()) or "REAL"


def _ist_today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _get_backend() -> Any:
    global _backend, _backend_error
    if _backend is None and _backend_error is None:
        try:
            _backend = backends.get_backend(trade_mode=_trade_mode())
        except backends.BackendError as e:
            _backend_error = str(e)[:300]
            logger.error("tsfm backend unavailable: %s", _backend_error)
    if _backend is None:
        raise HTTPException(status_code=503, detail=_backend_error or "backend unavailable")
    return _backend


def _load_cache() -> None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("records"), dict):
            _records.update(doc["records"])
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("tsfm cache not loaded (%s): starting empty", str(e)[:100])


def _save_cache() -> None:
    try:
        d = os.path.dirname(CACHE_PATH) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tsfm_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "saved_at": time.time(), "records": _records}, fh, separators=(",", ":"))
        os.replace(tmp, CACHE_PATH)                               # atomic: a crash never leaves half a file
    except Exception as e:  # noqa: BLE001
        logger.warning("tsfm cache not saved: %s", str(e)[:100])


def _fetch_candles(symbol: str) -> list:
    sym = symbol.strip().upper().replace(".NS", "")
    r = httpx.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": "2y", "interval": "1d"}, timeout=HISTORY_TIMEOUT_S)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="no history for symbol")
    r.raise_for_status()
    body = r.json()
    return (body.get("candles") or body.get("data") or []) if isinstance(body, dict) else (body if isinstance(body, list) else [])


def _is_fresh(rec: Dict[str, Any]) -> bool:
    try:
        return (_ist_today() - date.fromisoformat(str(rec.get("as_of")))).days <= MAX_AGE_DAYS
    except ValueError:
        return False


def _compute(symbol: str, horizon: int) -> Dict[str, Any]:
    sym = symbol.strip().upper()
    try:
        candles = _fetch_candles(sym)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"history fetch failed: {str(e)[:120]}")
    backend = _get_backend()
    with _lock:
        try:
            rec = engine.build_record(backend, sym, candles, horizon=horizon, today=_ist_today(), trade_mode=_trade_mode())
        except engine.BadHistory as e:
            raise HTTPException(status_code=422, detail=str(e))
        except (tf.FeatureError, backends.BackendError, ValueError) as e:
            raise HTTPException(status_code=422, detail=f"forecast failed: {str(e)[:160]}")
    _records[sym] = rec
    return rec


@app.on_event("startup")
def _startup() -> None:
    _load_cache()
    if (os.getenv("TSFM_PRELOAD") or "").strip().lower() in ("1", "true", "yes"):
        def _preload() -> None:
            try:
                _get_backend()
            except HTTPException:
                pass                                              # the reason is kept in _backend_error and shown by /health
        threading.Thread(target=_preload, daemon=True).start()


@app.get("/health")
def health() -> Dict[str, Any]:
    name = getattr(_backend, "name", None) or (os.getenv("TSFM_BACKEND") or "timesfm25")
    return {"status": "ok", "backend": name, "backend_error": _backend_error, "trade_mode": _trade_mode(), "records": len(_records),
            "horizon": HORIZON, "max_age_days": MAX_AGE_DAYS}


class BatchRequest(BaseModel):
    symbols: List[str] = Field(min_length=1)
    horizon: int = Field(default=HORIZON, ge=1, le=20)
    force: bool = False


@app.post("/batch")
def batch(req: BatchRequest) -> Dict[str, Any]:
    """Forecast each symbol and cache the result. A symbol whose cached record is already for the newest complete bar is skipped (force=true
    recomputes). One failing symbol never stops the rest."""
    syms = [s.strip().upper() for s in req.symbols if s and s.strip()][:BATCH_MAX]
    done, skipped, failed = [], [], []
    for sym in syms:
        try:
            old = _records.get(sym)
            if old is not None and not req.force and old.get("horizon") == req.horizon and _is_fresh(old):
                try:
                    newest = engine.prepare_bars(_fetch_candles(sym), today=_ist_today(), min_bars=1)[-1]["date"]
                except Exception:  # noqa: BLE001
                    newest = None
                if newest == old.get("as_of"):
                    skipped.append(sym)
                    continue
            _compute(sym, req.horizon)
            done.append(sym)
        except HTTPException as e:
            failed.append({"symbol": sym, "status": e.status_code, "error": str(e.detail)[:160]})
        except Exception as e:  # noqa: BLE001
            failed.append({"symbol": sym, "status": 500, "error": str(e)[:160]})
    if done:
        _save_cache()
    return {"requested": len(req.symbols), "processed": len(syms), "done": done, "skipped": skipped, "failed": failed,
            "records": len(_records), "truncated_to": BATCH_MAX if len(req.symbols) > BATCH_MAX else None}


@app.get("/features/{symbol}")
def features(symbol: str, stop_pct: Optional[float] = None, target_pct: Optional[float] = None, cached_only: bool = True) -> Dict[str, Any]:
    """Parameters for one symbol. cached_only=true (the entry path) never runs the model: a missing or stale record is 404."""
    sym = symbol.strip().upper().replace(".NS", "")
    rec = _records.get(sym)
    if (rec is None or not _is_fresh(rec)) and not cached_only:
        rec = _compute(sym, HORIZON)
        _save_cache()
    if rec is None:
        raise HTTPException(status_code=404, detail="no cached forecast for symbol")
    if not _is_fresh(rec):
        raise HTTPException(status_code=404, detail=f"cached forecast is stale (as of {rec.get('as_of')})")
    try:
        return engine.features_from_record(rec, stop_pct, target_pct)
    except tf.FeatureError as e:
        raise HTTPException(status_code=422, detail=str(e))


class ForecastRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    candles: List[Dict[str, Any]] = Field(min_length=engine.MIN_BARS)
    horizon: int = Field(default=HORIZON, ge=1, le=20)
    stop_pct: Optional[float] = Field(default=None, gt=0, le=50)
    target_pct: Optional[float] = Field(default=None, gt=0, le=100)


@app.post("/forecast")
def forecast(req: ForecastRequest) -> Dict[str, Any]:
    """Stateless: parameters for the candles GIVEN (nothing is fetched, cached or stored). The offline probe (scripts/tsfm_probe.py) calls this with
    history truncated at a past decision day, so the answer can only use bars that existed then. Every bar sent is treated as complete."""
    backend = _get_backend()
    with _lock:
        try:
            rec = engine.build_record(backend, req.symbol, req.candles, horizon=req.horizon, today=None, trade_mode=_trade_mode())
            return engine.features_from_record(rec, req.stop_pct, req.target_pct)
        except engine.BadHistory as e:
            raise HTTPException(status_code=422, detail=str(e))
        except (tf.FeatureError, backends.BackendError, ValueError) as e:
            raise HTTPException(status_code=422, detail=f"forecast failed: {str(e)[:160]}")


@app.get("/records")
def records() -> Dict[str, Any]:
    rows = [{"symbol": k, "as_of": v.get("as_of"), "generated_at": (v.get("provenance") or {}).get("generated_at"),
             "backend": (v.get("provenance") or {}).get("backend")} for k, v in sorted(_records.items())]
    return {"count": len(rows), "records": rows}
