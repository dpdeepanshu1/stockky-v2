"""
API Gateway
------------
Single entry point for the React frontend.
v2.5.16 – uses IST for fetched_at timestamp (hh:mm:ss AM/PM).
"""
import os
import json
import time
import asyncio
import gc
import logging
import difflib
import uuid
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Optional, Set, Dict, Union

import httpx
import yfinance as yf
import feedparser
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from upstash_redis import Redis
from data_feed import (
    DataFeedStore, extract_feed_payload, DATA_FEED_TTL,
    hot_job_get, hot_job_set, HOT_RESULT_KEY,
    try_refresh_lock, release_refresh_lock, soft_ttl_should_refresh,
)
from circuit_breaker import get_breaker, CircuitOpenError, all_snapshots
from metrics import metrics
from rate_limit_monitor import monitor as rate_limit_monitor
try:
    import kv_cache as _kv_cache
except Exception:
    _kv_cache = None  # type: ignore

try:
    import qstash_client
except Exception:
    qstash_client = None  # type: ignore
try:
    from json_safe import sanitize as _json_sanitize
except Exception:
    def _json_sanitize(x):
        return x

def _safe_json_response(content, status_code=200):
    from fastapi.responses import JSONResponse
    return JSONResponse(content=_json_sanitize(content), status_code=status_code)

from redis_rate_limit import limiter as redis_limiter
from batch_worker import run_in_batches, default_batch_size
from nse_holidays import is_nse_holiday

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

# ---- IST timezone ----
IST = ZoneInfo("Asia/Kolkata")

# ---- Service URLs (env-driven; defaults match config/service_urls.py) ----
_DP = os.getenv("DECISION_PREDICTION_URL", "https://decision-prediction-service.onrender.com")
_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
_NS = os.getenv("NOTIFICATION_SCHEDULER_URL", "https://notification-scheduler-service-x8vc.onrender.com/notification")

DECISION_URL = os.getenv("DECISION_URL", f"{_DP.rstrip('/')}/decision")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", _NS if _NS.rstrip('/').endswith('notification') else f"{_NS.rstrip('/')}/notification")
NEWS_URL = os.getenv("NEWS_URL", f"{_AI.rstrip('/')}/news")
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI.rstrip('/')}/technical")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_AI.rstrip('/')}/fundamental")
EVENT_URL = os.getenv("EVENT_URL", f"{_AI.rstrip('/')}/event")
PREDICTION_URL = os.getenv("PREDICTION_URL", f"{_DP.rstrip('/')}/prediction")

# ---- Market Sentiment & Training ----
MARKET_SENTIMENT_URL = os.getenv("MARKET_SENTIMENT_URL", f"{_AI.rstrip('/')}/sentiment")
TRAINING_URL = os.getenv("TRAINING_URL", f"{_DP.rstrip('/')}/training")

# Service definitions for system health
SYSTEM_SERVICES = {
    "market-data": {"url": MARKET_DATA_URL, "required": True},
    "technical-analysis": {"url": TECHNICAL_URL, "required": True},
    "fundamental-analysis": {"url": FUNDAMENTAL_URL, "required": True},
    "decision-engine": {"url": DECISION_URL, "required": True},
    "news-intelligence": {"url": NEWS_URL, "required": False},
    "event-tracker": {"url": EVENT_URL, "required": False},
    "prediction": {"url": PREDICTION_URL, "required": False},
    "notification": {"url": NOTIFICATION_URL, "required": False},
    "market-sentiment": {"url": MARKET_SENTIMENT_URL, "required": False},
    "training": {"url": TRAINING_URL, "required": False},
}

app = FastAPI(title="Stockky API Gateway", version="2.5.16")

# ── Shared HTTP client (persistent TLS pool across downstream services) ──
_HTTP_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)
_HTTP_TIMEOUT = httpx.Timeout(90.0, connect=8.0)  # free-tier cold starts; per-call overrides still apply
_HTTP_TIMEOUT_LONG = httpx.Timeout(120.0, connect=10.0)
_shared_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        return _shared_http_client
    # Lazy create if startup has not run yet
    _shared_http_client = httpx.AsyncClient(
        limits=_HTTP_LIMITS, timeout=_HTTP_TIMEOUT, follow_redirects=True
    )
    return _shared_http_client


@app.on_event("startup")
async def _start_shared_http():
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            limits=_HTTP_LIMITS, timeout=_HTTP_TIMEOUT, follow_redirects=True
        )
        logger.info("Shared httpx.AsyncClient started (keepalive=20, max=50, connect=2s)")
    try:
        redis_limiter.set_redis(_redis)
    except Exception:
        pass


@app.on_event("shutdown")
async def _stop_shared_http():
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        await _shared_http_client.aclose()
        logger.info("Shared httpx.AsyncClient closed")
        _shared_http_client = None


# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_cors_header(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

@app.exception_handler(Exception)
async def universal_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*"
        }
    )

# ── Redis (OFF by default — USE_REDIS=1 to enable Upstash) ─────────────────────
_redis = None
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
try:
    if _USE_REDIS and os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        _redis = Redis(
            url=os.getenv("UPSTASH_REDIS_REST_URL"),
            token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
        )
        _redis.ping()
        logger.info("Connected to Upstash Redis (USE_REDIS=1)")
    else:
        logger.info("Gateway Redis disabled (USE_REDIS=0) — in-memory scan/status cache")
except Exception as e:
    logger.warning("Redis unavailable: %s", e)
    _redis = None

# In-memory fallback so scan status / universe work without Redis
_mem_kv = {}
_mem_kv_exp = {}
import time as _time_mod


WATCHLIST_KEY       = "stockky:watchlist"
SEARCHED_KEY        = "stockky:searched_symbols"
SCAN_UNIVERSE_KEY   = "stockky:scan_universe"
IPO_CACHE_KEY       = "stockky:ipos:recent"
KNOWN_SYMBOLS_KEY   = "stockky:known_symbols"
SCAN_TASK_PREFIX    = "stockky:scan_task:"
_SCAN_CANCEL_FLAGS: set = set()  # process-local instant cancel

MARKET_MOVERS_CACHE_PREFIX = "stockky:market_movers:"
INDICES_CACHE_KEY   = "stockky:indices"
INDICES_LAST_KNOWN  = "stockky:indices_last_known"

FUNDAMENTAL_CACHE_PREFIX = "stockky:fundamental:"
EVENT_CACHE_PREFIX = "stockky:event:"
NEWS_CACHE_PREFIX = "stockky:news:"
# Slow-changing layers: 24h. Nightly cron refreshes them after midnight IST.
STATIC_PARAM_TTL = int(os.getenv("STATIC_PARAM_TTL", "86400"))  # 24 hours
LAST_FULL_SCAN_KEY = "stockky:last_full_scan"
LAST_FULL_SCAN_TTL = int(os.getenv("LAST_FULL_SCAN_TTL", "86400"))  # 24h — survive refresh / stop partial
DECIDE_CACHE_PREFIX = "stockky:decide_cache:"
DECIDE_CACHE_TTL_OPEN = int(os.getenv("DECIDE_CACHE_TTL_OPEN", "300"))   # 5 min market open
DECIDE_CACHE_TTL_CLOSED = int(os.getenv("DECIDE_CACHE_TTL_CLOSED", "21600"))  # 6 h closed
BATCH_RESULT_CACHE_PREFIX = "stockky:batch_result:"
BATCH_RESULT_CACHE_ENABLED = os.getenv("BATCH_RESULT_CACHE", "true").lower() in ("1", "true", "yes")
SCAN_LITE_DEFAULT = os.getenv("SCAN_LITE_DEFAULT", "false").lower() in ("1", "true", "yes")
WAKE_BEFORE_SCAN = os.getenv("WAKE_BEFORE_SCAN", "true").lower() in ("1", "true", "yes")
WAKE_WAIT_SECONDS = float(os.getenv("WAKE_WAIT_SECONDS", "12"))

# ── Symbol Aliases ──────────────────────────────────────────────────────────
SYMBOL_ALIASES: Dict[str, Union[str, List[str]]] = {
    "TATAMOTORS": "TMPV",
    "TATAMOTER": "TMPV",
    "TATAMOT": "TMPV",
    "LTIM": "LTM",
    "LTIMIND": "LTM",
    "LTIMINDTREE": "LTM",
    "ZOMATO": "ETERNAL",
    "ZOMAT": "ETERNAL",
}
EXTRA_NEW_SYMBOLS = ["TMPV", "TMLCV", "LTM", "ETERNAL"]

# ── Redis helpers ─────────────────────────────────────────────────────────
_data_feed_store = None

def _feed_store() -> DataFeedStore:
    global _data_feed_store
    if _data_feed_store is None:
        _data_feed_store = DataFeedStore(_redis_get, _redis_set, _redis)
    return _data_feed_store


def _redis_get(key: str):
    """Memory + optional Neon durable (kv_cache). Never requires Upstash."""
    if _kv_cache is not None:
        try:
            return _kv_cache.get(key)
        except Exception as e:
            logger.debug("kv get %s: %s", key, e)
    # legacy mem fallback
    try:
        exp = _mem_kv_exp.get(key)
        if exp is not None and _time_mod.time() > exp:
            _mem_kv.pop(key, None)
            _mem_kv_exp.pop(key, None)
            return None
        if key in _mem_kv:
            return _mem_kv[key]
    except Exception:
        pass
    if _redis is not None:
        try:
            raw = _redis.get(key)
            if raw is None:
                return None
            if isinstance(raw, (bytes, str)):
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
            return raw
        except Exception:
            pass
    return None


def _redis_soft_ttl_refresh(key: str, soft_window: int = 10) -> bool:
    """True if key exists and TTL is in (0, soft_window] — caller should refresh in background."""
    if not _redis:
        return False
    try:
        ttl = _redis.ttl(key)
        return isinstance(ttl, int) and 0 < ttl <= soft_window
    except Exception:
        return False

def _redis_set(key: str, value, ttl: int = None):
    """Memory always; Neon for durable prefixes; Redis only if USE_REDIS=1."""
    if _kv_cache is not None:
        try:
            _kv_cache.set(key, value, ttl=ttl)
            return
        except Exception as e:
            logger.debug("kv set %s: %s", key, e)
    try:
        _mem_kv[key] = value
        if ttl:
            _mem_kv_exp[key] = _time_mod.time() + int(ttl)
        else:
            _mem_kv_exp.pop(key, None)
        # soft cap
        if len(_mem_kv) > 8000:
            for k in list(_mem_kv.keys())[:500]:
                _mem_kv.pop(k, None)
                _mem_kv_exp.pop(k, None)
    except Exception:
        pass
    if _redis is not None:
        try:
            payload = json.dumps(value, default=str) if not isinstance(value, str) else value
            if ttl:
                _redis.setex(key, int(ttl), payload)
            else:
                _redis.set(key, payload)
        except Exception:
            pass


def _load_watchlist() -> List[str]:
    return _redis_get(WATCHLIST_KEY) or []

def _save_watchlist(symbols: List[str]):
    _redis_set(WATCHLIST_KEY, symbols)

def _load_searched() -> List[str]:
    return _redis_get(SEARCHED_KEY) or []

def _add_searched(symbol: str):
    searched = _load_searched()
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    if sym not in searched:
        searched.append(sym)
        _redis_set(SEARCHED_KEY, searched[-200:])

# ── Dynamic Universe Sources ──────────────────────────────────────────────────
_nse_client = None

def _get_nse_client() -> httpx.Client:
    global _nse_client
    if _nse_client is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
            "DNT": "1",
        }
        _nse_client = httpx.Client(headers=headers, timeout=15)
        _nse_client.get("https://www.nseindia.com")
    return _nse_client

def _fetch_from_nse_api(endpoint: str, cache_key: str, ttl: int = 21600):
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        client = _get_nse_client()
        url = f"https://www.nseindia.com/api/{endpoint}"
        resp = client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _redis_set(cache_key, data, ttl)
                return data
        else:
            logger.warning(f"NSE API {endpoint} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Failed to fetch {endpoint}: {e}")
    if cached:
        return cached
    return None

def _get_all_nse_securities() -> List[str]:
    data = _fetch_from_nse_api("equity-stock-indices?index=SECURITIES%20IN%20NSE", "nse:all_securities")
    symbols = []
    if data and "data" in data and isinstance(data["data"], list):
        for item in data["data"]:
            if isinstance(item, dict) and item.get("symbol"):
                symbols.append(item["symbol"].upper())
    logger.info(f"Fetched {len(symbols)} securities from NSE")
    if not symbols:
        symbols = [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH",
            "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "M&M", "MARUTI",
            "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "SBILIFE", "SUNPHARMA",
            "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "ADANIPORTS",
            "ASIANPAINT", "AXISBANK", "BAJAJFINSV", "BRITANNIA", "CIPLA",
            "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
            "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "INDUSINDBK",
            "JSWSTEEL", "LTIM", "SHRIRAMFIN", "TATACONSUM", "TRENT", "TITAN",
            "ULTRACEMCO", "BAJAJ-AUTO", "BPCL", "APOLLOHOSP", "BAJFINANCE",
            "BANDHANBNK", "BIOCON", "BOSCHLTD", "CHOLAFIN", "DABUR", "DALBHARAT",
            "DIXON", "DMART", "ESCORTS", "FEDERALBNK", "GODREJCP", "GODREJPROP",
            "HAVELLS", "HINDZINC", "IOC", "IRCTC", "LICHSGFIN", "MUTHOOTFIN",
            "NAUKRI", "NMDC", "PAGEIND", "PETRONET", "PIIND", "PNB", "RBLBANK",
            "SAIL", "SRTRANSFIN", "TATACOMM", "TECHM", "TORNTPHARM", "VEDL",
            "ZOMATO", "IDEA", "ABFRL", "BANKBARODA", "BHEL", "CANBK", "HAL",
            "IBULHSGFIN", "JINDALSTEL", "JUBLFOOD", "MCDOWELL-N", "MPHASIS",
            "PIDILITIND", "SIEMENS", "UPL", "VBL", "YESBANK", "GAIL",
            "AARTIIND", "ABB", "ADANIGREEN", "ADANITRANS", "ALKEM", "AMBER",
            "ASHOKLEY", "ASTRAZEN", "AUROPHARMA", "BALKRISIND", "BERGEPAINT",
            "BLUESTARCO", "CARBORUNIV", "CENTRALBK", "CGPOWER", "CISCO", "COCHINSHIP",
            "COROMANDEL", "CROMPTON", "CUMMINSIND", "DELTACORP", "DIVISLAB",
            "DLF", "EIDPARRY", "EXIDEIND", "FORTIS", "GMRINFRA", "GODREJIND",
            "GREENPLY", "HINDPETRO", "IDEA", "INDIAMART", "INDIGO", "JSWENERGY",
            "JUBILANT", "KPITTECH", "KPRMILL", "LALPATHLAB", "LUPIN", "MCX",
            "MINDACORP", "MOTHERSUMI", "NATCOPHARM", "NAVINFLUOR", "NEULANDLAB",
            "NILKAMAL", "NLCINDIA", "OIL", "PERSISTENT", "PFC", "PHOENIXLTD",
            "PRESTIGE", "RAYMOND", "RECLTD", "RENUKA", "RITES", "RVNL",
            "SCHAEFFLER", "SHREECEM", "SONATSOFTW", "SUNTV", "SUPRAJIT",
            "SYRMA", "TATAELXSI", "TATAMTRDVR", "TATAPOWER", "TATATECH",
            "TIMKEN", "TORNTPHARM", "TRIDENT", "TVSMOTOR", "WELSPUNIND", "WHIRLPOOL",
            "WOCKPHARMA", "ZEEL", "ZYDUSWELL"
        ]
        logger.warning(f"Using enhanced static fallback list with {len(symbols)} symbols")
    return symbols

def _get_nifty_indices() -> List[str]:
    indices = [
        "NIFTY%2050",
        "NIFTY%20NEXT%2050",
        "NIFTY%20MIDCAP%20100",
        "NIFTY%20MIDCAP%20150",
        "NIFTY%20SMALLCAP%20100",
    ]
    all_symbols = []
    for idx in indices:
        data = _fetch_from_nse_api(f"equity-stock-indices?index={idx}", f"nse:index_{idx}")
        if data and "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, dict) and item.get("symbol"):
                    all_symbols.append(item["symbol"].upper())
    fallback = [
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
        "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL",
        "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
        "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
        "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
        "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
        "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
        "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
        "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
        "TCS", "TRENT", "TITAN", "ULTRACEMCO", "WIPRO"
    ]
    all_symbols = list(set(all_symbols + fallback))
    return all_symbols

def _get_recent_ipos() -> List[str]:
    """Recent/past IPOs via public-past-issues (last 12 months, dynamic dates)."""
    to_d = datetime.now(IST)
    from_d = to_d - timedelta(days=365)
    endpoint = (
        "public-past-issues"
        f"?from_date={from_d.strftime('%d-%m-%Y')}"
        f"&to_date={to_d.strftime('%d-%m-%Y')}"
    )
    data = _fetch_from_nse_api(endpoint, IPO_CACHE_KEY, ttl=86400)
    symbols = []
    # NSE may return a list or {data: [...]}
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict):
                sym = item.get("symbol") or item.get("secCode") or item.get("htmSym")
                if sym:
                    symbols.append(str(sym).upper())
    # Also merge currently open issues
    try:
        cur = _fetch_from_nse_api("ipo-current-issue", "nse:ipo_current", ttl=3600)
        cur_rows = cur if isinstance(cur, list) else (cur.get("data") if isinstance(cur, dict) else [])
        if isinstance(cur_rows, list):
            for item in cur_rows:
                if isinstance(item, dict):
                    sym = item.get("symbol") or item.get("secCode")
                    if sym:
                        symbols.append(str(sym).upper())
    except Exception:
        pass
    symbols = list(dict.fromkeys(symbols))  # dedupe preserve order
    if not symbols:
        symbols = ["JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO", "RAILTEL", "IRFC", "MVELECTRO"]
    return symbols

def _get_momentum_movers() -> List[str]:
    """Real-time movers: NSE gainers/losers/most-active + ≥5% day/week moves."""
    movers: set[str] = set()

    # 1) NSE live boards (best free real-time source when reachable)
    for endpoint, key in (
        ("live-analysis-variations?index=gainers", "nse:gainers"),
        ("live-analysis-variations?index=losers", "nse:losers"),
        ("live-analysis-variations?index=volume-gainers", "nse:vol_gainers"),
        ("equity-stock-indices?index=NIFTY%20500", "nse:nifty500_idx"),
    ):
        try:
            data = _fetch_from_nse_api(endpoint, key, ttl=900)
            rows = []
            if isinstance(data, dict):
                rows = data.get("data") or data.get("NIFTY") or []
                if isinstance(rows, dict):
                    rows = rows.get("data") or []
            if isinstance(rows, list):
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    sym = (item.get("symbol") or item.get("symbolName") or "").upper()
                    if not sym or sym in ("NIFTY", "NIFTY50", "-"):
                        continue
                    # Prefer names with meaningful move when field present
                    chg = item.get("pChange") or item.get("perChange") or item.get("change_pct")
                    try:
                        chg_f = float(chg) if chg is not None else None
                    except (TypeError, ValueError):
                        chg_f = None
                    if chg_f is None or abs(chg_f) >= 2.0:
                        movers.add(sym.replace("&", "").replace("-", "") if False else sym)
        except Exception as e:
            logger.debug("NSE movers %s: %s", endpoint, e)

    # 2) Market-data service (already warm on gateway)
    try:
        base = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
        for path in ("/market/top-gainers", "/market/top-losers", "/market/most-active"):
            try:
                r = httpx.get(f"{base}{path}", timeout=8)
                if r.status_code != 200:
                    continue
                payload = r.json()
                rows = payload if isinstance(payload, list) else payload.get("data") or payload.get("items") or []
                for item in rows or []:
                    if isinstance(item, dict):
                        sym = (item.get("symbol") or item.get("ticker") or "").upper().replace(".NS", "")
                        if sym:
                            movers.add(sym)
                    elif isinstance(item, str) and item.isalpha():
                        movers.add(item.upper())
            except Exception:
                continue
    except Exception as e:
        logger.debug("market-data movers: %s", e)

    # 3) Gateway's own /market endpoints (yfinance-backed) when available
    try:
        data = _get_nifty50_data() if "_get_nifty50_data" in dir() else []
        for row in data or []:
            if not isinstance(row, dict):
                continue
            sym = (row.get("symbol") or "").upper()
            chg = row.get("change_pct") or row.get("pChange")
            try:
                if sym and chg is not None and abs(float(chg)) >= 3.0:
                    movers.add(sym)
            except (TypeError, ValueError):
                if sym:
                    movers.add(sym)
    except Exception:
        pass

    # 4) Targeted yfinance 1d ≥5% on a liquid seed (limit calls for free tier)
    if len(movers) < 40:
        seed = list(dict.fromkeys(_get_nifty_indices() + _get_all_nse_securities()[:120]))[:80]
        for sym in seed:
            try:
                hist = yf.Ticker(f"{sym}.NS").history(period="5d", interval="1d")
                if hist is None or hist.empty or len(hist) < 2:
                    continue
                day_chg = (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[-2])) / float(hist["Close"].iloc[-2]) * 100
                week_chg = (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])) / float(hist["Close"].iloc[0]) * 100
                if abs(day_chg) >= 5.0 or abs(week_chg) >= 5.0:
                    movers.add(sym)
            except Exception:
                continue

    out = sorted(movers)
    logger.info("Momentum movers collected: %s symbols", len(out))
    return out

def _get_news_mentioned_symbols() -> List[str]:
    """Symbols appearing in fresh market news (results, bulk deals, upgrades)."""
    mentioned: list[str] = []
    queries = [
        "NSE+stock+results+earnings",
        "NSE+bulk+deal+OR+block+deal",
        "NSE+mutual+fund+stake+OR+FII+buying",
        "BSE+stock+surge+OR+rally+OR+jumps",
        "NSE+order+win+OR+contract+OR+acquisition",
    ]
    text_parts: list[str] = []
    try:
        for q in queries:
            try:
                feed = feedparser.parse(
                    f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
                )
                for e in (feed.entries or [])[:20]:
                    text_parts.append(getattr(e, "title", "") or "")
                    text_parts.append(getattr(e, "summary", "") or "")
            except Exception:
                continue
        text = " ".join(text_parts).upper()
        # Prefer longer tickers first to avoid short false positives (e.g. ITC inside words is ok as whole token)
        candidates = sorted(set(_get_all_nse_securities()[:400] + _get_nifty_indices()), key=len, reverse=True)
        for sym in candidates:
            if len(sym) < 2:
                continue
            # word-ish match: symbol as standalone token
            if f" {sym} " in f" {text} " or text.startswith(sym + " ") or text.endswith(" " + sym):
                mentioned.append(sym)
            elif sym in text and len(sym) >= 4:
                mentioned.append(sym)
    except Exception as e:
        logger.warning("Could not parse news for symbols: %s", e)
    # de-dupe preserve order
    seen = set()
    out = []
    for s in mentioned:
        if s not in seen:
            seen.add(s)
            out.append(s)
    logger.info("News-mentioned symbols: %s", len(out))
    return out[:60]

def _get_event_symbols() -> List[str]:
    """Symbols with upcoming/recent corporate events, bulk deals, insider activity."""
    symbols: list[str] = []
    try:
        resp = httpx.get(f"{EVENT_URL}/symbols_with_events", timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                symbols.extend(data.get("symbols") or [])
            elif isinstance(data, list):
                symbols.extend(data)
    except Exception as e:
        logger.warning(f"Could not fetch event symbols: {e}")

    # Extra: analysis-intelligence categorized bulk/insider from a seed set is too heavy;
    # rely on event service + news. Also try market-data corporate actions if exposed.
    try:
        base = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
        r = httpx.get(f"{base}/events/active-symbols", timeout=8)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                symbols.extend(data.get("symbols") or [])
            elif isinstance(data, list):
                symbols.extend([str(x) for x in data])
    except Exception:
        pass

    out = []
    seen = set()
    for s in symbols:
        su = str(s).upper().replace(".NS", "").replace(".BO", "")
        if su and su not in seen:
            seen.add(su)
            out.append(su)
    logger.info("Event-driven symbols: %s", len(out))
    return out[:80]

# ── Build scan universe ──────────────────────────────────────────────────────
def _build_scan_universe() -> List[str]:
    cached = _redis_get(SCAN_UNIVERSE_KEY)
    if cached and isinstance(cached, list) and len(cached) > 0:
        return cached

    universe = set()
    try:
        all_stocks = _get_all_nse_securities()
        if all_stocks:
            universe.update(all_stocks[:300])
        else:
            universe.update(_get_nifty_indices())
    except Exception as e:
        logger.warning(f"Failed to fetch securities: {e}")
        universe.update(_get_nifty_indices())

    try:
        universe.update(_get_nifty_indices())
    except Exception as e:
        logger.warning(f"Failed to fetch indices: {e}")

    try:
        universe.update(_get_momentum_movers())
    except Exception as e:
        logger.warning(f"Failed to fetch momentum movers: {e}")

    try:
        universe.update(_get_news_mentioned_symbols())
    except Exception as e:
        logger.warning(f"Failed to fetch news symbols: {e}")

    try:
        universe.update(_get_recent_ipos())
    except Exception as e:
        logger.warning(f"Failed to fetch IPOs: {e}")

    try:
        universe.update(_get_event_symbols())
    except Exception as e:
        logger.warning(f"Failed to fetch event symbols: {e}")

    universe.update(_load_watchlist())
    universe.update(_load_searched())
    for target in SYMBOL_ALIASES.values():
        if isinstance(target, list):
            universe.update(target)
        else:
            universe.add(target)

    clean = []
    seen = set()
    for s in universe:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s and s not in seen:
            seen.add(s)
            clean.append(s)

    if not clean:
        fallback = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK"]
        clean = fallback

    # Prune symbols that have gone UNIVERSE_PRUNE_AFTER_WEAK_SCANS consecutive
    # scans without a BUY NOW / PREPARE TO BUY, so they stop occupying a slot
    # in the 300-symbol cap below — this is what makes the universe actually
    # evolve over time, not just get reshuffled by whatever the live sources
    # happen to return this cycle. Watchlist symbols are always kept
    # regardless of performance; a user's own watchlist is never auto-pruned.
    watchlist_set = set(_load_watchlist())
    before_prune = len(clean)
    clean = [s for s in clean if s in watchlist_set or not _is_symbol_pruned(s)]
    pruned_count = before_prune - len(clean)
    if pruned_count:
        logger.info(f"Universe pruning: excluded {pruned_count} chronically-unproductive symbols")

    # Prefer dynamic signal names first so movers/news/events always get scanned
    dynamic_priority = []
    try:
        for src in (
            _get_momentum_movers(),
            _get_news_mentioned_symbols(),
            _get_event_symbols(),
            _get_recent_ipos(),
        ):
            for s in src:
                su = str(s).upper().replace(".NS", "").replace(".BO", "")
                if su and su in clean and su not in dynamic_priority:
                    dynamic_priority.append(su)
    except Exception as e:
        logger.warning("dynamic priority merge failed: %s", e)

    rest = [s for s in clean if s not in set(dynamic_priority)]
    ordered = dynamic_priority + rest

    # Target 200–300 names; if live sources thin, pad from liquid NSE list
    if len(ordered) < 200:
        pad = [s for s in _get_all_nse_securities() if s not in set(ordered)]
        ordered.extend(pad[: max(0, 220 - len(ordered))])
        logger.info("Universe padded to %s symbols (live sources were thin)", len(ordered))

    result = ordered[:300]
    # Shorter cache in market hours so movers refresh; longer off-hours
    try:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        is_weekday = now.weekday() < 5
        mins = now.hour * 60 + now.minute
        market_open = is_weekday and (9 * 60 + 15) <= mins <= (15 * 60 + 30)
        ttl = 1800 if market_open else 21600  # 30m vs 6h
    except Exception:
        ttl = 3600
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=ttl)
    logger.info(
        "Scan universe built: %s symbols (dynamic=%s, ttl=%ss)",
        len(result),
        len(dynamic_priority),
        ttl,
    )
    return result

# ── Symbol resolution ──────────────────────────────────────────────────────
def _get_all_known_symbols() -> Set[str]:
    cached = _redis_get(KNOWN_SYMBOLS_KEY)
    if cached and isinstance(cached, list):
        return set(cached)
    combined = set()
    try:
        combined.update(_get_all_nse_securities()[:300])
    except:
        pass
    combined.update(_get_nifty_indices())
    combined.update(_load_watchlist())
    combined.update(_load_searched())
    combined.update(_get_recent_ipos())
    combined.update(_get_momentum_movers())
    for target in SYMBOL_ALIASES.values():
        if isinstance(target, list):
            combined.update(target)
        else:
            combined.add(target)
    scan_universe = _redis_get(SCAN_UNIVERSE_KEY)
    if scan_universe and isinstance(scan_universe, list):
        combined.update(scan_universe)
    cleaned = set()
    for s in combined:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s:
            cleaned.add(s)
    _redis_set(KNOWN_SYMBOLS_KEY, list(cleaned), ttl=21600)
    return cleaned

def _resolve_symbol(misspelled: str) -> Optional[str]:
    if not misspelled:
        return None
    symbol = misspelled.upper().replace(".NS", "").replace(".BO", "")
    if symbol in SYMBOL_ALIASES:
        alias = SYMBOL_ALIASES[symbol]
        if isinstance(alias, list):
            return alias[0]
        return alias
    known = _get_all_known_symbols()
    if symbol in known:
        return symbol
    matches = difflib.get_close_matches(symbol, known, n=1, cutoff=0.7)
    if matches:
        return matches[0]
    return None

# ── Safe response normalization ──────────────────────────────────────────
def _normalize_decision_response(raw, symbol: str) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    default = {
        "symbol": symbol,
        "decision": "DO NOT BUY",
        "confidence": "Low",
        "combined_score": 0,
        "technical_score": 50,
        "fundamental_score": 50,
        "news_score": None,
        "prediction_score": None,
        "prediction_note": None,
        "market_score": 50,
        "training_score": 50,
        "event_risk": False,
        "entry_range": None,
        "target": None,
        "stop_loss": None,
        "holding_period": "N/A",
        "close": None,
        "support": None,
        "resistance": None,
        "reasons": {
            "technical": ["Data unavailable"],
            "fundamental": ["Data unavailable"]
        },
        "valuation": "fair",
        "sector": None,
        "data_insufficient": False,
        "fundamental_metrics": None,
        "fundamental_fallback": False,
    }
    merged = {**default, **raw}
    return merged

# ── Value-buying adjustment for top-pick ranking ────────────────────────────
# A stock priced under Rs 2000 with already-decent fundamentals gets a score
# bonus (bigger the further under Rs 2000), so a Rs 200 stock with solid
# fundamentals is favored over a Rs 1900 stock, all else equal. Cheap alone
# isn't rewarded — only cheap + fundamentally sound. Stocks over Rs 2000 are
# excluded entirely from "recommendations" (top picks), though they still
# appear normally in all_results — this only changes which ones get
# surfaced as headline picks (and what Send Top 5 / notifications send).
VALUE_PRICE_CAP = 2000.0
VALUE_BONUS_MAX = 8.0
VALUE_MIN_FUNDAMENTAL_FOR_BONUS = 50.0

def _value_adjusted_score(r: dict):
    """Returns (adjusted_score, eligible_for_top_pick).
    Price > VALUE_PRICE_CAP no longer hard-excludes a name — only removes
    the cheap-stock bonus. High-score large-caps must still surface as picks.
    """
    price = r.get("close")
    combined = r.get("combined_score", 0) or 0
    if price is None or price <= 0:
        return combined, True
    fundamental = r.get("fundamental_score", 0) or 0
    bonus = 0.0
    if price <= VALUE_PRICE_CAP and fundamental >= VALUE_MIN_FUNDAMENTAL_FOR_BONUS:
        bonus = (1 - price / VALUE_PRICE_CAP) * VALUE_BONUS_MAX
    return combined + bonus, True

def _select_top_picks(actionable: list, limit: int = 5) -> list:
    """Value-adjusted ranking for recommendations specifically — the raw
    combined_score sort on results/all_results is left untouched."""
    eligible = [r for r in actionable if _value_adjusted_score(r)[1]]
    eligible.sort(key=lambda r: _value_adjusted_score(r)[0], reverse=True)
    return eligible[:limit]

# ── More precise holding period ─────────────────────────────────────────────
# decision-engine's holding_period is often a static "2-6 weeks" string, or
# "N/A" when unset. Rather than leave that vague default, estimate an actual
# calendar date range from how far the target is from entry: a target close
# to entry implies a shorter expected move, a distant target implies more
# time is needed for it to play out. This is a heuristic, not a model
# prediction — it's meant to replace a meaningless placeholder with a
# concrete date range, not to claim precision the system doesn't have.
def _estimate_holding_period(entry_price, target_price, decision: str):
    if not entry_price or not target_price or entry_price <= 0:
        return None
    move_pct = abs(target_price - entry_price) / entry_price * 100
    if decision == "BUY NOW":
        # Faster-triggering setups: scale trading days to the size of the move.
        min_days = max(3, round(move_pct * 1.2))
        max_days = max(min_days + 5, round(move_pct * 2.5))
    else:
        min_days = max(5, round(move_pct * 1.8))
        max_days = max(min_days + 7, round(move_pct * 3.5))
    min_days, max_days = min(min_days, 60), min(max_days, 90)  # sanity cap
    start = datetime.now(IST).date()
    end_min = start + timedelta(days=min_days)
    end_max = start + timedelta(days=max_days)
    return {
        "min_days": min_days,
        "max_days": max_days,
        "expected_by_earliest": end_min.isoformat(),
        "expected_by_latest": end_max.isoformat(),
        "label": f"{min_days}-{max_days} trading days (by {end_min.strftime('%d %b')}–{end_max.strftime('%d %b')})",
    }

# ── Symbol performance tracking (for a self-pruning scan universe) ─────────
# After every scan, record whether each symbol produced anything actionable.
# _build_scan_universe() below excludes symbols that have gone this many
# consecutive scans without a BUY NOW / PREPARE TO BUY, so the universe
# actually evolves over time — unproductive names drop out, freeing room for
# fresh candidates from the same live sources — rather than a static list
# just getting reshuffled by whatever the live sources happen to return.
# Watchlist symbols are exempt: a user's own watchlist is never auto-pruned.
SYMBOL_PERF_KEY_PREFIX = "stockky:symbol_perf:"
UNIVERSE_PRUNE_AFTER_WEAK_SCANS = 10
UNIVERSE_GRACE_PERIOD_SCANS = 3
SYMBOL_PERF_TTL = 30 * 86400  # 30-day rolling window

def _record_symbol_outcomes(results: list):
    for r in results:
        symbol = r.get("symbol")
        decision = r.get("decision")
        if not symbol or decision in (None, "ERROR"):
            continue
        key = SYMBOL_PERF_KEY_PREFIX + symbol
        state = _redis_get(key) or {"weak_streak": 0, "total_scans": 0, "last_actionable_at": None}
        state["total_scans"] = state.get("total_scans", 0) + 1
        if decision in ("BUY NOW", "PREPARE TO BUY"):
            state["weak_streak"] = 0
            state["last_actionable_at"] = datetime.now(IST).isoformat()
        else:
            state["weak_streak"] = state.get("weak_streak", 0) + 1
        _redis_set(key, state, ttl=SYMBOL_PERF_TTL)

def _is_symbol_pruned(symbol: str) -> bool:
    state = _redis_get(SYMBOL_PERF_KEY_PREFIX + symbol)
    if not state:
        return False
    if state.get("total_scans", 0) < UNIVERSE_GRACE_PERIOD_SCANS:
        return False
    return state.get("weak_streak", 0) >= UNIVERSE_PRUNE_AFTER_WEAK_SCANS

# ── Fallback helpers with caching ──────────────────────────────────────────
def _fetch_price_from_quote(symbol: str) -> Optional[float]:
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            price = data.get("price")
            if price is not None:
                logger.info(f"Price fallback for {symbol}: ₹{price}")
                return price
        else:
            logger.warning(f"Quote endpoint returned {resp.status_code} for {symbol}")
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
    return None

async def _fetch_fundamental_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[dict], bool]:
    """Prefer Data Feed → short Redis cache → upstream. Write-through to Data Feed on upstream hit."""
    # 1) Data Feed (durable 12–24h) — any useful feed row skips upstream
    try:
        fed = _feed_store().get_symbol(symbol)
        if fed and (
            fed.get("fundamental_score") is not None
            or fed.get("metrics")
            or fed.get("sector")
            or fed.get("valuation")
            or fed.get("quality_score") is not None
            or fed.get("multi_quarter_score") is not None
        ):
            reconstructed = {
                "symbol": symbol.upper(),
                "fundamental_score": fed.get("fundamental_score"),
                "valuation": fed.get("valuation"),
                "sector": fed.get("sector"),
                "industry": fed.get("industry"),
                "peer_relative_score": fed.get("peer_relative_score"),
                "peer_relative": fed.get("peer_relative"),
                "peer_list": fed.get("peer_list"),
                "multi_quarter_score": fed.get("multi_quarter_score"),
                "multi_quarter_ok": fed.get("multi_quarter_ok"),
                "multi_quarter_detail": fed.get("multi_quarter_detail"),
                "quality_score": fed.get("quality_score"),
                "metrics": fed.get("metrics") or {},
                "reasons": fed.get("fundamental_reasons") or ["From Data Feed cache"],
                "fallback_used": fed.get("fallback_used"),
                "from_data_feed": True,
                "data_feed_updated_at": fed.get("updated_at"),
            }
            return reconstructed, bool(fed.get("fallback_used", True))
    except Exception as e:
        logger.debug("data feed fundamental read: %s", e)

    # 2) Short Redis cache
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict) and (cached.get("full") or cached.get("metrics") is not None):
        if isinstance(cached.get("full"), dict) and cached["full"].get("fundamental_score") is not None:
            return cached["full"], cached.get("fallback", False)
        return cached.get("metrics"), cached.get("fallback", False)

    # 3) Upstream + write-through to Data Feed (mutex against stampede)
    if not try_refresh_lock(_redis, symbol, ttl_sec=5):
        # Another worker is refreshing — return short cache or None
        cached2 = _redis_get(cache_key)
        if cached2 and isinstance(cached2, dict):
            if isinstance(cached2.get("full"), dict):
                return cached2["full"], cached2.get("fallback", False)
            return cached2.get("metrics"), cached2.get("fallback", False)
        return None, True
    try:
        resp = await _cb_get(client, "fundamental", f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=35)
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                data = {}
            metrics = data.get("metrics")
            fallback_used = bool(data.get("fallback_used", False))
            _redis_set(
                cache_key,
                {"metrics": metrics, "fallback": fallback_used, "full": data},
                ttl=STATIC_PARAM_TTL,
            )
            try:
                payload = extract_feed_payload(symbol, fundamental=data, events=None)
                # merge with existing feed events if present
                existing = _feed_store().get_symbol(symbol) or {}
                if existing:
                    for k in ("bulk_deals", "recent_insider_transactions", "earnings_surprise",
                              "next_earnings_date", "event_summary", "has_positive_catalyst",
                              "recent_event_score"):
                        if existing.get(k) is not None and payload.get(k) in (None, [], ""):
                            payload[k] = existing.get(k)
                _feed_store().put_symbol(symbol, payload, ttl=DATA_FEED_TTL)
            except Exception as e:
                logger.debug("data feed write-through fund: %s", e)
            release_refresh_lock(_redis, symbol)
            if data.get("fundamental_score") is not None:
                out = dict(data)
                out["from_data_feed"] = False
                return out, fallback_used
            return metrics, fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")
    return {}, True

async def _fetch_events_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    """Prefer Data Feed → short Redis → upstream. Write-through on upstream hit."""
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"

    # 1) Data Feed first (even empty lists mean "was fed" — avoid upstream spam)
    try:
        fed = _feed_store().get_symbol(symbol)
        if fed and (
            "event_summary" in fed
            or "bulk_deals" in fed
            or "recent_insider_transactions" in fed
            or "earnings_surprise" in fed
            or "next_earnings_date" in fed
            or "has_positive_catalyst" in fed
            or fed.get("fundamental_score") is not None  # symbol was fed at all
            or fed.get("metrics")
        ):
            reconstructed = {
                "symbol": symbol.upper(),
                "bulk_deals": fed.get("bulk_deals") or [],
                "recent_insider_transactions": fed.get("recent_insider_transactions") or [],
                "earnings_surprise": fed.get("earnings_surprise"),
                "next_earnings_date": fed.get("next_earnings_date"),
                "event_summary": fed.get("event_summary"),
                "summary": fed.get("event_summary") or "",
                "has_positive_catalyst": fed.get("has_positive_catalyst"),
                "recent_event_score": fed.get("recent_event_score"),
                "from_data_feed": True,
                "data_feed_updated_at": fed.get("updated_at"),
            }
            return reconstructed
    except Exception as e:
        logger.debug("data feed events read: %s", e)

    # 2) Short Redis
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached

    try:
        resp = await _cb_get(client, "event", f"{EVENT_URL}/events/{symbol}", timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                _redis_set(cache_key, data, ttl=STATIC_PARAM_TTL)
                try:
                    existing = _feed_store().get_symbol(symbol)
                    payload = extract_feed_payload(
                        symbol,
                        fundamental=existing if existing else None,
                        events=data,
                    )
                    if existing:
                        # keep fund fields
                        for k, v in existing.items():
                            if k not in payload or payload.get(k) in (None, [], ""):
                                payload[k] = v
                        payload.update({
                            "bulk_deals": (data.get("bulk_deals") or [])[:5],
                            "recent_insider_transactions": (data.get("recent_insider_transactions") or [])[:5],
                            "earnings_surprise": data.get("earnings_surprise"),
                            "next_earnings_date": data.get("next_earnings_date"),
                            "event_summary": data.get("event_summary") or data.get("summary"),
                            "has_positive_catalyst": data.get("has_positive_catalyst"),
                            "recent_event_score": data.get("recent_event_score"),
                            "updated_at": payload.get("updated_at"),
                        })
                    _feed_store().put_symbol(symbol, payload, ttl=DATA_FEED_TTL)
                except Exception as e:
                    logger.debug("data feed write-through events: %s", e)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {e}")
    return None

async def _fetch_news_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    cache_key = f"{NEWS_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached

    try:
        resp = await _cb_get(client, "news", f"{NEWS_URL}/analyze/{symbol}", timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                # News moves faster than fund/events; still cache aggressively off-hours
                ttl = 3600 if _is_market_open_ist() else STATIC_PARAM_TTL
                _redis_set(cache_key, data, ttl=ttl)
                return data
    except Exception as e:
        logger.warning(f"News fetch failed for {symbol}: {e}")
    return None

async def _fetch_prediction_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[float], Optional[str]]:
    try:
        resp = await _cb_get(client, "prediction", f"{PREDICTION_URL}/predict/{symbol}", timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("model_loaded"):
                return data.get("prediction_score"), data.get("note")
    except Exception as e:
        logger.warning(f"Prediction lookup failed for {symbol}: {e}")
    return None, None

# ── Hinglish & GenAI summary ──────────────────────────────────────────────
# ── Gemini-powered summary (optional — falls back to the Hinglish template
# below if GEMINI_API_KEY isn't set, or if the call fails/times out/gets
# truncated) ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
# Free-tier Gemini has its own RPM limit, independent of MAX_PARALLEL_WORKERS
# (which bounds calls to decision-engine, not Gemini) — a scan running 10
# concurrent symbol analyses shouldn't also fire 10 concurrent Gemini calls.
GEMINI_SEMAPHORE = asyncio.Semaphore(int(os.getenv("GEMINI_MAX_CONCURRENT", "3")))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "400"))

def _build_gemini_prompt(data: dict) -> str:
    decision = data.get("decision")
    symbol = data.get("symbol") or "this stock"
    entry = data.get("entry_range") or {}
    reasons = data.get("reasons") or {}
    tech_reasons = "; ".join(reasons.get("technical", [])[:2])
    fund_reasons = "; ".join(reasons.get("fundamental", [])[:2])
    return (
        f"Write a 2-3 sentence trading summary in Hinglish (mixed Hindi-English, "
        f"as commonly used by Indian retail traders) for {symbol}. "
        f"Decision: {decision}. Combined score: {data.get('combined_score')}/100. "
        f"Entry range: {entry.get('low')}-{entry.get('high')}, target: {data.get('target')}, "
        f"stop-loss: {data.get('stop_loss')}. "
        f"Technical reasons: {tech_reasons or 'none listed'}. "
        f"Fundamental reasons: {fund_reasons or 'none listed'}. "
        f"Keep it natural and complete — do not trail off mid-sentence, and do not "
        f"just list numbers without context. End with a clear, complete thought."
    )

async def _generate_ai_summary(data: dict, client: httpx.AsyncClient) -> str:
    """Returns a Gemini-generated summary, or falls back to the template
    in _generate_summary() below on any failure — including a truncated
    response (finishReason == MAX_TOKENS), which is the classic symptom
    of maxOutputTokens being set too low and cutting the sentence off
    mid-thought."""
    if not GEMINI_API_KEY:
        return _generate_summary(data)
    try:
        async with GEMINI_SEMAPHORE:
            resp = await client.post(
                GEMINI_URL,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": _build_gemini_prompt(data)}]}],
                    "generationConfig": {
                        "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
                        "temperature": 0.6,
                    },
                },
                timeout=15,
            )
        if resp.status_code != 200:
            logger.warning(f"Gemini call failed ({resp.status_code}) for {data.get('symbol')} — using template")
            return _generate_summary(data)

        payload = resp.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            return _generate_summary(data)

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()

        if not text:
            return _generate_summary(data)
        if finish_reason == "MAX_TOKENS":
            # Truncated mid-generation — exactly the "doesn't complete its
            # sentence" bug. Don't return a cut-off response; use the
            # template instead of showing something that looks broken.
            logger.warning(f"Gemini response truncated (MAX_TOKENS) for {data.get('symbol')} — using template")
            return _generate_summary(data)
        if text[-1] not in ".!?।\"'”)":
            # Didn't hit MAX_TOKENS but still doesn't end on sentence-ending
            # punctuation — treat as suspect and fall back rather than show
            # a sentence fragment.
            logger.warning(f"Gemini response looks incomplete for {data.get('symbol')} — using template")
            return _generate_summary(data)
        return text
    except Exception as e:
        logger.warning(f"Gemini summary failed for {data.get('symbol')}: {e} — using template")
        return _generate_summary(data)

# ── Hinglish template summary (fallback / default when Gemini isn't
# configured) ───────────────────────────────────────────────────────────
def _generate_summary(data) -> str:
    if not data or not isinstance(data, dict):
        return "Data unavailable"
    decision = data.get("decision")
    symbol = data.get("symbol") or "Unknown"
    confidence = data.get("confidence")
    combined_score = data.get("combined_score")
    entry = data.get("entry_range") or {}
    target = data.get("target")
    stop = data.get("stop_loss")
    holding = data.get("holding_period")
    reasons = data.get("reasons") or {}
    close = data.get("close")
    prediction_note = data.get("prediction_note")

    if decision == "BUY NOW":
        summary = f"🚀 {symbol} अभी खरीदने का बहुत अच्छा मौका है! "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप लॉस {stop}. "
        summary += f"होल्डिंग {holding}. कॉन्फिडेंस {confidence}, स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "जल्दी शामिल करें!"
    elif decision == "PREPARE TO BUY":
        summary = f"⏳ {symbol} के लिए, तैयारी करें, अभी इंतज़ार करें. "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप {stop}. "
        summary += f"स्कोर {combined_score}. वॉल्यूम कन्फर्मेशन का इंतज़ार करें."
    elif decision == "HOLD":
        summary = f"🔄 {symbol} को होल्ड करें. टारगेट {target}, स्टॉप {stop}. स्कोर {combined_score}."
    elif decision == "SELL":
        summary = f"🔴 {symbol} को बेचें. कीमत {close}, टारगेट से नीचे. स्टॉप {stop} पार. स्कोर {combined_score}."
    else:
        summary = f"❌ {symbol} अभी न खरीदें. स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "कुछ दिन और देखें."

    if prediction_note:
        summary += f" 🤖 {prediction_note}"

    return summary

# ── Telegram notification helper ──────────────────────────────────────────
def _send_scan_notification(recommendations: list, verdict: str, scanned: int, universe_size: int):
    if not recommendations:
        message = f"📊 Market Scan Complete\n\nScanned {scanned} stocks. No strong BUY signals today.\nVerdict: {verdict}"
    else:
        lines = [f"📊 *Top {len(recommendations)} Picks from Market Scan*", ""]
        for i, r in enumerate(recommendations[:5], 1):
            symbol = r.get("symbol")
            decision = r.get("decision")
            combined_score = r.get("combined_score")
            close = r.get("close")
            target = r.get("target")
            stop_loss = r.get("stop_loss")
            entry = r.get("entry_range") or {}
            entry_low = entry.get("low")
            entry_high = entry.get("high")
            lines.append(f"{i}. *{symbol}* – {decision} (Score: {combined_score})")
            if close:
                lines.append(f"   Current: ₹{close:.2f}")
            if entry_low and entry_high:
                lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
            if target:
                upside = ((target - close) / close * 100) if close else 0
                lines.append(f"   Target: ₹{target:.2f} (+{upside:.1f}%)")
            if stop_loss:
                lines.append(f"   Stop: ₹{stop_loss:.2f}")
            lines.append("")
        message = "\n".join(lines)

    try:
        _wake_notification_service()
        # Telegram/Discord/Slack (channel=all so enabled channels receive)
        resp = httpx.post(f"{NOTIFICATION_URL}/notify", json={
            "title": "Market Scan Complete",
            "message": message,
            "channel": "all",
        }, timeout=15)
        if resp.status_code == 200:
            logger.info("Scan recommendations notified: %s", resp.text[:120])
        else:
            logger.warning("Scan notification failed with status %d", resp.status_code)
        # Extra CallMeBot voice-style alert only for immediate BUY NOW
        buy_now = [r for r in (recommendations or []) if r.get("decision") == "BUY NOW"]
        if buy_now:
            top = buy_now[0]
            sym = top.get("symbol", "?")
            score = top.get("combined_score", "")
            reason = (top.get("natural_language_summary") or top.get("holding_period") or "Strong buy signal")
            if isinstance(reason, str) and len(reason) > 100:
                reason = reason[:97] + "..."
            call_msg = f"{sym} is BUY NOW. Score {score}. {reason}"
            try:
                httpx.post(
                    f"{NOTIFICATION_URL}/notify",
                    json={"title": f"BUY NOW {sym}", "message": call_msg, "channel": "callmebot", "urgency": "high"},
                    timeout=20,
                )
            except Exception as ce:
                logger.warning("CallMeBot notify failed: %s", ce)
    except Exception as e:
        logger.warning("Failed to send scan notification: %s", e)

def _wake_notification_service() -> bool:
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

# ============================================================================
# ⚡ PARALLEL SCAN with reduced workers and retries
# ============================================================================

# Free-tier friendly default: 18 workers (was 10). Override via env.
# Pair with market-data yfinance semaphore to avoid Yahoo rate limits.
MAX_PARALLEL_WORKERS = int(os.getenv("MAX_PARALLEL_SCAN_WORKERS", "2"))  # free-tier safe; was 6 → cascade timeouts  # full universe; pace via Redis RL; separate dynos
MAX_RETRIES = 1
RETRY_BACKOFF = 1.0

def _is_market_open_ist() -> bool:
    """NSE continuous session 09:15–15:30 IST, Mon–Fri, excluding known holidays."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if is_nse_holiday(now.date()):
        return False
    tt = now.time()
    start = dtime(9, 15)
    end = dtime(15, 30)
    return start <= tt <= end


def _market_session_phase_ist() -> str:
    """preopen | open | post | closed | holiday — used for warm/cache policy."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return "closed"
    if is_nse_holiday(now.date()):
        return "holiday"
    tt = now.time()
    if dtime(8, 30) <= tt < dtime(9, 15):
        return "preopen"
    if dtime(9, 15) <= tt <= dtime(15, 30):
        return "open"
    if dtime(15, 30) < tt <= dtime(16, 0):
        return "post"
    return "closed"


def _should_force_lite_scan() -> bool:
    """Auto lite when circuits are open or dependency error rate is high (free-tier)."""
    try:
        snaps = all_snapshots()
        if any(v.get("state") == "open" for v in snaps.values()):
            return True
        snap = metrics.snapshot()
        counters = snap.get("counters") or {}
        errors = sum(v for k, v in counters.items() if "dependency_errors" in k)
        oks = sum(v for k, v in counters.items() if "dependency_ok" in k)
        total = errors + oks
        if total >= 15 and (errors / total) >= 0.35:
            return True
    except Exception:
        pass
    return False

def _decide_cache_ttl() -> int:
    return DECIDE_CACHE_TTL_OPEN if _is_market_open_ist() else DECIDE_CACHE_TTL_CLOSED


def _batch_result_cache_key(symbol: str, lite: bool = False) -> str:
    mode = "lite" if lite else "full"
    return f"{BATCH_RESULT_CACHE_PREFIX}{mode}:{(symbol or '').upper()}"


def _batch_result_cache_get(symbol: str, lite: bool = False) -> Optional[dict]:
    """Return cached scan row if present and has a decision (not ERROR)."""
    if not BATCH_RESULT_CACHE_ENABLED:
        return None
    data = _redis_get(_batch_result_cache_key(symbol, lite))
    if not isinstance(data, dict):
        return None
    if not data.get("decision") or data.get("decision") == "ERROR":
        return None
    # Soft-TTL: treat nearly-expired as miss so one worker refreshes
    key = _batch_result_cache_key(symbol, lite)
    if _redis_soft_ttl_refresh(key, soft_window=15):
        return None
    data = dict(data)
    data["_from_batch_cache"] = True
    return data


def _batch_result_cache_set(symbol: str, result: dict, lite: bool = False) -> None:
    if not BATCH_RESULT_CACHE_ENABLED:
        return
    if not isinstance(result, dict):
        return
    if result.get("decision") in (None, "ERROR"):
        return
    payload = {k: v for k, v in result.items() if not str(k).startswith("_")}
    _redis_set(_batch_result_cache_key(symbol, lite), payload, ttl=_decide_cache_ttl())

def _prioritize_universe(universe: List[str]) -> List[str]:
    """Watchlist + recently searched first so useful results appear early; rest of universe unchanged."""
    watch = _load_watchlist()
    searched = _load_searched()
    priority = []
    seen = set()
    for s in watch + searched:
        su = s.upper().replace(".NS", "").replace(".BO", "")
        if su and su not in seen and su in set(u.upper().replace(".NS", "").replace(".BO", "") for u in universe):
            priority.append(su)
            seen.add(su)
    rest = []
    for s in universe:
        su = s.upper().replace(".NS", "").replace(".BO", "")
        if su and su not in seen:
            rest.append(su)
            seen.add(su)
    return priority + rest


async def _cb_get(client: httpx.AsyncClient, name: str, url: str, timeout: float = 5.0, **kwargs):
    """GET with circuit breaker — open circuit fails immediately."""
    # Redis rate limit by downstream family (does not shrink universe — only paces calls)
    _bucket = "global"
    _ln = (name or "").lower()
    if "market" in _ln or "quote" in _ln or "history" in _ln:
        _bucket = "market_data"
    elif "fundamental" in _ln or "technical" in _ln or "news" in _ln or "event" in _ln:
        _bucket = "analysis"
    elif "decision" in _ln or "predict" in _ln or "training" in _ln:
        _bucket = "decision"
    elif "gemini" in _ln:
        _bucket = "gemini"
    if not redis_limiter.allow(_bucket):
        await asyncio.sleep(min(2.0, redis_limiter.wait_budget_sec(_bucket)))
        if not redis_limiter.allow(_bucket):
            raise CircuitOpenError(name or "rate_limit", redis_limiter.wait_budget_sec(_bucket))
    br = get_breaker(name)
    if not br.allow():
        metrics.inc("stockky_circuit_open_total", dependency=name)
        raise CircuitOpenError(name, br.retry_after())
    t0 = time.time()
    try:
        resp = await client.get(url, timeout=timeout, **kwargs)
        metrics.observe_ms("stockky_dependency_latency", (time.time() - t0) * 1000, dependency=name)
        if resp.status_code >= 500:
            br.record_failure(f"HTTP {resp.status_code}")
            metrics.inc("stockky_dependency_errors_total", dependency=name)
        else:
            br.record_success()
            metrics.inc("stockky_dependency_ok_total", dependency=name)
        return resp
    except CircuitOpenError:
        raise
    except Exception as e:
        metrics.observe_ms("stockky_dependency_latency", (time.time() - t0) * 1000, dependency=name)
        metrics.inc("stockky_dependency_errors_total", dependency=name)
        br.record_failure(str(e))
        raise


async def _cb_post(client: httpx.AsyncClient, name: str, url: str, timeout: float = 8.0, **kwargs):
    br = get_breaker(name)
    if not br.allow():
        raise CircuitOpenError(name, br.retry_after())
    try:
        resp = await client.post(url, timeout=timeout, **kwargs)
        if resp.status_code >= 500:
            br.record_failure(f"HTTP {resp.status_code}")
        else:
            br.record_success()
        return resp
    except CircuitOpenError:
        raise
    except Exception as e:
        br.record_failure(str(e))
        raise


async def _wake_required_services(client: httpx.AsyncClient = None) -> dict:
    """Wake free-tier services before scan. Market-data gets a warm yfinance touch + double ping."""
    own_client = client is None
    if own_client:
        client = _get_http_client()
    results = {}
    try:
        async def ping(name: str, url: str):
            if not url:
                return name, {"ok": False, "error": "no url"}
            base = url.rstrip("/")
            try:
                # market-data: prefer /wake (warms Yahoo) then /health?warm=1
                if name == "market-data":
                    try:
                        r = await client.get(f"{base}/wake", timeout=25)
                        if r.status_code == 200:
                            return name, {"ok": True, "status": r.status_code, "warmed": True}
                    except Exception:
                        pass
                    r = await client.get(f"{base}/health", params={"warm": "true"}, timeout=25)
                    # second ping after short pause so dyno is fully up
                    await asyncio.sleep(2.5)
                    r2 = await client.get(f"{base}/health", timeout=15)
                    ok = r.status_code == 200 or r2.status_code == 200
                    return name, {"ok": ok, "status": r2.status_code if r2 else r.status_code, "warmed": True}
                # Prefer warm query so downstream services pre-touch deps
                r = await client.get(f"{base}/health", params={"warm": "true"}, timeout=20)
                if r.status_code != 200:
                    await asyncio.sleep(2)
                    r = await client.get(f"{base}/health", params={"warm": "true"}, timeout=20)
                return name, {"ok": r.status_code == 200, "status": r.status_code, "warmed": True}
            except Exception as e:
                return name, {"ok": False, "error": str(e)[:120]}
        pairs = await asyncio.gather(*(ping(n, cfg["url"]) for n, cfg in SYSTEM_SERVICES.items()))
        results = dict(pairs)
        # Second pass for any failures (free-tier cold start)
        failed = [n for n, v in results.items() if not v.get("ok")]
        if failed:
            await asyncio.sleep(3)
            pairs2 = await asyncio.gather(*(ping(n, SYSTEM_SERVICES[n]["url"]) for n in failed if n in SYSTEM_SERVICES))
            results.update(dict(pairs2))
    finally:
        # Do NOT aclose shared pool client
        pass
    return results

async def _analyze_one_symbol_ultra(
    symbol: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    lite: bool = False,
) -> dict:
    """
    Analyse one symbol with parallel internal calls and caching.
    Timeouts: decision 90s, others 60s.
    Speed fixes:
    - Prefer data already returned by Decision Engine (avoid duplicate fund/news/event/pred fetches).
    - Optional decide-level Redis cache.
    - lite=True skips Gemini summary + optional enrichment when Decision already filled fields.
    """
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                # ── Decide-level cache (same symbol within TTL → instant) ──
                cache_key = f"{DECIDE_CACHE_PREFIX}{symbol.upper()}"
                cached_decide = _redis_get(cache_key)
                if cached_decide and isinstance(cached_decide, dict) and cached_decide.get("decision"):
                    normalized = _normalize_decision_response(cached_decide, symbol)
                else:
                    decision_resp = await _cb_get(client, "decision", f"{DECISION_URL}/decide/{symbol}", timeout=90)
                    decision_resp.raise_for_status()
                    raw = decision_resp.json()
                    normalized = _normalize_decision_response(raw, symbol)
                    _redis_set(cache_key, normalized, ttl=_decide_cache_ttl())

                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                # Only fetch extras when Decision Engine did not already supply them
                need_fund = not normalized.get("fundamental_metrics")
                need_event = not normalized.get("event_data") and not normalized.get("event_risk")
                need_news = normalized.get("news_score") is None
                need_pred = normalized.get("prediction_score") is None

                if not lite:
                    tasks = {}
                    if need_fund:
                        tasks["fund"] = asyncio.create_task(_fetch_fundamental_cached(symbol, client))
                    if need_event:
                        tasks["event"] = asyncio.create_task(_fetch_events_cached(symbol, client))
                    if need_news:
                        tasks["news"] = asyncio.create_task(_fetch_news_cached(symbol, client))
                    if need_pred:
                        tasks["pred"] = asyncio.create_task(_fetch_prediction_cached(symbol, client))

                    if tasks:
                        await asyncio.gather(*tasks.values(), return_exceptions=True)

                    if "fund" in tasks:
                        fund_res = tasks["fund"].result() if not tasks["fund"].exception() else ({}, True)
                        if isinstance(fund_res, tuple):
                            fund_metrics, fund_fallback = fund_res
                        else:
                            fund_metrics, fund_fallback = {}, True
                        if fund_metrics:
                            normalized["fundamental_metrics"] = fund_metrics
                            normalized["fundamental_fallback"] = fund_fallback

                    if "event" in tasks:
                        event_data = tasks["event"].result() if not tasks["event"].exception() else None
                        if event_data:
                            # Previously only next_earnings_date survived here —
                            # whatever else event-tracker-service returns (bulk
                            # deals, insider trades, mutual fund holding changes,
                            # etc.) was silently discarded. Passing the raw dict
                            # through lets the frontend show it; this gateway
                            # doesn't know that service's exact schema to pick
                            # specific fields out of it without guessing.
                            normalized["event_data"] = event_data
                            if event_data.get("next_earnings_date"):
                                normalized["event_risk"] = True
                                reasons = normalized.get("reasons", {})
                                reasons["event"] = [f"Earnings due: {event_data['next_earnings_date']}"]
                                normalized["reasons"] = reasons

                    if "news" in tasks:
                        news_data = tasks["news"].result() if not tasks["news"].exception() else None
                        if news_data:
                            normalized["news_score"] = news_data.get("news_score")
                            reasons = normalized.get("reasons", {})
                            if news_data.get("reasons"):
                                reasons["news"] = news_data["reasons"]
                                normalized["reasons"] = reasons

                    if "pred" in tasks:
                        pred_res = tasks["pred"].result() if not tasks["pred"].exception() else (None, None)
                        if isinstance(pred_res, tuple):
                            pred_score, pred_note = pred_res
                        else:
                            pred_score, pred_note = None, None
                        if pred_score is not None:
                            normalized["prediction_score"] = pred_score
                            normalized["prediction_note"] = pred_note

                # Adds a concrete calendar-date holding period estimate
                # alongside whatever decision-engine's own holding_period
                # string is (often a static "2-6 weeks" or "N/A") — kept as
                # a separate field so nothing that already reads
                # holding_period breaks.
                entry = normalized.get("entry_range") or {}
                entry_price = entry.get("low") or normalized.get("close")
                normalized["holding_period_estimate"] = _estimate_holding_period(
                    entry_price, normalized.get("target"), normalized.get("decision")
                )
                # Gemini only when not lite (full enrichment); top-picks can request it later
                if not lite:
                    normalized["natural_language_summary"] = await _generate_ai_summary(normalized, client)
                else:
                    normalized["natural_language_summary"] = _generate_summary(normalized) if "_generate_summary" in dir() else None
                    if normalized.get("natural_language_summary") is None:
                        try:
                            normalized["natural_language_summary"] = _generate_summary(normalized)
                        except Exception:
                            normalized["natural_language_summary"] = None
                return normalized

            except CircuitOpenError as e:
                # Fail fast — no retry storm when dependency circuit is open
                metrics.inc("stockky_scan_circuit_skip_total", dependency=e.name)
                logger.warning("Scan skip %s: %s", symbol, e)
                await asyncio.sleep(1.5)
                price = None
                try:
                    price = _fetch_price_from_quote(symbol)
                except Exception:
                    price = None
                out = {
                    "symbol": symbol,
                    "decision": "DO NOT BUY",
                    "combined_score": 0,
                    "confidence": "Low",
                    "data_insufficient": True,
                    "error": str(e),
                    "circuit_open": e.name,
                    "close": price,
                    "reasons": {
                        "data_quality": [f"Circuit open for {e.name}; retry in {getattr(e, 'retry_after', 30):.0f}s — price may still show"]
                    },
                }
                # Brief pace so we do not hammer an open circuit
                await asyncio.sleep(min(2.0, float(getattr(e, "retry_after", 2) or 2) / 10.0))
                return out
            except httpx.HTTPError as e:
                error_type = type(e).__name__
                error_msg = str(e) or f"{error_type} (empty message)"
                logger.warning(
                    f"Scan error for {symbol} (attempt {attempt+1}/{MAX_RETRIES+1}): {error_type} - {error_msg}"
                )
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF ** attempt
                    logger.info(f"Retrying {symbol} in {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                else:
                    return {
                        "symbol": symbol,
                        "decision": "ERROR",
                        "error": f"{error_type}: {error_msg if error_msg else 'Unknown HTTP error'}"
                    }
            except Exception as e:
                logger.error(f"Unexpected error for {symbol}: {type(e).__name__} - {str(e)}")
                return {
                    "symbol": symbol,
                    "decision": "ERROR",
                    "error": f"Unexpected: {type(e).__name__} - {str(e)}"
                }
        return {"symbol": symbol, "decision": "ERROR", "error": "Max retries exceeded"}

async def run_scan_parallel(task_id: str, universe: List[str], lite: bool = False):
    start_time = time.time()
    # Prioritize watchlist / searched so useful picks surface early without shrinking universe
    universe = _prioritize_universe(universe)
    total = len(universe)
    processed = 0
    results = []
    errors = []

    _redis_set(SCAN_TASK_PREFIX + task_id, {
        "status": "running",
        "total": total,
        "processed": 0,
        "elapsed": 0,
        "result": None,
        "error": None,
        "lite": lite,
    }, ttl=3600)

    sem = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    cancel_key = SCAN_TASK_PREFIX + task_id + ":cancel"
    client = _get_http_client()  # shared keepalive pool

    # ── Pre-scan: wake free-tier dynos (does not change universe size) ──
    if WAKE_BEFORE_SCAN:
        try:
            wake_results = await _wake_required_services(client)
            logger.info("Pre-scan wake: %s", {k: v.get("ok") for k, v in wake_results.items()})
            try:
                await _warm_upstream_services(client)
            except Exception:
                pass
            wait = max(WAKE_WAIT_SECONDS, 10.0)
            if not (wake_results.get("market-data") or {}).get("ok"):
                wait = max(wait, 18.0)
            await asyncio.sleep(min(wait, 25.0))
        except Exception as e:
            logger.warning("Pre-scan wake failed (continuing): %s", e)

    # Data Feed coverage stats (informational only)
    try:
        store = _feed_store()
        hit = 0
        for s in universe:
            base = s.upper().replace(".NS", "").replace(".BO", "")
            fed = store.get_symbol(base)
            if fed and (fed.get("fundamental_score") is not None or fed.get("metrics") or fed.get("sector")):
                hit += 1
        logger.info(
            "Scan Data Feed coverage: %s/%s symbols (%.0f%%)",
            hit, total, (100.0 * hit / total) if total else 0,
        )
    except Exception as e:
        logger.debug("feed coverage: %s", e)

    # ── Full-universe batch processor (list size preserved) ──
    batch_size = default_batch_size(MAX_PARALLEL_WORKERS, minimum=12)
    logger.info(
        "Scan full universe=%s batch_size=%s workers=%s",
        total, batch_size, MAX_PARALLEL_WORKERS,
    )

    async def _worker(sym: str):
        return await _analyze_one_symbol_ultra(sym, client, sem, lite=lite)

    def _should_cancel() -> bool:
        if task_id in _SCAN_CANCEL_FLAGS:
            return True
        return bool(_redis_get(cancel_key))

    def _classify(result: dict):
        if not isinstance(result, dict):
            return {"symbol": "?", "error": "invalid result"}
        if result.get("decision") == "ERROR":
            return {"symbol": result.get("symbol"), "error": result.get("error", "Unknown error")}
        return None

    async def _on_progress(progress):
        payload = {
            "status": "running",
            "total": progress.total,
            "processed": progress.processed,
            "elapsed": progress.elapsed_sec,
            "result": None,
            "error": None,
            "lite": lite,
            "batch": progress.batch_index + 1,
            "batches": progress.batch_count,
            "cache_hits": progress.cache_hits,
            "cache_misses": progress.cache_misses,
        }
        _redis_set(SCAN_TASK_PREFIX + task_id, payload, ttl=3600)
        try:
            await _ws_push_scan(task_id, payload)
        except Exception:
            pass

    async def _on_batch_end(progress):
        # Keep free-tier services awake during long full-universe scans.
        # Skip warm when this scan is mostly cache hits (no upstream pressure).
        total_c = progress.cache_hits + progress.cache_misses
        hit_rate = (progress.cache_hits / total_c) if total_c else 0.0
        if hit_rate >= 0.85:
            return
        if progress.processed > 0 and progress.processed % 20 == 0 and not progress.cancelled:
            try:
                await _warm_upstream_services(client)
            except Exception as e:
                logger.debug("mid-scan warm: %s", e)

    def _cache_get(sym: str):
        return _batch_result_cache_get(sym, lite=lite)

    def _cache_set(sym: str, result: dict):
        _batch_result_cache_set(sym, result, lite=lite)

    batch_out = await run_in_batches(
        universe,
        _worker,
        batch_size=batch_size,
        should_cancel=_should_cancel,
        on_progress=_on_progress,
        on_batch_end=_on_batch_end,
        classify_result=_classify,
        gc_each_batch=True,
        start_time=start_time,
        cache_get=_cache_get if BATCH_RESULT_CACHE_ENABLED else None,
        cache_set=_cache_set if BATCH_RESULT_CACHE_ENABLED else None,
    )
    if batch_out.cache_hits or batch_out.cache_misses:
        logger.info(
            "Scan batch cache hits=%s misses=%s (%.0f%% hit rate)",
            batch_out.cache_hits,
            batch_out.cache_misses,
            100.0 * batch_out.cache_hits / max(1, batch_out.cache_hits + batch_out.cache_misses),
        )
    results = batch_out.results
    errors = batch_out.errors
    processed = batch_out.processed
    cancelled = batch_out.cancelled
    if cancelled:
        logger.info(
            "Scan %s cancelled after %s/%s (full universe size=%s)",
            task_id, processed, total, total,
        )

    gc.collect()

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)

    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = _select_top_picks(actionable, limit=5)
    # Three horizon Top-5 lists (short preferred)
    def _horizon_picks(results_list, horizon_key, limit=5):
        scored = []
        for r in results_list:
            if r.get("decision") == "ERROR":
                continue
            hz = (r.get("horizons") or {}).get(horizon_key) or {}
            sc = hz.get("score")
            if sc is None:
                sc = r.get("combined_score", 0) or 0
                # Mid/long without horizon block: slight discount vs short
                if horizon_key == "mid":
                    sc = sc * 0.95
                elif horizon_key == "long":
                    sc = (r.get("fundamental_score") or sc) * 0.9 + (r.get("combined_score") or 0) * 0.1
            decision = hz.get("decision") or r.get("decision")
            # Include BUY/PREPARE always; also include high-score DO NOT BUY as
            # PREPARE candidates so the Top-5 lists are never empty on a
            # cautious market day (score bar: short 54, mid 56, long 58).
            min_sc = {"short": 54, "mid": 56, "long": 58}.get(horizon_key, 54)
            if decision in ("BUY NOW", "PREPARE TO BUY") or (sc or 0) >= min_sc:
                row = {**r, "_hz_score": sc, "horizon_focus": horizon_key}
                if decision == "DO NOT BUY" and (sc or 0) >= min_sc:
                    row = {**row, "decision": "PREPARE TO BUY", "promoted_from_score": True}
                scored.append(row)
        scored.sort(key=lambda x: x.get("_hz_score", 0), reverse=True)
        return scored[:limit]
    top_picks_short = _horizon_picks(results, "short")
    top_picks_mid = _horizon_picks(results, "mid")
    top_picks_long = _horizon_picks(results, "long")
    if not top_picks_short:
        top_picks_short = top_picks
    final_verdict_scan = {
        "preferred_horizon": "short",
        "short_count": len(top_picks_short),
        "mid_count": len(top_picks_mid),
        "long_count": len(top_picks_long),
        "headline": (
            f"Short-term focus: {len(top_picks_short)} pick(s). "
            f"Mid: {len(top_picks_mid)}, Long: {len(top_picks_long)}."
        ),
        "best_short": (top_picks_short[0].get("symbol") if top_picks_short else None),
    }
    _record_symbol_outcomes(results)  # feeds universe self-pruning — see _build_scan_universe
    watchlist_candidates = []
    if not top_picks:
        watchlist_candidates = results[:3]

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
    sell_count = len([r for r in results if r.get("decision") == "SELL"])
    hold_count = len([r for r in results if r.get("decision") == "HOLD"])

    if buy_count >= 5:
        market_mood = "Bullish"
    elif sell_count > buy_count:
        market_mood = "Bearish"
    elif buy_count > 0:
        market_mood = "Selective"
    else:
        market_mood = "Cautious"

    verdict = f"{len(top_picks)} strong opportunity(ies) found" if top_picks else "DO NOT BUY ANY STOCK TODAY — market conditions cautious"
    if cancelled:
        verdict = f"Scan stopped early ({processed}/{total} stocks checked) — " + verdict

    final_result = {
        "scanned": len(results),
        "universe_size": len(universe),
        "watchlist_size": len(_load_watchlist()),
        "recommendations": top_picks_short if "top_picks_short" in dir() else top_picks,
        "recommendations_short": top_picks_short if "top_picks_short" in dir() else top_picks,
        "recommendations_mid": top_picks_mid if "top_picks_mid" in dir() else [],
        "recommendations_long": top_picks_long if "top_picks_long" in dir() else [],
        "final_verdict": final_verdict_scan if "final_verdict_scan" in dir() else None,
        "watchlist_candidates": watchlist_candidates,
        "verdict": verdict,
        "market_mood": market_mood,
        "cancelled": cancelled,
        "market_stats": {
            "buy_signals": buy_count,
            "sell_signals": sell_count,
            "hold_signals": hold_count,
            "cautious": len(results) - buy_count - sell_count - hold_count,
        },
        "all_results": results,
        "errors": errors,
    }

    elapsed_final = round(time.time() - start_time, 1)
    final_result["elapsed_seconds"] = elapsed_final
    final_result["lite"] = lite
    final_result["scanned_at"] = datetime.now(IST).isoformat()

    if cancelled and isinstance(final_result, dict):
        final_result = {
            **final_result,
            "partial": True,
            "stopped_early": True,
            "verdict": final_result.get("verdict")
            or f"Stopped early — {processed}/{total} symbols scored",
        }
    _done_payload = {
        "status": "done",  # always done so UI can load partial result
        "cancelled": bool(cancelled),
        "partial": bool(cancelled),
        "total": total,
        "processed": processed,
        "elapsed": elapsed_final,
        "result": final_result,
        "error": None,
    }
    _redis_set(SCAN_TASK_PREFIX + task_id, _done_payload, ttl=3600)
    try:
        await _ws_push_scan(task_id, _done_payload)
    except Exception:
        pass
    try:
        metrics.inc("stockky_scan_complete_total")
        metrics.set_gauge("stockky_last_scan_symbols", float(processed))
        metrics.set_gauge("stockky_last_scan_elapsed_sec", float(elapsed_final))
    except Exception:
        pass

    # Cache scan result (full OR partial after Stop) so refresh / dashboard keep last scan
    if results:
        _redis_set(LAST_FULL_SCAN_KEY, {
            "task_id": task_id,
            "result": final_result,
            "scanned_at": final_result.get("scanned_at"),
            "universe_size": final_result.get("universe_size"),
            "partial": bool(cancelled),
            "cancelled": bool(cancelled),
            "processed": processed,
            "total": total,
        }, ttl=LAST_FULL_SCAN_TTL)

    if not cancelled:
        _send_scan_notification(
            final_result.get("recommendations", []),
            final_result["verdict"],
            final_result["scanned"],
            final_result["universe_size"],
        )

# ── Cached Market Movers Data ──────────────────────────────────────────────
def _get_nifty50_data() -> List[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{MARKET_MOVERS_CACHE_PREFIX}{today}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, list) and len(cached) > 0:
        logger.info("Serving cached market movers data for %s", today)
        return cached

    logger.info("Fetching fresh market movers data from yfinance for %s", today)
    nifty_symbols = _get_nifty_indices()[:50]
    data = []
    for sym in nifty_symbols:
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            hist = ticker.history(period="1d", interval="1m")
            if hist.empty:
                continue
            latest = hist.iloc[-1]
            prev_close = hist.iloc[0]["Close"]
            change_pct = (latest["Close"] - prev_close) / prev_close * 100
            data.append({
                "symbol": sym,
                "price": round(latest["Close"], 2),
                "change": round(latest["Close"] - prev_close, 2),
                "change_pct": round(change_pct, 2),
                "volume": int(latest["Volume"]),
                "high": round(latest["High"], 2),
                "low": round(latest["Low"], 2),
            })
        except Exception as e:
            logger.warning(f"Could not fetch {sym}: {e}")
    _redis_set(cache_key, data, ttl=86400)
    return data

# ── Pydantic models ──────────────────────────────────────────────────────────
class WatchlistUpdate(BaseModel):
    symbols: List[str]

class NotificationChannelUpdate(BaseModel):
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    callmebot_user: str | None = None
    callmebot_phone: str | None = None
    callmebot_apikey: str | None = None
    callmebot_users: str | None = None
    enabled: dict | None = None

# ── Routes ──────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Stockky API Gateway",
        "version": "2.5.16",
        "status": "running",
        "parallel_workers": MAX_PARALLEL_WORKERS,
        "endpoints": {
            "/health": "GET – health check",
            "/ready": "GET – lightweight readiness check",
            "/system/health": "GET – health of all downstream services",
            "/wake/all": "POST – wake all services",
            "/watchlist": "GET/POST – manage watchlist",
            "/watchlist/add": "POST – add symbols",
            "/watchlist/{symbol}": "DELETE – remove symbol",
            "/stock/{symbol}": "GET – get decision for a symbol",
            "/scan": "GET – synchronous scan (legacy)",
            "/scan/start": "POST – start async parallel scan, returns task_id",
            "/scan/status/{task_id}": "GET – get progress/result of async scan",
            "/scan/watchlist": "GET – scan only your watchlist",
            "/scan/universe": "GET – preview current scan universe",
            "/scan/universe/cache": "DELETE – clear universe cache",
            "/searched": "GET – list searched symbols",
            "/market/top-gainers": "GET – top 10 gainers",
            "/market/top-losers": "GET – top 10 losers",
            "/market/most-active": "GET – top 10 most active by volume",
            "/market/trending": "GET – trending stocks (momentum + news)",
            "/market/indices": "GET – live NIFTY 50 & SENSEX (IST time, Cache-Control)",
            "/notifications/health": "GET – notification service health",
            "/notifications/config": "GET/POST – get/update notification config",
            "/notifications/config/{channel}": "DELETE – clear a channel",
            "/notifications/test": "POST – test notifications",
            "/notifications/send-picks": "POST – manually send picks to Telegram (splits long messages)",
            "/training/status": "GET – get training model status",
            "/training/train": "POST – trigger a new training run",
            "/training/score/{symbol}": "GET – get training intelligence score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/quote/{symbol}")
def proxy_quote(symbol: str):
    """Near-realtime quote proxy (market-data). Used by stock detail 30s refresh."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        metrics.inc("stockky_quote_proxy_total")
    except Exception:
        pass
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{sym}", timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            price = data.get("price") or data.get("regularMarketPrice") or data.get("close") or data.get("last")
            return {
                "symbol": sym,
                "price": price,
                "close": data.get("close") or price,
                "as_of": data.get("as_of") or datetime.now(IST).isoformat(),
                "source": data.get("source") or "market-data",
                "raw": {k: data.get(k) for k in ("volume", "delivery_pct", "change_pct") if k in data},
            }
    except Exception as e:
        logger.warning("quote proxy %s: %s", sym, e)
    # fallback yfinance light
    try:
        t = yf.Ticker(f"{sym}.NS")
        info = t.fast_info if hasattr(t, "fast_info") else {}
        price = None
        try:
            price = float(info.get("last_price") or info.get("lastPrice") or 0) or None
        except Exception:
            price = None
        return {"symbol": sym, "price": price, "close": price, "as_of": datetime.now(IST).isoformat(), "source": "yfinance_fast"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"quote unavailable: {e}")


@app.get("/circuits")
def circuits_status():
    """Circuit breaker states for downstream dependencies."""
    snaps = all_snapshots()
    open_n = sum(1 for v in snaps.values() if v.get("state") == "open")
    metrics.set_gauge("stockky_circuits_open", float(open_n))
    return {"circuits": snaps}


@app.get("/ops/rate-limits")
async def ops_rate_limits():
    """Dashboard payload: rate-limit events + circuit breakers (last 1h)."""
    return rate_limit_monitor.snapshot(circuits=all_snapshots())


@app.post("/ops/rate-limits/event")
async def ops_rate_limits_event(payload: dict):
    """Services may POST {source, status, path?, detail?, symbol?} when they hit 429/503."""
    try:
        rate_limit_monitor.record(
            source=str(payload.get("source") or "unknown"),
            status=int(payload.get("status") or 0),
            path=str(payload.get("path") or ""),
            detail=str(payload.get("detail") or ""),
            symbol=str(payload.get("symbol") or ""),
        )
        metrics.inc("rate_limit_events", source=str(payload.get("source") or "unknown"), status=str(payload.get("status") or 0))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=400)


@app.get("/metrics")
def metrics_endpoint(request: Request):
    """JSON metrics by default; ?format=prom for Prometheus text."""
    fmt = (request.query_params.get("format") or "json").lower()
    if fmt in ("prom", "prometheus", "text"):
        return Response(content=metrics.prometheus_text(), media_type="text/plain; version=0.0.4")
    return metrics.snapshot()


@app.post("/ops/check-alert")
async def ops_check_alert():
    """Evaluate simple thresholds and notify if unhealthy (for cron).

    Alerts when any circuit is open or recent dependency error rate is high.
    """
    snaps = all_snapshots()
    open_circuits = [k for k, v in snaps.items() if v.get("state") == "open"]
    snap = metrics.snapshot()
    counters = snap.get("counters") or {}
    errors = sum(v for k, v in counters.items() if "dependency_errors" in k)
    oks = sum(v for k, v in counters.items() if "dependency_ok" in k)
    total = errors + oks
    err_rate = (errors / total) if total else 0.0

    problems = []
    if open_circuits:
        problems.append(f"Open circuits: {', '.join(open_circuits)}")
    if total >= 20 and err_rate >= 0.4:
        problems.append(f"Dependency error rate {err_rate:.0%} ({int(errors)}/{int(total)})")

    if not problems:
        return {"alerted": False, "ok": True, "open_circuits": [], "error_rate": err_rate}

    title = "⚠️ Stockky ops alert"
    message = " | ".join(problems)
    delivered = False
    try:
        client = _get_http_client()  # shared keepalive pool
        if True:
            r = await client.post(
                f"{NOTIFICATION_URL.rstrip('/')}/notify",
                json={
                    "title": title,
                    "message": message[:1500],
                    "channel": "all",
                    "urgency": "high",
                },
            )
            delivered = r.status_code == 200 and bool((r.json() or {}).get("delivered"))
    except Exception as e:
        logger.warning("ops alert notify failed: %s", e)
    metrics.inc("stockky_ops_alerts_total")
    return {
        "alerted": True,
        "delivered": delivered,
        "problems": problems,
        "open_circuits": open_circuits,
        "error_rate": err_rate,
    }


@app.post("/ops/refresh-static-params")
async def ops_refresh_static_params(limit: int = 60):
    """Nightly job: re-fetch fundamentals / events / news for scan universe into Redis (24h TTL).

    Live quotes & decisions still refresh during market hours; this only warms
    slow-changing layers so daytime traffic hits cache and stays under rate limits.
    """
    universe = _build_scan_universe()[: max(10, min(limit, 80))]
    refreshed = {"fundamental": 0, "event": 0, "news": 0, "errors": 0}
    client = _get_http_client()  # shared keepalive pool
    if True:
        for sym in universe:
            base = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
            if not base:
                continue
            try:
                # Force refresh: clear cache keys then re-fetch into 24h TTL
                try:
                    if _redis:
                        _redis.delete(f"{FUNDAMENTAL_CACHE_PREFIX}{base}")
                        _redis.delete(f"{EVENT_CACHE_PREFIX}{base}")
                        _redis.delete(f"{NEWS_CACHE_PREFIX}{base}")
                except Exception:
                    pass
                metrics_data, _fb = await _fetch_fundamental_cached(base, client)
                if metrics_data is not None:
                    refreshed["fundamental"] += 1
                ev = await _fetch_events_cached(base, client)
                if ev is not None:
                    refreshed["event"] += 1
                news = await _fetch_news_cached(base, client)
                if news is not None:
                    refreshed["news"] += 1
            except Exception as e:
                refreshed["errors"] += 1
                logger.warning("refresh-static-params %s: %s", base, e)
            await asyncio.sleep(0.25)

    # Also refresh hot-stocks cache so next UI load is cheap
    try:
        if _redis:
            _redis.delete(HOT_STOCKS_CACHE_KEY)
    except Exception:
        pass

    return {
        "status": "ok",
        "universe_size": len(universe),
        "refreshed": refreshed,
        "ttl_seconds": STATIC_PARAM_TTL,
        "at": datetime.now(IST).isoformat(),
    }





@app.get("/market/history/{symbol}")
async def market_history(symbol: str, period: str = "1mo"):
    """Chart candles — market-data first, training history fallback."""
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
    md_period = {"1d": "1mo", "5d": "1mo", "1mo": "1mo", "1y": "1y", "5y": "5y", "3mo": "3mo", "6mo": "6mo"}.get(period, "1mo")
    last_err = None
    client = _get_http_client()  # shared keepalive pool
    if True:
        try:
            try:
                await client.get(f"{MARKET_DATA_URL}/health", params={"warm": "true"})
            except Exception:
                pass
            r = await client.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": md_period, "interval": "1d"})
            if r.status_code == 200:
                data = r.json()
                candles = data.get("candles") or []
                points = []
                for c in candles:
                    if c.get("close") is None:
                        continue
                    points.append({
                        "date": c.get("date"),
                        "open": c.get("open"),
                        "high": c.get("high"),
                        "low": c.get("low"),
                        "close": c.get("close"),
                        "volume": c.get("volume") or 0,
                    })
                if points:
                    first = points[0]["close"] or 0
                    last = points[-1]["close"] or 0
                    chg = round((last - first) / first * 100, 2) if first else None
                    return {"symbol": sym, "period": period, "points": points, "change_pct": chg, "source": "market-data"}
            last_err = f"market-data HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        # training service history
        try:
            r2 = await client.get(f"{TRAINING_URL}/api/stock/history/{sym}", params={"period": period if period in ("1d","5d","1mo","1y","5y") else "1mo"})
            if r2.status_code == 200:
                data = r2.json()
                data["source"] = data.get("source") or "training"
                return data
            last_err = f"{last_err}; training HTTP {r2.status_code}"
        except Exception as e:
            last_err = f"{last_err}; training {e}"
    raise HTTPException(status_code=503, detail=f"Chart unavailable for {sym}: {last_err}")


@app.get("/ops/db-status")
async def ops_db_status():
    """Frontend banner: is Supabase/Postgres connected on decision-prediction/training?"""
    try:
        client = _get_http_client()  # shared keepalive pool
        if True:
            r = await client.get(f"{TRAINING_URL.rstrip('/')}/health")
            if r.status_code == 200:
                data = r.json()
                return {
                    "ok": True,
                    "source": "training",
                    **{k: data.get(k) for k in (
                        "db_backend", "db_durable", "db_connected", "db_provider",
                        "db_message", "db_error", "status",
                    ) if k in data or True},
                }
            return {
                "ok": False,
                "db_connected": False,
                "db_message": f"Training service health HTTP {r.status_code}",
                "db_error": f"Training service health HTTP {r.status_code}",
            }
    except Exception as e:
        return {
            "ok": False,
            "db_connected": False,
            "db_backend": "unknown",
            "db_durable": False,
            "db_message": f"Cannot reach training service: {e}",
            "db_error": str(e)[:200],
        }


@app.post("/ops/idle-tick")
async def ops_idle_tick():
    """Called by frontend after ~5 min idle during market hours only.

    Light background work: refresh indices cache if stale, optionally warm
    hot-stocks if cache missing. Never runs heavy scans. Off-hours: no-op.
    """
    phase = _market_session_phase_ist()
    if phase not in ("preopen", "open", "post"):
        return {
            "ran": False,
            "reason": "off_market",
            "phase": phase,
            "note": "Background idle work only during market window; use manual Wake otherwise.",
        }
    did = []
    try:
        # Indices: cheap, 5 min cache already
        get_market_indices(force_refresh=False)
        did.append("indices")
    except Exception as e:
        logger.debug("idle-tick indices: %s", e)
    try:
        cached = _redis_get(HOT_STOCKS_CACHE_KEY)
        if not cached:
            # Only rebuild if empty — respects short market TTL
            await stockky_hot_stocks(force=False)
            did.append("hot_stocks_miss")
        else:
            did.append("hot_stocks_cached")
    except Exception as e:
        logger.debug("idle-tick hot: %s", e)
    return {
        "ran": True,
        "phase": phase,
        "actions": did,
        "at": datetime.now(IST).isoformat(),
    }



@app.post("/ops/qstash/tick")
@app.get("/ops/qstash/tick")
async def ops_qstash_tick(request: Request):
    """QStash callback: light keepalive + optional evaluate. No Redis required."""
    try:
        sig = request.headers.get("Upstash-Signature") or request.headers.get("upstash-signature") or ""
        body = await request.body()
        if qstash_client is not None and not qstash_client.verify_signature(sig, body):
            raise HTTPException(status_code=401, detail="Invalid QStash signature")
    except HTTPException:
        raise
    except Exception:
        pass
    # Light warm only
    results = {"ok": True, "source": "qstash", "warm": []}
    try:
        client = _get_http_client()
        for path in ["/health", "/ops/keepalive"]:
            try:
                # local self
                results["warm"].append(path)
            except Exception as e:
                results["warm"].append(f"{path}: {e}")
        # Fan-out wake is expensive — only if body asks
        try:
            import json as _json
            payload = _json.loads(body.decode() or "{}") if body else {}
        except Exception:
            payload = {}
        if payload.get("action") in ("wake", "wake-all", "scan"):
            try:
                await ops_keepalive(deep=False)
                results["keepalive"] = True
            except Exception as e:
                results["keepalive_error"] = str(e)[:120]
    except Exception as e:
        results["error"] = str(e)[:200]
    return results


@app.post("/ops/qstash/publish")
async def ops_qstash_publish(request: Request):
    """Manually publish a delayed callback (admin / scheduler)."""
    if qstash_client is None or not qstash_client.enabled():
        return {"ok": False, "error": "QStash not configured (set QSTASH_TOKEN)"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dest = body.get("url") or body.get("destination")
    delay = int(body.get("delay_seconds") or 0)
    if not dest:
        return qstash_client.schedule_gateway_tick(delay_seconds=delay, body=body)
    return qstash_client.publish(dest, body.get("payload") or {}, delay_seconds=delay)


@app.get("/wake-all")
@app.post("/wake-all")
async def wake_all_services():
    """Hit health?warm=1 on all upstream services (free-tier anti-sleep)."""
    results = await _wake_required_services()
    ok = sum(1 for v in results.values() if v.get("ok"))
    return {
        "status": "ok" if ok else "degraded",
        "warmed_ok": ok,
        "total": len(results),
        "services": results,
    }


@app.get("/health")
def health(warm: bool = False):
    return {
        "status": "ok",
        "service": "api-gateway",
        "redis": bool(_redis),
        "ready": True
    }

@app.get("/ready")
def ready():
    return {"ready": bool(_redis)}

@app.get("/system/health")
async def system_health():
    async def check(name: str, url: str, required: bool):
        if not url:
            return name, {"ok": False, "required": required, "status": "not_configured", "url": None}
        timeout = 15 if name == "market-data" else 10
        try:
            client = _get_http_client()  # shared keepalive pool
            if True:
                resp = await client.get(f"{url.rstrip('/')}/health")
            if resp.status_code == 200:
                return name, {"ok": True, "required": required, "status": "up", "url": url}
            return name, {"ok": False, "required": required, "status": f"http_{resp.status_code}", "url": url}
        except Exception as e:
            return name, {"ok": False, "required": required, "status": "unreachable", "error": str(e)[:100], "url": url}

    results = await asyncio.gather(
        *(check(name, cfg["url"], cfg["required"]) for name, cfg in SYSTEM_SERVICES.items())
    )
    services = {"api-gateway": {"ok": True, "required": True, "status": "up", "url": None}}
    services.update(dict(results))
    required_ok = all(v["ok"] for v in services.values() if v["required"])
    all_ok = all(v["ok"] for v in services.values())
    return {"required_ok": required_ok, "all_ok": all_ok, "services": services}

# ── Wake all services ──────────────────────────────────────────────────
@app.post("/wake/all")
async def wake_all_services():
    results = {}
    client = _get_http_client()  # shared keepalive pool
    if True:
        for name, svc in SYSTEM_SERVICES.items():
            url = svc["url"]
            if not url:
                results[name] = {"ok": False, "error": "no url"}
                continue
            try:
                resp = await client.get(f"{url}/health")
                results[name] = {"ok": resp.status_code == 200, "status": resp.status_code}
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
    return {"results": results}

# ── Watchlist endpoints ──────────────────────────────────────────────────────
@app.get("/watchlist")
def get_watchlist():
    return {"symbols": _load_watchlist()}

@app.post("/watchlist")
def set_watchlist(update: WatchlistUpdate):
    symbols = [s.strip().upper() for s in update.symbols]
    _save_watchlist(symbols)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"symbols": symbols}

@app.post("/watchlist/add")
def add_to_watchlist(update: WatchlistUpdate):
    current = set(_load_watchlist())
    added, already = [], []
    for s in update.symbols:
        su = s.strip().upper().replace(".NS", "").replace(".BO", "")
        if not su:
            continue
        if su in current:
            already.append(su)
        else:
            current.add(su)
            added.append(su)
    symbols = sorted(current)
    _save_watchlist(symbols)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    msg = None
    if already and not added:
        msg = "Already in watchlist"
    elif already:
        msg = f"Already in watchlist: {', '.join(already)}"
    return {"symbols": symbols, "added": added, "already": already, "message": msg}

@app.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    current = _load_watchlist()
    updated = [s for s in current if s != symbol.upper()]
    _save_watchlist(updated)
    return {"symbols": updated}

# ── Added from first version: /events/{symbol} endpoint ─────────────────
@app.get("/events/{symbol}")
def get_symbol_events(symbol: str):
    """Frontend-facing proxy for the Analysis page's event section —
    previously the only event data reaching the frontend was whatever
    decision-engine happened to pass through on the Decision object
    (next_earnings_date only); this exposes event-tracker-service's
    full categorized (upcoming/recent/recent_changes) view directly."""
    try:
        resp = httpx.get(f"{EVENT_URL}/events/{symbol.upper()}/categorized", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch categorized events for {symbol}: {e}")
        raise HTTPException(status_code=502, detail=f"event-tracker-service unreachable: {e}")

# ── Searched symbols ────────────────────────────────────────────────────────
@app.get("/searched")
def get_searched_symbols():
    return {"symbols": _load_searched()}

# ── Stock decision ──────────────────────────────────────────────────────────
@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    original = symbol.strip()
    resolved = _resolve_symbol(original)
    if resolved is None:
        symbol_to_use = original.upper()
        corrected_from = None
    elif resolved != original.upper():
        symbol_to_use = resolved
        corrected_from = original.upper()
    else:
        symbol_to_use = original.upper()
        corrected_from = None

    _add_searched(symbol_to_use)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass

    try:
        # Interactive Analyse always requests live decide (skip weak cache)
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol_to_use}",
            params={"already_owned": already_owned, "force": "true"},
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json()
        result = _normalize_decision_response(raw, symbol_to_use)

        reasons = result.get("reasons") if isinstance(result.get("reasons"), dict) else {}
        if not isinstance(reasons, dict):
            reasons = {}
            result["reasons"] = reasons

        # Live quote + S/R always (works off-hours too)
        price = result.get("close")
        if price is None:
            price = _fetch_price_from_quote(symbol_to_use)
            if price is not None:
                result["close"] = price
        if result.get("close") is not None:
            result["data_insufficient"] = False
            c = float(result["close"])
            if result.get("support") is None:
                result["support"] = round(c * 0.97, 2)
            if result.get("resistance") is None:
                result["resistance"] = round(c * 1.03, 2)

        # Live technical when decide said unavailable / default 50
        tech_blob = " ".join(str(x) for x in (reasons.get("technical") or [])).lower()
        need_tech = (
            "temporarily unavailable" in tech_blob
            or "error processing" in tech_blob
            or "recovering" in tech_blob
            or result.get("technical_score") in (None, 50)
        )
        if need_tech:
            try:
                tr = httpx.get(f"{TECHNICAL_URL}/analyze/{symbol_to_use}", timeout=45)
                if tr.status_code == 200:
                    td = tr.json() or {}
                    if td.get("technical_score") is not None:
                        result["technical_score"] = td.get("technical_score")
                    if td.get("close") is not None:
                        result["close"] = td.get("close")
                        result["data_insufficient"] = False
                    if td.get("support") is not None:
                        result["support"] = td.get("support")
                    if td.get("resistance") is not None:
                        result["resistance"] = td.get("resistance")
                    if td.get("reasons"):
                        reasons["technical"] = td.get("reasons") if isinstance(td.get("reasons"), list) else [str(td.get("reasons"))]
                        result["reasons"] = reasons
                    elif td.get("close"):
                        reasons["technical"] = [f"Live technical refreshed · close ₹{td.get('close')}"]
                        result["reasons"] = reasons
            except Exception as te:
                logger.warning("live technical enrich %s: %s", symbol_to_use, te)

        # Ensure S/R after technical enrich
        if result.get("close") is not None:
            c = float(result["close"])
            if result.get("support") is None:
                result["support"] = round(c * 0.97, 2)
            if result.get("resistance") is None:
                result["resistance"] = round(c * 1.03, 2)

        _merge_fundamentals(result, symbol_to_use)

        # If fund still fallback / unavailable text, refresh reasons from live fundamental
        fund_blob = " ".join(str(x) for x in (reasons.get("fundamental") or [])).lower()
        if "unavailable" in fund_blob or "error processing" in fund_blob or result.get("fundamental_fallback"):
            try:
                fr = httpx.get(f"{FUNDAMENTAL_URL}/analyze/{symbol_to_use}", timeout=45)
                if fr.status_code == 200:
                    fd = fr.json() or {}
                    if fd.get("fundamental_score") is not None:
                        result["fundamental_score"] = fd.get("fundamental_score")
                    if fd.get("metrics"):
                        result["fundamental_metrics"] = fd.get("metrics")
                    if fd.get("reasons"):
                        reasons["fundamental"] = fd["reasons"] if isinstance(fd["reasons"], list) else [str(fd["reasons"])]
                        result["reasons"] = reasons
                    result["fundamental_fallback"] = bool(fd.get("fallback_used"))
                    if fd.get("valuation"):
                        result["valuation"] = fd.get("valuation")
                    if fd.get("sector"):
                        result["sector"] = fd.get("sector")
            except Exception as fe:
                logger.warning("live fundamental enrich %s: %s", symbol_to_use, fe)

        if result.get("news_score") is None:
            news = _fetch_news(symbol_to_use)
            if news:
                result["news_score"] = news.get("news_score")
                reasons = result.get("reasons", {})
                if news.get("reasons"):
                    reasons["news"] = news["reasons"]
                    result["reasons"] = reasons

        if result.get("event_risk") is False and not result.get("reasons", {}).get("event"):
            events = _fetch_events(symbol_to_use)
            if events and events.get("next_earnings_date"):
                result["event_risk"] = True
                reasons = result.get("reasons", {})
                reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                result["reasons"] = reasons

        if result.get("prediction_score") is None:
            try:
                pred_resp = httpx.get(f"{PREDICTION_URL}/predict/{symbol_to_use}", timeout=60)
                if pred_resp.status_code == 200:
                    pred_data = pred_resp.json()
                    if pred_data.get("model_loaded"):
                        result["prediction_score"] = pred_data.get("prediction_score")
                        result["prediction_note"] = pred_data.get("note")
            except Exception as e:
                logger.warning(f"Prediction service lookup failed for {symbol_to_use}: {e}")

        if corrected_from:
            result["corrected_from"] = corrected_from
            result["symbol"] = symbol_to_use

        result["natural_language_summary"] = _generate_summary(result)

        # Soft data-quality flags for UI (free-tier honesty)
        flags = []
        level = "high"
        if result.get("fundamental_fallback") or result.get("data_insufficient"):
            flags.append("Fundamentals partial/fallback")
            level = "medium"
        if result.get("news_score") is None:
            flags.append("News unavailable")
            level = "low" if level != "high" else "medium"
        if result.get("prediction_score") is None:
            flags.append("Model score missing")
        tech_reasons = (result.get("reasons") or {}).get("technical") or []
        if any("delivery" in str(r).lower() and "unavailable" in str(r).lower() for r in tech_reasons):
            flags.append("Delivery % not official")
            level = "medium"
        if result.get("training_score") in (None, 0, 50):
            flags.append("Training signal thin")
        result["data_quality"] = {
            "level": level if flags else "high",
            "flags": flags,
            "note": (
                "Scores may be soft — limited free data"
                if flags
                else "Core inputs present"
            ),
        }
        return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            suggestions = difflib.get_close_matches(symbol_to_use, _get_all_known_symbols(), n=3, cutoff=0.5)
            suggestion_text = f"Symbol '{symbol_to_use}' not found. Did you mean: {', '.join(suggestions)}?" if suggestions else f"Symbol '{symbol_to_use}' not found."
            raise HTTPException(status_code=404, detail=suggestion_text)
        else:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Decision engine unreachable: {e}")

# ── Legacy sync fallback helpers ──────────────────────────────────────────
def _merge_fundamentals(normalized: dict, symbol: str):
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        metrics = cached.get("metrics")
        fallback_used = cached.get("fallback", False)
        if metrics:
            normalized["fundamental_metrics"] = metrics
            normalized["fundamental_fallback"] = fallback_used
            return

    try:
        resp = httpx.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            metrics = data.get("metrics")
            fallback_used = data.get("fallback_used", False)
            _redis_set(cache_key, {"metrics": metrics, "fallback": fallback_used}, ttl=STATIC_PARAM_TTL)
            normalized["fundamental_metrics"] = metrics if metrics else {}
            normalized["fundamental_fallback"] = fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")

def _fetch_news(symbol: str) -> Optional[dict]:
    cache_key = f"{NEWS_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        resp = httpx.get(f"{NEWS_URL}/analyze/{symbol}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                ttl = 3600 if _is_market_open_ist() else STATIC_PARAM_TTL
                _redis_set(cache_key, data, ttl=ttl)
                return data
    except Exception as e:
        logger.warning(f"News fetch failed for {symbol}: {e}")
    return None

def _fetch_events(symbol: str) -> Optional[dict]:
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        resp = httpx.get(f"{EVENT_URL}/events/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                _redis_set(cache_key, data, ttl=STATIC_PARAM_TTL)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {e}")
    return None

# ── Legacy synchronous scan ──────────────────────────────────────────────────
@app.get("/scan")
def run_scan(force_refresh: bool = False):
    if force_refresh and _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass

    universe = _build_scan_universe()
    results = []
    errors = []

    with httpx.Client(timeout=150) as client:
        for symbol in universe:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                raw = resp.json()
                normalized = _normalize_decision_response(raw, symbol)

                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                _merge_fundamentals(normalized, symbol)

                if normalized.get("news_score") is None:
                    news = _fetch_news(symbol)
                    if news:
                        normalized["news_score"] = news.get("news_score")
                        reasons = normalized.get("reasons", {})
                        if news.get("reasons"):
                            reasons["news"] = news["reasons"]
                            normalized["reasons"] = reasons

                if normalized.get("event_risk") is False and not normalized.get("reasons", {}).get("event"):
                    events = _fetch_events(symbol)
                    if events:
                        # See the async analyzer's equivalent block for why
                        # the full dict is passed through, not just
                        # next_earnings_date.
                        normalized["event_data"] = events
                        if events.get("next_earnings_date"):
                            normalized["event_risk"] = True
                            reasons = normalized.get("reasons", {})
                            reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                            normalized["reasons"] = reasons

                if normalized.get("prediction_score") is None:
                    try:
                        pred_resp = client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
                        if pred_resp.status_code == 200:
                            pred_data = pred_resp.json()
                            if pred_data.get("model_loaded"):
                                normalized["prediction_score"] = pred_data.get("prediction_score")
                                normalized["prediction_note"] = pred_data.get("note")
                    except Exception as e:
                        logger.warning(f"Prediction service lookup failed during scan for {symbol}: {e}")

                # Adds a concrete calendar-date holding period estimate
                # alongside whatever decision-engine's own holding_period
                # string is (often a static "2-6 weeks" or "N/A") — kept as
                # a separate field so nothing that already reads
                # holding_period breaks.
                entry = normalized.get("entry_range") or {}
                entry_price = entry.get("low") or normalized.get("close")
                normalized["holding_period_estimate"] = _estimate_holding_period(
                    entry_price, normalized.get("target"), normalized.get("decision")
                )
                normalized["natural_language_summary"] = _generate_summary(normalized)
                results.append(normalized)
            except httpx.HTTPError as e:
                logger.warning("Scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = _select_top_picks(actionable, limit=5)
    # horizon picks for sync scan as well (though sync is legacy)
    def _horizon_picks_sync(results_list, horizon_key, limit=5):
        scored = []
        for r in results_list:
            if r.get("decision") == "ERROR":
                continue
            hz = (r.get("horizons") or {}).get(horizon_key) or {}
            sc = hz.get("score")
            if sc is None:
                sc = r.get("combined_score", 0) or 0
                if horizon_key == "mid":
                    sc = sc * 0.95
                elif horizon_key == "long":
                    sc = (r.get("fundamental_score") or sc) * 0.9 + (r.get("combined_score") or 0) * 0.1
            decision = hz.get("decision") or r.get("decision")
            min_sc = {"short": 54, "mid": 56, "long": 58}.get(horizon_key, 54)
            if decision in ("BUY NOW", "PREPARE TO BUY") or (sc or 0) >= min_sc:
                row = {**r, "_hz_score": sc, "horizon_focus": horizon_key}
                if decision == "DO NOT BUY" and (sc or 0) >= min_sc:
                    row = {**row, "decision": "PREPARE TO BUY", "promoted_from_score": True}
                scored.append(row)
        scored.sort(key=lambda x: x.get("_hz_score", 0), reverse=True)
        return scored[:limit]
    top_picks_short = _horizon_picks_sync(results, "short")
    top_picks_mid = _horizon_picks_sync(results, "mid")
    top_picks_long = _horizon_picks_sync(results, "long")
    if not top_picks_short:
        top_picks_short = top_picks
    final_verdict_scan = {
        "preferred_horizon": "short",
        "short_count": len(top_picks_short),
        "mid_count": len(top_picks_mid),
        "long_count": len(top_picks_long),
        "headline": f"Short: {len(top_picks_short)} pick(s). Mid: {len(top_picks_mid)}, Long: {len(top_picks_long)}.",
        "best_short": top_picks_short[0].get("symbol") if top_picks_short else None,
    }
    _record_symbol_outcomes(results)  # feeds universe self-pruning — see _build_scan_universe
    watchlist_candidates = []
    if not top_picks:
        watchlist_candidates = results[:3]

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
    sell_count = len([r for r in results if r.get("decision") == "SELL"])
    hold_count = len([r for r in results if r.get("decision") == "HOLD"])

    if buy_count >= 5:
        market_mood = "Bullish"
    elif sell_count > buy_count:
        market_mood = "Bearish"
    elif buy_count > 0:
        market_mood = "Selective"
    else:
        market_mood = "Cautious"

    verdict = f"{len(top_picks)} strong opportunity(ies) found" if top_picks else "DO NOT BUY ANY STOCK TODAY — market conditions cautious"

    result = {
        "scanned": len(results),
        "universe_size": len(universe),
        "watchlist_size": len(_load_watchlist()),
        "recommendations": top_picks_short,
        "recommendations_short": top_picks_short,
        "recommendations_mid": top_picks_mid,
        "recommendations_long": top_picks_long,
        "final_verdict": final_verdict_scan,
        "watchlist_candidates": watchlist_candidates,
        "verdict": verdict,
        "market_mood": market_mood,
        "market_stats": {
            "buy_signals": buy_count,
            "sell_signals": sell_count,
            "hold_signals": hold_count,
            "cautious": len(results) - buy_count - sell_count - hold_count,
        },
        "all_results": results,
        "errors": errors,
    }

    _send_scan_notification(result.get("recommendations", []), result["verdict"], result["scanned"], result["universe_size"])
    return result

# ============================================================================
# NEW: Batch scan endpoint – used by GitHub Actions runner
# ============================================================================

@app.post("/scan/batch")
async def scan_batch(request: Request):
    """
    Analyse a batch of symbols (max 15) and return results quickly.
    Uses the same parallel logic as the full scan but limited to the batch.
    """
    data = await request.json()
    symbols = data.get("symbols", [])
    if len(symbols) > 15:
        raise HTTPException(status_code=400, detail="Maximum 15 symbols per batch")

    # Use a semaphore to limit concurrent downstream calls inside this batch
    sem = asyncio.Semaphore(10)
    client = _get_http_client()  # shared keepalive pool
    if True:
        tasks = [_analyze_one_symbol_ultra(sym, client, sem) for sym in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        final_results = []
        for sym, result in zip(symbols, results):
            if isinstance(result, Exception):
                final_results.append({"symbol": sym, "decision": "ERROR", "error": str(result)})
            else:
                final_results.append(result)
        return {"results": final_results}

# ── Async scan endpoints ──────────────────────────────────────────────────
@app.post("/scan/start")
def start_scan(
    force_refresh: bool = False,
    lite: bool = None,
    background_tasks: BackgroundTasks = None,
):
    """
    Start async parallel market scan.
    - force_refresh: rebuild universe + ignore last-scan cache
    - lite: skip Gemini + redundant enrichment when Decision already has data (faster on free tier)
    If a recent full scan exists (LAST_FULL_SCAN_TTL) and force_refresh is false, returns cached task.
    """
    try:
        if lite is None:
            use_lite = SCAN_LITE_DEFAULT or _should_force_lite_scan()
        else:
            use_lite = bool(lite)
        # When circuits open, always prefer lite unless caller forced full (lite=False explicitly is respected)

        if force_refresh and _redis:
            try:
                _redis.delete(SCAN_UNIVERSE_KEY)
                _redis.delete(LAST_FULL_SCAN_KEY)
            except Exception:
                pass

        # Serve recent scan from cache unless force_refresh
        if not force_refresh:
            cached = _redis_get(LAST_FULL_SCAN_KEY)
            if cached and isinstance(cached, dict) and cached.get("result"):
                task_id = cached.get("task_id") or str(uuid.uuid4())
                # Re-publish as a finished task so /scan/status works the same
                _redis_set(SCAN_TASK_PREFIX + task_id, {
                    "status": "done",
                    "total": cached["result"].get("universe_size", 0),
                    "processed": cached["result"].get("scanned", 0),
                    "elapsed": cached["result"].get("elapsed_seconds", 0),
                    "result": cached["result"],
                    "error": None,
                    "from_cache": True,
                    "scanned_at": cached.get("scanned_at"),
                }, ttl=3600)
                return {
                    "task_id": task_id,
                    "from_cache": True,
                    "scanned_at": cached.get("scanned_at"),
                    "message": "Returning recent scan result (within cache TTL). Use force_refresh=true for a new run.",
                }

        universe = _build_scan_universe()
        task_id = str(uuid.uuid4())
        background_tasks.add_task(run_scan_parallel, task_id, universe, use_lite)
        return {"task_id": task_id, "from_cache": False, "lite": use_lite, "universe_size": len(universe)}
    except Exception as e:
        logger.error(f"Scan start failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")

@app.get("/scan/last")
def get_last_scan():
    """Last full or partial market scan (Redis). Used on dashboard refresh."""
    cached = _redis_get(LAST_FULL_SCAN_KEY)
    if not cached or not isinstance(cached, dict):
        return {"ok": False, "detail": "No scan cached yet", "result": None}
    return {
        "ok": True,
        "task_id": cached.get("task_id"),
        "scanned_at": cached.get("scanned_at"),
        "partial": bool(cached.get("partial") or cached.get("cancelled")),
        "processed": cached.get("processed"),
        "total": cached.get("total") or cached.get("universe_size"),
        "result": cached.get("result"),
    }


@app.get("/scan/status/{task_id}")
def get_scan_status(task_id: str):
    """Return scan progress. Missing task → soft response (not hard 404) so UI can stop cleanly."""

    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not data:
        return {
            "task_id": task_id,
            "status": "unknown",
            "processed": 0,
            "total": 0,
            "cancelled": True,
            "partial": True,
            "message": "Scan task not found (expired or power-off). Safe to start a new scan.",
            "ok": False,
        }
        # was: raise HTTPException
    if data.get("status") == "running":
        processed = data.get("processed", 0)
        total = data.get("total", 0)
        elapsed = data.get("elapsed", 0)
        if processed > 0 and elapsed > 0:
            avg_time_per_stock = elapsed / processed
            remaining_stocks = total - processed
            estimated_remaining = round(remaining_stocks * avg_time_per_stock, 1)
            data["estimated_remaining"] = estimated_remaining
        else:
            data["estimated_remaining"] = None
    return data

@app.post("/scan/cancel/{task_id}")
def cancel_scan(task_id: str):
    """Request cancel — process-local flag + durable key; stops ASAP."""
    _SCAN_CANCEL_FLAGS.add(task_id)
    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not data:
        return {"status": "cancel_requested", "message": "Task not in memory (already stopped or expired)", "ok": True}
    if data.get("status") != "running":
        return {"status": "already_finished", "task_status": data.get("status"),
                "processed_so_far": data.get("processed", 0), "total": data.get("total", 0)}
    # Dedicated cancel key (progress writes must not wipe this)
    _SCAN_CANCEL_FLAGS.add(task_id)
    _redis_set(SCAN_TASK_PREFIX + task_id + ":cancel", True, ttl=3600)
    # Also mark progress payload so UI sees cancel immediately
    try:
        data = dict(data)
        data["cancel_requested"] = True
        _redis_set(SCAN_TASK_PREFIX + task_id, data, ttl=3600)
    except Exception:
        pass
    return {
        "status": "cancel_requested",
        "processed_so_far": data.get("processed", 0),
        "total": data.get("total", 0),
    }

# ── Watchlist-only scan ──────────────────────────────────────────────────
@app.get("/scan/watchlist")
def scan_watchlist():
    watchlist = _load_watchlist()
    if not watchlist:
        return {
            "scanned": 0,
            "universe_size": 0,
            "watchlist_size": 0,
            "recommendations": [],
            "watchlist_candidates": [],
            "verdict": "Watchlist is empty. Add some symbols first.",
            "market_mood": "Neutral",
            "market_stats": {
                "buy_signals": 0,
                "sell_signals": 0,
                "hold_signals": 0,
                "cautious": 0,
            },
            "all_results": [],
            "errors": [],
        }

    results = []
    errors = []

    with httpx.Client(timeout=180) as client:
        for symbol in watchlist:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                raw = resp.json()
                normalized = _normalize_decision_response(raw, symbol)

                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                _merge_fundamentals(normalized, symbol)

                if normalized.get("news_score") is None:
                    news = _fetch_news(symbol)
                    if news:
                        normalized["news_score"] = news.get("news_score")
                        reasons = normalized.get("reasons", {})
                        if news.get("reasons"):
                            reasons["news"] = news["reasons"]
                            normalized["reasons"] = reasons

                if normalized.get("event_risk") is False and not normalized.get("reasons", {}).get("event"):
                    events = _fetch_events(symbol)
                    if events:
                        # See the async analyzer's equivalent block for why
                        # the full dict is passed through, not just
                        # next_earnings_date.
                        normalized["event_data"] = events
                        if events.get("next_earnings_date"):
                            normalized["event_risk"] = True
                            reasons = normalized.get("reasons", {})
                            reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                            normalized["reasons"] = reasons

                if normalized.get("prediction_score") is None:
                    try:
                        pred_resp = client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
                        if pred_resp.status_code == 200:
                            pred_data = pred_resp.json()
                            if pred_data.get("model_loaded"):
                                normalized["prediction_score"] = pred_data.get("prediction_score")
                                normalized["prediction_note"] = pred_data.get("note")
                    except Exception as e:
                        logger.warning(f"Prediction service lookup failed during watchlist scan for {symbol}: {e}")

                # Adds a concrete calendar-date holding period estimate
                # alongside whatever decision-engine's own holding_period
                # string is (often a static "2-6 weeks" or "N/A") — kept as
                # a separate field so nothing that already reads
                # holding_period breaks.
                entry = normalized.get("entry_range") or {}
                entry_price = entry.get("low") or normalized.get("close")
                normalized["holding_period_estimate"] = _estimate_holding_period(
                    entry_price, normalized.get("target"), normalized.get("decision")
                )
                normalized["natural_language_summary"] = _generate_summary(normalized)
                results.append(normalized)
            except httpx.HTTPError as e:
                logger.warning("Watchlist scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = _select_top_picks(actionable, limit=5)
    # horizon picks for watchlist scan
    def _horizon_picks_wl(results_list, horizon_key, limit=5):
        scored = []
        for r in results_list:
            if r.get("decision") == "ERROR":
                continue
            hz = (r.get("horizons") or {}).get(horizon_key) or {}
            sc = hz.get("score")
            if sc is None:
                sc = r.get("combined_score", 0) or 0
                if horizon_key == "mid":
                    sc = sc * 0.95
                elif horizon_key == "long":
                    sc = (r.get("fundamental_score") or sc) * 0.9 + (r.get("combined_score") or 0) * 0.1
            decision = hz.get("decision") or r.get("decision")
            min_sc = {"short": 54, "mid": 56, "long": 58}.get(horizon_key, 54)
            if decision in ("BUY NOW", "PREPARE TO BUY") or (sc or 0) >= min_sc:
                row = {**r, "_hz_score": sc, "horizon_focus": horizon_key}
                if decision == "DO NOT BUY" and (sc or 0) >= min_sc:
                    row = {**row, "decision": "PREPARE TO BUY", "promoted_from_score": True}
                scored.append(row)
        scored.sort(key=lambda x: x.get("_hz_score", 0), reverse=True)
        return scored[:limit]
    top_picks_short = _horizon_picks_wl(results, "short")
    top_picks_mid = _horizon_picks_wl(results, "mid")
    top_picks_long = _horizon_picks_wl(results, "long")
    if not top_picks_short:
        top_picks_short = top_picks
    final_verdict_scan = {
        "preferred_horizon": "short",
        "short_count": len(top_picks_short),
        "mid_count": len(top_picks_mid),
        "long_count": len(top_picks_long),
        "headline": f"Short: {len(top_picks_short)} pick(s). Mid: {len(top_picks_mid)}, Long: {len(top_picks_long)}.",
        "best_short": top_picks_short[0].get("symbol") if top_picks_short else None,
    }
    _record_symbol_outcomes(results)  # feeds universe self-pruning — see _build_scan_universe

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
    sell_count = len([r for r in results if r.get("decision") == "SELL"])
    hold_count = len([r for r in results if r.get("decision") == "HOLD"])

    if buy_count >= 5:
        market_mood = "Bullish"
    elif sell_count > buy_count:
        market_mood = "Bearish"
    elif buy_count > 0:
        market_mood = "Selective"
    else:
        market_mood = "Cautious"

    verdict = f"{len(top_picks)} opportunity(ies) found" if top_picks else "No strong signals in your watchlist"

    result = {
        "scanned": len(results),
        "universe_size": len(watchlist),
        "watchlist_size": len(watchlist),
        "recommendations": top_picks_short,
        "recommendations_short": top_picks_short,
        "recommendations_mid": top_picks_mid,
        "recommendations_long": top_picks_long,
        "final_verdict": final_verdict_scan,
        "watchlist_candidates": [],
        "verdict": verdict,
        "market_mood": market_mood,
        "market_stats": {
            "buy_signals": buy_count,
            "sell_signals": sell_count,
            "hold_signals": hold_count,
            "cautious": len(results) - buy_count - sell_count - hold_count,
        },
        "all_results": results,
        "errors": errors,
    }

    _send_scan_notification(result.get("recommendations", []), result["verdict"], result["scanned"], result["universe_size"])
    return result

# ── Market routes ────────────────────────────────────────────────────────────
@app.get("/market/session")
def market_session():
    """Current NSE session phase for UI / keep-warm / quote policy."""
    now = datetime.now(IST)
    phase = _market_session_phase_ist()
    return {
        "phase": phase,
        "is_open": phase == "open",
        "is_market_day": phase not in ("closed", "holiday") or (now.weekday() < 5 and not is_nse_holiday(now.date())),
        "is_holiday": phase == "holiday",
        "now_ist": now.isoformat(),
        "session_window": "09:15–15:30 IST Mon–Fri (ex holidays)",
        "quote_polling": phase in ("preopen", "open", "post"),
    }


@app.get("/market/top-gainers")
def market_top_gainers():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["change_pct"], reverse=True)[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/top-losers")
def market_top_losers():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["change_pct"])[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/most-active")
def market_most_active():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["volume"], reverse=True)[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/trending")
def market_trending():
    movers = _get_momentum_movers()
    news = _get_news_mentioned_symbols()
    trending = list(set(movers + news))
    trending_data = []
    for sym in trending[:10]:
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = round(hist["Close"].iloc[-1], 2)
                change = round(hist["Close"].iloc[-1] - hist["Open"].iloc[-1], 2)
                change_pct = round(change / hist["Open"].iloc[-1] * 100, 2)
                trending_data.append({
                    "symbol": sym,
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                })
        except:
            pass
    return {"data": trending_data, "count": len(trending_data)}

# ── IMPROVED /market/indices with IST time ──────────────────────────────
@app.get("/market/indices")
def get_market_indices(force_refresh: bool = False):
    """
    Fetch real-time NIFTY 50 and SENSEX index values with a moderated market score.
    - Uses mapping: -0.3 percentage points -> 0, 0% -> 50, +0.3 percentage points -> 100.
    - Uses IST (Asia/Kolkata) for the fetched_at timestamp, formatted as hh:mm:ss AM/PM.
    - Adds Cache-Control headers to prevent browser caching.
    """
    now_ist = datetime.now(IST)
    fetched_at_str = now_ist.strftime("%I:%M:%S %p")

    if not force_refresh:
        cached = _redis_get(INDICES_CACHE_KEY)
        if cached and isinstance(cached, dict):
            cached["fetched_at"] = fetched_at_str
            return JSONResponse(
                content=cached,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )

    try:
        nifty = yf.Ticker("^NSEI")
        sensex = yf.Ticker("^BSESN")
        nifty_hist = nifty.history(period="1d")
        sensex_hist = sensex.history(period="1d")
        if nifty_hist.empty or sensex_hist.empty:
            raise HTTPException(status_code=503, detail="Index data temporarily unavailable")

        nifty_close = nifty_hist['Close'].iloc[-1]
        nifty_open = nifty_hist['Open'].iloc[0]
        nifty_prev_close = nifty_hist['Close'].iloc[0] if len(nifty_hist) > 1 else nifty_open
        nifty_change = nifty_close - nifty_prev_close
        nifty_change_pct = (nifty_change / nifty_prev_close) * 100

        sensex_close = sensex_hist['Close'].iloc[-1]
        sensex_open = sensex_hist['Open'].iloc[0]
        sensex_prev_close = sensex_hist['Close'].iloc[0] if len(sensex_hist) > 1 else sensex_open
        sensex_change = sensex_close - sensex_prev_close
        sensex_change_pct = (sensex_change / sensex_prev_close) * 100

        avg_change = (nifty_change_pct + sensex_change_pct) / 2
        sensitivity = 0.3
        raw_score = 50 + (avg_change / sensitivity) * 50
        market_score = max(0, min(100, raw_score))

        if market_score >= 60:
            mood = "BULLISH"
        elif market_score <= 40:
            mood = "BEARISH"
        else:
            mood = "NEUTRAL"

        result = {
            "nifty": {
                "price": round(nifty_close, 2),
                "change": round(nifty_change, 2),
                "change_pct": round(nifty_change_pct, 2)
            },
            "sensex": {
                "price": round(sensex_close, 2),
                "change": round(sensex_change, 2),
                "change_pct": round(sensex_change_pct, 2)
            },
            "market_mood": mood,
            "market_score": round(market_score),
            "fetched_at": fetched_at_str,
        }
        _redis_set(INDICES_CACHE_KEY, result, ttl=300)
        _redis_set(INDICES_LAST_KNOWN, result, ttl=86400)
        return JSONResponse(
            content=result,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

    except Exception as e:
        logger.error(f"Error fetching indices: {e}")
        last_known = _redis_get(INDICES_LAST_KNOWN)
        if last_known and isinstance(last_known, dict):
            last_known["fetched_at"] = fetched_at_str
            last_known["stale"] = True
            _redis_set(INDICES_CACHE_KEY, last_known, ttl=60)
            return JSONResponse(
                content=last_known,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )
        else:
            fallback = {
                "nifty": {"price": 0, "change": 0, "change_pct": 0},
                "sensex": {"price": 0, "change": 0, "change_pct": 0},
                "market_mood": "NEUTRAL",
                "market_score": 50,
                "fetched_at": fetched_at_str,
                "stale": True,
                "fallback": True
            }
            _redis_set(INDICES_CACHE_KEY, fallback, ttl=60)
            _redis_set(INDICES_LAST_KNOWN, fallback, ttl=86400)
            return JSONResponse(
                content=fallback,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )

# ── Universe preview ──────────────────────────────────────────────────────
@app.get("/scan/universe")
def get_scan_universe():
    universe = _build_scan_universe()
    searched = _load_searched()
    movers = _get_momentum_movers()
    return {
        "total": len(universe),
        "symbols": universe,
        "searched_symbols_included": [s for s in searched if s in universe],
        "momentum_movers": movers,
    }

@app.delete("/scan/universe/cache")
def clear_universe_cache():
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"message": "Scan universe cache cleared — will rebuild on next scan"}

# ── Notification endpoints ──────────────────────────────────────────────────
@app.get("/notifications/health")
def notifications_health():
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.get("/notifications/config")
def get_notification_config():
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/config", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.post("/notifications/config")
def set_notification_config(update: NotificationChannelUpdate):
    try:
        payload = update.model_dump(exclude_none=True)
        # Wake notification service (free-tier cold start)
        try:
            httpx.get(f"{NOTIFICATION_URL}/health", timeout=8)
        except Exception:
            pass
        resp = httpx.post(
            f"{NOTIFICATION_URL}/config",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.delete("/notifications/config/{channel}")
def delete_notification_channel(channel: str):
    try:
        resp = httpx.delete(f"{NOTIFICATION_URL}/config/{channel}", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


@app.api_route("/notifications/call/me", methods=["GET", "POST"])
def notifications_call_me(request: Request, message: str = "Stockky test call alert"):
    """Proxy CallMeBot test / manual call. CallMeBot can be slow — use long timeout."""
    try:
        msg = message or "Stockky test call alert"
        try:
            httpx.get(f"{NOTIFICATION_URL}/health", timeout=10)
        except Exception:
            pass
        resp = httpx.post(
            f"{NOTIFICATION_URL}/call/me",
            params={"message": msg},
            timeout=70,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="CallMeBot timed out (slow network or free-tier cold start). Config may still be OK — try Test again in 20s.",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"CallMeBot unreachable: {e}")

@app.post("/notifications/test")
def test_notification_channels():
    try:
        resp = httpx.post(f"{NOTIFICATION_URL}/test", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.post("/notifications/send-picks")
def send_picks_to_telegram(payload: dict):
    recs = payload.get("recommendations", [])
    if not recs:
        raise HTTPException(status_code=400, detail="No recommendations provided")

    _wake_notification_service()

    msg_type = payload.get("type", "top5")
    if msg_type == "top5":
        title = "📊 *Top 5 Picks from Market Scan*"
        picks = recs[:5]
    else:
        title = "📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)*"
        picks = recs

    def format_pick(r, index):
        symbol = r.get("symbol", "?")
        decision = r.get("decision", "UNKNOWN")
        score = r.get("combined_score", 0)
        close = r.get("close")
        target = r.get("target")
        stop = r.get("stop_loss")
        entry = r.get("entry_range", {})
        entry_low = entry.get("low")
        entry_high = entry.get("high")
        holding = r.get("holding_period", "N/A")
        lines = []
        lines.append(f"{index}. *{symbol}* – {decision} (Score: {score})")
        if close:
            lines.append(f"   Current: ₹{close:.2f}")
        if entry_low and entry_high:
            lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
        if target:
            upside = ((target - close) / close * 100) if close else 0
            lines.append(f"   Target: ₹{target:.2f} (+{upside:.1f}%)")
        if stop:
            lines.append(f"   Stop: ₹{stop:.2f}")
        if holding != "N/A":
            lines.append(f"   Hold: {holding}")
        return "\n".join(lines)

    if msg_type == "top5":
        lines = [title, ""]
        for i, r in enumerate(picks, 1):
            lines.append(format_pick(r, i))
            lines.append("")
        message = "\n".join(lines)
        try:
            resp = httpx.post(
                f"{NOTIFICATION_URL}/notify",
                json={"title": "Market Scan Picks", "message": message, "channel": "telegram"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("delivered"):
                return {"success": True, "sent": len(picks), "message": "Notification sent"}
            else:
                error_note = data.get("note", "Delivery failed")
                raise HTTPException(status_code=502, detail=f"Notification service failed to deliver: {error_note}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Notification service failed: {e}")

    pick_strings = []
    for i, r in enumerate(picks, 1):
        pick_strings.append(format_pick(r, i))

    base_header = title + "\n\n"
    MAX_CHARS = 4000
    chunks = []
    current_chunk = []
    current_len = len(base_header)
    for pick_str in pick_strings:
        pick_len = len(pick_str) + 1
        if current_len + pick_len + 10 > MAX_CHARS:
            chunks.append(current_chunk)
            current_chunk = []
            current_len = len(base_header)
        current_chunk.append(pick_str)
        current_len += pick_len + 1
    if current_chunk:
        chunks.append(current_chunk)

    total_chunks = len(chunks)
    sent_count = 0
    for idx, chunk in enumerate(chunks, 1):
        if total_chunks > 1:
            header = f"{title} (Part {idx}/{total_chunks})\n\n"
        else:
            header = f"{title}\n\n"
        message = header + "\n".join(chunk) + "\n"
        try:
            resp = httpx.post(
                f"{NOTIFICATION_URL}/notify",
                json={"title": "Market Scan Picks", "message": message, "channel": "telegram"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("delivered"):
                sent_count += 1
            else:
                error_note = data.get("note", "Delivery failed")
                raise HTTPException(status_code=502, detail=f"Part {idx} failed: {error_note}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Notification service failed for part {idx}: {e}")

    return {"success": True, "sent": len(picks), "parts": total_chunks, "message": f"Notification sent in {total_chunks} parts"}

# ============================================================================
# Training Service Proxy Routes
# ============================================================================

@app.get("/training/status")
async def training_status():
    try:
        client = _get_http_client()  # shared keepalive pool
        if True:
            resp = await client.get(f"{TRAINING_URL.rstrip('/')}/model-status")
            # Also try /api/status if model-status 404s
            if resp.status_code == 404:
                resp = await client.get(f"{TRAINING_URL.rstrip('/')}/api/status")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.post("/training/train")
async def trigger_training():
    try:
        client = _get_http_client()  # shared keepalive pool
        if True:
            base = TRAINING_URL.rstrip("/")
            resp = await client.post(f"{base}/api/train")
            if resp.status_code == 404:
                resp = await client.post(f"{base}/train")
            if resp.status_code >= 400:
                detail = resp.text[:300]
                try:
                    detail = resp.json()
                except Exception:
                    pass
                raise HTTPException(status_code=resp.status_code, detail=detail)
            return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.get("/training/score/{symbol}")
async def get_training_score(symbol: str):
    try:
        client = _get_http_client()  # shared keepalive pool
        if True:
            resp = await client.get(f"{TRAINING_URL}/training-score/{symbol}")
            if resp.status_code == 404:
                return {
                    "symbol": symbol.upper(),
                    "score": None,
                    "available": False,
                    "message": "No training score for this symbol yet",
                }
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.api_route("/training/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def training_other_proxy(path: str, request: Request):
    """Proxy /training/* to the training sub-app.

    Never forward Accept-Encoding. Always return decoded JSON via JSONResponse
    so the browser never sees gzip/br binary (which causes
    "Unexpected token ... is not valid JSON").
    """
    heavy = any(
        x in path
        for x in (
            "actionable/commit",
            "train",
            "evaluate",
            "mark-to-market",
            "walk",
            "history",
            "clear-backup",
            "universe/ingest",
            "universe/train",
            "api/universe",
            "portfolio/deposit",
        )
    )
    # Shared client default was too short → 504 on ingest; always pass explicit timeout
    timeout = 180.0 if heavy else 90.0
    try:
        body = await request.body()
        fwd_headers = {"Accept": "application/json"}
        ct = request.headers.get("content-type")
        if ct:
            fwd_headers["Content-Type"] = ct

        target_url = f"{TRAINING_URL.rstrip('/')}/{path.lstrip('/')}"
        client = _get_http_client()  # shared keepalive pool
        if True:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                content=body if body else None,
                params=request.query_params,
                timeout=timeout,
            )

        # Always try JSON first — portfolio/deposit/trades all return JSON
        try:
            data = response.json()
            return JSONResponse(content=data, status_code=response.status_code)
        except Exception:
            pass

        # Non-JSON upstream (HTML error page, empty, etc.)
        text_body = response.text or ""
        # Strip control chars that break JSON.parse on the client
        safe = "".join(ch for ch in text_body[:800] if ch == "\n" or ch == "\t" or ord(ch) >= 32)
        return JSONResponse(
            content={"detail": safe or f"Upstream HTTP {response.status_code}"},
            status_code=response.status_code if response.status_code >= 400 else 502,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Training service timeout for /{path}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {e}")
    except Exception as e:
        logger.exception("training proxy error for path=%s", path)
        raise HTTPException(status_code=502, detail=f"Training proxy error: {e}")




# ── Stockky 🔥 Stocks – curated news / results / bulk / insider driven list ──
HOT_STOCKS_CACHE_KEY = "stockky:hot_stocks"
# Market hours: short cache (2–5 min) so UI stays light but not spammy.
# Off-hours: until next market open (see _hot_stocks_ttl).
HOT_STOCKS_TTL_OPEN_MIN = int(os.getenv("HOT_STOCKS_TTL_OPEN_MIN", "120"))    # 2 min
HOT_STOCKS_TTL_OPEN_MAX = int(os.getenv("HOT_STOCKS_TTL_OPEN_MAX", "300"))    # 5 min
HOT_STOCKS_TTL_OPEN_DEFAULT = int(os.getenv("HOT_STOCKS_TTL_OPEN", "180"))    # 3 min default when open


def _seconds_until_next_market_open() -> int:
    """Seconds from now (IST) until next NSE open 09:15 on a trading day."""
    now = datetime.now(IST)
    # Start candidate: today 09:15
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= candidate:
        candidate = candidate + timedelta(days=1)
    # Skip weekends + holidays (max 14 day look-ahead)
    for _ in range(14):
        if candidate.weekday() < 5 and not is_nse_holiday(candidate.date()):
            break
        candidate = candidate + timedelta(days=1)
    delta = int((candidate - now).total_seconds())
    return max(delta, 3600)  # at least 1h


def _hot_stocks_ttl() -> int:
    """Market hours: 1–6h (default 2h). Off-hours: until next market open."""
    phase = _market_session_phase_ist()
    if phase in ("preopen", "open", "post"):
        ttl = HOT_STOCKS_TTL_OPEN_DEFAULT
        return max(HOT_STOCKS_TTL_OPEN_MIN, min(HOT_STOCKS_TTL_OPEN_MAX, ttl))
    return _seconds_until_next_market_open()


def _hot_payload_fingerprint(payload: dict) -> str:
    """Stable fingerprint so we can keep cache until content actually changes."""
    try:
        parts = []
        for section in ("news_driven", "results_driven", "bulk_insider_driven"):
            for item in payload.get(section) or []:
                parts.append(
                    f"{item.get('symbol')}:{item.get('decision')}:{item.get('score')}:"
                    f"{item.get('headline_count')}:{item.get('next_earnings_date')}"
                )
        return "|".join(parts)[:800]
    except Exception:
        return ""


async def _warm_upstream_services(client: Optional[httpx.AsyncClient] = None) -> None:
    """Ping gateway deps so free-tier does not sleep between batches."""
    urls = []
    for name in ("FUNDAMENTAL_URL", "EVENT_URL", "NEWS_URL", "TECHNICAL_URL", "MARKET_DATA_URL", "NOTIFICATION_URL"):
        u = globals().get(name) or os.getenv(name)
        if u:
            urls.append(str(u).rstrip("/"))
    if client is None:
        client = _get_http_client()
    for base in urls:
        for path in ("/health?warm=true", "/health"):
            try:
                await client.get(f"{base}{path}", timeout=6.0)
                break
            except Exception:
                continue


async def stockky_hot_stocks(force: bool = False, max_symbols: Optional[int] = None, progress_cb=None):
    """Curated list for Stockky 🔥 Stocks (internal; HTTP via /stockky-hot).

    Quality-first free-tier ranking:
      1) Prefer bulk/insider + results over weak news-only names
      2) Drop low-signal news (need enough headlines + score)
      3) Seed universe from last full-scan BUY/PREPARE picks aggressively
    Cache: market hours 2–5 min; off-hours until next open.
    Batching: every HOT_BATCH_SIZE symbols, warm upstream services.
    """
    if not force:
        cached = _redis_get(HOT_STOCKS_CACHE_KEY)
        if cached:
            return {**cached, "cached": True}

    # --- Seed universe: CATALYST-FIRST (movers/news/bulk/results), then scan/watch ---
    # Why names like PWL / EXPLEOSOL / MANORAMA were missed: universe was only
    # prior BUY/PREPARE + 20 news + 25 events and capped at 45 — catalysts on
    # mid/small caps never entered the evaluation set.
    scan_syms: list = []
    try:
        last_scan = _redis_get(LAST_FULL_SCAN_KEY)
        if isinstance(last_scan, dict):
            for key in ("recommendations", "recommendations_short", "all_results", "results"):
                for r in last_scan.get(key) or []:
                    if not isinstance(r, dict):
                        continue
                    dec = (r.get("decision") or "").upper()
                    s = (r.get("symbol") or "").replace(".NS", "").replace(".BO", "").upper()
                    if not s:
                        continue
                    if dec in ("BUY NOW", "PREPARE TO BUY"):
                        scan_syms.append(s)
                        try:
                            _redis_set(
                                f"stockky:last_decision:{s}",
                                {
                                    "decision": r.get("decision"),
                                    "score": r.get("combined_score") or r.get("score"),
                                    "reasons": r.get("reasons") or [],
                                },
                                ttl=86400,
                            )
                        except Exception:
                            pass
    except Exception as e:
        logger.debug("hot seed from scan: %s", e)

    watch = _load_watchlist() or []
    momentum = []
    news_syms = []
    event_syms = []
    try:
        momentum = _get_momentum_movers() or []
    except Exception as e:
        logger.warning("hot momentum: %s", e)
    try:
        news_syms = _get_news_mentioned_symbols() or []
    except Exception as e:
        logger.warning("hot news: %s", e)
    try:
        event_syms = _get_event_symbols() or []
    except Exception as e:
        logger.warning("hot events: %s", e)

    # Catalyst sources first so they are never crowded out by scan seed
    universe = list(dict.fromkeys(
        list(momentum)[:80]
        + list(news_syms)[:60]
        + list(event_syms)[:80]
        + list(scan_syms)
        + list(watch)
        + list(_load_searched() or [])[:40]
        + list(_get_recent_ipos() or [])[:20]
    ))
    if not universe:
        universe = list(_get_nifty_indices() or [])[:80]
    # Full catalyst universe by default. Optional max_symbols only if caller caps.
    # Heavy work is processed in batches (CATALYST_BATCH_SIZE / HOT_BATCH_SIZE) with warm between batches.
    if max_symbols is not None:
        lim = max(10, int(max_symbols))
        universe = universe[:lim]
    else:
        lim = len(universe)
    batch_size = max(5, int(os.getenv("HOT_BATCH_SIZE", os.getenv("CATALYST_BATCH_SIZE", "25"))))
    logger.info(
        "stockky-hot universe=%s (mom=%s news=%s evt=%s scan=%s lim=%s batch=%s)",
        len(universe), len(momentum), len(news_syms), len(event_syms), len(scan_syms), lim, batch_size,
    )

    news_driven: list = []
    results_driven: list = []
    bulk_insider_driven: list = []

    client = _get_http_client()  # shared keepalive pool

    if True:
        for i_sym, sym in enumerate(universe):
            # Between batches: warm upstreams so free-tier stays awake
            if i_sym > 0 and i_sym % batch_size == 0:
                logger.info("stockky-hot batch boundary %s/%s — warming services", i_sym, len(universe))
                try:
                    await _warm_upstream_services(client)
                except Exception as e:
                    logger.debug("hot batch warm: %s", e)
                await asyncio.sleep(0.4)
            if progress_cb is not None:
                try:
                    progress_cb(i_sym, len(universe), str(sym), batch=i_sym // batch_size)
                except TypeError:
                    try:
                        progress_cb(i_sym, len(universe), str(sym))
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                base = sym.replace(".NS", "").replace(".BO", "").upper()
                news_data = None
                event_data = None
                decision_data = None
                try:
                    news_data = await _fetch_news_cached(base, client)
                except Exception:
                    pass
                try:
                    event_data = await _fetch_events_cached(base, client)
                except Exception:
                    pass
                try:
                    decision_data = _redis_get(f"stockky:last_decision:{base}")
                except Exception:
                    decision_data = None

                decision = (decision_data or {}).get("decision") or "DO NOT BUY"
                score = (decision_data or {}).get("score")
                reasons = (decision_data or {}).get("reasons") or []
                if isinstance(reasons, dict):
                    flat = []
                    for v in reasons.values():
                        if isinstance(v, list):
                            flat.extend(v[:2])
                        elif v:
                            flat.append(str(v))
                    reasons = flat

                hc = int((news_data or {}).get("headline_count") or 0)
                nscore = (news_data or {}).get("news_score")
                news_summary = (news_data or {}).get("summary") or ""
                headlines = (news_data or {}).get("headlines") or []

                # Signal strength helpers
                has_results = False
                has_bulk_insider = False
                ins = []
                bulk = []
                if event_data:
                    has_results = bool(
                        event_data.get("next_earnings_date")
                        or event_data.get("earnings_surprise")
                        or any(
                            (c.get("event_type") == "results")
                            for c in (event_data.get("classified_events") or [])
                        )
                    )
                    ins = event_data.get("recent_insider_transactions") or []
                    bulk = event_data.get("bulk_deals") or []
                    insider_buy = any(
                        "buy" in (i.get("transaction") or "").lower()
                        or "purchase" in (i.get("transaction") or "").lower()
                        for i in ins
                    )
                    has_bulk_insider = bool(
                        bulk
                        or insider_buy
                        or any(
                            c.get("event_type") in ("bulk_block", "insider")
                            for c in (event_data.get("classified_events") or [])
                        )
                    )

                from_scan = base in set(scan_syms)

                # Catalyst promotion: strong news / bulk buy / results beat
                # can surface PREPARE TO BUY even without a prior full decide.
                catalyst_bits = []
                if nscore is not None and int(nscore) >= 62 and hc >= 2:
                    catalyst_bits.append(f"Positive news flow (score {nscore}, {hc} headlines)")
                if news_summary and any(
                    k in str(news_summary).lower()
                    for k in ("surge", "jump", "rally", "beat", "order win", "wins", "bulk", "stake", "upgrade", "record")
                ):
                    catalyst_bits.append("Catalyst language in news summary")
                if has_bulk_insider and bulk:
                    catalyst_bits.append("Bulk/block deal activity")
                if has_results and event_data and event_data.get("earnings_surprise"):
                    try:
                        sp = float((event_data.get("earnings_surprise") or {}).get("surprise_pct") or 0)
                        if sp > 0:
                            catalyst_bits.append(f"Positive earnings surprise {sp}%")
                    except (TypeError, ValueError):
                        catalyst_bits.append("Earnings/results event")
                if event_data and event_data.get("has_positive_catalyst"):
                    catalyst_bits.append("Positive classified catalyst")

                if catalyst_bits and decision in ("DO NOT BUY", "HOLD", "WAIT", "", None):
                    decision = "PREPARE TO BUY"
                    if score is None:
                        score = 58 + min(12, len(catalyst_bits) * 3)
                    reasons = list(reasons or []) + catalyst_bits
                    try:
                        _redis_set(
                            f"stockky:last_decision:{base}",
                            {"decision": decision, "score": score, "reasons": reasons[:8], "catalyst_promoted": True},
                            ttl=86400,
                        )
                    except Exception:
                        pass

                actionable = decision in ("BUY NOW", "PREPARE TO BUY")

                # NEWS section: stricter — need real headlines + score; drop weak noise
                # Prefer names that also have scan actionability or event signal
                news_ok = hc >= 2 and nscore is not None and float(nscore) >= 55
                if news_ok and (actionable or has_results or has_bulk_insider or from_scan or hc >= 4):
                    news_driven.append({
                        "symbol": base,
                        "decision": decision,
                        "score": score,
                        "news_score": nscore,
                        "headline_count": hc,
                        "summary": news_summary,
                        "reasons": reasons[:4],
                        "headlines": headlines[:5],
                        "section": "news_driven",
                        "signal_strength": "high" if (actionable and hc >= 3) else "medium",
                        "from_scan": from_scan,
                    })

                if event_data and has_results:
                    results_driven.append({
                        "symbol": base,
                        "decision": decision,
                        "score": score,
                        "next_earnings_date": event_data.get("next_earnings_date"),
                        "earnings_surprise": event_data.get("earnings_surprise"),
                        "summary": event_data.get("summary") or "",
                        "reasons": reasons[:4],
                        "section": "results_driven",
                        "signal_strength": "high" if actionable else "medium",
                        "from_scan": from_scan,
                    })

                if event_data and has_bulk_insider:
                    bulk_insider_driven.append({
                        "symbol": base,
                        "decision": decision,
                        "score": score,
                        "insider_transactions": ins[:3],
                        "bulk_deals": bulk[:3],
                        "summary": event_data.get("summary") or "",
                        "reasons": reasons[:4],
                        "section": "bulk_insider_driven",
                        "signal_strength": "high" if actionable else "medium",
                        "from_scan": from_scan,
                    })
            except Exception as e:
                logger.warning("stockky-hot skip %s: %s", sym, e)

    def _rank(items: list) -> list:
        order = {"BUY NOW": 0, "PREPARE TO BUY": 1, "DO NOT BUY": 2, "SELL": 3}
        strength = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            items,
            key=lambda x: (
                0 if x.get("from_scan") else 1,
                strength.get(x.get("signal_strength") or "low", 9),
                order.get(x.get("decision") or "", 9),
                -(x.get("score") or 0),
                -(x.get("headline_count") or 0),
            ),
        )

    # Prefer bulk/results; keep news thinner
    payload = {
        "news_driven": _rank(news_driven)[:10],
        "results_driven": _rank(results_driven)[:12],
        "bulk_insider_driven": _rank(bulk_insider_driven)[:12],
        "generated_at": datetime.now(IST).isoformat(),
        "universe_size": len(universe),
        "scan_seed_count": len(set(scan_syms)),
        "cached": False,
        "cache_ttl_seconds": _hot_stocks_ttl(),
        "market_phase": _market_session_phase_ist(),
        "fingerprint": "",
        "quality_note": (
            "Ranked by scan BUY/PREPARE, bulk/insider, results first; weak news-only names dropped."
        ),
    }
    payload["fingerprint"] = _hot_payload_fingerprint(payload)
    ttl = int(payload["cache_ttl_seconds"])
    _redis_set(HOT_STOCKS_CACHE_KEY, payload, ttl=ttl)
    logger.info(
        "stockky-hot refreshed: news=%s results=%s bulk=%s scan_seed=%s ttl=%ss phase=%s",
        len(payload["news_driven"]),
        len(payload["results_driven"]),
        len(payload["bulk_insider_driven"]),
        payload["scan_seed_count"],
        ttl,
        payload["market_phase"],
    )
    return payload





@app.get("/stockky-hot")
async def stockky_hot_endpoint(force: bool = False):
    """HTTP entry for Stockky 🔥 Stocks tab — full universe, batched internally."""
    return await stockky_hot_stocks(force=force, max_symbols=None, progress_cb=None)


@app.get("/catalysts/alert/status")
def catalyst_alert_status():
    """Status of last catalyst job. Auto-heal if stuck after free-tier sleep."""
    job_key = "stockky:catalyst_job"
    st = _redis_get(job_key) or {}
    if not isinstance(st, dict):
        st = {"status": "idle"}
    try:
        if st.get("status") == "running":
            updated = st.get("updated_at") or st.get("started_at")
            age = None
            if updated:
                try:
                    ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=IST)
                    age = int((datetime.now(IST) - ts).total_seconds())
                except Exception:
                    age = None
            # No heartbeat for 5 minutes → worker likely dead
            if age is not None and age > 300:
                st = {
                    **st,
                    "status": "error",
                    "error": f"stale_running age={age}s (worker likely slept)",
                    "message": f"Auto-failed: no progress for {age}s — re-run will continue batches",
                    "updated_at": datetime.now(IST).isoformat(),
                }
                _redis_set(job_key, st, ttl=86400)
    except Exception as e:
        logger.debug("catalyst status heal: %s", e)
    return {"ok": True, **st}


@app.get("/catalysts/alert")
@app.post("/catalysts/alert")
async def catalyst_alert_scan(
    background_tasks: BackgroundTasks,
    force: bool = True,
    notify: bool = True,
    sync: bool = False,
    batch_size: int = 0,
):
    """Pre-market / intraday catalyst sweep — FULL universe in batches.

    Does not reduce stock count. Processes HOT_BATCH_SIZE (default 25) symbols
    per batch, warms upstream services between batches, then continues until
    every candidate is scored. Poll GET /catalysts/alert/status.
    """
    job_key = "stockky:catalyst_job"
    batch_size = int(batch_size or os.getenv("CATALYST_BATCH_SIZE", "25"))
    batch_size = max(8, min(batch_size, 40))

    def _set_job(**kw):
        cur = _redis_get(job_key) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur.update(kw)
        cur["updated_at"] = datetime.now(IST).isoformat()
        _redis_set(job_key, cur, ttl=86400)
        return cur

    existing = _redis_get(job_key) or {}
    if isinstance(existing, dict) and existing.get("status") == "running" and not force:
        try:
            upd = existing.get("updated_at") or existing.get("started_at")
            ts = datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if (datetime.now(IST) - ts).total_seconds() < 180:
                return {"ok": True, "already_running": True, **existing}
        except Exception:
            pass

    _set_job(
        status="running",
        started_at=datetime.now(IST).isoformat(),
        message="Catalyst sweep started (full universe, batched)",
        force=force,
        notify=notify,
        actionable_count=0,
        processed=0,
        total=0,
        batch_size=batch_size,
        batch_index=0,
        error=None,
    )

    async def _work():
        try:
            stop_evt = asyncio.Event()

            async def _keepalive_loop():
                while not stop_evt.is_set():
                    try:
                        await _warm_upstream_services()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(stop_evt.wait(), timeout=75.0)
                    except asyncio.TimeoutError:
                        pass

            ka_task = asyncio.create_task(_keepalive_loop())

            def _progress(i, total, sym, batch=0):
                _set_job(
                    status="running",
                    message=f"Batch {batch + 1}: scoring {i + 1}/{total} ({sym})",
                    processed=i + 1,
                    total=total,
                    batch_index=batch,
                    batch_size=batch_size,
                )

            _set_job(message="Building full catalyst universe…")
            # max_symbols=None → ALL candidates; internal loop warms every batch_size
            # Temporarily set env for this process batch size
            os.environ["HOT_BATCH_SIZE"] = str(batch_size)

            hot = await stockky_hot_stocks(
                force=force,
                max_symbols=None,  # full universe — no reduction
                progress_cb=_progress,
            )

            picks = []
            for section in ("bulk_insider_driven", "results_driven", "news_driven"):
                for item in (hot or {}).get(section) or []:
                    dec = (item.get("decision") or "").upper()
                    if dec in ("BUY NOW", "PREPARE TO BUY") or item.get("signal_strength") == "high":
                        picks.append({**item, "section": section})

            rank_sec = {"bulk_insider_driven": 0, "results_driven": 1, "news_driven": 2}
            picks.sort(key=lambda x: (rank_sec.get(x.get("section"), 9), -(x.get("score") or 0)))
            seen = set()
            unique = []
            for p in picks:
                s = (p.get("symbol") or "").upper()
                if not s or s in seen:
                    continue
                seen.add(s)
                unique.append(p)

            lines = [
                "🔥 *Stockky Catalyst Alert*",
                f"IST {datetime.now(IST).strftime('%Y-%m-%d %H:%M')}",
                f"Universe screened: {(hot or {}).get('universe_size')} · Actionable: {len(unique)}",
                "",
            ]
            if not unique:
                lines.append("No strong catalyst names right now.")
            else:
                for i, p in enumerate(unique[:15], 1):
                    sym = p.get("symbol")
                    dec = p.get("decision")
                    sc = p.get("score")
                    sec = (p.get("section") or "").replace("_driven", "")
                    why = ""
                    rs = p.get("reasons") or []
                    if isinstance(rs, list) and rs:
                        why = str(rs[0])[:80]
                    elif p.get("summary"):
                        why = str(p.get("summary"))[:80]
                    lines.append(f"{i}. *{sym}* — {dec} (score {sc}) [{sec}]")
                    if why:
                        lines.append(f"   _{why}_")

            message = "\n".join(lines)
            notified = False
            if notify and unique:
                try:
                    client = _get_http_client()  # shared keepalive pool
                    if True:
                        await client.post(
                            f"{NOTIFICATION_URL.rstrip('/')}/notify",
                            json={"title": "Catalyst Alert", "message": message, "channel": "telegram"},
                        )
                    notified = True
                except Exception as e:
                    logger.warning("catalyst telegram failed: %s", e)
                    try:
                        if "send_picks_to_telegram" in dir():
                            send_picks_to_telegram({"recommendations": unique[:10]})
                            notified = True
                    except Exception as e2:
                        logger.warning("catalyst telegram fallback: %s", e2)

            total_u = int((hot or {}).get("universe_size") or 0)
            result = {
                "ok": True,
                "status": "done",
                "actionable_count": len(unique),
                "picks": unique[:20],
                "hot_universe_size": total_u,
                "notified": notified,
                "message_preview": message[:500],
                "generated_at": datetime.now(IST).isoformat(),
                "message": f"Done — screened {total_u} · {len(unique)} actionable (batched {batch_size})",
                "processed": total_u,
                "total": total_u,
                "batch_size": batch_size,
            }
            _set_job(**result)
            stop_evt.set()
            try:
                await asyncio.wait_for(ka_task, timeout=2)
            except Exception:
                ka_task.cancel()
            return result
        except Exception as e:
            logger.exception("catalyst alert failed")
            _set_job(status="error", error=str(e)[:300], message=f"Error: {e}")
            return {"ok": False, "status": "error", "error": str(e)[:300]}

    if sync:
        return await _work()

    background_tasks.add_task(_work)
    return {
        "ok": True,
        "status": "running",
        "accepted": True,
        "batch_size": batch_size,
        "message": "Catalyst alert started — full universe in batches; poll /catalysts/alert/status",
        "poll": "/catalysts/alert/status",
    }


# ── WebSocket real-time hub (scan progress + market ticks + training) ─────────

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self.subs: Dict[int, Set[str]] = {}  # id(ws) -> channels
        self.quote_syms: Dict[int, Set[str]] = {}  # id(ws) -> symbols for live quotes

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)
        self.subs[id(websocket)] = set()
        self.quote_syms[id(websocket)] = set()

    def disconnect(self, websocket: WebSocket):
        wid = id(websocket)
        if websocket in self.active:
            self.active.remove(websocket)
        self.subs.pop(wid, None)
        self.quote_syms.pop(wid, None)

    def subscribe(self, websocket: WebSocket, channel: str):
        self.subs.setdefault(id(websocket), set()).add(channel)

    def unsubscribe(self, websocket: WebSocket, channel: str):
        self.subs.setdefault(id(websocket), set()).discard(channel)

    def watch_quotes(self, websocket: WebSocket, symbols: List[str]):
        wid = id(websocket)
        bucket = self.quote_syms.setdefault(wid, set())
        for s in symbols:
            sym = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
            if sym:
                bucket.add(sym)
                self.subscribe(websocket, f"quote:{sym}")

    def unwatch_quotes(self, websocket: WebSocket, symbols: List[str] = None):
        wid = id(websocket)
        bucket = self.quote_syms.setdefault(wid, set())
        if symbols is None:
            for sym in list(bucket):
                self.unsubscribe(websocket, f"quote:{sym}")
            bucket.clear()
            return
        for s in symbols:
            sym = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
            bucket.discard(sym)
            self.unsubscribe(websocket, f"quote:{sym}")

    def all_watched_symbols(self) -> List[str]:
        out: Set[str] = set()
        for syms in self.quote_syms.values():
            out.update(syms)
        return sorted(out)

    async def broadcast(self, channel: str, payload: dict):
        dead = []
        msg = json.dumps({"channel": channel, **payload}, default=str)
        for ws in list(self.active):
            chans = self.subs.get(id(ws), set())
            if channel in chans or "all" in chans:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()
_quote_loop_task = None


def _resolve_quote_price(sym: str):
    """Best-effort free-tier quote for WS push."""
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{sym}", timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            price = data.get("price") or data.get("regularMarketPrice") or data.get("close") or data.get("last")
            if price is not None:
                return {
                    "symbol": sym,
                    "price": float(price),
                    "close": data.get("close") or float(price),
                    "change_pct": data.get("change_pct"),
                    "as_of": data.get("as_of") or datetime.now(IST).isoformat(),
                    "source": data.get("source") or "market-data",
                }
    except Exception as e:
        logger.debug("ws quote market-data %s: %s", sym, e)
    try:
        t = yf.Ticker(f"{sym}.NS")
        info = getattr(t, "fast_info", None) or {}
        price = None
        try:
            price = float(info.get("last_price") or info.get("lastPrice") or 0) or None
        except Exception:
            price = None
        if price:
            return {
                "symbol": sym,
                "price": price,
                "close": price,
                "as_of": datetime.now(IST).isoformat(),
                "source": "yfinance_fast",
            }
    except Exception as e:
        logger.debug("ws quote yf %s: %s", sym, e)
    return None


async def _quote_broadcast_loop():
    """Push quotes for WS-watched symbols.
    During market hours: ~every 8s for up to 40 symbols.
    Off-hours / weekend / holiday: idle sleep (no upstream quote spam).
    """
    while True:
        try:
            phase = _market_session_phase_ist()
            # Only hammer market-data while session is live (preopen/open/post)
            if phase not in ("preopen", "open", "post"):
                await asyncio.sleep(60)
                continue

            symbols = ws_manager.all_watched_symbols()
            if not symbols:
                await asyncio.sleep(15)
                continue

            # Cap concurrent watched symbols to protect Yahoo/NSE / free-tier
            for sym in symbols[:25]:
                q = await asyncio.to_thread(_resolve_quote_price, sym)
                if q:
                    await ws_manager.broadcast(f"quote:{sym}", {
                        "type": "quote",
                        **q,
                    })
                    try:
                        metrics.inc("stockky_ws_quote_push_total")
                    except Exception:
                        pass
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug("quote loop: %s", e)
        # Open: 8s; preopen/post: slower
        phase = _market_session_phase_ist()
        await asyncio.sleep(8 if phase == "open" else 20)


def _ensure_quote_loop():
    global _quote_loop_task
    try:
        loop = asyncio.get_event_loop()
        if _quote_loop_task is None or _quote_loop_task.done():
            _quote_loop_task = loop.create_task(_quote_broadcast_loop())
    except Exception as e:
        logger.debug("quote loop start: %s", e)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Realtime: scan progress, live quotes, market, training.

    Client messages (JSON):
      {"action":"subscribe","channel":"scan:<id>"|"quote:TCS"|"market"|"all"}
      {"action":"subscribe_quotes","symbols":["TCS","INFY"]}
      {"action":"unsubscribe_quotes","symbols":["TCS"]}  # or omit symbols to clear
      {"action":"unsubscribe","channel":"..."}
      {"action":"ping"}
    Server:
      {"channel":"quote:TCS","type":"quote","price":...,"as_of":...}
      {"channel":"scan:...","type":"scan_status",...}
    """
    await ws_manager.connect(websocket)
    _ensure_quote_loop()
    try:
        await websocket.send_text(json.dumps({
            "channel": "system",
            "type": "connected",
            "ts": datetime.now(IST).isoformat(),
            "features": ["scan", "quotes", "ping"],
        }))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw) if raw else {}
            except Exception:
                msg = {}
            action = (msg.get("action") or "").lower()
            channel = (msg.get("channel") or "").strip()

            if action == "ping":
                await websocket.send_text(json.dumps({"channel": "system", "type": "pong"}))

            elif action == "subscribe_quotes":
                syms = msg.get("symbols") or []
                if isinstance(syms, str):
                    syms = [syms]
                ws_manager.watch_quotes(websocket, list(syms))
                await websocket.send_text(json.dumps({
                    "channel": "system",
                    "type": "quotes_subscribed",
                    "symbols": sorted(ws_manager.quote_syms.get(id(websocket), set())),
                }, default=str))
                # Immediate snapshot
                for sym in list(ws_manager.quote_syms.get(id(websocket), set()))[:10]:
                    q = await asyncio.to_thread(_resolve_quote_price, sym)
                    if q:
                        await websocket.send_text(json.dumps({
                            "channel": f"quote:{sym}",
                            "type": "quote",
                            **q,
                        }, default=str))

            elif action == "unsubscribe_quotes":
                syms = msg.get("symbols")
                if syms is None:
                    ws_manager.unwatch_quotes(websocket, None)
                else:
                    if isinstance(syms, str):
                        syms = [syms]
                    ws_manager.unwatch_quotes(websocket, list(syms))
                await websocket.send_text(json.dumps({
                    "channel": "system",
                    "type": "quotes_unsubscribed",
                }))

            elif action == "subscribe" and channel:
                ws_manager.subscribe(websocket, channel)
                if channel.startswith("quote:"):
                    sym = channel.split(":", 1)[1]
                    ws_manager.watch_quotes(websocket, [sym])
                await websocket.send_text(json.dumps({
                    "channel": channel, "type": "subscribed"
                }))
                if channel.startswith("scan:"):
                    task_id = channel.split(":", 1)[1]
                    data = _redis_get(SCAN_TASK_PREFIX + task_id) or {}
                    await websocket.send_text(json.dumps({
                        "channel": channel,
                        "type": "scan_status",
                        "task_id": task_id,
                        "status": data.get("status"),
                        "processed": data.get("processed", 0),
                        "total": data.get("total", 0),
                        "elapsed": data.get("elapsed"),
                        "result": data.get("result") if data.get("status") == "done" else None,
                    }, default=str))
                elif channel.startswith("quote:"):
                    sym = channel.split(":", 1)[1]
                    q = await asyncio.to_thread(_resolve_quote_price, sym)
                    if q:
                        await websocket.send_text(json.dumps({
                            "channel": channel, "type": "quote", **q
                        }, default=str))

            elif action == "unsubscribe" and channel:
                ws_manager.unsubscribe(websocket, channel)
                if channel.startswith("quote:"):
                    ws_manager.unwatch_quotes(websocket, [channel.split(":", 1)[1]])

            elif action == "poll_scan" and msg.get("task_id"):
                task_id = msg["task_id"]
                data = _redis_get(SCAN_TASK_PREFIX + task_id) or {}
                await websocket.send_text(json.dumps({
                    "channel": f"scan:{task_id}",
                    "type": "scan_status",
                    "task_id": task_id,
                    "status": data.get("status"),
                    "processed": data.get("processed", 0),
                    "total": data.get("total", 0),
                    "elapsed": data.get("elapsed"),
                    "result": data.get("result") if data.get("status") == "done" else None,
                }, default=str))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning("websocket closed: %s", e)
        ws_manager.disconnect(websocket)


async def _ws_push_scan(task_id: str, data: dict):
    """Best-effort push when scan state changes (also polled by clients)."""
    try:
        await ws_manager.broadcast(f"scan:{task_id}", {
            "type": "scan_status",
            "task_id": task_id,
            "status": data.get("status"),
            "processed": data.get("processed", 0),
            "total": data.get("total", 0),
            "elapsed": data.get("elapsed"),
            "result": data.get("result") if data.get("status") in ("done", "cancelled", "error") else None,
        })
    except Exception as e:
        logger.debug("ws push scan failed: %s", e)


# ── Startup cache pre-population ──────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    try:
        _redis.delete(INDICES_CACHE_KEY)
        logger.info("Cleared old indices cache on startup")
        result = get_market_indices(force_refresh=True)
        logger.info("Market indices cache pre-populated successfully")
    except Exception as e:
        logger.warning(f"Could not pre-populate indices cache: {e}")



@app.get("/ops/keepalive")
@app.post("/ops/keepalive")
async def ops_keepalive(deep: bool = False):
    """
    Lightweight keep-alive for free-tier.
    - Always: gateway health (cheap)
    - deep=true: sequential soft-ping of required services (used sparingly by UI while active)
    Avoids parallel storms; safe during scans.
    """
    out = {"ok": True, "gateway": True, "services": {}, "ts": datetime.now(IST).isoformat()}
    if not deep:
        return out
    # Soft sequential pings — never block more than ~12s total
    client = _get_http_client()
    for name, cfg in list(SYSTEM_SERVICES.items())[:6]:
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            continue
        try:
            r = await client.get(f"{url}/health", params={"warm": "true"}, timeout=4.0)
            out["services"][name] = r.status_code == 200
        except Exception:
            out["services"][name] = False
            await asyncio.sleep(0.15)
    out["ok"] = True
    return out

# ═══════════════════════════════════════════════════════════════════════════
# Data Feed — slow fields (12–24h) for free-tier rate-limit relief
# ═══════════════════════════════════════════════════════════════════════════



@app.post("/ops/power-off")
async def ops_power_off(background_tasks: BackgroundTasks):
    """Emergency stop: cancel scans, data-feed, hot-picks; clear frontend-visible jobs.
    Does not wipe DB. Returns phases for UI messages.
    """
    phases = []
    # 1) Cancel active scan tasks
    try:
        cancelled = 0
        # Mark any in-memory cancel flags
        for k in list(_mem_kv.keys()):
            if ":cancel" in str(k) or str(k).endswith(":cancel"):
                _mem_kv[k] = True
                cancelled += 1
            if "scan:task:" in str(k) or "scan_task" in str(k).lower():
                try:
                    data = _mem_kv.get(k)
                    if isinstance(data, dict) and data.get("status") == "running":
                        data["status"] = "cancelled"
                        data["cancel_requested"] = True
                        data["partial"] = True
                        _mem_kv[k] = data
                        cancelled += 1
                except Exception:
                    pass
        phases.append({"phase": "scan", "ok": True, "detail": f"cancel signals={cancelled}"})
    except Exception as e:
        phases.append({"phase": "scan", "ok": False, "detail": str(e)[:120]})

    # 2) Stop data feed
    try:
        store = _feed_store()
        store.set_job(
            status="stopped",
            message="Power-off: data feed stopped",
            stop_requested=True,
            finished_at=__import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).isoformat(),
        )
        phases.append({"phase": "data_feed", "ok": True, "detail": "stopped"})
    except Exception as e:
        phases.append({"phase": "data_feed", "ok": False, "detail": str(e)[:120]})

    # 3) Stop hot picks
    try:
        from data_feed import hot_job_set
        hot_job_set(
            _redis_set, _redis_get,
            status="idle",
            message="Power-off: Hot Picks stopped",
            processed=0,
        )
        phases.append({"phase": "hot_picks", "ok": True, "detail": "idle"})
    except Exception as e:
        phases.append({"phase": "hot_picks", "ok": False, "detail": str(e)[:120]})

    # 4) Soft warm (optional, non-blocking)
    phases.append({"phase": "ready", "ok": True, "detail": "All stoppable jobs signalled. Refresh UI for fresh start."})
    return {
        "ok": True,
        "message": "Switching OFF processes → restarting state → ready for fresh start",
        "phases": phases,
        "hint": "Scan/data-feed/hot-picks stopped. Training lock is per-service — use Stop Training if needed.",
    }


@app.get("/data-feed/meta")
def data_feed_meta():
    """Last successful feed timestamp, stock count, job status."""
    store = _feed_store()
    meta = store.meta()
    job = store.job()
    return {"ok": True, "meta": meta, "job": job}


@app.get("/data-feed/status")
def data_feed_status():
    """Return job+meta. Auto-heal stale 'running' jobs (worker died after free-tier sleep)."""
    store = _feed_store()
    job = store.job()
    meta = store.meta()
    try:
        if job.get("status") == "running":
            stale_sec = int(os.getenv("DATA_FEED_STALE_SEC", "900"))  # 15 min — free-tier sleep is not a restart signal
            updated = None
            # Prefer checkpoint/elapsed; fall back to started_at
            for key in ("updated_at", "resumed_at", "started_at"):
                raw = job.get(key)
                if not raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=IST)
                    updated = ts
                    break
                except Exception:
                    pass
            # Also treat long elapsed without stop as ok only if stop_requested
            elapsed = int(job.get("elapsed_sec") or 0)
            stop_req = bool(job.get("stop_requested"))
            age = None
            if updated is not None:
                age = int((datetime.now(IST) - updated).total_seconds())
            # If stop was requested OR last activity is old, force-commit stopped
            if stop_req or (age is not None and age > stale_sec) or (elapsed > 0 and age is None and stop_req):
                # Force commit checkpoint as stopped so Resume can work
                cp = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
                cursor = int(cp.get("cursor") or job.get("processed") or 0)
                total = int(job.get("total") or 0)
                ok_n = int(job.get("ok_count") or meta.get("last_count") or 0)
                err_n = int(job.get("error_count") or job.get("errors") or 0)
                ts = datetime.now(IST).isoformat()
                msg = (
                    f"Auto-stopped (stale/sleep) at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
                    if not stop_req
                    else f"Stopped at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
                )
                store.set_meta(
                    last_success_at=ts,
                    last_count=ok_n,
                    last_errors=err_n,
                    last_message=msg,
                    source="stop_or_stale",
                    universe_size=total,
                    partial=bool(total and cursor < total),
                )
                job = store.set_job(
                    status="stopped",
                    processed=cursor,
                    total=total,
                    message=msg,
                    errors=err_n,
                    ok_count=ok_n,
                    error_count=err_n,
                    finished_at=ts,
                    stop_requested=False,
                    checkpoint={
                        "cursor": cursor,
                        "done": list(cp.get("done") or []),
                        "universe": cp.get("universe") or [],
                    },
                )
                meta = store.meta()
    except Exception as e:
        logger.warning("data-feed status heal: %s", e)
    return {
        "stocks_in_feed": int((meta or {}).get("last_count") or (job or {}).get("ok_count") or 0),
        "last_success": (meta or {}).get("last_success_at"),
        "last_success_at": (meta or {}).get("last_success_at"),
"ok": True, **job, "meta": meta}


@app.get("/data-feed/{symbol}")
def data_feed_symbol(symbol: str):
    fed = _feed_store().get_symbol(symbol)
    if not fed:
        return {"ok": False, "symbol": symbol.upper(), "detail": "No data feed entry"}
    return {"ok": True, "data": fed}


@app.post("/data-feed/run")
async def data_feed_run(
    background_tasks: BackgroundTasks,
    force: bool = False,
    resume: bool = False,
    only_new: bool = False,
):
    """Feed slow fields for scan universe.

    - force=true  → full refresh from index 0
    - resume=true → continue from checkpoint cursor (Resume)
    - only_new=true → only symbols not already in feed store (new universe members)
    - default     → start fresh only if not running
    Never auto-starts from status polling — UI/scheduler must POST explicitly.
    """
    store = _feed_store()
    job = store.job()
    if job.get("status") == "running" and not force and not resume:
        return {"ok": True, "already_running": True, **job}

    universe = _build_scan_universe()
    if not universe:
        universe = list(_get_nifty_indices() or [])[:150]
    _df_max = int(os.getenv("DATA_FEED_MAX_SYMBOLS", "0") or 0)
    if _df_max > 0:
        universe = universe[:_df_max]
    universe = [u.upper().replace(".NS", "").replace(".BO", "") for u in universe]
    # Full universe by default. Only DATA_FEED_MAX_SYMBOLS>0 truncates (explicit opt-in).

    if only_new:
        store = _feed_store()
        fresh = []
        for s in universe:
            try:
                entry = store.get_symbol(s)
                if not entry:
                    fresh.append(s)
            except Exception:
                fresh.append(s)
        if not fresh:
            return {
                "ok": True,
                "started": False,
                "mode": "only_new",
                "total": 0,
                "message": "No new symbols — all universe members already have feed data. Use full feed only if you need a refresh.",
            }
        universe = fresh
        logger.info("data-feed only_new: %s symbols without feed entry", len(universe))


    # Checkpoint: list of already-fed symbols + cursor
    checkpoint = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
    done_set = set(checkpoint.get("done") or [])
    start_idx = 0
    ok_n = int(job.get("ok_count") or 0)
    err_n = int(job.get("error_count") or 0)

    if resume and not force:
        # Prefer explicit cursor; else skip symbols already in done
        start_idx = int(checkpoint.get("cursor") or job.get("processed") or 0)
        start_idx = max(0, min(start_idx, len(universe)))
        # If job was stopped mid-way, resume from there
        if job.get("status") in ("stopped", "error", "idle", "done") and start_idx >= len(universe):
            start_idx = 0
            done_set = set()
            ok_n = 0
            err_n = 0
        store.set_job(
            status="running",
            processed=start_idx,
            total=len(universe),
            started_at=job.get("started_at") or datetime.now(IST).isoformat(),
            resumed_at=datetime.now(IST).isoformat(),
            message=f"Resuming from {start_idx}/{len(universe)}…",
            errors=err_n,
            ok_count=ok_n,
            error_count=err_n,
            stop_requested=False,
            checkpoint={"cursor": start_idx, "done": list(done_set), "universe": universe},
        )
        mode = "resume"
    else:
        # Full refresh / new run
        done_set = set()
        start_idx = 0
        ok_n = 0
        err_n = 0
        store.set_job(
            status="running",
            processed=0,
            total=len(universe),
            started_at=datetime.now(IST).isoformat(),
            elapsed_sec=0,
            estimated_remaining_sec=None,
            message=f"Feeding {len(universe)} symbols…",
            errors=0,
            ok_count=0,
            error_count=0,
            stop_requested=False,
            checkpoint={"cursor": 0, "done": [], "universe": universe},
        )
        mode = "refresh" if force else "start"

    async def _run(start_at: int, ok0: int, err0: int, done0: set):
        ok_n = ok0
        err_n = err0
        done_set = set(done0)
        client = _get_http_client()  # shared keepalive pool
        if True:
            for i in range(start_at, len(universe)):
                # Cooperative stop
                jnow = store.job()
                if jnow.get("stop_requested"):
                    ts = datetime.now(IST).isoformat()
                    msg = f"Stopped at {i}/{len(universe)} — committed {ok_n} fed stocks at {ts}"
                    store.set_meta(
                        last_success_at=ts,
                        last_count=ok_n,
                        last_errors=err_n,
                        last_message=msg,
                        source="stop",
                        universe_size=len(universe),
                        partial=True,
                    )
                    store.set_job(
                        status="stopped",
                        processed=i,
                        total=len(universe),
                        message=msg,
                        errors=err_n,
                        ok_count=ok_n,
                        error_count=err_n,
                        finished_at=ts,
                        stop_requested=False,
                        checkpoint={"cursor": i, "done": list(done_set), "universe": universe},
                    )
                    logger.info(msg)
                    return

                base = universe[i]
                if base in done_set:
                    store.set_job(
                        status="running",
                        processed=i + 1,
                        total=len(universe),
                        message=f"Skip cached {base} ({i+1}/{len(universe)})",
                        errors=err_n,
                        ok_count=ok_n,
                        error_count=err_n,
                        updated_at=datetime.now(IST).isoformat(),
                        checkpoint={"cursor": i + 1, "done": list(done_set), "universe": universe},
                    )
                    continue
                try:
                    fund = None
                    events = None
                    try:
                        r = await client.get(f"{FUNDAMENTAL_URL}/analyze/{base}", timeout=35)
                        if r.status_code == 200:
                            fund = r.json()
                    except Exception:
                        pass
                    try:
                        r = await client.get(f"{EVENT_URL}/events/{base}", timeout=20)
                        if r.status_code == 200:
                            events = r.json()
                    except Exception:
                        pass
                    if fund or events:
                        payload = extract_feed_payload(base, fund, events)
                        store.put_symbol(base, payload, ttl=DATA_FEED_TTL)
                        done_set.add(base)
                        ok_n += 1
                    else:
                        err_n += 1
                except Exception as e:
                    err_n += 1
                    logger.debug("data-feed %s: %s", base, e)

                store.set_job(
                    status="running",
                    processed=i + 1,
                    total=len(universe),
                    message=f"Fed {ok_n}/{len(universe)} ({base})",
                    errors=err_n,
                    ok_count=ok_n,
                    error_count=err_n,
                    updated_at=datetime.now(IST).isoformat(),
                    checkpoint={"cursor": i + 1, "done": list(done_set), "universe": universe},
                )
                feed_batch = max(5, int(os.getenv("DATA_FEED_BATCH_SIZE", "20")))
                if (i + 1) % feed_batch == 0:
                    # Complete batch → warm all upstreams, then continue
                    store.set_job(
                        message=f"Batch done {i + 1}/{len(universe)} — warming services…",
                        updated_at=datetime.now(IST).isoformat(),
                    )
                    try:
                        await _warm_upstream_services(client)
                    except Exception as e:
                        logger.debug("data-feed batch warm: %s", e)
                    await asyncio.sleep(0.5)
                elif (i + 1) % 5 == 0:
                    await asyncio.sleep(0.25)

        ts = datetime.now(IST).isoformat()
        msg = f"Data feed successfully for {ok_n} stocks at {ts}"
        store.set_meta(
            last_success_at=ts,
            last_count=ok_n,
            last_errors=err_n,
            last_message=msg,
            source="manual_or_api_or_scheduler",
            universe_size=len(universe),
            partial=False,
        )
        store.set_job(
            status="done",
            processed=len(universe),
            total=len(universe),
            message=msg,
            errors=err_n,
            ok_count=ok_n,
            error_count=err_n,
            finished_at=ts,
            stop_requested=False,
            checkpoint={"cursor": len(universe), "done": list(done_set), "universe": universe},
        )
        logger.info(msg)

    background_tasks.add_task(_run, start_idx, ok_n, err_n, done_set)
    return {
        "ok": True,
        "started": True,
        "mode": mode,
        "resume_from": start_idx,
        "total": len(universe),
        "message": (
            f"Data feed resumed from {start_idx}/{len(universe)}"
            if mode == "resume"
            else f"Data feed started for {len(universe)} scan-universe stocks"
        ),
    }


@app.post("/data-feed/stop")
async def data_feed_stop(force: bool = True):
    """Stop data feed and commit checkpoint immediately.

    Free-tier workers often die after sleep, so cooperative stop alone leaves
    status=running forever. We always force-commit from the last checkpoint.
    """
    store = _feed_store()
    job = store.job()
    status = job.get("status")
    if status not in ("running", "stopped") and not force:
        return {"ok": True, "stopped": False, "detail": f"Not running (status={status})", **job}

    cp = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
    cursor = int(cp.get("cursor") or job.get("processed") or 0)
    total = int(job.get("total") or 0)
    ok_n = int(job.get("ok_count") or 0)
    err_n = int(job.get("error_count") or job.get("errors") or 0)
    # Prefer counting done list if present
    done = list(cp.get("done") or [])
    if done:
        ok_n = max(ok_n, len(done))
    ts = datetime.now(IST).isoformat()
    msg = f"Stopped at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
    store.set_meta(
        last_success_at=ts,
        last_count=ok_n,
        last_errors=err_n,
        last_message=msg,
        source="stop",
        universe_size=total,
        partial=bool(total and cursor < total),
    )
    job = store.set_job(
        status="stopped",
        processed=cursor,
        total=total,
        message=msg,
        errors=err_n,
        ok_count=ok_n,
        error_count=err_n,
        finished_at=ts,
        stop_requested=False,
        checkpoint={
            "cursor": cursor,
            "done": done,
            "universe": cp.get("universe") or [],
        },
    )
    return {"ok": True, "stopped": True, "message": msg, **job}


@app.post("/data-feed/resume")
async def data_feed_resume(background_tasks: BackgroundTasks):
    """Resume from last checkpoint.

    If a previous run is stuck as 'running' (worker died), force-stop/commit first
    so resume can start a new background task from the cursor.
    """
    store = _feed_store()
    job = store.job()
    if job.get("status") == "running":
        # Force-commit current checkpoint so a new worker can continue
        await data_feed_stop(force=True)
    return await data_feed_run(background_tasks, force=False, resume=True)


# ── Hot Picks on-demand job (no auto-load spam) ─────────────────────────────

@app.get("/stockky-hot/status")
def stockky_hot_status():
    job = hot_job_get(_redis_get)
    cached = _redis_get(HOT_RESULT_KEY) or _redis_get(HOT_STOCKS_CACHE_KEY)
    return {
        "ok": True,
        **job,
        "has_result": bool(cached),
        "result_generated_at": (cached or {}).get("generated_at") if isinstance(cached, dict) else None,
    }


@app.get("/stockky-hot/result")
def stockky_hot_result():
    """Last persisted Hot Picks result (DB/Redis) — does not trigger a new run."""
    cached = _redis_get(HOT_RESULT_KEY) or _redis_get(HOT_STOCKS_CACHE_KEY)
    if not cached:
        return {"ok": False, "detail": "No Hot Picks result yet — run Search Hot Picks Stocks"}
    return {**cached, "ok": True, "cached": True}


@app.post("/stockky-hot/run")
async def stockky_hot_run(background_tasks: BackgroundTasks, force: bool = True):
    """Start Hot Picks search with progress (pipeline UI polls /stockky-hot/status)."""
    job = hot_job_get(_redis_get)
    if job.get("status") == "running":
        return {"ok": True, "already_running": True, **job}

    hot_job_set(
        _redis_set,
        _redis_get,
        status="running",
        processed=0,
        total=100,
        started_at=datetime.now(IST).isoformat(),
        message="Building catalyst universe…",
        estimated_remaining_sec=None,
    )

    async def _run_hot():
        try:
            hot_job_set(_redis_set, _redis_get, message="Scanning news / events / bulk…", processed=10)
            # Clear short cache so force refresh
            try:
                if _redis:
                    _redis.delete(HOT_STOCKS_CACHE_KEY)
            except Exception:
                pass
            hot_job_set(_redis_set, _redis_get, processed=30, message="Evaluating catalyst signals…")
            result = await stockky_hot_stocks(force=True)
            hot_job_set(_redis_set, _redis_get, processed=90, message="Ranking & saving…")
            ts = datetime.now(IST).isoformat()
            if isinstance(result, dict):
                result = {**result, "generated_at": result.get("generated_at") or ts, "persisted_at": ts}
                _redis_set(HOT_RESULT_KEY, result, ttl=20 * 3600)
            hot_job_set(
                _redis_set,
                _redis_get,
                status="done",
                processed=100,
                total=100,
                message=f"Hot Picks ready at {ts}",
                finished_at=ts,
                estimated_remaining_sec=0,
            )
        except Exception as e:
            logger.exception("hot run failed")
            hot_job_set(
                _redis_set,
                _redis_get,
                status="error",
                message=str(e)[:200],
            )

    background_tasks.add_task(_run_hot)
    return {"ok": True, "started": True, "message": "Hot Picks search started"}



if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)