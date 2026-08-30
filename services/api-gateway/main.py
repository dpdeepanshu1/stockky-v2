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
from symbol_aliases import resolve_ns_ticker

# yfinance keeps a small timezone cache on disk. Left to its own defaults it
# writes under $HOME/.cache, which on Render/containers is often read-only or
# unset — and because several workers boot at once, they race on the same mkdir
# and one of them logs "Failed to create TzCache folder ... File exists". Neither
# is fatal (yfinance falls back to no cache) but it means every Ticker call
# re-derives the exchange timezone, so it is pure repeated work AND log noise.
# Creating the directory ourselves with exist_ok=True removes the race, and
# pointing it at a writable path (env-overridable so the Oracle VM can use a
# persistent disk instead of /tmp) makes the cache actually stick.
try:
    _TZ_CACHE_DIR = os.getenv("YF_TZ_CACHE_DIR", "/tmp/yfinance_tz")
    os.makedirs(_TZ_CACHE_DIR, exist_ok=True)
    yf.set_tz_cache_location(_TZ_CACHE_DIR)
except Exception as _tz_e:  # pragma: no cover
    logging.getLogger(__name__).debug("yfinance tz cache setup skipped: %s", _tz_e)

try:
    import rate_limiter as _rl
    _rl.patch_yfinance()
except Exception as _rl_e:
    logging.getLogger(__name__).warning("rate_limiter patch skipped: %s", _rl_e)

import feedparser
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
# NOTE: upstash_redis is imported LAZILY only when USE_REDIS=1.
# Top-level import was removed so free-tier cold starts never touch Redis.
from data_feed import (
    DataFeedStore, extract_feed_payload, DATA_FEED_TTL,
    hot_job_get, hot_job_set, HOT_RESULT_KEY,
    hot_premarket_job_get, hot_premarket_job_set,
    try_refresh_lock, release_refresh_lock, soft_ttl_should_refresh,
    request_data_feed_stop, clear_data_feed_stop, data_feed_stop_requested,
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
_HTTP_TIMEOUT = httpx.Timeout(90.0, connect=15.0)  # free-tier cold starts; longer connect for Render spin-up
_HTTP_TIMEOUT_LONG = httpx.Timeout(120.0, connect=15.0)
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
    """Non-blocking startup — UI must never freeze on 'Connecting to Backend...'."""
    global _shared_http_client
    try:
        if _shared_http_client is None or _shared_http_client.is_closed:
            _shared_http_client = httpx.AsyncClient(
                limits=_HTTP_LIMITS, timeout=_HTTP_TIMEOUT, follow_redirects=True
            )
            logger.info("Shared httpx.AsyncClient started (keepalive=20, max=50)")
    except Exception as e:
        logger.warning("Startup warning (http client, non-fatal): %s", e)
    try:
        redis_limiter.set_redis(_redis)
    except Exception:
        pass
    # Container-amnesia cure: wipe ONLY stuck job status — never stock payloads
    # Wrapped tightly so a slow/Neon-down DB never blocks FastAPI boot.
    try:
        from data_feed import clear_stuck_feed_job_on_boot
        result = clear_stuck_feed_job_on_boot()
        logger.info("Boot feed-job heal: %s", result)
    except Exception as e:
        logger.warning("Startup warning (feed-job heal, non-fatal): %s", e)


@app.on_event("shutdown")
async def _graceful_shutdown():
    """FastAPI/uvicorn SIGTERM path: commit work, stop loops, close clients."""
    global _shared_http_client, _quote_loop_task
    logger.info("Graceful shutdown starting…")
    try:
        phases = _graceful_shutdown_commit(reason="process_shutdown")
        logger.info("Graceful shutdown commit: %s", phases)
    except Exception as e:
        logger.warning("Graceful shutdown commit failed: %s", e)
    # Cancel quote loop task
    try:
        if _quote_loop_task is not None and not _quote_loop_task.done():
            _quote_loop_task.cancel()
            try:
                await _quote_loop_task
            except (asyncio.CancelledError, Exception):
                pass
            _quote_loop_task = None
    except Exception as e:
        logger.debug("quote loop cancel: %s", e)
    # Close shared HTTP client
    try:
        if _shared_http_client is not None and not _shared_http_client.is_closed:
            await _shared_http_client.aclose()
            logger.info("Shared httpx.AsyncClient closed")
            _shared_http_client = None
    except Exception as e:
        logger.warning("http client close: %s", e)
    logger.info("Graceful shutdown complete")


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
# CRITICAL: Never import or ping Upstash unless USE_REDIS=1.
# Residual REDIS_URL / UPSTASH_* env vars on Render must not cause a handshake.
_redis = None
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes") or \
   os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    _USE_REDIS = False

if _USE_REDIS:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    tok = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and tok:
        try:
            from upstash_redis import Redis as _UpstashRedis
            _redis = _UpstashRedis(url=url, token=tok)
            _redis.ping()
            logger.info("Connected to Upstash Redis (USE_REDIS=1)")
        except Exception as e:
            logger.warning("Redis unavailable (falling back to memory+Neon): %s", e)
            _redis = None
    else:
        logger.info("USE_REDIS=1 but UPSTASH credentials missing — memory+Neon only")
else:
    logger.info(
        "Gateway Redis disabled (USE_REDIS=0 / DISABLE_REDIS) — "
        "in-memory + Neon durable cache only; no Upstash handshake"
    )

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

# ── Global activity gate (Power Off / force-stop) ───────────────────────────
# When True: no scan workers, no data-feed worker progress, no hot-picks run,
# no WS quote upstream fan-out. Only /health and light keepalive remain.
_ACTIVITY_PAUSED = False
_QUOTE_LOOP_ENABLED = True
_SCAN_IN_PROGRESS = False  # True while market scan runs — pauses WS quote upstream


def activity_paused() -> bool:
    return bool(_ACTIVITY_PAUSED)


def scan_in_progress() -> bool:
    return bool(_SCAN_IN_PROGRESS)


def set_activity_paused(paused: bool) -> None:
    global _ACTIVITY_PAUSED, _QUOTE_LOOP_ENABLED
    _ACTIVITY_PAUSED = bool(paused)
    if paused:
        _QUOTE_LOOP_ENABLED = False
        try:
            request_data_feed_stop()
        except Exception:
            pass
    else:
        _QUOTE_LOOP_ENABLED = True
        try:
            clear_data_feed_stop()
        except Exception:
            pass


def _graceful_shutdown_commit(reason: str = "shutdown") -> list:
    """
    Commit in-progress work and force-stop background activity.
    Safe to call from Power Off, FastAPI shutdown, or SIGTERM/SIGINT.
    Does not close HTTP clients (caller may do that after).
    """
    phases = []
    try:
        set_activity_paused(True)
        phases.append({"phase": "activity_gate", "ok": True, "detail": f"paused ({reason})"})
    except Exception as e:
        phases.append({"phase": "activity_gate", "ok": False, "detail": str(e)[:120]})

    # Cancel all scans — preserve partial status in durable kv
    cancelled = 0
    try:
        _SCAN_CANCEL_FLAGS.add("__ALL__")
        try:
            for k in list(_mem_kv.keys()):
                sk = str(k)
                if sk.startswith(SCAN_TASK_PREFIX) and not sk.endswith(":cancel"):
                    data = _mem_kv.get(k)
                    if isinstance(data, dict) and data.get("status") == "running":
                        data = dict(data)
                        data["cancel_requested"] = True
                        data["status"] = "cancelled"
                        data["partial"] = True
                        data["message"] = f"{reason}: scan stopped (partial committed)"
                        _mem_kv[k] = data
                        try:
                            _redis_set(k, data, ttl=3600)
                            _redis_set(sk + ":cancel", True, ttl=3600)
                        except Exception:
                            pass
                        cancelled += 1
        except Exception:
            pass
        phases.append({"phase": "scan", "ok": True, "detail": f"cancel committed partial={cancelled}"})
    except Exception as e:
        phases.append({"phase": "scan", "ok": False, "detail": str(e)[:120]})

    # Data feed — force stop + checkpoint
    try:
        try:
            request_data_feed_stop()
        except Exception:
            pass
        store = _feed_store()
        job = store.job() or {}
        store.set_job(
            status="stopped",
            message=f"{reason}: data feed stopped (checkpoint committed)",
            stop_requested=True,
            finished_at=datetime.now(IST).isoformat(),
            processed=int(job.get("processed") or 0),
            ok_count=int(job.get("ok_count") or job.get("processed") or 0),
        )
        phases.append({"phase": "data_feed", "ok": True, "detail": "stopped + checkpoint"})
    except Exception as e:
        phases.append({"phase": "data_feed", "ok": False, "detail": str(e)[:120]})

    # Hot picks idle
    try:
        hot_job_set(
            _redis_set,
            _redis_get,
            status="idle",
            message=f"{reason}: Hot Picks stopped",
            processed=0,
            estimated_remaining_sec=0,
        )
        phases.append({"phase": "hot_picks", "ok": True, "detail": "idle"})
    except Exception as e:
        phases.append({"phase": "hot_picks", "ok": False, "detail": str(e)[:120]})

    # Drop WS quote watches + close sockets
    try:
        for ws in list(getattr(ws_manager, "active", []) or []):
            try:
                ws_manager.unwatch_quotes(ws, None)
            except Exception:
                pass
            try:
                # Best-effort close; may already be gone
                import asyncio as _aio
                try:
                    loop = _aio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(ws.close())
                    else:
                        loop.run_until_complete(ws.close())
                except Exception:
                    pass
            except Exception:
                pass
        phases.append({"phase": "websocket", "ok": True, "detail": "unwatched + close signalled"})
    except Exception as e:
        phases.append({"phase": "websocket", "ok": False, "detail": str(e)[:120]})

    # Stop quote broadcast task
    try:
        task = globals().get("_quote_loop_task")
        if task is not None and hasattr(task, "done") and not task.done():
            task.cancel()
        phases.append({"phase": "quote_loop", "ok": True, "detail": "cancelled"})
    except Exception as e:
        phases.append({"phase": "quote_loop", "ok": False, "detail": str(e)[:120]})

    logger.info("graceful_shutdown_commit reason=%s phases=%s", reason, phases)
    return phases


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
        # Warm local meta/job/index from Neon so UI is not blank after cold start
        try:
            _data_feed_store.meta()
            _data_feed_store.job()
            _data_feed_store.list_symbols()
        except Exception as e:
            logger.debug("data_feed warm: %s", e)
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
    """Load watchlist from dedicated durable table stockky_watchlist (survives hard-reset)."""
    try:
        import kv_cache as _kc
        val = _kc.watchlist_get()
        if isinstance(val, list):
            return [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in val if s]
        if isinstance(val, dict) and isinstance(val.get("symbols"), list):
            return [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in val["symbols"] if s]
    except Exception as e:
        logger.debug("watchlist_get fallback: %s", e)
    # Legacy fallback
    raw = _redis_get(WATCHLIST_KEY) or []
    if isinstance(raw, list):
        return [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in raw if s]
    return []

def _save_watchlist(symbols: List[str]):
    """Persist watchlist to stockky_watchlist table — never wiped by data-feed hard-reset."""
    clean = [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in (symbols or []) if s]
    try:
        import kv_cache as _kc
        _kc.watchlist_set(clean)
    except Exception as e:
        logger.warning("watchlist_set failed, legacy write: %s", e)
        _redis_set(WATCHLIST_KEY, clean, ttl=None)

def _load_searched() -> List[str]:
    return _redis_get(SEARCHED_KEY) or []

def _add_searched(symbol: str):
    searched = _load_searched()
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    if sym not in searched:
        searched.append(sym)
        _redis_set(SEARCHED_KEY, searched[-200:])

# ── Dynamic Universe Sources ──────────────────────────────────────────────────
# 2026-08-24: root cause of the recurring "GET https://www.nseindia.com
# HTTP/1.1 403 Forbidden" log line. Two separate bugs, same fix pattern
# ipo_scanner.py already uses for its own NSE session (_nse_session() /
# NSE_BOOTSTRAP_HEADERS there):
#   1. The client's default headers declared Accept: "application/json,
#      text/plain, */*" and were reused for the bootstrap GET to
#      https://www.nseindia.com — an HTML document. NSE's Akamai WAF treats
#      a JSON-Accept request with no Sec-Fetch-* navigation hints hitting an
#      HTML URL as a bot signature and returns 403. The 403 body still sets
#      cookies, so nothing raised/looked broken, but the session only ever
#      got the weak anonymous cookie pair, not the real nsit/nseappid pair —
#      every subsequent /api/ call rode on that weak session and got
#      rate-limited/blocked far more aggressively.
#   2. _nse_client was a permanent, never-refreshed module singleton: once
#      bootstrapped (well or badly), it was reused for the lifetime of the
#      process with no retry and no TTL, so a bad bootstrap at startup
#      poisoned every NSE call until the next deploy.
_nse_client: Optional[httpx.Client] = None
_nse_client_ts: float = 0.0
_NSE_CLIENT_TTL_SECONDS = 300  # matches ipo_scanner.py's _NSE_SESSION_TTL_SECONDS

_NSE_CLIENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "DNT": "1",
}
# Browser-navigation headers for the bootstrap HTML hop ONLY (see note 1
# above) — kept separate from _NSE_CLIENT_HEADERS so the subsequent /api/
# JSON calls keep declaring Accept: application/json as before.
_NSE_CLIENT_BOOTSTRAP_HEADERS = {
    "User-Agent": _NSE_CLIENT_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def _get_nse_client(force_new: bool = False) -> httpx.Client:
    global _nse_client, _nse_client_ts
    age = time.time() - _nse_client_ts
    if not force_new and _nse_client is not None and age < _NSE_CLIENT_TTL_SECONDS:
        return _nse_client
    old = _nse_client
    c = httpx.Client(headers=_NSE_CLIENT_HEADERS, timeout=15, follow_redirects=True)
    try:
        r = c.get("https://www.nseindia.com", headers=_NSE_CLIENT_BOOTSTRAP_HEADERS)
        names = set(c.cookies.keys())
        if not (names & {"nsit", "nseappid"}):
            logger.info(
                "main._get_nse_client: bootstrap cookies weak (%s; status %s) — "
                "payloads may be rate-limited",
                ",".join(sorted(names))[:120], r.status_code,
            )
    except Exception as e:
        logger.debug("main._get_nse_client bootstrap failed: %s", e)
    _nse_client = c
    _nse_client_ts = time.time()
    if old is not None and old is not c:
        try:
            old.close()
        except Exception:
            pass
    return _nse_client

def _fetch_from_nse_api(endpoint: str, cache_key: str, ttl: int = 21600):
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        client = _get_nse_client()
        url = f"https://www.nseindia.com/api/{endpoint}"
        resp = client.get(url)
        if resp.status_code in (401, 403):
            # Session's gone bad mid-TTL (NSE started blocking it) — don't
            # keep handing the same poisoned client to every caller for the
            # rest of the window. Force one fresh bootstrap and retry once.
            logger.info("NSE API %s -> HTTP %s, forcing session refresh", endpoint, resp.status_code)
            client = _get_nse_client(force_new=True)
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

# ---------------------------------------------------------------------------
# Symbol hygiene — keep index pseudo-tickers and delisted/renamed names out of
# the yfinance batches. NSE's equity-stock-indices response includes the index
# itself as a row ("NIFTY 50", "NIFTY MIDCAP 100"), and a few hardcoded fallback
# names are stale. Sending those to Yahoo produced the endless 404/503 spam
# ("NIFTY 50.NS", "NIFTY MIDCAP 100.NS", "NIFTYNEXT50.NS", "MOTHERSUMI.NS",
# "IBULHSGFIN.NS", "WELSPUNIND.NS") and wasted rate-limit budget. Filtered here
# at the source so every downstream universe/scan is clean.
_INDEX_PSEUDO_TOKENS = {
    "NIFTY", "NIFTY50", "NIFTYNEXT50", "NIFTY100", "NIFTY200", "NIFTY500",
    "NIFTYMIDCAP", "NIFTYMIDCAP50", "NIFTYMIDCAP100", "NIFTYMIDCAP150",
    "NIFTYSMALLCAP", "NIFTYSMALLCAP50", "NIFTYSMALLCAP100", "NIFTYSMALLCAP250",
    "BANKNIFTY", "NIFTYBANK", "FINNIFTY", "NIFTYFIN", "NIFTYIT", "NIFTYAUTO",
    "NIFTYPHARMA", "NIFTYFMCG", "NIFTYMETAL", "NIFTYREALTY", "NIFTYENERGY",
    "SENSEX", "BANKEX", "INDIAVIX",
}
# 2026-08-24: this dict used to be its OWN independently-maintained rename
# table — a 3rd/4th copy alongside symbol_aliases.py's SYMBOL_RENAMES and
# market-data-service's SMART_SYMBOL_MAP, none of which synced with each
# other (that drift is exactly how "JUBILANT" ended up in one static list
# but no rename table at all). Kept as a small LOCAL fast-path only (this
# function runs over the entire NSE securities list on every universe
# refresh, so a dict lookup here avoids importing symbol_aliases per-symbol
# for the common cases) but symbol_aliases.py is now the single source of
# truth: _clean_equity_symbol() below falls through to it for anything not
# in this fast-path, so a new rename/delisting only ever needs to be added
# in ONE place going forward.
_DELISTED_RENAME = {
    # stale NSE symbol -> current tradable symbol, or None to drop entirely
    "MOTHERSUMI": "MOTHERSON",   # Samvardhana Motherson renamed
    "SRTRANSFIN": "SHRIRAMFIN",  # Shriram Transport merged into Shriram Finance
    "ADANITRANS": "ADANIENSOL",  # Adani Transmission -> Adani Energy Solutions
    "IBULHSGFIN": None,          # Indiabulls Housing Finance — delisted/renamed, drop
    "WELSPUNIND": None,          # Welspun India (now WELSPUNLIV) — drop to be safe
    "MCDOWELL-N": None,          # legacy alias — drop
    "JUBILANT": "JUBLFOOD",      # was never a real ticker; see symbol_aliases.py
    "TATAMTRDVR": None,          # genuine 2024 merger into TATAMOTORS, not a rename
}


def _clean_equity_symbol(sym) -> Optional[str]:
    """Return a tradable NSE equity symbol, or None if it's an index
    pseudo-ticker or a delisted/renamed name that must not reach yfinance.

    Checks the small local fast-path table first, then falls through to
    symbol_aliases.py (the durable, KV-backed, learned-rename source of
    truth shared with the repair/premarket paths) so this universe builder
    can never again silently miss a rename that's already been solved
    elsewhere in the app — closing the exact gap that let JUBILANT sit in
    the universe with zero rename entry anywhere.
    """
    if not sym:
        return None
    s = str(sym).strip().upper()
    if not s or s == "-":
        return None
    # Real NSE equity tickers never contain a space; index display names do.
    if " " in s:
        return None
    if s in _INDEX_PSEUDO_TOKENS:
        return None
    if s in _DELISTED_RENAME:
        return _DELISTED_RENAME[s]  # may be a renamed ticker or None (drop)
    try:
        from symbol_aliases import resolve_base_symbol, is_known_delisted
        if is_known_delisted(s):
            return None
        resolved = resolve_base_symbol(s)
        return resolved  # None => known non-NSE / confirmed delisted, drop
    except Exception:
        return s


def _filter_equities(symbols) -> List[str]:
    """Clean + dedupe a list of raw symbols, dropping index/delisted entries."""
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        c = _clean_equity_symbol(raw)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _get_all_nse_securities() -> List[str]:
    data = _fetch_from_nse_api("equity-stock-indices?index=SECURITIES%20IN%20NSE", "nse:all_securities")
    symbols = []
    if data and "data" in data and isinstance(data["data"], list):
        for item in data["data"]:
            if isinstance(item, dict) and item.get("symbol"):
                symbols.append(item["symbol"].upper())
    logger.info(f"Fetched {len(symbols)} securities from NSE")
    if not symbols:
        # §3: NSE live API unreachable — use bhavcopy universe (real EQ/BE/BZ
        # symbols that actually traded, independent of the live JSON API).
        try:
            resp = httpx.get(
                f"{MARKET_DATA_URL}/bhavcopy/universe",
                timeout=15.0,
            )
            resp.raise_for_status()
            symbols = resp.json().get("symbols") or []
            logger.warning(
                "NSE live API unreachable — using bhavcopy universe fallback "
                "(%d symbols)", len(symbols)
            )
        except Exception as e:
            logger.error("bhavcopy universe fallback failed: %s", e)
            symbols = []

    if not symbols:
        # Absolute last resort — both NSE live API and bhavcopy are down.
        # Keep this list but it is NEVER the expected steady-state path.
        symbols = [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH",
            "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "M&M", "MARUTI",
            "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "BAJFINANCE", "BAJAJFINSV",
            "WIPRO", "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT",
            "AXISBANK", "BPCL", "BRITANNIA", "CIPLA", "COALINDIA",
            "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM", "HDFCLIFE",
            "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "INDUSINDBK", "JSWSTEEL",
            "LTIM", "LTTS", "MANKIND", "MOTHERSON", "MUTHOOTFIN",
            "PIDILITIND", "RECLTD", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
            "TATAMOTORS", "TATASTEEL", "TECHM", "TITAN", "TORNTPOWER",
            "TRENT", "ULTRACEMCO", "UPL", "VEDL", "ZOMATO",
        ]
        logger.warning("Using static last-resort list with %d symbols (both live sources down)", len(symbols))

    return _filter_equities(symbols)

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
    all_symbols = _filter_equities(all_symbols + fallback)
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
        # §3: try ipo_scanner's independent calendar source before dropping to static list
        try:
            from ipo_scanner import fetch_ipoalerts_calendar
            alt_rows = fetch_ipoalerts_calendar()
            symbols = [r.get("symbol") for r in (alt_rows or []) if r.get("symbol")]
            if symbols:
                logger.info("_get_recent_ipos: ipoalerts fallback returned %d symbols", len(symbols))
        except Exception as e:
            logger.warning("ipoalerts fallback for recent-ipos failed: %s", e)

    if not symbols:
        symbols = ["JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO", "RAILTEL", "IRFC", "MVELECTRO"]
        logger.warning("_get_recent_ipos: using static 9-name fallback list")
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
                    sym = _clean_equity_symbol(sym)
                    if not sym:
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
                yf_ticker = resolve_ns_ticker(sym)
                if not yf_ticker:
                    continue  # known non-NSE / delisted — skip, don't burn a call
                hist = yf.Ticker(yf_ticker).history(period="5d", interval="1d")
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
        # Cover the full scan universe (was a hardcoded 400, stale since
        # SCAN_UNIVERSE_TARGET went 300 -> 500) or a news mention of a symbol in
        # slot 401-500 never becomes a momentum mover.
        candidates = sorted(
            set(_get_all_nse_securities()[:max(400, SCAN_UNIVERSE_TARGET)] + _get_nifty_indices()),
            key=len, reverse=True,
        )
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
    return out[:120]

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


def _get_bulk_deal_symbols() -> List[str]:
    """Symbols appearing in recent NSE bulk / block deals (institutional flow)."""
    out: List[str] = []
    seen = set()

    def _add(sym: str):
        s = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not s or s in seen or len(s) < 2:
            return
        seen.add(s)
        out.append(s)

    # 1) NSE official bulk-deals board
    for endpoint, key in (
        ("equity-stockIndices?index=SECURITIES%20IN%20F%26O", "nse:fo_bulk_seed"),
        ("historical/bulk-deals", "nse:bulk_deals"),
        ("historical/block-deals", "nse:block_deals"),
    ):
        try:
            data = _fetch_from_nse_api(endpoint, key, ttl=1800)
            rows = []
            if isinstance(data, dict):
                rows = data.get("data") or data.get("bulkDeals") or data.get("blockDeals") or []
            elif isinstance(data, list):
                rows = data
            if isinstance(rows, list):
                for item in rows[:80]:
                    if isinstance(item, dict):
                        _add(item.get("symbol") or item.get("symbolName") or item.get("scm"))
                    elif isinstance(item, str):
                        _add(item)
        except Exception as e:
            logger.debug("bulk deals %s: %s", endpoint, e)

    # 2) Market-data service bulk/block endpoints (if exposed)
    try:
        base = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
        for path in ("/market/bulk-deals", "/market/block-deals", "/nse/bulk-deals"):
            try:
                r = httpx.get(f"{base}{path}", timeout=10)
                if r.status_code != 200:
                    continue
                payload = r.json()
                rows = payload if isinstance(payload, list) else (payload.get("data") or payload.get("deals") or [])
                for item in (rows or [])[:80]:
                    if isinstance(item, dict):
                        _add(item.get("symbol") or item.get("Symbol"))
                    elif isinstance(item, str):
                        _add(item)
            except Exception:
                continue
    except Exception as e:
        logger.debug("market-data bulk: %s", e)

    # 3) Event service bulk/insider tags
    try:
        resp = httpx.get(f"{EVENT_URL}/bulk_deals", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            rows = data if isinstance(data, list) else (data.get("symbols") or data.get("data") or [])
            for item in rows[:80]:
                if isinstance(item, dict):
                    _add(item.get("symbol"))
                elif isinstance(item, str):
                    _add(item)
    except Exception:
        pass

    logger.info("Bulk/block deal symbols: %s", len(out))
    return out[:100]


def _get_52w_extreme_symbols() -> List[str]:
    """Symbols near 52-week high/low or with sharp multi-day moves (surprise raise)."""
    out: List[str] = []
    seen = set()

    def _add(sym: str):
        s = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    base = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
    for path in (
        "/market/near-52w-high",
        "/market/near-52w-low",
        "/market/52-week-high",
        "/market/52-week-low",
        "/market/top-gainers",
        "/market/most-active",
    ):
        try:
            r = httpx.get(f"{base}{path}", timeout=10)
            if r.status_code != 200:
                continue
            payload = r.json()
            rows = payload if isinstance(payload, list) else (
                payload.get("data") or payload.get("symbols") or payload.get("stocks") or []
            )
            for item in (rows or [])[:50]:
                if isinstance(item, dict):
                    # Prefer names with ≥4% day move or near 52w when fields exist
                    chg = item.get("change_pct") or item.get("pChange") or item.get("pctChange")
                    try:
                        chg_f = abs(float(chg)) if chg is not None else None
                    except (TypeError, ValueError):
                        chg_f = None
                    near = item.get("near_52w_high") or item.get("near_52w_low") or item.get("at_52w_high")
                    if near or chg_f is None or chg_f >= 3.0:
                        _add(item.get("symbol") or item.get("Symbol") or item.get("ticker"))
                elif isinstance(item, str):
                    _add(item)
        except Exception as e:
            logger.debug("52w/movers %s: %s", path, e)

    # NSE gainers already partially covered; add all-time/52w boards if present
    for endpoint, key in (
        ("live-analysis-variations?index=gainers", "nse:gainers52"),
        ("liveEquity-market?index=gainers", "nse:live_gainers"),
    ):
        try:
            data = _fetch_from_nse_api(endpoint, key, ttl=900)
            rows = []
            if isinstance(data, dict):
                rows = data.get("data") or []
            for item in (rows or [])[:40]:
                if isinstance(item, dict):
                    chg = item.get("pChange") or item.get("perChange")
                    try:
                        if chg is not None and abs(float(chg)) < 3.0:
                            continue
                    except (TypeError, ValueError):
                        pass
                    _add(item.get("symbol") or item.get("symbolName"))
        except Exception:
            continue

    logger.info("52w/extreme move symbols: %s", len(out))
    return out[:80]


# ── Universe price gate — OFF by default (0 = no cap; full universe) ───────
# Set MAX_UNIVERSE_PRICE (or MAX_STOCK_PRICE) in the environment to re-enable
# an explicit cap. VALUE_BUY_THRESHOLD is a display/tag-only hint.
MAX_UNIVERSE_PRICE = float(os.getenv("MAX_UNIVERSE_PRICE", os.getenv("MAX_STOCK_PRICE", "0")) or 0)
VALUE_BUY_THRESHOLD = float(os.getenv("VALUE_BUY_THRESHOLD", "2000") or 2000)

# ── Universe size ────────────────────────────────────────────────────────────
# SCAN_UNIVERSE_TARGET is how many symbols _build_scan_universe() tries to
# assemble (raised from 300 -> 500 stocks). SCAN_UNIVERSE_HARD_CAP is a
# separate, unrelated safety ceiling — an absolute cap on total tracked
# symbols so a bug in universe assembly (dedup failure, a runaway source,
# etc.) can never grow the feed unboundedly; it should never actually be hit
# in normal operation at a 500-symbol target.
SCAN_UNIVERSE_TARGET = int(os.getenv("SCAN_UNIVERSE_TARGET", "500"))
SCAN_UNIVERSE_HARD_CAP = int(os.getenv("SCAN_UNIVERSE_HARD_CAP", "5000"))


def _row_price_over_cap(row: dict, symbol: Optional[str] = None) -> bool:
    """
    True when a feed row's resolved price is a known, positive value that
    exceeds MAX_UNIVERSE_PRICE, OR when the row/caller-supplied symbol is on
    the static KNOWN_HIGH_PRICE_SYMBOLS denylist (symbol_aliases.py). Used
    by every data-feed WRITE path so a >₹5000 stock can never be persisted
    (previously only the bulk-Yahoo and repair-price paths enforced this —
    /api/feed/update, /api/feed/batch and /api/feed/update-batch did not,
    which is how stocks like BOSCHLTD (₹48,750) ended up sitting in the
    Database Feed Health audit table with no way for Repair to "fix" them,
    since they were never supposed to be fed in the first place).

    The by-name check matters for rows that have NO stored price yet (still
    being built / never successfully fetched): previously those fell
    through to "unknown, allow it" and were classified as "incomplete" in
    the audit rather than "over_cap" — so Database Feed Health kept
    offering a Repair button that could only ever discover (via a live
    quote burn) what the static denylist already knows for free, every
    single audit/repair cycle, for the same symbols, forever.

    Rows with no resolvable price AND an unlisted symbol are allowed
    through — this only blocks rows that already carry a price over cap,
    or whose symbol is known-expensive by name.
    """
    if MAX_UNIVERSE_PRICE <= 0:
        return False  # no cap configured — every eligible stock passes
    sym = symbol or (row.get("symbol") if isinstance(row, dict) else None)
    if sym:
        try:
            from symbol_aliases import is_known_high_price
            if is_known_high_price(sym):
                return True
        except Exception:
            pass
    try:
        for k in ("price", "close", "cmp", "ltp", "last_price", "current_price"):
            raw = row.get(k)
            if raw in (None, ""):
                m = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
                raw = m.get(k)
            if raw in (None, ""):
                continue
            s = str(raw).replace(",", "").replace(" ", "").strip()
            if not s or s.upper() in ("-", "NA", "N/A", "NONE", "NULL"):
                continue
            px = float(s)
            if px > 0:
                return px > MAX_UNIVERSE_PRICE
    except (TypeError, ValueError):
        pass
    return False


def _filter_symbols_under_max_price(symbols: List[str]) -> List[str]:
    """
    Drop symbols whose known feed/DB price is > ₹5000.
    Unknown price (0) is KEPT so a later live fetch can still populate them;
    the scanner AVOID path will kill any that remain missing or over-limit.
    Aligns frontend "Scanned" vs "Total" counters with the root price gate.
    """
    if not symbols:
        return []
    clean = []
    seen = set()
    for s in symbols:
        su = str(s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if su and su not in seen:
            seen.add(su)
            clean.append(su)
    if not clean:
        return []

    feeds: dict = {}
    try:
        from data_feed import get_all_stock_feeds
        feeds = get_all_stock_feeds(clean) or {}
    except Exception as e:
        logger.debug("universe price filter feed load: %s", e)
        feeds = {}

    try:
        from price_resolver import resolve_display_price
    except Exception:
        resolve_display_price = None  # type: ignore

    try:
        from symbol_aliases import is_known_high_price
    except Exception:
        is_known_high_price = lambda _s: False  # noqa: E731

    kept: List[str] = []
    dropped = 0
    for sym in clean:
        # Static denylist only applies when a cap is actually configured —
        # with no cap, no symbol is excluded by price, known-expensive or not.
        if MAX_UNIVERSE_PRICE > 0 and is_known_high_price(sym):
            dropped += 1
            continue
        feed_item = feeds.get(sym) or {}
        price = 0.0
        if resolve_display_price is not None:
            try:
                price = float(resolve_display_price(sym, {}, feed_item) or 0)
            except Exception:
                price = 0.0
        else:
            for k in ("price", "close", "cmp", "ltp", "last_price", "prev_close"):
                try:
                    v = float(feed_item.get(k) or 0)
                    if v > 0:
                        price = v
                        break
                except (TypeError, ValueError):
                    pass
        # Keep if unknown (0), or price is within the configured cap, or no
        # cap is configured at all (MAX_UNIVERSE_PRICE <= 0 => unrestricted)
        if price <= 0 or MAX_UNIVERSE_PRICE <= 0 or price <= MAX_UNIVERSE_PRICE:
            kept.append(sym)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "Universe ≤₹%.0f filter: kept=%s dropped=%s",
            MAX_UNIVERSE_PRICE, len(kept), dropped,
        )
    return kept


# ── Build scan universe ──────────────────────────────────────────────────────
def _build_scan_universe() -> List[str]:

    cached = _redis_get(SCAN_UNIVERSE_KEY)
    if cached and isinstance(cached, list) and len(cached) > 0:
        # Always re-apply ≤₹5000 gate so a stale cache cannot reintroduce high-ticket names
        return _filter_symbols_under_max_price(cached)

    universe = set()
    try:
        all_stocks = _get_all_nse_securities()
        if all_stocks:
            universe.update(all_stocks[:SCAN_UNIVERSE_TARGET])
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
        universe.update(_get_bulk_deal_symbols())
        universe.update(_get_52w_extreme_symbols())
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
        fallback = [
            # Under typical ≤₹5000 liquid names (mega-caps like RELIANCE/TCS excluded —
            # they fail the price gate and would never get durable price rows)
            "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH", "ITC", "SBIN",
            "BHARTIARTL", "KOTAKBANK", "AXISBANK", "WIPRO",
        ]
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

    # Prefer dynamic signal names first so movers/news/bulk/52w/events always get scanned
    dynamic_priority = []
    try:
        for src in (
            _get_bulk_deal_symbols(),
            _get_52w_extreme_symbols(),
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

    # Target ~SCAN_UNIVERSE_TARGET names (default 500); if live sources thin, pad
    # from the liquid NSE list. Padding floor scales with the target instead of
    # a fixed 200/220 so the same "thin sources" logic still makes sense at a
    # larger target size.
    pad_floor = max(200, int(SCAN_UNIVERSE_TARGET * 0.4))
    if len(ordered) < pad_floor:
        pad = [s for s in _get_all_nse_securities() if s not in set(ordered)]
        ordered.extend(pad[: max(0, pad_floor + 20 - len(ordered))])
        logger.info("Universe padded to %s symbols (live sources were thin)", len(ordered))

    result = ordered[:SCAN_UNIVERSE_TARGET]
    # Absolute safety ceiling — should never actually trigger at a 500-symbol
    # target, but guards against a future dedup/merge bug silently growing
    # the tracked universe without bound.
    if len(result) > SCAN_UNIVERSE_HARD_CAP:
        result = result[:SCAN_UNIVERSE_HARD_CAP]
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
    result = _filter_symbols_under_max_price(result)
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=ttl)
    logger.info(
        "Scan universe built: %s symbols (dynamic=%s, ttl=%ss, ≤₹%.0f gate)",
        len(result),
        len(dynamic_priority),
        ttl,
        MAX_UNIVERSE_PRICE,
    )
    return result

# ── Symbol resolution ──────────────────────────────────────────────────────
def _get_all_known_symbols() -> Set[str]:
    cached = _redis_get(KNOWN_SYMBOLS_KEY)
    if cached and isinstance(cached, list):
        return set(cached)
    combined = set()
    try:
        # Must cover at least the whole scan universe: this set is what symbol
        # resolution / search treats as "a real symbol", so a hardcoded 300 here
        # after SCAN_UNIVERSE_TARGET was raised to 500 meant symbols in slots
        # 301-500 were scanned by the scanner but rejected as unknown by lookup.
        combined.update(_get_all_nse_securities()[:max(300, SCAN_UNIVERSE_TARGET)])
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
    try:
        from price_resolver import ensure_row_price
        merged = ensure_row_price(merged)
    except Exception:
        pass
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
    """Live price with short timeout (free-tier). Tries several JSON fields."""
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=4.0)
        if resp.status_code != 200:
            return None
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            return None
        for k in ("price", "close", "ltp", "regularMarketPrice", "last"):
            v = data.get(k)
            if v is not None:
                try:
                    px = float(v)
                    if px > 0:
                        return px
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.debug("Price fetch failed for %s: %s", symbol, e)
    return None


async def _fetch_prices_bulk_async(symbols: list, client: httpx.AsyncClient) -> dict:
    """Concurrent short quotes for a scan chunk → {BASE: float}."""
    out = {}
    sem = asyncio.Semaphore(8)  # aligned with MAX_PARALLEL_WORKERS (free-tier safe)

    async def one(sym: str):
        base = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
        async with sem:
            try:
                r = await client.get(
                    f"{MARKET_DATA_URL.rstrip('/')}/quote/{sym}",
                    timeout=3.5,
                )
                if r.status_code != 200:
                    return
                data = r.json()
                if not isinstance(data, dict):
                    return
                for k in ("price", "close", "ltp", "regularMarketPrice", "last"):
                    v = data.get(k)
                    if v is not None:
                        try:
                            px = float(v)
                            if px > 0:
                                out[base] = px
                                return
                        except (TypeError, ValueError):
                            pass
            except Exception:
                return

    await asyncio.gather(*(one(s) for s in symbols), return_exceptions=True)
    return out

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
    mid-thought.

    BUG FIX: this used to trust the `client` argument passed in by the
    caller, captured at the start of that request. If a graceful shutdown
    (container restart/redeploy — common on Render free-tier cold-start
    cycling) closed `_shared_http_client` while this request was still
    in flight, the caller's reference stayed pointing at the now-closed
    client object even though `_get_http_client()` would hand any *new*
    caller a fresh one. Result: "Cannot send a request, as the client
    has been closed" — confirmed this was the actual failure, not a
    Gemini-side issue, since decision-prediction-service's own Gemini
    call succeeded in the same log window. Fetching fresh here means
    this call always gets whichever client is currently alive."""
    if not GEMINI_API_KEY:
        return _generate_summary(data)
    client = _get_http_client()  # always use the live client, ignore a possibly-stale one passed in
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

# Free-tier safe default: 8 concurrent workers (was 12).
# Higher values (12–20) spawn 4–5 internal HTTP calls per stock → 50–100+ concurrent
# requests into analysis-intelligence-service, causing PoolTimeout / ReadTimeout and
# circuit-breaker opens that feed neutral 50.0 scores into the ML model.
# Override via MAX_PARALLEL_SCAN_WORKERS if you move to paid tier.
MAX_PARALLEL_WORKERS = int(os.getenv("MAX_PARALLEL_SCAN_WORKERS", "8"))  # free-tier safe
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "8"))  # aligned with workers; bulk Neon removes DB N+1

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
    """GET with circuit breaker + one ReadTimeout retry (cold-start safe)."""
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
    last_err = None
    for attempt in range(2):  # 1 retry on ReadTimeout / ConnectTimeout only
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
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(0.6 + attempt * 0.4)  # brief backoff for cold start
                continue
            metrics.observe_ms("stockky_dependency_latency", (time.time() - t0) * 1000, dependency=name)
            metrics.inc("stockky_dependency_errors_total", dependency=name)
            br.record_failure(str(e))
            raise
        except Exception as e:
            metrics.observe_ms("stockky_dependency_latency", (time.time() - t0) * 1000, dependency=name)
            metrics.inc("stockky_dependency_errors_total", dependency=name)
            br.record_failure(str(e))
            raise
    if last_err:
        raise last_err


async def _cb_post(client: httpx.AsyncClient, name: str, url: str, timeout: float = 8.0, **kwargs):
    """POST with circuit breaker + one ReadTimeout retry."""
    br = get_breaker(name)
    if not br.allow():
        raise CircuitOpenError(name, br.retry_after())
    last_err = None
    for attempt in range(2):
        try:
            resp = await client.post(url, timeout=timeout, **kwargs)
            if resp.status_code >= 500:
                br.record_failure(f"HTTP {resp.status_code}")
            else:
                br.record_success()
            return resp
        except CircuitOpenError:
            raise
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(0.6)
                continue
            br.record_failure(str(e))
            raise
        except Exception as e:
            br.record_failure(str(e))
            raise
    if last_err:
        raise last_err


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
    feed_row: dict = None,
    prefetched_feeds: dict = None,
    skip_gemini: bool = False,
) -> dict:
    """
    Analyse one symbol with parallel internal calls and caching.
    Timeouts: decision 90s, others 60s.
    Speed fixes:
    - Prefer data already returned by Decision Engine (avoid duplicate fund/news/event/pred fetches).
    - Optional decide-level Redis cache.
    - lite=True skips Gemini summary + optional enrichment when Decision already filled fields.
    - skip_gemini=True is a NARROWER switch than lite: it only skips the Gemini
      natural-language summary (falls back to the free Hinglish template) while
      still running the full technical/fundamental/news/event/prediction pillars.
      Run Market Scan passes skip_gemini=True for every symbol (Gemini's RPM
      limit can't cover 300-500 symbols per scan) while lite stays False so scan
      quality is unaffected; single-stock Analyse leaves skip_gemini=False so
      that path keeps the real Gemini summary.
    """
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                base_sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
                # ── Neon Data Feed (stockky_kv) — prefer bulk-prefetched row ──
                # Scan stream / batch passes prefetched_feeds from one get_symbols_bulk()
                # so we do NOT re-query Neon per symbol (that was the slow path).
                if feed_row is None and isinstance(prefetched_feeds, dict) and base_sym:
                    feed_row = prefetched_feeds.get(base_sym)
                if feed_row is None:
                    try:
                        feed_row = _feed_store().get_symbol(base_sym)
                    except Exception:
                        feed_row = None
                if isinstance(feed_row, dict) and feed_row:
                    # Warm process-local cache so later get_symbol hits memory
                    try:
                        from data_feed import DATA_FEED_PREFIX as _dfp, _LOCAL_SYMBOLS as _ls, _LOCAL_INDEX as _li
                        _ls[_dfp + base_sym] = dict(feed_row)
                        _li.add(base_sym)
                    except Exception:
                        pass

                # Lite mode: NEVER wait on decision-engine (45s) — free-tier scan must stay fast.
                # Prefer Neon feed row; otherwise quote-only HOLD with a real price when available.
                if lite:
                    try:
                        close_px = None
                        if isinstance(feed_row, dict) and feed_row:
                            for pk in ("close", "price", "ltp", "prev_close", "last"):
                                if feed_row.get(pk) is not None:
                                    try:
                                        close_px = float(feed_row[pk])
                                        break
                                    except (TypeError, ValueError):
                                        pass
                        if close_px is None:
                            try:
                                close_px = _fetch_price_from_quote(symbol)
                            except Exception:
                                close_px = None

                        has_feed = isinstance(feed_row, dict) and feed_row and (
                            feed_row.get("fundamental_score") is not None
                            or feed_row.get("metrics")
                            or feed_row.get("combined_score") is not None
                            or feed_row.get("decision")
                        )
                        if has_feed or close_px is not None:
                            # Always take lite fast-path (skip decision HTTP)
                            fr = feed_row if isinstance(feed_row, dict) else {}
                            score = (
                                fr.get("combined_score")
                                or fr.get("fundamental_score")
                                or fr.get("technical_score")
                                or 50
                            )
                            try:
                                score = float(score)
                            except Exception:
                                score = 50.0
                            fast = _normalize_decision_response({
                                "symbol": base_sym,
                                "decision": fr.get("decision") or "HOLD",
                                "combined_score": score,
                                "confidence": fr.get("confidence") or "Low",
                                "close": close_px,
                                "fundamental_score": fr.get("fundamental_score"),
                                "fundamental_metrics": fr.get("metrics"),
                                "sector": fr.get("sector"),
                                "industry": fr.get("industry"),
                                "technical_score": fr.get("technical_score"),
                                "news_score": fr.get("news_score"),
                                "event_risk": fr.get("event_risk"),
                                "prediction_score": fr.get("prediction_score"),
                                "reasons": {
                                    "lite": [
                                        "Lite scan: skipped decision-engine for speed",
                                        "Price from data-feed or market-data quote when available",
                                    ]
                                },
                                "from_data_feed": bool(fr),
                                "lite_fastpath": True,
                            }, symbol)
                            if fast.get("close") is None and close_px is not None:
                                fast["close"] = close_px
                            if fast.get("close") is not None:
                                try:
                                    c = float(fast["close"])
                                    if fast.get("support") is None:
                                        fast["support"] = round(c * 0.95, 2)
                                    if fast.get("resistance") is None:
                                        fast["resistance"] = round(c * 1.05, 2)
                                except Exception:
                                    pass
                            fast["natural_language_summary"] = (
                                f"{base_sym}: lite path — "
                                f"decision={fast.get('decision')} score={fast.get('combined_score')} "
                                f"close={fast.get('close')}"
                            )
                            return fast
                    except Exception as e:
                        logger.debug("lite fastpath %s: %s", base_sym, e)

                # ── Decide-level cache (same symbol within TTL → instant) ──
                cache_key = f"{DECIDE_CACHE_PREFIX}{base_sym}"
                cached_decide = _redis_get(cache_key)
                if cached_decide and isinstance(cached_decide, dict) and cached_decide.get("decision"):
                    normalized = _normalize_decision_response(cached_decide, symbol)
                    normalized["from_decide_cache"] = True
                else:
                    # Prefer POST /decide/evaluate when Neon feed already has RSI/PE/scores.
                    # This short-circuits internal HTTP fan-out inside the decision service
                    # and prevents free-tier PoolTimeout / circuit opens.
                    feed_has_data = isinstance(feed_row, dict) and any(
                        feed_row.get(k) is not None
                        for k in (
                            "rsi", "pe_ratio", "pe", "technical_score",
                            "fundamental_score", "news_score", "close", "price", "ltp",
                        )
                    )
                    if feed_has_data:
                        eval_payload = {
                            "symbol": base_sym or symbol,
                            "rsi": feed_row.get("rsi"),
                            "pe_ratio": feed_row.get("pe_ratio") or feed_row.get("pe"),
                            "technical_score": feed_row.get("technical_score"),
                            "fundamental_score": feed_row.get("fundamental_score"),
                            "news_score": feed_row.get("news_score"),
                            "sentiment_score": feed_row.get("sentiment_score") or feed_row.get("market_score"),
                            "close": feed_row.get("close") or feed_row.get("price") or feed_row.get("ltp") or feed_row.get("cmp"),
                            "support": feed_row.get("support"),
                            "resistance": feed_row.get("resistance"),
                            "sector": feed_row.get("sector"),
                            "valuation": feed_row.get("valuation"),
                            "metrics": feed_row.get("metrics"),
                            "events": feed_row.get("events"),
                            "event_risk": feed_row.get("event_risk"),
                        }
                        # Drop None values to keep payload clean
                        eval_payload = {k: v for k, v in eval_payload.items() if v is not None}
                        # Bottleneck fix: during a scan (this function processes the
                        # whole universe), a symbol with a partially-filled feed row
                        # (e.g. news_score missing) previously made decision-service
                        # fall through to a LIVE per-symbol HTTP fetch (news/events/
                        # prediction/training) inside /decide/evaluate — that's what
                        # was slowing down full & lite scans and quietly serializing
                        # hundreds of upstream calls. skip_http tells decision-service
                        # to score strictly off what we already have (missing pillars
                        # default to neutral 50) instead of reaching upstream again.
                        # Single-stock "Analyse" (get_stock_decision, below) is a
                        # separate code path and is untouched — it still always does
                        # one live force=true /decide/{symbol} call.
                        eval_payload["skip_http"] = True
                        decision_resp = await _cb_post(
                            client,
                            "decision",
                            f"{DECISION_URL}/decide/evaluate",
                            timeout=20,
                            json=eval_payload,
                        )
                    else:
                        decision_resp = await _cb_get(
                            client, "decision", f"{DECISION_URL}/decide/{symbol}", timeout=15
                        )
                    decision_resp.raise_for_status()
                    raw = decision_resp.json()
                    normalized = _normalize_decision_response(raw, symbol)
                    _redis_set(cache_key, normalized, ttl=_decide_cache_ttl())

                # Overlay durable Neon feed onto decision (do not call fund/news upstream if present)
                if isinstance(feed_row, dict) and feed_row:
                    normalized.setdefault("from_data_feed", True)
                    normalized["data_feed_updated_at"] = feed_row.get("updated_at")
                    if normalized.get("fundamental_metrics") is None and feed_row.get("metrics"):
                        normalized["fundamental_metrics"] = feed_row.get("metrics")
                    if normalized.get("fundamental_score") is None and feed_row.get("fundamental_score") is not None:
                        normalized["fundamental_score"] = feed_row.get("fundamental_score")
                    for k in ("sector", "industry", "valuation", "quality_score", "multi_quarter_score"):
                        if normalized.get(k) is None and feed_row.get(k) is not None:
                            normalized[k] = feed_row.get(k)
                    if not normalized.get("event_data") and (
                        feed_row.get("events") or feed_row.get("event_risk") is not None
                    ):
                        normalized["event_data"] = feed_row.get("events") or {
                            "event_risk": feed_row.get("event_risk")
                        }
                    if normalized.get("event_risk") is None and feed_row.get("event_risk") is not None:
                        normalized["event_risk"] = feed_row.get("event_risk")
                    if normalized.get("news_score") is None and feed_row.get("news_score") is not None:
                        normalized["news_score"] = feed_row.get("news_score")
                    # Price from feed if upstream quote is rate-limited
                    if normalized.get("close") is None:
                        for pk in ("close", "price", "last", "ltp"):
                            if feed_row.get(pk) is not None:
                                try:
                                    normalized["close"] = float(feed_row[pk])
                                    break
                                except (TypeError, ValueError):
                                    pass

                if normalized.get("close") is None:
                    # Avoid hammering market-data during Yahoo 429 storms — soft try only
                    try:
                        price = _fetch_price_from_quote(symbol)
                    except Exception:
                        price = None
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
                        try:
                            fund_res = tasks["fund"].result()
                        except Exception:
                            fund_res = ({}, True)
                        if isinstance(fund_res, tuple):
                            fund_metrics, fund_fallback = fund_res
                        else:
                            fund_metrics, fund_fallback = {}, True
                        if fund_metrics:
                            normalized["fundamental_metrics"] = fund_metrics
                            normalized["fundamental_fallback"] = fund_fallback

                    if "event" in tasks:
                        try:
                            event_data = tasks["event"].result()
                        except Exception:
                            event_data = None
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
                        try:
                            news_data = tasks["news"].result()
                        except Exception:
                            news_data = None
                        if news_data:
                            normalized["news_score"] = news_data.get("news_score")
                            reasons = normalized.get("reasons", {})
                            if news_data.get("reasons"):
                                reasons["news"] = news_data["reasons"]
                                normalized["reasons"] = reasons

                    if "pred" in tasks:
                        try:
                            pred_res = tasks["pred"].result()
                        except Exception:
                            pred_res = (None, None)
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
                # Gemini only when neither lite nor skip_gemini is set — Run Market
                # Scan passes skip_gemini=True (see call sites) to keep Gemini
                # reserved for single-stock Analyse; both flags fall back to the
                # free Hinglish template so the field is never left empty.
                if not lite and not skip_gemini:
                    normalized["natural_language_summary"] = await _generate_ai_summary(normalized, client)
                else:
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
    global _SCAN_IN_PROGRESS
    start_time = time.time()
    _SCAN_IN_PROGRESS = True
    try:
        set_activity_paused(False)
        _SCAN_CANCEL_FLAGS.discard("__ALL__")
    except Exception:
        pass

    # Prioritize watchlist / searched so useful picks surface early without shrinking universe
    universe = _prioritize_universe(universe)
    total = len(universe)
    processed = 0
    results = []
    errors = []

    def _status(processed_n=0, message=None, **extra):
        payload = {
            "status": "running",
            "total": total,
            "processed": processed_n,
            "elapsed": round(time.time() - start_time, 1),
            "result": None,
            "error": None,
            "lite": lite,
            "message": message,
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
        _redis_set(SCAN_TASK_PREFIX + task_id, payload, ttl=3600)

    _status(0, message="Starting — Neon data-feed preferred; upstream only for live fields")
    logger.info("Scan %s started universe=%s lite=%s", task_id, total, lite)

    sem = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    cancel_key = SCAN_TASK_PREFIX + task_id + ":cancel"
    client = _get_http_client()  # shared keepalive pool

    # Data Feed coverage FIRST — ONE Neon bulk query (not 300 sequential hits)
    feed_hit = 0
    prefetched_feeds: dict = {}
    try:
        _status(0, message="Bulk-loading Neon data-feed (single query)…")
        from data_feed import get_all_stock_feeds
        bases = [s.upper().replace(".NS", "").replace(".BO", "").strip() for s in universe]
        prefetched_feeds = get_all_stock_feeds(bases) or {}
        for base, fed in prefetched_feeds.items():
            if fed and (
                fed.get("fundamental_score") is not None
                or fed.get("metrics")
                or fed.get("sector")
                or fed.get("close") is not None
            ):
                feed_hit += 1
        logger.info(
            "Scan Data Feed bulk coverage: %s/%s symbols (%.0f%%) in 1 query",
            feed_hit, total, (100.0 * feed_hit / total) if total else 0,
        )
        _status(0, message=f"Neon bulk feed {feed_hit}/{total} — starting batches")
    except Exception as e:
        logger.debug("feed bulk coverage: %s", e)
        prefetched_feeds = {}


    # Pre-scan wake only when feed is cold (<50%). Full wake is slow and unnecessary
    # when Data Feed already holds fundamentals for the universe.
    feed_ratio = (feed_hit / total) if total else 0
    if WAKE_BEFORE_SCAN and feed_ratio < 0.5:
        try:
            _status(0, message="Feed cold — waking decision / market-data…")
            wake_results = await asyncio.wait_for(_wake_required_services(client), timeout=12.0)
            logger.info("Pre-scan wake: %s", {k: v.get("ok") for k, v in wake_results.items()})
            await asyncio.sleep(min(float(WAKE_WAIT_SECONDS or 4), 6.0))
        except Exception as e:
            logger.warning("Pre-scan wake failed/timeout (continuing Neon-first): %s", e)
    else:
        logger.info(
            "Skipping long pre-scan wake (Neon feed coverage %.0f%%) — proceed to batches",
            feed_ratio * 100,
        )

    # Seed per-symbol batch cache from last partial scan so 60/300 style
    # resumes don't re-score already completed names (Neon/redis durable).
    try:
        last = _redis_get(LAST_FULL_SCAN_KEY)
        if last and isinstance(last, dict):
            prev = (last.get("result") or {})
            prev_all = prev.get("all_results") or []
            seeded = 0
            for row in prev_all:
                if not isinstance(row, dict):
                    continue
                sym = (row.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "")
                if not sym or row.get("decision") == "ERROR":
                    continue
                if not _batch_result_cache_get(sym, lite=lite):
                    _batch_result_cache_set(sym, row, lite=lite)
                    seeded += 1
            if seeded:
                logger.info("Seeded batch_result cache with %s symbols from last partial scan", seeded)
    except Exception as e:
        logger.debug("seed batch cache: %s", e)

    # ── Full-universe batch processor (list size preserved) ──
    batch_size = max(4, min(SCAN_BATCH_SIZE, default_batch_size(MAX_PARALLEL_WORKERS, minimum=6)))
    if activity_paused():
        # Should be rare — we clear pause at start; still never leave UI at 0/N forever
        logger.warning("Scan aborted — activity paused (Power Off)")
        _redis_set(SCAN_TASK_PREFIX + task_id, {
            "status": "cancelled",
            "total": total,
            "processed": 0,
            "elapsed": round(time.time() - start_time, 1),
            "result": None,
            "error": "activity_paused",
            "message": "Scan aborted — system was Powered Off. Click Power Off→Resume or Run Scan again.",
            "cancelled": True,
            "partial": True,
        }, ttl=3600)
        _SCAN_IN_PROGRESS = False
        return
    logger.info(
        "Scan full universe=%s batch_size=%s workers=%s (Neon data-feed preferred)",
        total, batch_size, MAX_PARALLEL_WORKERS,
    )
    _status(0, message=f"Processing {total} symbols — Neon feed first, upstream for live fields only")

    async def _worker(sym: str):
        base = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
        fed = prefetched_feeds.get(base) if isinstance(prefetched_feeds, dict) else None
        return await _analyze_one_symbol_ultra(
            sym,
            client,
            sem,
            lite=lite,
            feed_row=fed,
            prefetched_feeds=prefetched_feeds,
            # Full-universe Run Market Scan — Gemini reserved for single-stock
            # Analyse only, see _analyze_one_symbol_ultra docstring.
            skip_gemini=True,
        )


    def _should_cancel() -> bool:
        if task_id in _SCAN_CANCEL_FLAGS or "__ALL__" in _SCAN_CANCEL_FLAGS or activity_paused():
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

    # Guarantee Top-5 boards are never empty ("No picks in this horizon").
    # Fall back to overall ranked results sliced into non-overlapping bands.
    sorted_all = sorted(
        [r for r in results if r.get("decision") != "ERROR"],
        key=lambda x: x.get("combined_score", 0) or 0,
        reverse=True,
    )
    if not top_picks_short:
        top_picks_short = top_picks or sorted_all[:5]
    if not top_picks_mid:
        top_picks_mid = sorted_all[5:10] if len(sorted_all) >= 10 else sorted_all[:min(5, len(sorted_all))]
    if not top_picks_long:
        top_picks_long = sorted_all[10:15] if len(sorted_all) >= 15 else sorted_all[:min(5, len(sorted_all))]

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
    _SCAN_IN_PROGRESS = False
    logger.info("Scan %s finished processed=%s/%s", task_id, processed, total)
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
            yf_ticker = resolve_ns_ticker(sym)
            if not yf_ticker:
                continue
            ticker = yf.Ticker(yf_ticker)
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
            "/scan/stream": "GET – NDJSON stream of scan results (incremental UI, avoids 100s timeout)",
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
            "/api/surprise/scan": "GET – lightweight surprise momentum scan",
            "/api/surprise/scan/stream": "GET – NDJSON stream of surprise hits",
            "/api/surprise/static": "GET – surprise_static_feed baselines",
            "/surprise/premarket": "POST/GET – premarket baselines (injects scan universe)",

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
        yf_ticker = resolve_ns_ticker(sym)
        if not yf_ticker:
            raise ValueError(f"{sym} not resolvable on NSE")
        t = yf.Ticker(yf_ticker)
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
@app.get("/api/rate-limits")
@app.get("/api/ops/rate-limits")
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



def _neon_keepalive_ping() -> dict:
    """Lightweight SELECT 1 against Neon to keep free-tier compute warm."""
    out = {"ok": False, "neon_connected": False, "error": None}
    try:
        if _kv_cache is None:
            out["error"] = "kv_cache not loaded"
            return out
        if hasattr(_kv_cache, "status"):
            st = _kv_cache.status() or {}
            out["neon_connected"] = bool(st.get("neon_connected"))
            out["ok"] = bool(st.get("neon_connected"))
            if st.get("neon_error"):
                out["error"] = st.get("neon_error")
            return out
        _kv_cache.get("__neon_keepalive__")
        out["ok"] = True
        out["neon_connected"] = True
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


@app.get("/ops/neon-keepalive")
@app.post("/ops/neon-keepalive")
async def ops_neon_keepalive():
    """
    Cron-friendly Neon keep-alive (every ~4 minutes recommended).

    Prevents Neon free-tier compute auto-suspend so the next user scan
    does not pay 0.5–2.5s cold-start latency.

    Example (GitHub Actions / external cron):
      curl -X POST https://<api-gateway>/ops/neon-keepalive
    """
    result = _neon_keepalive_ping()
    return {
        **result,
        "at": datetime.now(IST).isoformat(),
        "hint": "Schedule every 4 minutes while you need warm Neon",
    }


@app.get("/ops/wake-db-all")
@app.post("/ops/wake-db-all")
async def ops_wake_db_all():
    """
    One button/one call to wake every Neon-backed database this app talks to:
      - gateway's own Neon connection (kv_cache / feed store)
      - training service's DB (TRAINING_DATABASE_URL / DATABASE_URL — via its
        own /health, which pings the DB on the way through)
      - training service's separate cache DB, if CACHE_DATABASE_URL is set
        (kv_cache.py on the training side falls back through
        TRAINING_DATABASE_URL -> DATABASE_URL -> CACHE_DATABASE_URL -> KV_DATABASE_URL,
        so hitting /training/health also warms whichever one is actually wired up)

    Called automatically on frontend page load, and available as a manual
    "Wake DB" button in the Dashboard header and Settings tab so a cold Neon
    compute doesn't make the first click of the day wait 1-3s per query.
    """
    out: dict = {"ok": True, "at": datetime.now(IST).isoformat(), "targets": {}}

    # 1) Gateway's own Neon connection
    try:
        gw = _neon_keepalive_ping()
        out["targets"]["gateway_neon"] = gw
    except Exception as e:
        out["targets"]["gateway_neon"] = {"ok": False, "error": str(e)[:200]}

    # 2) Training service DB (+ its cache DB, whichever env var it resolves to)
    #    /training/health itself performs a DB check on the training side.
    try:
        client = _get_http_client()
        url = (TRAINING_URL or "").rstrip("/")
        if url:
            r = await client.get(f"{url}/health", params={"warm": "true"}, timeout=15.0)
            training_ok = r.status_code == 200
            data = {}
            try:
                data = r.json() if training_ok else {}
            except Exception:
                data = {}
            out["targets"]["training_db"] = {
                "ok": training_ok,
                "db_connected": data.get("db_connected"),
                "db_backend": data.get("db_backend"),
            }
        else:
            out["targets"]["training_db"] = {"ok": False, "error": "TRAINING_URL not configured"}
    except Exception as e:
        out["targets"]["training_db"] = {"ok": False, "error": str(e)[:200]}

    out["ok"] = all(bool(t.get("ok")) for t in out["targets"].values())
    return out


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
        nk = _neon_keepalive_ping()
        did.append("neon_keepalive_ok" if nk.get("ok") else "neon_keepalive_error")
    except Exception as e:
        logger.debug("idle-tick neon keepalive: %s", e)
        did.append("neon_keepalive_error")
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
                body = {}
                try:
                    body = resp.json() if resp.content else {}
                except Exception:
                    body = {}
                failed = body.get("failed") or []
                mounts = body.get("mounts") or {}
                # Sub-services that expose MOUNT_STATUS: degraded if any mount failed
                mount_ok = not failed
                ok = mount_ok
                status = "up" if ok else "degraded"
                entry = {
                    "ok": ok,
                    "required": required,
                    "status": status,
                    "url": url,
                }
                if failed:
                    entry["failed_mounts"] = failed
                if mounts:
                    entry["mounts"] = mounts
                if body.get("status"):
                    entry["upstream_status"] = body.get("status")
                return name, entry
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
async def get_stock_decision(symbol: str, already_owned: bool = False):
    """
    Single-stock Analyse.

    1) One live decide/{symbol}?force=true (decision service already parallel-gathers
       technical/fundamental/news/events/prediction).
    2) Optional enrichment only when decide clearly lacked a pillar — all conditional
       re-fetches run concurrently via asyncio.gather (no serial waterfall).
    """
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

    def _reason_blob(reasons: dict, key: str) -> str:
        return " ".join(str(x) for x in (reasons.get(key) or [])).lower()

    def _pillar_failed(blob: str) -> bool:
        """True only when decide explicitly reported failure — not merely a neutral score."""
        needles = (
            "temporarily unavailable",
            "error processing",
            "recovering",
            "unavailable",
            "timed out",
            "timeout",
            "failed to",
            "not available",
            "data insufficient",
            "could not",
        )
        return any(n in blob for n in needles)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
            # ── 1) Live decide (already fans out in parallel downstream) ──
            resp = await client.get(
                f"{DECISION_URL}/decide/{symbol_to_use}",
                params={"already_owned": str(already_owned).lower(), "force": "true"},
            )
            resp.raise_for_status()
            raw = resp.json()
            result = _normalize_decision_response(raw, symbol_to_use)

            reasons = result.get("reasons") if isinstance(result.get("reasons"), dict) else {}
            if not isinstance(reasons, dict):
                reasons = {}
                result["reasons"] = reasons

            # Price fill if decide returned no close
            if result.get("close") is None:
                try:
                    price = await asyncio.to_thread(_fetch_price_from_quote, symbol_to_use)
                    if price is not None:
                        result["close"] = price
                except Exception:
                    pass
            if result.get("close") is not None:
                result["data_insufficient"] = False
                try:
                    c = float(result["close"])
                    if result.get("support") is None:
                        result["support"] = round(c * 0.97, 2)
                    if result.get("resistance") is None:
                        result["resistance"] = round(c * 1.03, 2)
                except (TypeError, ValueError):
                    pass

            tech_blob = _reason_blob(reasons, "technical")
            fund_blob = _reason_blob(reasons, "fundamental")
            news_blob = _reason_blob(reasons, "news")

            # Tighten: neutral score 50 alone is NOT a re-fetch trigger
            need_tech = _pillar_failed(tech_blob) or (
                result.get("technical_score") is None and result.get("data_insufficient")
            )
            need_fund = _pillar_failed(fund_blob) or (
                result.get("fundamental_score") is None
                and not result.get("fundamental_metrics")
            )
            need_news = result.get("news_score") is None and (
                _pillar_failed(news_blob) or not (reasons.get("news") or [])
            )
            need_events = (
                not result.get("event_data")
                and not reasons.get("event")
                and not result.get("event_risk")  # None or False both mean "no event risk known yet"
            )
            need_pred = result.get("prediction_score") is None and result.get("prediction_note") is None

            async def _get(url: str, timeout: float = 45.0):
                try:
                    r = await client.get(url, timeout=timeout)
                    if r.status_code == 200 and r.content:
                        data = r.json()
                        return data if isinstance(data, dict) else None
                except Exception as e:
                    logger.debug("enrich %s: %s", url, e)
                return None

            tasks = {}
            if need_tech:
                tasks["tech"] = asyncio.create_task(
                    _get(f"{TECHNICAL_URL}/analyze/{symbol_to_use}?force=true", 45.0)
                )
            if need_fund:
                tasks["fund"] = asyncio.create_task(
                    _get(f"{FUNDAMENTAL_URL}/analyze/{symbol_to_use}?force=true", 60.0)
                )
            if need_news:
                tasks["news"] = asyncio.create_task(
                    _get(f"{NEWS_URL}/analyze/{symbol_to_use}", 30.0)
                )
            if need_events:
                tasks["events"] = asyncio.create_task(
                    _get(f"{EVENT_URL}/events/{symbol_to_use}?force=true", 45.0)
                )
            if need_pred:
                tasks["pred"] = asyncio.create_task(
                    _get(f"{PREDICTION_URL}/predict/{symbol_to_use}", 30.0)
                )

            fetched = {}
            if tasks:
                keys = list(tasks.keys())
                vals = await asyncio.gather(*(tasks[k] for k in keys), return_exceptions=True)
                for k, v in zip(keys, vals):
                    if isinstance(v, Exception):
                        logger.debug("enrich task %s failed: %s", k, v)
                        fetched[k] = None
                    else:
                        fetched[k] = v

            # Apply enrichment
            td = fetched.get("tech")
            if isinstance(td, dict):
                if td.get("technical_score") is not None:
                    result["technical_score"] = td["technical_score"]
                for k in ("support", "resistance", "trend_strength", "volume_surge", "rsi", "close"):
                    if td.get(k) is not None and result.get(k) is None:
                        result[k] = td[k]
                if td.get("reasons"):
                    reasons["technical"] = td["reasons"] if isinstance(td["reasons"], list) else [str(td["reasons"])]

            fd = fetched.get("fund")
            if isinstance(fd, dict):
                if fd.get("fundamental_score") is not None:
                    result["fundamental_score"] = fd["fundamental_score"]
                metrics = fd.get("metrics")
                if metrics:
                    result["fundamental_metrics"] = metrics
                result["fundamental_fallback"] = bool(fd.get("fallback_used"))
                if fd.get("reasons"):
                    reasons["fundamental"] = fd["reasons"] if isinstance(fd["reasons"], list) else [str(fd["reasons"])]
                # Write real fundamentals back to Neon data-feed (merge, never wipe)
                try:
                    from data_feed import save_stock_feed, extract_feed_payload
                    row = extract_feed_payload(symbol_to_use, fundamental=fd, extra={
                        "fundamental_score": fd.get("fundamental_score"),
                        "source": "stock_enrich_fundamental",
                    })
                    if row:
                        save_stock_feed(symbol_to_use, row)
                except Exception as _fe:
                    logger.debug("fund writeback %s: %s", symbol_to_use, _fe)

            nd = fetched.get("news")
            if isinstance(nd, dict) and nd.get("news_score") is not None:
                result["news_score"] = nd.get("news_score")
                if nd.get("reasons"):
                    reasons["news"] = nd["reasons"] if isinstance(nd["reasons"], list) else [str(nd["reasons"])]

            ed = fetched.get("events")
            if isinstance(ed, dict):
                result["event_data"] = ed
                if ed.get("next_earnings_date"):
                    result["event_risk"] = True
                    reasons.setdefault("event", [])
                    if isinstance(reasons["event"], list):
                        reasons["event"] = list(reasons["event"]) + [f"Earnings due: {ed['next_earnings_date']}"]

            pd = fetched.get("pred")
            if isinstance(pd, dict) and pd.get("model_loaded"):
                if pd.get("prediction_score") is not None:
                    result["prediction_score"] = pd.get("prediction_score")
                if pd.get("note"):
                    result["prediction_note"] = pd.get("note")

            result["reasons"] = reasons
            result["enrichment"] = {
                "need_tech": need_tech,
                "need_fund": need_fund,
                "need_news": need_news,
                "need_events": need_events,
                "need_pred": need_pred,
                "fetched": [k for k, v in fetched.items() if v],
            }

        if corrected_from:
            result["corrected_from"] = corrected_from
            result["symbol"] = symbol_to_use

        # Single-stock Analyse is the ONLY place Gemini is allowed to run (per
        # product decision: Gemini is too slow/rate-limited to fire once per
        # symbol across a 300-500 name Run Market Scan, but firing it once for
        # a single stock the user is actively looking at is fine). Falls back
        # to the Hinglish template automatically if GEMINI_API_KEY is unset or
        # the call fails/times out — see _generate_ai_summary.
        try:
            result["natural_language_summary"] = await _generate_ai_summary(result, client)
        except Exception:
            result["natural_language_summary"] = _generate_summary(result)

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
            suggestion_text = (
                f"Symbol '{symbol_to_use}' not found. Did you mean: {', '.join(suggestions)}?"
                if suggestions
                else f"Symbol '{symbol_to_use}' not found."
            )
            raise HTTPException(status_code=404, detail=suggestion_text)
        else:
            # Upstream (decision-prediction-service) returned a real error
            # response (commonly 500 when it OOMs / crashes on Render's free
            # tier). Previously this re-raised the same status code, which
            # the platform edge often surfaces to the user as a raw
            # "502 Bad Gateway" with no explanation. Degrade gracefully
            # instead — same pattern as /api/scan/find-buys: a decision
            # result the UI can render, flagged as low-confidence, beats an
            # error page.
            logger.warning(
                "decision engine HTTP %s for %s: %s",
                e.response.status_code, symbol_to_use, str(e.response.text)[:200],
            )
            return {
                "ok": True,
                "symbol": symbol_to_use,
                "decision": "HOLD",
                "data_insufficient": True,
                "error": f"decision engine returned HTTP {e.response.status_code}",
                "natural_language_summary": (
                    f"{symbol_to_use}: decision engine is temporarily unavailable "
                    f"(HTTP {e.response.status_code}) — showing a neutral HOLD until it recovers."
                ),
                "data_quality": {
                    "level": "low",
                    "flags": ["Decision engine error"],
                    "note": "Upstream decision-prediction-service returned an error — scores unavailable.",
                },
            }
    except httpx.HTTPError as e:
        # Connection refused / timeout / DNS failure — service is down or
        # asleep (Render free-tier cold start), not a client error. Same
        # graceful-degrade as above instead of bubbling a raw 502.
        logger.warning("decision engine unreachable for %s: %s", symbol_to_use, e)
        return {
            "ok": True,
            "symbol": symbol_to_use,
            "decision": "HOLD",
            "data_insufficient": True,
            "error": f"decision engine unreachable: {e}",
            "natural_language_summary": (
                f"{symbol_to_use}: decision engine is unreachable right now "
                f"(cold start or outage) — showing a neutral HOLD until it recovers."
            ),
            "data_quality": {
                "level": "low",
                "flags": ["Decision engine unreachable"],
                "note": "Could not reach decision-prediction-service — scores unavailable.",
            },
        }


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
@app.post("/scan")
async def run_scan_post(
    force_refresh: bool = True,
    lite: bool = False,
    background_tasks: BackgroundTasks = None,
):
    """
    Compatibility alias: overnight cron and external tools used POST /scan and got 405
    because only GET /scan existed. Delegate to the async parallel scan starter.
    """
    # FastAPI injects BackgroundTasks when annotated; guard None for safety
    if background_tasks is None:
        background_tasks = BackgroundTasks()
    return start_scan(force_refresh=force_refresh, lite=lite, background_tasks=background_tasks)


@app.get("/scan")
async def run_scan(force_refresh: bool = False, lite: bool = False):
    """
    Legacy synchronous scan entrypoint.

    Previously: sequential httpx loop over the full universe hitting decide/{symbol}
    live with no Neon prefetch (slow + rate-limit heavy).

    Now: thin wrapper around the same parallel Neon-prefetch pipeline used by
    POST /scan/start and GET /api/scan/stream. Starts run_scan_parallel, waits for
    completion, returns the final result payload (or task status if still running
    past the wait budget).
    """
    if force_refresh and _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
            _redis.delete(LAST_FULL_SCAN_KEY)
        except Exception:
            pass

    # Serve recent complete scan from cache when not forcing
    if not force_refresh:
        cached = _redis_get(LAST_FULL_SCAN_KEY)
        if cached and isinstance(cached, dict) and cached.get("result"):
            res = cached.get("result") or {}
            processed = int(cached.get("processed") or res.get("scanned") or 0)
            total = int(cached.get("total") or res.get("universe_size") or processed or 0)
            is_partial = bool(
                cached.get("partial")
                or cached.get("cancelled")
                or res.get("partial")
                or (total > 0 and processed < int(total * 0.9))
            )
            if not is_partial and total > 0 and processed >= int(total * 0.9):
                return res

    universe = _build_scan_universe()
    if not universe:
        return {
            "scanned_at": datetime.now(IST).isoformat(),
            "scanned": 0,
            "universe_size": 0,
            "recommendations": [],
            "all_results": [],
            "errors": ["empty_universe"],
            "verdict": "NO_DATA",
        }

    use_lite = bool(lite) if lite else (SCAN_LITE_DEFAULT or _should_force_lite_scan())
    task_id = str(uuid.uuid4())
    # Run the real parallel pipeline in-process (Neon bulk + semaphore workers)
    try:
        await run_scan_parallel(task_id, universe, use_lite)
    except Exception as e:
        logger.exception("legacy /scan parallel wrapper failed: %s", e)
        data = _redis_get(SCAN_TASK_PREFIX + task_id) or {}
        if data.get("result"):
            return data["result"]
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)[:200]}")

    data = _redis_get(SCAN_TASK_PREFIX + task_id) or {}
    result = data.get("result")
    if isinstance(result, dict) and result:
        return result

    # Fallback shape if parallel task stored status without nested result
    return {
        "scanned_at": datetime.now(IST).isoformat(),
        "scanned": data.get("processed") or 0,
        "universe_size": data.get("total") or len(universe),
        "recommendations": (data.get("result") or {}).get("recommendations", []) if isinstance(data.get("result"), dict) else [],
        "all_results": (data.get("result") or {}).get("all_results", []) if isinstance(data.get("result"), dict) else [],
        "errors": [data.get("error")] if data.get("error") else [],
        "verdict": (data.get("result") or {}).get("verdict", "UNKNOWN") if isinstance(data.get("result"), dict) else "UNKNOWN",
        "task_id": task_id,
        "status": data.get("status"),
        "parallel": True,
        "deprecated_note": "GET /scan now uses parallel Neon-prefetch pipeline (same as /scan/start)",
    }



@app.post("/scan/batch")
async def scan_batch(request: Request):
    """
    Analyse a batch of symbols (max 15) and return results quickly.
    Already uses Neon bulk prefetch + asyncio.Semaphore parallel workers
    (same family as /scan/start and /api/scan/stream). Kept for GHA runners.
    """
    data = await request.json()
    raw_symbols = data.get("symbols", [])
    if len(raw_symbols) > 15:
        raise HTTPException(status_code=400, detail="Maximum 15 symbols per batch")

    # Normalize once so feed keys and analyze() symbol always match (RELIANCE.NS → RELIANCE)
    symbols = [
        str(s).upper().replace(".NS", "").replace(".BO", "").strip()
        for s in raw_symbols
        if str(s).strip()
    ]

    # Use a semaphore to limit concurrent downstream calls inside this batch
    # Aligned with MAX_PARALLEL_WORKERS=8 so analysis service is never overloaded
    sem = asyncio.Semaphore(8)
    client = _get_http_client()  # shared keepalive pool
    if True:
        # One Neon bulk for this batch
        try:
            batch_feeds = _feed_store().get_symbols_bulk(symbols) or {}
        except Exception:
            batch_feeds = {}
        tasks = [
            _analyze_one_symbol_ultra(
                sym,
                client,
                sem,
                feed_row=batch_feeds.get(sym),
                prefetched_feeds=batch_feeds,
                # Multi-symbol batch — Gemini reserved for single-stock Analyse.
                skip_gemini=True,
            )
            for sym in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for sym, result in zip(symbols, results):
            if isinstance(result, Exception):
                final_results.append({"symbol": sym, "decision": "ERROR", "error": str(result)})
            else:
                if isinstance(result, dict):
                    result["symbol"] = sym  # enforce normalized symbol
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

        # Serve recent COMPLETE scan from cache unless force_refresh.
        # Partial scans (e.g. 60/300 after Stop) must CONTINUE: cache-hit the done
        # symbols via batch_result/Neon, then process the remaining universe upstream.
        if not force_refresh:
            cached = _redis_get(LAST_FULL_SCAN_KEY)
            if cached and isinstance(cached, dict) and cached.get("result"):
                res = cached.get("result") or {}
                processed = int(
                    cached.get("processed")
                    or res.get("scanned")
                    or 0
                )
                total = int(
                    cached.get("total")
                    or res.get("universe_size")
                    or processed
                    or 0
                )
                is_partial = bool(
                    cached.get("partial")
                    or cached.get("cancelled")
                    or res.get("partial")
                    or res.get("stopped_early")
                    or res.get("cancelled")
                    or (total > 0 and processed < int(total * 0.9))
                )
                if not is_partial and total > 0 and processed >= int(total * 0.9):
                    task_id = cached.get("task_id") or str(uuid.uuid4())
                    _redis_set(SCAN_TASK_PREFIX + task_id, {
                        "status": "done",
                        "total": total,
                        "processed": processed,
                        "elapsed": res.get("elapsed_seconds", 0),
                        "result": res,
                        "error": None,
                        "from_cache": True,
                        "scanned_at": cached.get("scanned_at"),
                    }, ttl=3600)
                    return {
                        "task_id": task_id,
                        "from_cache": True,
                        "scanned_at": cached.get("scanned_at"),
                        "universe_size": total,
                        "message": "Returning recent complete scan (within cache TTL). Use force_refresh=true for a new run.",
                    }
                else:
                    logger.info(
                        "Last scan was partial (%s/%s) — continuing full universe "
                        "(batch_result/Neon cache hits for already scored symbols)",
                        processed, total,
                    )

        if force_refresh and _redis:
            try:
                _redis.delete(SCAN_UNIVERSE_KEY)
            except Exception:
                pass

        universe = _build_scan_universe()
        task_id = str(uuid.uuid4())
        background_tasks.add_task(run_scan_parallel, task_id, universe, use_lite)
        return {
            "task_id": task_id,
            "from_cache": False,
            "lite": use_lite,
            "universe_size": len(universe),
            "message": f"Scanning {len(universe)} symbols (dynamic universe; Neon/batch cache for hits, upstream for rest)",
        }
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



def _lite_evaluate_from_feed(symbol: str, feed: dict, live_price: float = 0.0) -> dict:
    """
    Lite path: ZERO downstream HTTP.
    Delegates to instant_scanner (Neon feed + live tick → full score card).
    """
    tick = {"price": live_price} if live_price and live_price > 0 else {}
    try:
        from instant_scanner import compute_instant_scores
        return compute_instant_scores(symbol, feed if isinstance(feed, dict) else {}, tick)
    except Exception as e:
        logger.warning("instant_scanner failed for %s: %s — minimal fallback", symbol, e)
        base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
        feed = feed if isinstance(feed, dict) else {}
        px = float(live_price or 0) or 0.0
        try:
            from price_resolver import extract_safe_price, apply_price_aliases
            px = extract_safe_price(base, tick=tick, feed=feed) or px
            out = {
                "symbol": base,
                "decision": "HOLD",
                "confidence": "Low",
                "combined_score": 50,
                "technical_score": 50,
                "fundamental_score": feed.get("fundamental_score") or 50,
                "close": px,
                "price": px,
                "lite_fastpath": True,
                "status": "READY",
            }
            return apply_price_aliases(out, px) if px > 0 else out
        except Exception:
            return {"symbol": base, "decision": "HOLD", "combined_score": 50, "technical_score": 50, "fundamental_score": 50, "price": px, "lite_fastpath": True}

@app.get("/api/scan/stream")
@app.get("/scan/stream")
async def stream_market_scan(
    lite: bool = None,
    force_refresh: bool = False,
):
    """
    Stream market-scan results as application/x-ndjson.
    Each line is one completed symbol JSON object so the UI can update
    incrementally instead of waiting for the full 300-stock response
    (avoids Render 100s gateway timeout and perceived freezes).

    Flow:
      1. Bulk Neon get_many for all static feeds (1 query)
      2. Bounded concurrent workers (Semaphore)
      3. Yield each result as soon as its batch chunk finishes
    """
    if lite is None:
        use_lite = SCAN_LITE_DEFAULT or _should_force_lite_scan()
    else:
        use_lite = bool(lite)

    universe = _build_scan_universe()
    if force_refresh:
        try:
            _redis_set(SCAN_UNIVERSE_KEY, None, ttl=1)
        except Exception:
            pass
        universe = _build_scan_universe()
    universe = _prioritize_universe(universe)
    total = len(universe)

    async def event_generator():
        client = _get_http_client()
        sem = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
        start = time.time()
        # Never inherit a stale Power-Off / cancel from a previous session
        try:
            _SCAN_CANCEL_FLAGS.discard("__ALL__")
        except Exception:
            pass
        # Critical: clear activity_paused so a prior Power-Off does not abort
        # the stream after the first chunk (was causing 10–25 symbol "full" scans).
        try:
            set_activity_paused(False)
        except Exception:
            pass

        if total <= 0:
            yield json.dumps({
                "_meta": True,
                "event": "error",
                "error": "empty_universe",
                "total": 0,
            }) + "\n"
            yield json.dumps({
                "_meta": True,
                "event": "done",
                "processed": 0,
                "total": 0,
                "elapsed": 0,
            }) + "\n"
            return

        # 1) Single bulk Neon load (canonical stockky:data_feed:sym: + alias feed:)
        prefetched = {}
        try:
            from data_feed import get_all_stock_feeds
            bases = [
                s.upper().replace(".NS", "").replace(".BO", "").strip()
                for s in universe
            ]
            prefetched = get_all_stock_feeds(bases) or {}
            # Also warm process-local store for any later get_symbol
            try:
                store = _feed_store()
                for b, row in prefetched.items():
                    if isinstance(row, dict):
                        store.put_symbol  # attribute check
                # lightweight local warm without re-write to Neon
                from data_feed import DATA_FEED_PREFIX, FEED_ALIAS_PREFIX, _LOCAL_SYMBOLS, _LOCAL_INDEX
                for b, row in prefetched.items():
                    if not isinstance(row, dict):
                        continue
                    _LOCAL_SYMBOLS[DATA_FEED_PREFIX + b] = dict(row)
                    _LOCAL_SYMBOLS[FEED_ALIAS_PREFIX + b] = dict(row)
                    _LOCAL_INDEX.add(b)
            except Exception:
                pass
            yield json.dumps({
                "_meta": True,
                "event": "feed_bulk_loaded",
                "total": total,
                "feed_hits": len(prefetched),
                "lite": use_lite,
                "workers": MAX_PARALLEL_WORKERS,
            }) + "\n"
        except Exception as e:
            logger.warning("scan stream feed bulk: %s", e)
            yield json.dumps({
                "_meta": True,
                "event": "feed_bulk_error",
                "error": str(e)[:200],
                "total": total,
            }) + "\n"

        # 2) Process in chunks so results stream early
        chunk_size = max(10, min(25 if use_lite else SCAN_BATCH_SIZE, 25 if use_lite else 15))
        processed = 0
        for i in range(0, total, chunk_size):
            if "__ALL__" in _SCAN_CANCEL_FLAGS:
                yield json.dumps({
                    "_meta": True,
                    "event": "cancelled",
                    "processed": processed,
                    "total": total,
                }) + "\n"
                break
            # Explicit cancel only — residual activity_paused must not stop the stream.
            chunk = universe[i : i + chunk_size]
            # Keepalive meta so proxies/clients don't idle-timeout mid-universe
            try:
                yield json.dumps({
                    "_meta": True,
                    "event": "heartbeat",
                    "processed": processed,
                    "total": total,
                    "chunk": i // max(chunk_size, 1) + 1,
                    "elapsed": round(time.time() - start, 1),
                }) + "\n"
            except Exception:
                pass
            # Live prices for this chunk in parallel
            try:
                chunk_prices = await _fetch_prices_bulk_async(chunk, client)
            except Exception:
                chunk_prices = {}
            for sym in chunk:
                base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                px = chunk_prices.get(base)
                if px is None:
                    continue
                if not isinstance(prefetched, dict):
                    prefetched = {}
                row = dict(prefetched.get(base) or {})
                row.setdefault("close", px)
                row.setdefault("price", px)
                prefetched[base] = row

            # Sticky Fix Step 3: lite = pure gateway eval (no decision HTTP)
            if use_lite:
                batch = []
                for sym in chunk:
                    base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                    fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                    px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                    try:
                        batch.append(_lite_evaluate_from_feed(sym, fed or {}, px))
                    except Exception as e:
                        batch.append({"symbol": base, "decision": "ERROR", "error": str(e)[:200]})
            else:
                # Full mode: bounded wait per symbol; cold microservices must not stall the stream
                STREAM_SYM_TIMEOUT = float(os.getenv("SCAN_STREAM_SYMBOL_TIMEOUT", "12"))

                async def _ultra_or_instant(sym: str):
                    base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                    fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                    px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                    try:
                        return await asyncio.wait_for(
                            _analyze_one_symbol_ultra(
                                sym,
                                client,
                                sem,
                                lite=False,
                                # Gemini is reserved for single-stock Analyse
                                # (/stock/{symbol}) — a 300-500 symbol Run Market
                                # Scan calling Gemini once per stock would blow
                                # through its RPM limit and slow the whole scan
                                # down for a summary nobody reads mid-scan. Every
                                # other pillar still runs at full depth (lite stays
                                # False) — only the Gemini call is skipped.
                                skip_gemini=True,
                                feed_row=fed,
                                prefetched_feeds=prefetched,
                            ),
                            timeout=STREAM_SYM_TIMEOUT,
                        )
                    except Exception as e:
                        logger.debug("stream ultra fallback %s: %s", base, e)
                        try:
                            from instant_scanner import compute_instant_scores
                            out = compute_instant_scores(
                                base, fed or {}, {"price": px} if px > 0 else {}
                            )
                            out["fallback_instant"] = True
                            out["fallback_reason"] = str(e)[:120]
                            return out
                        except Exception as e2:
                            return {
                                "symbol": base,
                                "decision": "ERROR",
                                "error": str(e2)[:200],
                            }

                try:
                    batch = await asyncio.gather(
                        *[_ultra_or_instant(sym) for sym in chunk],
                        return_exceptions=True,
                    )
                except Exception as e:
                    logger.exception("scan stream chunk failed: %s", e)
                    for sym in chunk:
                        processed += 1
                        yield json.dumps({
                            "symbol": sym,
                            "decision": "ERROR",
                            "error": str(e)[:200],
                            "_progress": {
                                "processed": processed,
                                "total": total,
                                "elapsed": round(time.time() - start, 1),
                            },
                        }, default=str) + "\n"
                    continue
            for sym, res in zip(chunk, batch):
                processed += 1
                if isinstance(res, Exception):
                    try:
                        base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                        fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                        px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                        from instant_scanner import compute_instant_scores
                        out = compute_instant_scores(base, fed or {}, {"price": px} if px > 0 else {})
                        out["fallback_instant"] = True
                        out["fallback_reason"] = str(res)[:120]
                    except Exception:
                        out = {
                            "symbol": sym,
                            "decision": "ERROR",
                            "error": str(res)[:200],
                        }
                elif isinstance(res, dict):
                    out = res
                else:
                    out = {"symbol": sym, "decision": "ERROR", "error": "invalid"}
                # Sticky Fix Step 1: unified price resolution + all frontend aliases
                if isinstance(out, dict):
                    try:
                        from price_resolver import ensure_row_price, extract_safe_price, apply_price_aliases
                        base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                        fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else None
                        tick = {"price": None}
                        # Prefer bulk-injected feed close already in prefetched
                        out = ensure_row_price(out, feed=fed if isinstance(fed, dict) else None)
                        if extract_safe_price(decision=out) <= 0:
                            try:
                                px = _fetch_price_from_quote(sym)
                                if px is not None:
                                    out = apply_price_aliases(out, float(px))
                            except Exception:
                                pass
                    except Exception as _pe:
                        logger.debug("price_resolver stream: %s", _pe)
                out["_progress"] = {
                    "processed": processed,
                    "total": total,
                    "elapsed": round(time.time() - start, 1),
                }
                yield json.dumps(out, default=str) + "\n"

        yield json.dumps({
            "_meta": True,
            "event": "done",
            "processed": processed,
            "total": total,
            "elapsed": round(time.time() - start, 1),
        }) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )




@app.post("/api/scan/find-buys")
@app.post("/scan/find-buys")
async def find_actionable_buys(request: Request):
    """
    Buy Sniper — return 1–4 high-conviction setups from a scan result list.
    Always HTTP 200: empty suggestions is a valid outcome (not 400).
    Body: { "stocks": [...], "target_count": 4, "min_conviction": 58 }
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"stocks": payload if isinstance(payload, list) else []}
    try:
        from buy_sniper import suggestions_from_scan_payload
        out = suggestions_from_scan_payload(payload)
        if not isinstance(out, dict):
            return {"ok": True, "count": 0, "suggestions": []}
        out.setdefault("ok", True)
        out.setdefault("count", len(out.get("suggestions") or []))
        out.setdefault("suggestions", [])
        return out
    except Exception as e:
        logger.exception("find-buys failed")
        return {
            "ok": False,
            "count": 0,
            "suggestions": [],
            "error": str(e)[:300],
            "message": "Sniper could not evaluate candidates",
        }


@app.post("/scan/cancel/{task_id}")
def cancel_scan(task_id: str):
    """Request cancel — process-local flag + durable key; commit partial; stops ASAP."""
    _SCAN_CANCEL_FLAGS.add(task_id)
    _redis_set(SCAN_TASK_PREFIX + task_id + ":cancel", True, ttl=3600)
    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not isinstance(data, dict):
        data = {}
    try:

        data = dict(data)
        data["cancel_requested"] = True
        data["status"] = "cancelled" if data.get("status") in (None, "running") else data.get("status")
        data["partial"] = True
        data["message"] = data.get("message") or "Stop requested — partial results committed"
        _redis_set(SCAN_TASK_PREFIX + task_id, data, ttl=3600)
    except Exception:
        pass
    return {
        "ok": True,
        "status": "cancel_requested",
        "processed_so_far": data.get("processed", 0),
        "total": data.get("total", 0),
        "message": "Scan stop signalled — worker will commit partial and exit",
    }


@app.post("/scan/stop-all")
def scan_stop_all():
    """Force-stop every running scan task (commit partial)."""
    _SCAN_CANCEL_FLAGS.add("__ALL__")
    n = 0
    try:
        for k in list(_mem_kv.keys()):
            if str(k).startswith(SCAN_TASK_PREFIX) and not str(k).endswith(":cancel"):
                data = _mem_kv.get(k)
                if isinstance(data, dict) and data.get("status") == "running":
                    data = dict(data)
                    data["cancel_requested"] = True
                    data["status"] = "cancelled"
                    data["partial"] = True
                    _mem_kv[k] = data
                    _redis_set(k, data, ttl=3600)
                    _redis_set(str(k) + ":cancel", True, ttl=3600)
                    n += 1
    except Exception:
        pass
    return {"ok": True, "stopped": n, "message": "All scans stop-signalled"}


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
    # Guarantee Top-5 boards never empty (watchlist scan path)
    sorted_all = sorted(
        [r for r in results if r.get("decision") != "ERROR"],
        key=lambda x: x.get("combined_score", 0) or 0,
        reverse=True,
    )
    if not top_picks_short:
        top_picks_short = top_picks or sorted_all[:5]
    if not top_picks_mid:
        top_picks_mid = sorted_all[5:10] if len(sorted_all) >= 10 else sorted_all[:min(5, len(sorted_all))]
    if not top_picks_long:
        top_picks_long = sorted_all[10:15] if len(sorted_all) >= 15 else sorted_all[:min(5, len(sorted_all))]
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
            yf_ticker = resolve_ns_ticker(sym)
            if not yf_ticker:
                continue
            ticker = yf.Ticker(yf_ticker)
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

# ── Universe preview + ≤ ₹5000 pre-filter ─────────────────────────────────
@app.get("/scan/universe")
def get_scan_universe():
    universe = _build_scan_universe()  # already ≤₹5000 filtered
    searched = _load_searched()
    movers = _get_momentum_movers()
    return {
        "total": len(universe),
        "symbols": universe,
        "searched_symbols_included": [s for s in searched if s in universe],
        "momentum_movers": movers,
        "max_price": MAX_UNIVERSE_PRICE,
    }


@app.get("/api/universe")
@app.get("/universe")
async def get_universe():
    """
    Stateless ≤ ₹5000 universe — prefers Neon data-feed (survives container sleep).
    Order:
      1) Neon feed symbols with price unknown or ≤ 5000 (durable, no RAM dependency)
      2) Decision-service training universe (if available)
      3) Local scan-universe builder
    Always re-applies the price gate.
    """
    symbols: List[str] = []

    # 1) Neon / data-feed (anti-amnesia primary source)
    try:
        from data_feed import list_feed_symbols_from_neon_under_max_price
        symbols = list_feed_symbols_from_neon_under_max_price(MAX_UNIVERSE_PRICE) or []
    except Exception as e:
        logger.debug("api/universe neon feed: %s", e)
        symbols = []

    # 2) Training universe fallback
    if not symbols:
        try:
            decision_base = os.getenv("DECISION_URL", DECISION_URL)
            root = decision_base.rstrip("/")
            if root.endswith("/decision"):
                root = root[: -len("/decision")]
            training_url = f"{root}/training/universe"
            async with httpx.AsyncClient() as client:
                res = await client.get(training_url, timeout=10.0)
                if res.status_code == 200:
                    body = res.json()
                    if isinstance(body, list):
                        symbols = [str(s) for s in body]
                    elif isinstance(body, dict):
                        symbols = [str(s) for s in (body.get("symbols") or body.get("universe") or [])]
        except Exception as e:
            logger.debug("api/universe training fetch: %s", e)
            symbols = []

    # 3) Dynamic scan universe last resort
    if not symbols:
        try:
            symbols = _build_scan_universe()
        except Exception:
            symbols = []

    filtered = _filter_symbols_under_max_price(symbols)
    return filtered

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
    """Proxy a real test send to every enabled channel.

    Fix: this used a flat 15s timeout, but the downstream /test dispatch can
    fan out to CallMeBot, which tries a voice call then a text fallback per
    recipient at up to 35s each (see notifications_call_me above, which
    already uses timeout=70 for the same reason) — so a config with
    CallMeBot enabled routinely took well past 15s and came back as a
    misleading "Notification service unreachable" 502 even though nothing
    was actually unreachable. Match the longer timeout and the wake-first +
    distinct-timeout-message pattern already used by call/me and config.
    """
    try:
        httpx.get(f"{NOTIFICATION_URL}/health", timeout=10)
    except Exception:
        pass
    try:
        resp = httpx.post(f"{NOTIFICATION_URL}/test", timeout=75)
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "Notification test timed out (CallMeBot voice+text fallback and/or "
                "free-tier cold start can take a while). Channels may still have "
                "fired — check each app, or try Test again in 20s."
            ),
        )
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

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def format_pick(r, index):
        if not isinstance(r, dict):
            return f"{index}. *(invalid pick)*"
        symbol = r.get("symbol") or "?"
        decision = r.get("decision") or "UNKNOWN"
        score = r.get("combined_score")
        if score is None:
            score = r.get("score") or 0
        close = _num(r.get("close"))
        target = _num(r.get("target"))
        stop = _num(r.get("stop_loss"))
        entry = r.get("entry_range")
        if not isinstance(entry, dict):
            entry = {}
        entry_low = _num(entry.get("low"))
        entry_high = _num(entry.get("high"))
        holding = r.get("holding_period") or r.get("holding_period_estimate") or "N/A"
        lines = [f"{index}. *{symbol}* – {decision} (Score: {score})"]
        if close is not None:
            lines.append(f"   Current: ₹{close:.2f}")
        if entry_low is not None and entry_high is not None:
            lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
        if target is not None:
            upside = ((target - close) / close * 100) if close else 0
            lines.append(f"   Target: ₹{target:.2f} (+{upside:.1f}%)")
        if stop is not None:
            lines.append(f"   Stop: ₹{stop:.2f}")
        if holding and holding != "N/A":
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


# ── Hot Picks durable store bridge (hotpicks_static_feed) ───────────────────
# Lazy + never fatal, same convention as the ipo_scanner imports below: if
# hotpicks_store.py or its DB is unavailable the scan still runs and still
# serves from kv_cache — it just loses the durable 24h table. Keeping these as
# module-level shims means the call sites inside stockky_hot_stocks() stay
# readable and the gateway can never fail to boot over an optional feature.
def hotpicks_stop_requested() -> bool:
    """True when the user has asked the running Hot Picks scan to stop."""
    try:
        from hotpicks_store import hotpicks_stop_requested as _f

        return bool(_f())
    except Exception:
        return False


def hotpicks_db_upsert(payload) -> int:
    """Persist a Hot Picks payload to hotpicks_static_feed; 0 if unavailable."""
    try:
        from hotpicks_store import hotpicks_db_upsert as _f

        return int(_f(payload) or 0)
    except Exception:
        return 0


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
    # Set when the user hits Stop mid-scan: whatever was already scored is kept
    # and persisted, the payload is marked partial, and its cache TTL is cut
    # short so a partial result never masquerades as a full one for hours.
    stopped_early = False
    processed_symbols = 0

    client = _get_http_client()  # shared keepalive pool

    if True:
        for i_sym, sym in enumerate(universe):
            # Stop requested? Break BEFORE doing this symbol's network work, so
            # Stop feels immediate rather than one full symbol behind.
            try:
                if hotpicks_stop_requested():
                    stopped_early = True
                    logger.info(
                        "stockky-hot stop requested at %s/%s — keeping partial results",
                        i_sym, len(universe),
                    )
                    break
            except Exception:
                pass
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
            processed_symbols = i_sym + 1
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

    # ── Price enrichment ────────────────────────────────────────────────
    # news_driven/results_driven/bulk_insider_driven items were built purely
    # from news_data/event_data — no price field was ever attached, so the
    # frontend's resolveDisplayPrice() found nothing and rendered "₹0"
    # (Price metric on every Hot Picks card, incl. the Results/Earnings
    # section). The ≤₹5000 universe's feed store already has a live/last
    # price for every symbol that's actually in-universe, so look it up
    # here — one cheap in-memory/KV read per symbol, no network calls —
    # instead of leaving the card with nothing to show. Symbols that are
    # over the ₹5000 cap (or otherwise purged from the feed store, e.g.
    # PERSISTENT/SOLARINDS surfaced purely because they were in the news)
    # legitimately have no price to show here; the frontend now renders
    # "₹—" for those instead of a misleading "₹0" (see ConvictionCard.tsx).
    try:
        _hot_store = _feed_store()
    except Exception:
        _hot_store = None
    if _hot_store is not None:
        for _bucket in (news_driven, results_driven, bulk_insider_driven):
            for _item in _bucket:
                try:
                    _row = _hot_store.get_symbol(_item.get("symbol") or "")
                    _px = _feed_resolved_price(_row) if _row else 0.0
                    if _px > 0:
                        _item["price"] = _px
                        _item["close"] = _px
                except Exception:
                    pass

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
        "partial": stopped_early,
        "stopped_early": stopped_early,
        "processed_symbols": processed_symbols,
        "quality_note": (
            "Ranked by scan BUY/PREPARE, bulk/insider, results first; weak news-only names dropped."
        ),
    }
    payload["fingerprint"] = _hot_payload_fingerprint(payload)
    ttl = int(payload["cache_ttl_seconds"])
    if stopped_early:
        # A user-stopped scan is incomplete by definition — keep it just long
        # enough to paint the tab, then let the next call do a real scan.
        ttl = min(ttl, int(os.getenv("HOT_PARTIAL_CACHE_TTL", "300")))
        payload["cache_ttl_seconds"] = ttl
        payload["quality_note"] = (
            f"PARTIAL — scan stopped after {processed_symbols}/{len(universe)} symbols. "
            "Rows shown were fully scored; run again for the rest."
        )
    _redis_set(HOT_STOCKS_CACHE_KEY, payload, ttl=ttl)
    # Durable 24h table (Neon on Render, Oracle ADB on the Oracle VM) — this is
    # what lets the tab paint instantly next time and what survives a redeploy
    # or a kv TTL expiry. Best-effort: never let a DB hiccup fail the scan.
    try:
        stored = hotpicks_db_upsert(payload)
        if stored:
            logger.info("stockky-hot persisted %s row(s) to hotpicks_static_feed", stored)
    except Exception as e:
        logger.debug("hotpicks persist skipped: %s", e)
    logger.info(
        "stockky-hot refreshed: news=%s results=%s bulk=%s scan_seed=%s ttl=%ss phase=%s partial=%s",
        len(payload["news_driven"]),
        len(payload["results_driven"]),
        len(payload["bulk_insider_driven"]),
        payload["scan_seed_count"],
        ttl,
        payload["market_phase"],
        stopped_early,
    )
    return payload






# ── Surprise momentum scanner (static Neon baselines + live ticks) ──────────

@app.post("/api/surprise/run-premarket-feed")
@app.post("/surprise/run-premarket-feed")
@app.get("/api/surprise/run-premarket-feed")
async def api_run_premarket_feed(force: bool = False, request: Request = None):
    """
    Market-aware surprise quote feed:
      - OPEN: cache ≤ 2h
      - CLOSED: durable cache (no Yahoo storm)
      - force=true: always refresh sequentially with 0.5s gaps
    """
    symbols = None
    try:
        if request is not None:
            body = await request.body()
            if body:
                import json as _json
                payload = _json.loads(body.decode("utf-8") or "{}")
                if isinstance(payload, dict) and payload.get("symbols"):
                    symbols = payload["symbols"]
    except Exception:
        symbols = None
    if not symbols:
        try:
            symbols = _build_scan_universe()[:200]
        except Exception:
            symbols = None
    try:
        from surprise_scanner import run_market_aware_surprise_feed
        return await run_market_aware_surprise_feed(
            symbols=symbols,
            market_data_url=MARKET_DATA_URL,
            force=force,
        )
    except Exception as e:
        logger.exception("run-premarket-feed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)[:240])



@app.post("/api/surprise/repair-batch")
@app.post("/surprise/repair-batch")
async def api_surprise_repair_batch(limit: int = 15, symbol: str = None):
    """Fill missing surprise quotes via market-data waterfall (0.5s pacing).
    Optional symbol= targets a single ticker (UI Repair button).
    """
    try:
        from surprise_scanner import repair_surprise_batch
        return repair_surprise_batch(
            limit=limit,
            market_data_url=MARKET_DATA_URL,
            symbol=symbol,
        )
    except Exception as e:
        logger.exception("surprise repair-batch: %s", e)
        raise HTTPException(status_code=500, detail=str(e)[:240])


@app.get("/api/surprise/audit")
@app.get("/surprise/audit")
async def api_surprise_audit():
    """Premarket / surprise feed health for the Surprise dashboard.

    Memoised for AUDIT_TTL_SEC — the panel refetches on every tab mount and this
    walks the whole tracked set, which is the bulk of the tab's load time. See
    _audit_cache_get for why a short TTL is safe here."""
    cached = _audit_cache_get("surprise_audit")
    if cached is not None:
        return cached
    try:
        from surprise_scanner import audit_surprise_feed
        return _audit_cache_put("surprise_audit", audit_surprise_feed())
    except Exception as e:
        logger.exception("surprise audit: %s", e)
        return {
            "ok": False,
            "total_tracked": 0,
            "fully_populated": 0,
            "missing_data": 0,
            "health_score": 0,
            "incomplete_stocks": [],
            "message": str(e)[:200],
        }


@app.get("/api/surprise/scan")
@app.get("/surprise/scan")
async def api_surprise_scan(
    force_reload: bool = False,
    symbols: str = None,
):
    """
    Lightweight surprise scan:
      1) bulk-load surprise_static_feed from Neon
      2) concurrent live quotes (bounded)
      3) score filter (>=60, change >1%)
    """
    try:
        from surprise_scanner import surprise_engine
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

    sym_list = None
    if symbols:
        sym_list = [x.strip() for x in symbols.replace(";", ",").split(",") if x.strip()]

    client = _get_http_client()
    result = await surprise_engine.scan(
        client=client,
        market_data_url=MARKET_DATA_URL,
        symbols=sym_list,
        force_reload_static=bool(force_reload),
    )
    return result


@app.get("/api/surprise/scan/stream")
@app.get("/surprise/scan/stream")
async def api_surprise_scan_stream(
    force_reload: bool = False,
    symbols: str = None,
):
    """
    NDJSON stream of surprise hits with progress (like /scan/stream).
    Lines: meta static_loaded → progress batches → scored stocks → done.
    """
    try:
        from surprise_scanner import surprise_engine
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

    sym_list = None
    if symbols:
        sym_list = [x.strip() for x in symbols.replace(";", ",").split(",") if x.strip()]

    async def event_generator():
        t0 = time.time()
        client = _get_http_client()
        n_static = surprise_engine.load_static_cache(force=bool(force_reload))
        yield json.dumps({
            "_meta": True,
            "event": "static_loaded",
            "static_loaded": n_static,
            "total": n_static,
        }) + "\n"
        if n_static == 0:
            yield json.dumps({
                "_meta": True,
                "event": "error",
                "error": "surprise_static_feed empty — run premarket first",
            }) + "\n"
            yield json.dumps({"_meta": True, "event": "done", "hits": 0, "universe": 0, "elapsed": 0}) + "\n"
            return

        if sym_list:
            keys = [
                s.upper().replace(".NS", "").replace(".BO", "").strip()
                for s in sym_list if s
            ]
            keys = [k for k in keys if k in surprise_engine.static_cache]
        else:
            keys = [
                k for k, v in surprise_engine.static_cache.items()
                if v.get("is_liquid", True)
            ] or list(surprise_engine.static_cache.keys())

        total = len(keys)
        hits = 0
        quotes_ok = 0
        chunk = 20
        yield json.dumps({
            "_meta": True,
            "event": "scan_start",
            "total": total,
            "static_loaded": n_static,
        }) + "\n"

        for i in range(0, total, chunk):
            batch = keys[i : i + chunk]
            try:
                ticks = await asyncio.gather(
                    *[surprise_engine._fetch_quote(client, MARKET_DATA_URL, s) for s in batch],
                    return_exceptions=True,
                )
            except Exception as e:
                logger.warning("surprise stream chunk: %s", e)
                ticks = [None] * len(batch)

            for sym, tick in zip(batch, ticks):
                if isinstance(tick, Exception) or not tick:
                    scored = surprise_engine.score_stock(sym, {})
                else:
                    quotes_ok += 1
                    scored = surprise_engine.score_stock(sym, tick)
                if scored:
                    hits += 1
                    scored["_progress"] = {
                        "processed": min(i + chunk, total),
                        "total": total,
                        "hits": hits,
                        "quotes_ok": quotes_ok,
                        "elapsed": round(time.time() - t0, 1),
                    }
                    yield json.dumps(scored, default=str) + "\n"

            processed = min(i + chunk, total)
            elapsed = round(time.time() - t0, 1)
            eta = None
            if processed > 0 and processed < total and elapsed > 0:
                eta = round((total - processed) * (elapsed / processed), 1)
            yield json.dumps({
                "_meta": True,
                "event": "progress",
                "processed": processed,
                "total": total,
                "hits": hits,
                "quotes_ok": quotes_ok,
                "elapsed": elapsed,
                "eta_sec": eta,
                "percent": int(100 * processed / max(total, 1)),
            }) + "\n"

        yield json.dumps({
            "_meta": True,
            "event": "done",
            "hits": hits,
            "universe": total,
            "quotes_ok": quotes_ok,
            "elapsed": round(time.time() - t0, 1),
        }) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



@app.get("/api/surprise/static")
@app.get("/surprise/static")
async def api_surprise_static(limit: int = 50):
    """Proxy / health peek of baselines (also tries local SQL if configured)."""
    try:
        from surprise_scanner import surprise_engine
        n = surprise_engine.load_static_cache()
        rows = list(surprise_engine.static_cache.values())[: max(1, min(limit, 200))]
        # JSON-safe
        out = []
        for r in rows:
            d = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
            out.append(d)
        return {"ok": True, "count": n, "rows": out, "source": "gateway_cache"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}


@app.post("/surprise/notify-top-picks")
async def api_surprise_notify_top_picks(top_n: int = Query(5, ge=1, le=20)):
    """
    Manual "send to Telegram" button for the Surprise Momentum tab.
    Re-uses the exact same surprise_engine.scan() the tab's live scan uses
    (already sorted by score, high to low), takes the top N, and forwards a
    formatted message to notification-scheduler-service's /notify with
    channel="telegram" (falls back to the outbox if Telegram isn't
    configured — see /notify's own behaviour for that).
    """
    try:
        from surprise_scanner import surprise_engine
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

    client = _get_http_client()
    result = await surprise_engine.scan(
        client=client,
        market_data_url=MARKET_DATA_URL,
        symbols=None,
        force_reload_static=False,
    )
    stocks = result.get("stocks") or []
    if not stocks:
        return {
            "ok": False,
            "sent": False,
            "count": 0,
            "message": result.get("error") or "No Surprise Momentum picks available right now.",
        }

    top = stocks[: max(1, min(int(top_n), 20))]
    lines = [f"🎯 *Surprise Momentum — Top {len(top)} Picks*"]
    for i, s in enumerate(top, 1):
        sym = s.get("symbol")
        score = s.get("score")
        tier = str(s.get("tier") or "").upper()
        price = s.get("price")
        chg = s.get("change_pct")
        target = s.get("target_1")
        stop = s.get("trailing_stop")
        trig = s.get("trigger_type") or ""
        try:
            chg_str = f"{float(chg):+.2f}%"
        except (TypeError, ValueError):
            chg_str = "—"
        lines.append(
            f"{i}. {sym} — {tier} (score {score}/100)\n"
            f"   ₹{price} ({chg_str}) · Target ₹{target} · Stop ₹{stop}\n"
            f"   {trig}"
        )
    message = "\n".join(lines)

    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/notify",
            json={
                "title": "Surprise Momentum — Top Picks",
                "message": message,
                "channel": "telegram",
            },
            timeout=15,
        )
        try:
            detail = resp.json()
        except Exception:
            detail = {"status_code": resp.status_code}
        delivered = bool(isinstance(detail, dict) and detail.get("delivered"))
        return {
            "ok": True,
            "sent": delivered,
            "count": len(top),
            "symbols": [s.get("symbol") for s in top],
            "notification_result": detail,
        }
    except Exception as e:
        return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}


@app.post("/ipo/scan")
@app.get("/ipo/scan")
@app.post("/surprise/ipo/scan")
@app.get("/surprise/ipo/scan")
async def api_ipo_scan(
    background: bool = Query(True),
    force: bool = Query(
        False,
        description=(
            "Bypass the ipo_static_feed 24h freshness cache. Defaults to "
            "False: the 'IPO Premarket Refresh' GitHub Action "
            "(.github/workflows/ipo-premarket.yml) runs a full discovery + "
            "scoring pass every trading morning before the session opens, "
            "so a normal 'Scan IPOs' click should read that already-fresh "
            "table (near-instant) rather than re-discovering NSE + "
            "re-pulling yfinance history for the whole universe in front "
            "of the user. The frontend's 'Force Rescan' button passes "
            "force=true explicitly when a real on-demand re-scan is wanted."
        ),
    ),
    background_tasks: BackgroundTasks = None,
):
    """
    Recent IPO scanner — Surprise tab subsection. Scores recently-listed
    (and listing-today) NSE IPOs for a short-term buy/sell decision using
    ipo_scanner.analyze_ipo(). Runs in the background like the premarket
    job; poll /surprise/ipo/status, then GET /surprise/ipo/list.
    """
    from ipo_scanner import run_ipo_scan, get_ipo_scan_progress

    current = get_ipo_scan_progress()
    if current.get("status") == "running":
        return {"accepted": True, "already_running": True, **current}

    if background and background_tasks is not None:
        background_tasks.add_task(run_ipo_scan, force=force)
        return {"accepted": True, "background": True, "force": force, "message": "IPO scan started"}
    result = run_ipo_scan(force=force)
    return {"accepted": True, "background": False, **result}


@app.get("/ipo/status")
@app.get("/surprise/ipo/status")
async def api_ipo_scan_status():
    try:
        from ipo_scanner import get_ipo_scan_progress
        return get_ipo_scan_progress()
    except Exception as e:
        return {"status": "error", "message": str(e)[:160]}


@app.get("/ipo/list")
@app.get("/surprise/ipo/list")
async def api_ipo_list(display_days: Optional[int] = Query(None)):
    """Current analyzed IPO list — listing-today/pre-listing first, then by
    score. Each row includes a ready-to-use `buy_suggestion` (same shape as
    /api/scan/find-buys) for BUY NOW / PREPARE TO BUY rows so the frontend
    can open the existing BuySniperModal directly.

    display_days optionally narrows the (up to ~1 year wide) scanned universe
    down to listings within the last N days for display, without re-scanning
    — defaults to ~30 days (IPO_CHECKER_DEFAULT_DISPLAY_DAYS). Pass a large
    value (e.g. 365) to see everything the scan actually found."""
    try:
        from ipo_scanner import get_ipo_list
        return get_ipo_list(display_days=display_days)
    except Exception as e:
        return {"results": [], "generated_at": None, "error": str(e)[:160]}


@app.get("/ipo/audit")
@app.get("/surprise/ipo/audit")
async def api_ipo_audit():
    """
    IPO Tracker's OWN feed-health audit (reads ipo_static_feed) — NOT the
    general stock-universe feed. This is what the IPO Tracker tab's
    Database health widget should call; it previously called the shared
    /api/feed/audit-missing (general stock feed), which is why the IPO tab
    showed unrelated stock symbols instead of IPO rows/scores.
    """
    try:
        from ipo_scanner import get_ipo_feed_audit
        return get_ipo_feed_audit()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}


@app.post("/ipo/stop")
@app.post("/surprise/ipo/stop")
async def api_ipo_stop():
    """Stop an in-progress IPO scan after the current symbol — IPO Checker
    tab's Stop button. Mirrors /api/data-feed/stop's behaviour: partial
    results already analyzed are kept, not discarded."""
    try:
        from ipo_scanner import request_ipo_stop
        request_ipo_stop()
        return {"ok": True, "message": "Stop requested — will halt after the current symbol."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


class IpoAddRequest(BaseModel):
    company_name: str


@app.post("/ipo/add")
@app.post("/surprise/ipo/add")
async def api_ipo_add(body: IpoAddRequest):
    """Manually register an IPO by NAME ONLY — the reliable path since
    NSE's IPO API blocks cloud-hosted IPs often enough that auto-discovery
    alone can't be the only path. Resolves symbol/issue price/listing date
    automatically (see ipo_scanner.add_manual_ipo_by_name) against NSE's
    calendar and, as a fallback, ipoalerts — the user never has to look up
    or type those themselves. A resolved entry is persisted as a manual
    IPO and included in every subsequent scan/list the same way any other
    manual entry is."""
    from ipo_scanner import add_manual_ipo_by_name
    result = add_manual_ipo_by_name(body.company_name)
    if not result.get("resolved"):
        return {
            "accepted": False,
            "message": result.get("message"),
            "suggestions": result.get("suggestions") or [],
        }
    return {"accepted": True, "entry": result.get("entry")}


@app.post("/ipo/repair-batch")
@app.post("/surprise/ipo/repair-batch")
async def api_ipo_repair_batch(limit: int = Query(15, ge=1, le=30), symbol: Optional[str] = Query(None)):
    """Targeted repair for ipo_static_feed rows missing price/score/decision
    — re-runs analyze_ipo() only for the specific missing symbols (bounded
    by `limit`), not a full-universe re-scan. Mirrors /stockky-hot/repair-batch
    and /api/surprise/repair-batch's naming and shape; backs the IPO Tracker
    health tab's Auto-Repair button."""
    try:
        from ipo_scanner import ipo_repair_batch
    except Exception as e:
        return {"status": "error", "error": f"ipo_scanner unavailable: {str(e)[:160]}"}
    return ipo_repair_batch(limit=limit, symbol=symbol)


@app.post("/ipo/notify-top-picks")
@app.post("/surprise/ipo/notify-top-picks")
async def api_ipo_notify_top_picks(top_n: int = Query(5, ge=1, le=20)):
    """Manual 'send to Telegram' button for the IPO Tracker tab — mirrors
    /surprise/notify-top-picks and /stockky-hot/notify-top-picks exactly
    (same notification-scheduler-service /notify call, channel='telegram'),
    just sourced from the IPO list (best score first, pre-listing/listing-
    day/upcoming stages first — same ordering run_ipo_scan already sorts
    the stored list into) instead of Surprise Momentum's or Hot Picks' own
    scan results."""
    try:
        from ipo_scanner import get_ipo_list
        listing = get_ipo_list(display_days=None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ipo_scanner import failed: {e}")

    results = listing.get("results") or []
    if not results:
        return {"ok": False, "sent": False, "count": 0, "message": "No IPO Tracker results available right now — run Scan IPOs first."}

    top = results[: max(1, min(int(top_n), 20))]
    lines = [f"🆕 *IPO Tracker — Top {len(top)}*"]
    for i, r in enumerate(top, 1):
        sym = r.get("symbol")
        decision = r.get("decision") or "—"
        score = r.get("ipo_score") or r.get("pre_listing_advisory_score")
        stage = str(r.get("stage") or "").replace("_", " ")
        issue_px = r.get("issue_price")
        cur_px = r.get("current_price")
        chg = r.get("current_vs_issue_pct")
        try:
            chg_str = f"{float(chg):+.2f}%" if chg is not None else "—"
        except (TypeError, ValueError):
            chg_str = "—"
        lines.append(
            f"{i}. {sym} — {decision} (score {score if score is not None else '—'}/100, {stage})\n"
            f"   Issue ₹{issue_px} → Current ₹{cur_px if cur_px is not None else '—'} ({chg_str})"
        )
    message = "\n".join(lines)

    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/notify",
            json={"title": "IPO Tracker — Top Picks", "message": message, "channel": "telegram"},
            timeout=15,
        )
        try:
            detail = resp.json()
        except Exception:
            detail = {"status_code": resp.status_code}
        delivered = bool(isinstance(detail, dict) and detail.get("delivered"))
        return {
            "ok": True,
            "sent": delivered,
            "count": len(top),
            "symbols": [r.get("symbol") for r in top],
            "notification_result": detail,
        }
    except Exception as e:
        return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}


@app.get("/surprise/premarket/status")
@app.get("/api/surprise/premarket/status")
async def api_surprise_premarket_status():
    """Premarket progress from gateway-local progress file."""
    try:
        from surprise_premarket import get_premarket_progress
        return get_premarket_progress()
    except Exception as e:
        return {
            "is_running": False,
            "stage": "error",
            "percent": 0,
            "error": str(e)[:160],
            "message": str(e)[:160],
        }


@app.post("/surprise/premarket")
@app.get("/surprise/premarket")
async def api_surprise_premarket_proxy(request: Request):
    """
    Premarket baselines → Neon surprise_static_feed.

    Prefer RUNNING ON THE GATEWAY (has DATABASE_URL / Neon).
    market-data often lacks DATABASE_URL → schema_failed; gateway always ensures schema
    and computes baselines with yfinance, then writes Neon.

    Body optional: {"symbols": [...]} — else injects scan universe.
    Query background=true (default) returns immediately; poll /surprise/premarket/status.
    """
    import threading
    import json as _json

    # Cache "schema already confirmed ready" for this process so a second
    # premarket click (or the frontend's own retry after a timeout) doesn't
    # re-run ensure_surprise_schema() synchronously on the request path.
    # ensure_surprise_schema() opens a fresh DB connection (cold Oracle
    # wallet handshake can take several seconds) BEFORE the background
    # thread is even spawned — on a slow/cold connection that alone can
    # exceed Render's/nginx's default proxy timeout and return 504, even
    # though background=true was requested and would have returned
    # instantly otherwise. Once schema is confirmed ready once, skip the
    # live check on every subsequent call (schema doesn't disappear).
    global _SURPRISE_SCHEMA_CONFIRMED
    try:
        _SURPRISE_SCHEMA_CONFIRMED
    except NameError:
        _SURPRISE_SCHEMA_CONFIRMED = False

    if _SURPRISE_SCHEMA_CONFIRMED:
        schema = {"ok": True, "table": "surprise_static_feed", "cached": True}
    else:
        try:
            from surprise_schema import ensure_surprise_schema
            schema = ensure_surprise_schema()
            if schema.get("ok"):
                _SURPRISE_SCHEMA_CONFIRMED = True
        except Exception as e:
            schema = {"ok": False, "error": str(e)[:200]}
            logger.warning("gateway ensure_surprise_schema: %s", e)

    if not schema.get("ok"):
        _sb = schema.get("backend") or "postgres"
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "schema_failed",
                "message": (
                    "Set ORACLE_DSN + wallet env on api-gateway (Oracle deployment)"
                    if _sb == "oracle"
                    else "Set DATABASE_URL or CACHE_DATABASE_URL on api-gateway to Neon pooler URL"
                ),
                "schema": schema,
            },
        )

    # Parse symbols
    raw = await request.body()
    payload = {}
    if raw:
        try:
            payload = _json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else None
    q = request.query_params.get("symbols")
    if not symbols and q:
        symbols = [x.strip() for x in q.split(",") if x.strip()]

    # 2026-08-25 fix: when no explicit symbol list is given, this used to
    # call _build_scan_universe() RIGHT HERE — synchronously, on the
    # request path, before the background thread below even exists. That
    # function hits the live NSE securities-list API (the same one this
    # session's NSE-403 fix already had to retry/refresh sessions for),
    # so on a slow or currently-blocked NSE response this alone can run
    # past Render's/nginx's proxy timeout and return 504 — even though
    # background=true was requested and the docstring promises an
    # immediate return. Universe resolution now happens INSIDE the
    # background thread (see _job() below) when background=true; it only
    # stays synchronous here for the non-background call, where blocking
    # is already an accepted part of that code path's contract.
    background = str(request.query_params.get("background", "true")).lower() in (
        "1", "true", "yes", ""
    )
    force = str(request.query_params.get("force", "false")).lower() in ("1", "true", "yes")

    from surprise_premarket import (
        precalculate_surprise_baselines,
        get_premarket_progress,
        default_universe_from_env,
    )

    explicit_symbols = bool(symbols)
    if not symbols and not background:
        # Synchronous path only: resolve the universe now, same as before.
        try:
            uni = _build_scan_universe()
            symbols = [
                str(s).upper().replace(".NS", "").replace(".BO", "").strip()
                for s in (uni or [])
                if s
            ]
        except Exception as e:
            logger.warning("universe inject failed: %s", e)
            symbols = []
        if not symbols:
            symbols = default_universe_from_env()

    prog = get_premarket_progress()
    if prog.get("is_running"):
        return {
            "ok": True,
            "accepted": False,
            "already_running": True,
            "message": "Premarket already running — poll /surprise/premarket/status",
            "progress": prog,
            "schema": schema,
        }

    if background:
        def _job(explicit_syms=(list(symbols) if explicit_symbols else None), force=force):
            try:
                syms = explicit_syms
                if not syms:
                    # Universe resolution deferred to here (see fix note
                    # above) — this thread can take as long as it needs;
                    # it no longer holds the HTTP response open.
                    try:
                        uni = _build_scan_universe()
                        syms = [
                            str(s).upper().replace(".NS", "").replace(".BO", "").strip()
                            for s in (uni or [])
                            if s
                        ]
                    except Exception as e:
                        logger.warning("gateway premarket job: universe inject failed: %s", e)
                        syms = []
                    if not syms:
                        syms = default_universe_from_env()
                precalculate_surprise_baselines(syms, force=force)
            except Exception as e:
                logger.exception("gateway premarket job: %s", e)

        threading.Thread(target=_job, daemon=True, name="gw-surprise-premarket").start()
        return {
            "ok": True,
            "accepted": True,
            "background": True,
            "symbols": len(symbols) if explicit_symbols else None,
            "universe_injected": "resolving in background" if not explicit_symbols else len(symbols),
            "runner": "api-gateway",
            "backend": schema.get("backend"),
            "message": (
                "Premarket started on gateway (%s) — poll /surprise/premarket/status"
                % ("Oracle ADB" if schema.get("backend") == "oracle" else "Neon")
            ),
            "progress": get_premarket_progress(),
            "schema": schema,
        }

    result = precalculate_surprise_baselines(symbols, force=force)
    result["runner"] = "api-gateway"
    result["schema"] = schema
    return result


@app.post("/surprise/stop")
@app.post("/api/surprise/stop")
async def api_surprise_stop():
    """Stop button for the Surprise tab — halts the premarket baseline job
    (between symbols) AND the live surprise scan's waterfall-fill loop, since
    both share the same stop flag (surprise_premarket.premarket_stop_requested).
    Whatever was already computed/fetched before Stop is kept, not discarded."""
    try:
        from surprise_premarket import request_premarket_stop
        request_premarket_stop()
        return {"ok": True, "message": "Stop requested — will halt after the current symbol/chunk."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


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
        yf_ticker = resolve_ns_ticker(sym)
        if not yf_ticker:
            raise ValueError(f"{sym} not resolvable on NSE")
        t = yf.Ticker(yf_ticker)
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
    During market hours: slower cadence, only when clients watch symbols.
    Power-off / activity pause: sleep only — zero upstream quote calls.
    Off-hours / weekend / holiday: idle sleep (no upstream quote spam).
    """
    while True:
        try:
            if activity_paused() or scan_in_progress() or not _QUOTE_LOOP_ENABLED:
                await asyncio.sleep(30)
                continue
            # No connected clients → do not hit market-data at all
            if not getattr(ws_manager, "active", None):
                await asyncio.sleep(20)
                continue
            phase = _market_session_phase_ist()
            # Only fetch while session is live (preopen/open/post)
            if phase not in ("preopen", "open", "post"):
                await asyncio.sleep(90)
                continue

            symbols = ws_manager.all_watched_symbols()
            if not symbols:
                await asyncio.sleep(20)
                continue

            # Cap concurrent watched symbols to protect Yahoo/NSE / free-tier
            for sym in list(symbols)[:12]:
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
        # Real-time price alerts (15-min cooldown per rule)
        try:
            from data_feed import evaluate_price_alerts
            triggered = evaluate_price_alerts()
            for t in triggered[:5]:
                try:
                    _wake_notification_service()
                    msg = (
                        f"⚡ {t.get('symbol')} ₹{t.get('current_price')} "
                        f"({t.get('direction')} target ₹{t.get('target_price')})"
                    )
                    httpx.post(
                        f"{NOTIFICATION_URL}/notify",
                        json={"title": f"Price Alert · {t.get('symbol')}", "message": msg, "channel": "all"},
                        timeout=8,
                    )
                    await ws_manager.broadcast("alerts", {
                        "type": "price_alert",
                        **{k: t.get(k) for k in ("symbol", "current_price", "target_price", "direction", "note", "id")},
                    })
                except Exception:
                    pass
        except Exception as e:
            logger.debug("quote loop alerts: %s", e)
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
      {"action":"subscribe","channel":"scan:<id>"|"quote:TCS"|"market"|"jobs"|"all"}
      {"action":"subscribe_quotes","symbols":["TCS","INFY"]}
      {"action":"unsubscribe_quotes","symbols":["TCS"]}  # or omit symbols to clear
      {"action":"unsubscribe","channel":"..."}
      {"action":"ping"}
    Server:
      {"channel":"quote:TCS","type":"quote","price":...,"as_of":...}
      {"channel":"scan:...","type":"scan_status",...}
      {"channel":"jobs","type":"jobs_snapshot","data_feed":{...},"refill_additional":{...},
       "surprise_premarket":{...},"rate_limits":{"yfinance":{...},"analysis":{...}}}
    """
    await ws_manager.connect(websocket)
    _ensure_quote_loop()
    _ensure_jobs_loop()
    try:
        await websocket.send_text(json.dumps({
            "channel": "system",
            "type": "connected",
            "ts": datetime.now(IST).isoformat(),
            "features": ["scan", "quotes", "jobs", "ping"],
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


# ── Real-time job progress hub ────────────────────────────────────────────
# Point 1 fix: Market Scan, the Surprise tab (premarket + bulk quote feed),
# Hot Picks, the Data Feed tab, and every "repair" button all previously
# only reported progress via polling (client re-fetching a /status endpoint
# on a timer). This loop pushes the same progress dicts those endpoints
# already compute over the existing WS hub (channel "jobs"), plus a live
# snapshot of the shared rate-limiter (rate_limiter.stats()) so the UI can
# show *why* something is moving slowly — queued behind a shared upstream
# limit — instead of just looking stuck. Clients subscribe once:
#   {"action": "subscribe", "channel": "jobs"}
# and receive a combined snapshot every ~2s while at least one job is
# running; the loop idles (cheap, no polling of the jobs themselves) when
# nothing is active.
_jobs_ws_task = None


def _collect_job_snapshots() -> dict:
    out: Dict[str, Any] = {}

    try:
        from data_feed import get_data_feed_store
        out["data_feed"] = get_data_feed_store().job()
    except Exception as e:
        logger.debug("jobs snapshot data_feed: %s", e)

    try:
        from refill_additional import get_refill_job
        out["refill_additional"] = get_refill_job()
    except Exception as e:
        logger.debug("jobs snapshot refill_additional: %s", e)

    try:
        from surprise_premarket import get_premarket_progress
        out["surprise_premarket"] = get_premarket_progress()
    except Exception as e:
        logger.debug("jobs snapshot surprise_premarket: %s", e)

    try:
        from ipo_scanner import get_ipo_scan_progress
        out["ipo_scan"] = get_ipo_scan_progress()
    except Exception as e:
        logger.debug("jobs snapshot ipo_scan: %s", e)

    try:
        out["rate_limits"] = _rl.stats()
    except Exception as e:
        logger.debug("jobs snapshot rate_limits: %s", e)

    try:
        r = httpx.get(f"{MARKET_DATA_URL}/internal/yahoo-ws-status", timeout=3)
        if r.status_code == 200:
            out["yahoo_ws_feed"] = r.json()
    except Exception as e:
        logger.debug("jobs snapshot yahoo_ws_feed: %s", e)

    return out


def _job_is_active(snap: dict) -> bool:
    for key in ("data_feed", "refill_additional", "surprise_premarket", "ipo_scan"):
        st = (snap.get(key) or {}).get("status")
        if st in ("running", "computing", "started"):
            return True
    return False


async def _jobs_broadcast_loop():
    idle_interval = 15
    active_interval = 2
    while True:
        try:
            if not getattr(ws_manager, "active", None):
                await asyncio.sleep(idle_interval)
                continue
            snap = await asyncio.to_thread(_collect_job_snapshots)
            await ws_manager.broadcast("jobs", {
                "type": "jobs_snapshot",
                "ts": datetime.now(IST).isoformat(),
                **snap,
            })
            await asyncio.sleep(active_interval if _job_is_active(snap) else idle_interval)
        except Exception as e:
            logger.debug("jobs broadcast loop: %s", e)
            await asyncio.sleep(idle_interval)


def _ensure_jobs_loop():
    global _jobs_ws_task
    try:
        loop = asyncio.get_event_loop()
        if _jobs_ws_task is None or _jobs_ws_task.done():
            _jobs_ws_task = loop.create_task(_jobs_broadcast_loop())
    except Exception as e:
        logger.debug("jobs loop start: %s", e)


# ── Startup cache pre-population ──────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    """Non-blocking indices warm — never delays app readiness."""
    try:
        try:
            _redis.delete(INDICES_CACHE_KEY)
            logger.info("Cleared old indices cache on startup")
        except Exception:
            pass
        # Offload to thread with a hard 8s budget so cold Yahoo cannot freeze boot
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(get_market_indices, True),
                timeout=8.0,
            )
            logger.info("Market indices cache pre-populated successfully")
        except asyncio.TimeoutError:
            logger.warning("Startup warning: indices warm timed out (non-fatal) — will fill on first request")
        except Exception as e:
            logger.warning("Startup warning: indices warm failed (non-fatal): %s", e)
    except Exception as e:
        logger.warning(f"Startup warning (indices, non-fatal): {e}")



@app.post("/ops/circuit-reset")
@app.get("/ops/circuit-reset")
async def ops_circuit_reset():
    """Force-close all circuit breakers after intentional warm-up / cold-start recovery.

    Use before a full market scan when services were sleeping and breakers opened
    on ReadTimeout. Safe to call any time.
    """
    try:
        from circuit_breaker import reset_all_breakers, all_snapshots
        names = reset_all_breakers()
        snaps = all_snapshots()
        return {
            "ok": True,
            "reset": names,
            "count": len(names),
            "snapshots": snaps,
            "message": f"Reset {len(names)} circuit breaker(s). Ready for scan.",
        }
    except Exception as e:
        logger.warning("circuit-reset: %s", e)
        return {"ok": False, "error": str(e)[:200]}


@app.get("/ops/circuit-status")
async def ops_circuit_status():
    """Inspect current circuit breaker states."""
    try:
        from circuit_breaker import all_snapshots
        return {"ok": True, "breakers": all_snapshots()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


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
    """Force-stop ALL user-initiated activity (same path as process shutdown).

    1) Commit / checkpoint in-progress scan + data-feed + hot-picks
    2) Signal cancel everywhere (process-local + durable)
    3) Pause activity gate so quote loop / workers idle
    4) Frontend should reload and only hit health/keepalive afterwards
    """
    phases = _graceful_shutdown_commit(reason="power_off")

    # Best-effort stop training lock on decision-prediction service
    try:
        client = _get_http_client()
        urls = [
            f"{TRAINING_URL.rstrip('/')}/api/train/stop",
            f"{TRAINING_URL.rstrip('/')}/training/stop",
            f"{DECISION_URL.rstrip('/')}/training/clear-lock",
        ]
        stopped_tr = False
        for u in urls:
            try:
                r = await client.post(u, timeout=8)
                if r.status_code < 500:
                    stopped_tr = True
                    break
            except Exception:
                continue
        phases.append({"phase": "training", "ok": True, "detail": "stop signalled" if stopped_tr else "best-effort"})
    except Exception as e:
        phases.append({"phase": "training", "ok": False, "detail": str(e)[:120]})

    phases.append({"phase": "ready", "ok": True, "detail": "All stoppable jobs force-stopped. Only health/keepalive allowed until resume."})
    return {
        "ok": True,
        "message": "Power Off complete — all activity stopped; checkpoints committed",
        "phases": phases,
        "activity_paused": True,
        "hint": "UI should reload. Background quote fan-out and jobs are paused. GitHub workflows remain independent.",
    }


@app.post("/ops/resume-activity")
def ops_resume_activity():
    """Clear Power-Off pause so scans / feeds / quotes can run again."""
    set_activity_paused(False)
    try:
        _SCAN_CANCEL_FLAGS.discard("__ALL__")
    except Exception:
        pass
    return {"ok": True, "activity_paused": False, "message": "Activity resumed"}


@app.get("/ops/activity")
def ops_activity_status():
    return {
        "ok": True,
        "activity_paused": activity_paused(),
        "quote_loop_enabled": bool(_QUOTE_LOOP_ENABLED),
        "scan_cancel_flags": len(_SCAN_CANCEL_FLAGS),
    }



@app.get("/api/quotes/bulk-cache")
@app.get("/data-feed/bulk-cache")
async def api_bulk_quote_cache():
    """Shared bulk quote cache status + map (market-aware TTL)."""
    from data_feed import get_bulk_quote_cache, bulk_cache_age_sec, should_refresh_bulk_cache
    cache = get_bulk_quote_cache()
    return {
        "ok": True,
        "age_sec": bulk_cache_age_sec(),
        "should_refresh": should_refresh_bulk_cache(),
        "meta": (cache or {}).get("_meta"),
        "count": len((cache or {}).get("quotes") or {}),
        "quotes": (cache or {}).get("quotes") or {},
    }


@app.get("/api/price-alerts")
@app.get("/price-alerts")
async def api_list_price_alerts():
    from data_feed import list_price_alerts
    alerts = list_price_alerts()
    return {"ok": True, "alerts": alerts, "count": len(alerts)}


@app.post("/api/price-alerts")
@app.post("/price-alerts")
async def api_add_price_alert(request: Request):
    """Body: {symbol, target_price, direction: above|below, note?}"""
    from data_feed import add_price_alert
    try:
        body = await request.json()
    except Exception:
        body = {}
    sym = str((body or {}).get("symbol") or "").strip()
    try:
        target = float((body or {}).get("target_price") or (body or {}).get("target") or 0)
    except (TypeError, ValueError):
        target = 0
    direction = str((body or {}).get("direction") or "above").lower()
    note = str((body or {}).get("note") or "")
    if not sym or target <= 0:
        raise HTTPException(status_code=400, detail="symbol and target_price required")
    entry = add_price_alert(sym, target, direction=direction, note=note)
    return {"ok": True, "alert": entry}


@app.delete("/api/price-alerts/{alert_id}")
@app.delete("/price-alerts/{alert_id}")
async def api_delete_price_alert(alert_id: str):
    from data_feed import delete_price_alert
    ok = delete_price_alert(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"ok": True, "deleted": alert_id}


@app.post("/api/price-alerts/evaluate")
@app.post("/price-alerts/evaluate")
async def api_evaluate_price_alerts():
    """Evaluate alerts vs bulk cache / feed; notify on triggers."""
    from data_feed import evaluate_price_alerts
    triggered = evaluate_price_alerts()
    notified = 0
    for t in triggered:
        try:
            _wake_notification_service()
            msg = (
                f"Price alert: {t.get('symbol')} is ₹{t.get('current_price')} "
                f"({t.get('direction')} ₹{t.get('target_price')})"
            )
            if t.get("note"):
                msg += f" — {t.get('note')}"
            resp = httpx.post(
                f"{NOTIFICATION_URL}/notify",
                json={"title": f"Price Alert · {t.get('symbol')}", "message": msg, "channel": "all"},
                timeout=12,
            )
            if resp.status_code == 200:
                notified += 1
        except Exception as e:
            logger.debug("price alert notify: %s", e)
    return {
        "ok": True,
        "triggered": triggered,
        "triggered_count": len(triggered),
        "notified": notified,
    }


@app.post("/data-feed/hard-reset")
@app.post("/api/data-feed/hard-reset")
@app.post("/api/feed/hard-reset")
async def hard_reset_database(preserve_days: int = 7):
    """
    Wipes VOLATILE stockky_kv fields (price/volume) and re-asserts unique
    constraint + index on k. Called by the frontend "Feed Fresh Data" button
    *before* /data-feed/run so a corrupted / over-₹5000 universe is nuked
    on autopilot.

    Durable/slow per-symbol fields (PE ratio, ROCE, sector, technical &
    fundamental scores, model prediction outputs, etc.) updated within the
    last `preserve_days` days are snapshotted and restored automatically —
    only stale (older than preserve_days) or price/volume fields are lost.
    Pass preserve_days=0 to force the old full-wipe behavior.
    """
    try:
        from kv_cache import hard_reset_stockky_kv
        from data_feed import clear_local_data_feed_caches, request_data_feed_stop, clear_data_feed_stop

        # Stop any in-flight feed first
        try:
            request_data_feed_stop()
        except Exception:
            pass

        result = hard_reset_stockky_kv(preserve_days=preserve_days)
        try:
            clear_local_data_feed_caches()
        except Exception:
            pass
        try:
            clear_data_feed_stop()
        except Exception:
            pass

        # Destroy Split-Brain ghosts: scan-universe cache, last-scan, known-symbols
        ghost_keys = [
            SCAN_UNIVERSE_KEY,
            "stockky:last_full_scan",
            "stockky:known_symbols",
            "stockky:data_feed:index",
            "stockky:data_feed:meta",
            "stockky:data_feed:job",
            "stockky:hot_result",
            "stockky:hot_result_db",
            "stockky:hot_job",
        ]
        for gk in ghost_keys:
            try:
                _redis_delete(gk) if "_redis_delete" in dir() else None
            except Exception:
                pass
            try:
                if _kv_cache is not None:
                    _kv_cache.kv_delete(gk)
            except Exception:
                pass
            try:
                if _redis:
                    _redis.delete(gk)
            except Exception:
                pass

        # Reset job/meta so UI shows 0 stocks immediately
        try:
            store = _feed_store()
            store.set_job(
                status="idle",
                message="Hard-reset complete — ready for fresh feed",
                stop_requested=False,
                processed=0,
                total=0,
                ok_count=0,
            )
            store.set_meta(
                last_success_at=None,
                last_count=0,
                last_message="Hard-reset — memory + Neon wiped",
                stock_count=0,
            )
        except Exception as e:
            logger.debug("hard-reset job/meta clear: %s", e)

        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("message", "hard-reset failed"))
        result["message"] = result.get("message") or "Database wiped, locked, and memory cleared. Ready for feed."
        result["ghosts_cleared"] = ghost_keys
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hard_reset_database: %s", e)
        raise HTTPException(status_code=500, detail=str(e)[:240])



@app.post("/data-feed/start-bulk-feed")
@app.post("/api/data-feed/start-bulk-feed")
@app.post("/api/feed/start-bulk-feed")
async def start_bulk_feed(
    force: bool = True,
    use_universe: bool = True,
    background_tasks: BackgroundTasks = None,
):
    """
    Yahoo bulk price feeder — NON-BLOCKING.

    Returns 200 in <100 ms. Heavy yfinance download runs in BackgroundTasks
    so Render/Vercel gateway never hits the 98-second timeout.

    1) Build symbol list from scan universe (or existing feed index)
    2) Mark job=running for UI polling
    3) Schedule background worker that does chunked yf.download + Neon upsert
    """
    from data_feed import run_bulk_yahoo_price_feed, clear_data_feed_stop

    try:
        clear_data_feed_stop()
    except Exception:
        pass

    symbols: list = []
    if use_universe:
        try:
            symbols = _build_scan_universe() or []
        except Exception as e:
            logger.warning("bulk-feed universe: %s", e)
            symbols = []
    if not symbols:
        try:
            symbols = _feed_store().list_symbols() or []
        except Exception:
            symbols = []
    if not symbols:
        try:
            symbols = list(_get_nifty_indices() or [])[:150]
        except Exception:
            symbols = []

    symbols = [
        str(s).upper().replace(".NS", "").replace(".BO", "").strip()
        for s in symbols
        if s
    ]
    # de-dupe preserve order
    seen = set()
    clean = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    symbols = clean

    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols available for bulk feed")

    # Mark job running for UI (instant)
    try:
        store = _feed_store()
        store.set_job(
            status="running",
            message=f"Bulk Yahoo feed started for {len(symbols)} symbols (background)…",
            processed=0,
            total=len(symbols),
            ok_count=0,
            error_count=0,
            stop_requested=False,
            started_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
            ).isoformat(),
        )
    except Exception:
        pass

    def _bulk_worker(syms: list):
        """Runs entirely outside the HTTP request — no gateway timeout risk."""
        try:
            result = run_bulk_yahoo_price_feed(syms, merge_existing=True)
            n = int((result or {}).get("tracked_stocks") or 0)
            store = _feed_store()
            store.set_job(
                status="done",
                message=(result or {}).get("message") or f"Bulk feed done: {n}",
                processed=n,
                total=len(syms),
                ok_count=n,
                error_count=0,
                finished_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                ).isoformat(),
            )
            store.set_meta(
                last_success_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                ).isoformat(),
                last_count=n,
                last_message=(result or {}).get("message"),
                source="yfinance_bulk_bg",
            )
            logger.info("background bulk feed finished: %s", result)
        except Exception as e:
            logger.exception("background bulk feed failed: %s", e)
            try:
                _feed_store().set_job(
                    status="error",
                    message=str(e)[:200],
                    finished_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                    ).isoformat(),
                )
            except Exception:
                pass

    if background_tasks is not None:
        background_tasks.add_task(_bulk_worker, symbols)
    else:
        # Fallback if FastAPI somehow omits BackgroundTasks (should not happen)
        import threading
        threading.Thread(target=_bulk_worker, args=(symbols,), daemon=True).start()

    return {
        "ok": True,
        "status": "started",
        "started": True,
        "total": len(symbols),
        "message": (
            f"Data feed background worker initiated for {len(symbols)} symbols. "
            "Ingesting into Cache DB. Poll /data-feed/status."
        ),
    }


@app.post("/data-feed/refresh-prepare-to-buy")
@app.post("/api/data-feed/refresh-prepare-to-buy")
@app.post("/api/feed/refresh-prepare-to-buy")
async def refresh_prepare_to_buy(
    min_score: float = 58.0,
    max_score: float = 68.0,
):
    """
    Surgical live-quote refresh for high-conviction "Prepare to Buy" setups only.
    Completely bypasses the 300-stock API storm — only candidates in the
    [min_score, max_score) band (default 58–68) are quoted, with a 0.3s gap.
    """
    import asyncio
    from data_feed import (
        find_prepare_to_buy_candidates,
        patch_feed_price,
    )

    try:
        candidates = find_prepare_to_buy_candidates(min_score=min_score, max_score=max_score)
    except Exception as e:
        logger.exception("refresh_prepare_to_buy candidates: %s", e)
        raise HTTPException(status_code=500, detail=str(e)[:200])

    if not candidates:
        return {
            "status": "success",
            "refreshed_count": 0,
            "symbols": [],
            "message": "No Prepare-to-Buy candidates in score band",
            "min_score": min_score,
            "max_score": max_score,
        }

    md_base = (MARKET_DATA_URL or "").rstrip("/")
    updated: list = []
    errors: list = []

    async with httpx.AsyncClient() as client:
        for symbol in candidates:
            try:
                q_res = await client.get(f"{md_base}/quote/{symbol}", timeout=3.0)
                if q_res.status_code == 200:
                    body = q_res.json() if q_res.content else {}
                    live_price = None
                    if isinstance(body, dict):
                        for k in ("cmp", "price", "ltp", "close", "last_price", "regularMarketPrice"):
                            try:
                                v = float(body.get(k) or 0)
                                if v > 0:
                                    live_price = v
                                    break
                            except (TypeError, ValueError):
                                pass
                    if live_price and patch_feed_price(symbol, live_price):
                        updated.append(symbol)
                else:
                    errors.append({"symbol": symbol, "status": q_res.status_code})
            except Exception as e:
                errors.append({"symbol": symbol, "error": str(e)[:120]})
            await asyncio.sleep(0.3)

    return {
        "status": "success",
        "refreshed_count": len(updated),
        "symbols": candidates,
        "updated": updated,
        "errors": errors[:20],
        "min_score": min_score,
        "max_score": max_score,
        "message": f"Refreshed {len(updated)}/{len(candidates)} Prepare-to-Buy quotes",
    }



@app.get("/data-feed/refill-additional/status")
@app.get("/api/data-feed/refill-additional/status")
def data_feed_refill_status():
    """Status of the Refill Additional Data job."""
    try:
        from refill_additional import get_refill_job
        return {"ok": True, **get_refill_job()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "status": "idle"}


@app.post("/data-feed/refill-additional")
@app.post("/api/data-feed/refill-additional")
async def data_feed_refill_additional(
    background_tasks: BackgroundTasks,
    force: bool = True,
):
    """
    Manual / API trigger: Refill Additional Data (fundamentals + technical + events)
    into the data-feed store with force=true upstream calls.

    Returns immediately; work runs in BackgroundTasks. Poll
    GET /data-feed/status or /data-feed/refill-additional/status.
    """
    try:
        from refill_additional import get_refill_job, run_refill_additional, _set_job
        job = get_refill_job()
        if job.get("status") == "running" and not force:
            return {"ok": True, "already_running": True, **job}

        # Resolve universe before returning so UI shows total quickly
        store = _feed_store()
        symbols = []
        try:
            symbols = list(store.list_symbols() or [])
        except Exception:
            symbols = []
        if not symbols:
            try:
                symbols = _build_scan_universe()
            except Exception:
                symbols = []
        symbols = [str(s).upper().replace(".NS", "").replace(".BO", "") for s in (symbols or []) if s]
        _set_job(
            status="running",
            message=f"Starting Refill Additional Data for {len(symbols)} symbols…",
            processed=0,
            total=len(symbols),
            ok_count=0,
            error_count=0,
        )

        def _worker(syms=symbols):
            try:
                run_refill_additional(syms)
            except Exception as e:
                logger.exception("refill_additional worker: %s", e)
                try:
                    _set_job(status="error", message=str(e)[:240])
                except Exception:
                    pass

        background_tasks.add_task(_worker)
        return {
            "ok": True,
            "status": "running",
            "message": f"Refill Additional Data started for {len(symbols)} symbols",
            "total": len(symbols),
        }
    except Exception as e:
        logger.exception("data_feed_refill_additional: %s", e)
        raise HTTPException(status_code=500, detail=str(e)[:240])


@app.get("/data-feed/meta")
@app.get("/api/data-feed/meta")
def data_feed_meta():
    """Last successful feed timestamp, stock count, job status."""
    store = _feed_store()
    meta = store.meta()
    job = store.job()
    return {"ok": True, "meta": meta, "job": job}


@app.get("/data-feed/status")
@app.get("/api/data-feed/status")
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
    # Prefer durable index count so UI survives cold start after a successful feed
    try:
        fed_count = int(store.count_symbols())
    except Exception:
        fed_count = 0
    if fed_count <= 0:
        fed_count = int((meta or {}).get("last_count") or (job or {}).get("ok_count") or 0)
    last_ok = (meta or {}).get("last_success_at") or (job or {}).get("finished_at")
    return {
        "ok": True,
        **job,
        "meta": meta,
        "stocks_in_feed": fed_count,
        "last_success": last_ok,
        "last_success_at": last_ok,
        "last_count": fed_count,
    }




_REQUIRED_FEED_FIELDS = ("price", "rsi", "pe_ratio", "roce", "sentiment_score")

# ── Short-TTL memo for the DB-health audit endpoints ─────────────────────────
# Both /surprise/audit and /data-feed/audit-missing walk every tracked symbol and
# check five fields on each, and both are refetched every single time their tab
# is mounted — which is why those tabs felt slow to open even when nothing had
# changed. A 20s memo is safe because the numbers only move when a scan or feed
# job writes, and those take minutes; the response carries "cached": true so the
# UI can tell a memoised answer from a fresh one, and any Refresh button that
# passes cache=False still forces a real recount.
AUDIT_TTL_SEC = float(os.getenv("AUDIT_TTL_SEC", "20"))
_AUDIT_MEMO: dict = {}
_AUDIT_MEMO_LOCK = __import__("threading").Lock()


def _audit_cache_get(key: str):
    """Return the memoised payload for `key`, or None when stale/absent."""
    if AUDIT_TTL_SEC <= 0:
        return None
    with _AUDIT_MEMO_LOCK:
        hit = _AUDIT_MEMO.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.time() - ts >= AUDIT_TTL_SEC:
        return None
    if isinstance(payload, dict):
        return {**payload, "cached": True, "cache_age_sec": round(time.time() - ts, 1)}
    return payload


def _audit_cache_put(key: str, payload):
    with _AUDIT_MEMO_LOCK:
        _AUDIT_MEMO[key] = (time.time(), payload)
    if isinstance(payload, dict):
        return {**payload, "cached": False}
    return payload


@app.get("/api/feed/audit-missing")
@app.get("/data-feed/audit-missing")
@app.get("/api/data-feed/audit-missing")
async def audit_missing_feed_data(limit: int = 500, cache: bool = True):
    """
    Audits data-feed records to calculate the exact DB Health Score.
    MUST be registered BEFORE /api/feed/{symbol} so 'audit-missing' is not
    captured as a symbol path (silent 404 / null → UI 0% and dashes).

    Memoised for AUDIT_TTL_SEC (pass cache=false to force a recount) — this walks
    every tracked symbol checking five fields each, and the DB Health tab
    refetches it on every mount, which was most of that tab's load time.
    """
    if cache:
        hit = _audit_cache_get(f"feed_audit:{limit}")
        if hit is not None:
            return hit
    store = _feed_store()
    symbols: list = []
    try:
        symbols = list(store.list_symbols() or [])
    except Exception as e:
        logger.debug("audit list_symbols: %s", e)
        symbols = []

    if not symbols:
        try:
            import kv_cache as _kc
            idx = _kc.kv_get("stockky:data_feed:index")
            if isinstance(idx, dict) and isinstance(idx.get("symbols"), list):
                symbols = [str(s).upper().strip() for s in idx["symbols"] if s]
            elif isinstance(idx, list):
                symbols = [str(s).upper().strip() for s in idx if s]
        except Exception as e:
            logger.debug("audit index fallback: %s", e)

    seen = set()
    clean = []
    for s in symbols:
        su = str(s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if su and su not in seen and not su.startswith("SYSTEM:"):
            seen.add(su)
            clean.append(su)
    symbols = clean

    incomplete = []
    over_cap = []
    complete_count = 0
    total = 0
    for sym in symbols:
        total += 1
        try:
            row = store.get_symbol(sym) or {}
        except Exception:
            row = {}
        if not isinstance(row, dict):
            row = {}
        # Stocks whose price is over the ₹MAX_UNIVERSE_PRICE cap should never
        # have been fed — they're not "incomplete" (repair can't legally
        # populate a price under-cap for them), they're stale writes from
        # before the write-path gate existed. Surface them separately so the
        # UI can offer Purge instead of an infinite Repair loop.
        if _row_price_over_cap(row, symbol=sym):
            over_cap.append({
                "symbol": sym,
                "current_price": _feed_resolved_price(row),
            })
            continue
        missing = _feed_missing_fields(row)
        if missing:
            incomplete.append({
                "symbol": sym,
                "current_price": _feed_resolved_price(row),
                "missing_fields": missing,
                "updated_at": str(row.get("updated_at") or row.get("repair_updated_at") or row.get("fed_at") or ""),
            })
        else:
            complete_count += 1

    incomplete.sort(key=lambda x: (-len(x["missing_fields"]), x["symbol"]))
    incomplete_total = len(incomplete)
    if limit and limit > 0:
        incomplete = incomplete[: int(limit)]

    health = round((complete_count / max(total, 1)) * 100, 1) if total > 0 else 0.0

    return _audit_cache_put(f"feed_audit:{limit}", {
        "ok": True,
        "total_universe": total,
        "fully_populated": complete_count,
        "incomplete_count": incomplete_total,
        "over_cap_count": len(over_cap),
        "over_cap_stocks": over_cap[:200],
        "health_score": health,
        "incomplete_stocks": incomplete,
        "required_fields": list(_REQUIRED_FEED_FIELDS),
        "message": (
            "No feed symbols tracked yet — run Data Feed first."
            if total == 0
            else (
                f"Health {health}% · {complete_count}/{total} complete"
                + (f" · {len(over_cap)} over ₹{MAX_UNIVERSE_PRICE:.0f} cap (use Purge)" if over_cap else "")
            )
        ),
    })


@app.post("/api/feed/purge-over-cap")
@app.post("/data-feed/purge-over-cap")
async def purge_over_cap_feed_symbols():
    """
    Delete every feed row whose stored price is above MAX_UNIVERSE_PRICE.
    These are stale writes from before the price-cap was enforced at every
    write path (see _row_price_over_cap) — Repair cannot legitimately "fix"
    them since a real price fetch would also be over cap, so the only
    correct action is to remove them from the feed store.
    """
    store = _feed_store()
    try:
        symbols = list(store.list_symbols() or [])
    except Exception:
        symbols = []
    purged = []
    for sym in symbols:
        try:
            row = store.get_symbol(sym) or {}
        except Exception:
            row = {}
        if isinstance(row, dict) and _row_price_over_cap(row, symbol=sym):
            try:
                store.delete_symbol(sym)
                purged.append(sym)
            except Exception as e:
                logger.warning("purge_over_cap: failed to delete %s: %s", sym, e)
    return {
        "ok": True,
        "purged_count": len(purged),
        "purged_symbols": purged,
        "message": f"Removed {len(purged)} symbol(s) above ₹{MAX_UNIVERSE_PRICE:.0f} from the feed.",
    }


@app.get("/data-feed/{symbol}")
@app.get("/api/data-feed/{symbol}")
@app.get("/api/feed/{symbol}")
def data_feed_symbol(symbol: str):
    fed = _feed_store().get_symbol(symbol)
    if not fed:
        return {"ok": False, "symbol": symbol.upper(), "detail": "No data feed entry"}
    return {"ok": True, "data": fed}


@app.post("/api/feed/update")
@app.post("/data-feed/update")
async def data_feed_update_single(request: Request):
    """Persist one symbol feed payload into Neon (canonical + alias keys)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "JSON object required"}
    symbol = payload.get("symbol")
    if not symbol:
        return {"ok": False, "error": "symbol required"}
    try:
        from data_feed import save_stock_feed, extract_feed_payload
        base = str(symbol).upper().replace(".NS", "").replace(".BO", "").strip()
        # Accept either full payload or nested fields
        body = dict(payload)
        body.pop("symbol", None)
        if body.get("fundamental") or body.get("events"):
            row = extract_feed_payload(
                base,
                fundamental=body.get("fundamental") if isinstance(body.get("fundamental"), dict) else body,
                events=body.get("events") if isinstance(body.get("events"), dict) else None,
                extra={k: v for k, v in body.items() if k not in ("fundamental", "events")},
            )
        else:
            row = dict(body)
            row["symbol"] = base
        # Universal ≤ ₹5000 gate — reject at the write boundary, not just at
        # the bulk-Yahoo seed. Previously this endpoint would happily persist
        # any price, which is how over-cap symbols entered the feed store.
        if _row_price_over_cap(row):
            return {
                "ok": False,
                "status": "REJECTED",
                "symbol": base,
                "error": f"price above ₹{MAX_UNIVERSE_PRICE:.0f} cap — not saved",
            }
        save_stock_feed(base, row)
        return {"ok": True, "status": "SUCCESS", "symbol": base}
    except Exception as e:
        logger.exception("data_feed update failed")
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/feed/batch")
@app.post("/data-feed/batch")
async def data_feed_update_batch(request: Request):
    """Bulk upsert: { "feeds": { "RELIANCE": {...}, ... } } or list of payloads."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    feeds = {}
    if isinstance(payload, dict):
        raw = payload.get("feeds") or payload.get("items") or payload.get("data")
        if isinstance(raw, dict):
            feeds = raw
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("symbol"):
                    feeds[str(item["symbol"])] = item
        else:
            # treat entire dict as symbol->payload if values are dicts
            if payload and all(isinstance(v, dict) for v in payload.values()):
                feeds = payload
    if not feeds:
        return {"ok": False, "error": "No feeds provided", "count": 0}
    try:
        from data_feed import save_stock_feed
        n = 0
        rejected = 0
        for sym, row in feeds.items():
            if not isinstance(row, dict):
                continue
            base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
            body = dict(row)
            body["symbol"] = base
            # Universal ≤ ₹5000 gate (see _row_price_over_cap) — bulk upsert
            # previously bypassed the cap entirely.
            if _row_price_over_cap(body):
                rejected += 1
                continue
            save_stock_feed(base, body)
            n += 1
        return {"ok": True, "status": "SUCCESS", "count": n, "rejected_over_cap": rejected}
    except Exception as e:
        logger.exception("data_feed batch failed")
        return {"ok": False, "error": str(e)[:300], "count": 0}


@app.post("/api/feed/update-batch")
@app.post("/data-feed/update-batch")
async def data_feed_update_batch_refresh(request: Request):
    """
    Process a small symbol list in ONE short HTTP request (GitHub Action driver).

    Body: { "symbols": ["RELIANCE", "TCS", ...] }  — max 15 recommended.
    Fetches fundamental + events for each, writes Neon feed, returns counts.
    Designed so each call finishes in <1–2 min (well under Render's ~100m hard cap).
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list) or not symbols:
        return {"ok": False, "error": "symbols list required", "ok_count": 0, "error_count": 0}
    # Hard cap protects free-tier CPU / upstream rate limits
    max_n = int(os.getenv("DATA_FEED_UPDATE_BATCH_MAX", "15"))
    symbols = [
        str(s).upper().replace(".NS", "").replace(".BO", "").strip()
        for s in symbols
        if s
    ][:max_n]
    if not symbols:
        return {"ok": False, "error": "no valid symbols", "ok_count": 0, "error_count": 0}

    from data_feed import extract_feed_payload
    store = _feed_store()
    ok_n = 0
    err_n = 0
    results = []
    client = _get_http_client()
    for base in symbols:
        try:
            fund = None
            events = None
            try:
                r = await client.get(f"{FUNDAMENTAL_URL}/analyze/{base}", timeout=35)
                if r.status_code == 200:
                    fund = r.json()
                elif r.status_code in (429, 503):
                    try:
                        rate_limit_monitor.record(
                            source="analysis",
                            status=r.status_code,
                            path=f"/fundamental/{base}",
                            detail="update-batch",
                            symbol=base,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("update-batch fund %s: %s", base, e)
            try:
                r = await client.get(f"{EVENT_URL}/events/{base}", timeout=20)
                if r.status_code == 200:
                    events = r.json()
            except Exception as e:
                logger.debug("update-batch events %s: %s", base, e)
            if fund or events:
                row = extract_feed_payload(base, fund, events)
                # Universal ≤ ₹5000 gate (see _row_price_over_cap) — the
                # GitHub Action driver previously wrote every price it fetched.
                if _row_price_over_cap(row):
                    err_n += 1
                    results.append({"symbol": base, "ok": False, "error": "price above cap"})
                    continue
                store.put_symbol(base, row, ttl=DATA_FEED_TTL)
                ok_n += 1
                results.append({"symbol": base, "ok": True})
            else:
                err_n += 1
                results.append({"symbol": base, "ok": False, "error": "no fund/events"})
        except Exception as e:
            err_n += 1
            results.append({"symbol": base, "ok": False, "error": str(e)[:120]})
        await asyncio.sleep(0.15)

    return {
        "ok": True,
        "ok_count": ok_n,
        "error_count": err_n,
        "processed": len(symbols),
        "results": results,
    }


# ── Surgical Data Repair (audit + non-destructive patch) ───────────────────
# _REQUIRED_FEED_FIELDS defined above (before audit-missing route)

REPAIR_COOLDOWN_SEC = float(os.getenv("REPAIR_COOLDOWN_SEC", "0.5"))


def _safe_float(val, default: float = 0.0) -> float:
    """Parse floats safely including NSE comma formats; never raises."""
    if val is None or val == "":
        return default
    try:
        if isinstance(val, (int, float)):
            f = float(val)
            return f if f == f else default  # NaN guard
        s = str(val).replace(",", "").replace(" ", "").strip()
        if not s or s.upper() in ("-", "NA", "N/A", "NONE", "NULL"):
            return default
        f = float(s)
        return f if f == f else default
    except (TypeError, ValueError):
        return default



def _feed_missing_fields(payload: dict) -> list:
    """Return list of missing/zeroed fields for a feed payload."""
    data = payload if isinstance(payload, dict) else {}
    m = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    missing = []
    # Price: comma-safe via _feed_resolved_price
    price = _feed_resolved_price(data)
    if price <= 0:
        missing.append("price")
    rsi = data.get("rsi", m.get("rsi"))
    if rsi is None or _safe_float(rsi) == 0:
        missing.append("rsi")
    pe = data.get("pe_ratio", data.get("pe", m.get("pe_ratio", m.get("pe"))))
    if pe is None or _safe_float(pe) == 0:
        missing.append("pe_ratio")
    roce = data.get("roce", m.get("roce"))
    if roce is None or _safe_float(roce) == 0:
        missing.append("roce")
    sent = data.get("sentiment_score", data.get("news_score", m.get("sentiment_score")))
    if sent is None:
        missing.append("sentiment_score")
    return missing


def _feed_resolved_price(payload: dict) -> float:
    data = payload if isinstance(payload, dict) else {}
    try:
        from data_feed import _payload_price
        px = float(_payload_price(data) or 0)
        if px > 0:
            return px
    except Exception:
        pass
    try:
        from price_resolver import resolve_display_price
        px = float(resolve_display_price(str(data.get("symbol") or ""), {}, data) or 0)
        if px > 0:
            return px
    except Exception:
        pass
    m = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    for k in ("price", "close", "ltp", "cmp", "last_price", "prev_close"):
        raw = data.get(k) if data.get(k) not in (None, "") else m.get(k)
        try:
            s = str(raw or "").replace(",", "").replace(" ", "").strip()
            if not s or s.upper() in ("-", "NA", "N/A"):
                continue
            v = float(s)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.0



async def _patch_single_stock_feed(symbol: str, client: httpx.AsyncClient) -> dict:
    """
    Surgically fetch ONLY missing fields and merge into existing Neon feed blob.
    Sequential calls with REPAIR_COOLDOWN_SEC (default 0.5s) between upstream
    hits — cures 429/401 crumb storms from parallel repair storms.
    Never wipes valid existing values. Uses MARKET_DATA_URL / TECHNICAL_URL /
    FUNDAMENTAL_URL / NEWS_URL env vars (never hardcoded localhost).
    """
    store = _feed_store()
    base = str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    current = dict(store.get_symbol(base) or {})

    # Known chronically->₹5000 names (static list — MRF, MARUTI, PAGEIND,
    # ...): purge immediately, no live quote needed. Previously this symbol
    # would sit as "price: missing" forever if the quote fetch below kept
    # failing/rate-limiting before it ever got the chance to discover the
    # price and purge — a static list sidesteps the network round trip
    # entirely for names we already know the answer for.
    try:
        from symbol_aliases import is_known_high_price
    except Exception:
        is_known_high_price = lambda _s: False  # noqa: E731
    if MAX_UNIVERSE_PRICE > 0 and is_known_high_price(base):
        try:
            store.delete_symbol(base)
        except Exception as e:
            logger.warning("repair: purge known-high-price %s failed: %s", base, e)
        return {
            "symbol": base,
            "patched_fields": [],
            "still_missing": [],
            "price": _feed_resolved_price(current),
            "complete": False,
            "purged": True,
            "message": f"{base} is a known ₹{MAX_UNIVERSE_PRICE:.0f}+ stock — removed from feed without a network call.",
        }

    # Over-cap rows can't be "repaired" — a real price fetch would also be
    # over ₹MAX_UNIVERSE_PRICE, so the Repair button used to sit there doing
    # nothing useful forever. Purge instead and say so plainly.
    if current and _row_price_over_cap(current):
        try:
            store.delete_symbol(base)
        except Exception as e:
            logger.warning("repair: purge over-cap %s failed: %s", base, e)
        return {
            "symbol": base,
            "patched_fields": [],
            "still_missing": [],
            "price": _feed_resolved_price(current),
            "complete": False,
            "purged": True,
            "message": f"{base} was above ₹{MAX_UNIVERSE_PRICE:.0f} cap — removed from feed instead of repaired.",
        }

    missing = set(_feed_missing_fields(current))
    patched = []
    cooldown = max(0.1, float(REPAIR_COOLDOWN_SEC))

    market_url = (os.getenv("MARKET_DATA_URL") or MARKET_DATA_URL or "").rstrip("/")
    technical_url = (os.getenv("TECHNICAL_URL") or TECHNICAL_URL or "").rstrip("/")
    fundamental_url = (os.getenv("FUNDAMENTAL_URL") or FUNDAMENTAL_URL or "").rstrip("/")
    news_url = (os.getenv("NEWS_URL") or NEWS_URL or "").rstrip("/")

    # Genuinely delisted/merged symbols (TATAMTRDVR etc.) and known non-NSE
    # tickers: purge immediately instead of burning a /quote round trip that
    # can only ever fail — same rationale as the known-high-price short
    # circuit above. This is checked before the "price" fetch below so a
    # symbol like TATAMTRDVR never even reaches market-data-service.
    try:
        from symbol_aliases import is_known_delisted as _is_known_delisted
        from symbol_aliases import resolve_with_fallback as _resolve_with_fallback
    except Exception:
        _is_known_delisted = lambda _s: False  # noqa: E731
        _resolve_with_fallback = None
    if _is_known_delisted(base):
        try:
            store.delete_symbol(base)
        except Exception as e:
            logger.warning("repair: purge known-delisted %s failed: %s", base, e)
        return {
            "symbol": base,
            "patched_fields": [],
            "still_missing": [],
            "price": _feed_resolved_price(current),
            "complete": False,
            "purged": True,
            "message": f"{base} is delisted/merged (not a rename) — removed from feed without a network call.",
        }

    if "price" in missing and market_url:
        try:
            r = await client.get(f"{market_url}/quote/{base}", timeout=8.0)
            # market-data-service's /quote/{symbol} does NOT return 404 for
            # a genuine "possibly delisted / all sources failed" miss — it
            # returns HTTP 200 with {"price": None, "source": "failed"}
            # (see its last-resort fallback block). The one exception is
            # our own new is_known_delisted() short-circuit above, which
            # does raise a real 404. So both shapes have to be treated as
            # "needs fallback resolution" here, or the self-heal path below
            # would simply never fire for the failure mode that's actually
            # in the logs (JUBILANT/TATAMTRDVR before their alias-table
            # entries existed, and any future unmapped rename).
            body_failed = False
            if r.status_code == 200:
                try:
                    _body = r.json()
                    if isinstance(_body, dict) and (
                        _body.get("source") == "failed" or not _body.get("price")
                    ):
                        body_failed = True
                except Exception:
                    body_failed = True
            needs_fallback = r.status_code == 404 or body_failed
            if needs_fallback and _resolve_with_fallback is not None:
                # Miss on the plain symbol: this may be an unresolved rename
                # or a freshly-learned delisting rather than a transient
                # blip. Try to self-heal once before giving up — this is
                # exactly the recovery path symbol_aliases.resolve_with_
                # fallback() exists for, but repair previously never
                # called it at all.
                fallback_ticker, info = _resolve_with_fallback(base)
                if fallback_ticker is None:
                    # Confirmed non-NSE / genuinely delisted — purge instead
                    # of leaving this stuck as "price: missing" forever.
                    try:
                        store.delete_symbol(base)
                    except Exception as e:
                        logger.warning("repair: purge unresolved %s failed: %s", base, e)
                    return {
                        "symbol": base,
                        "patched_fields": patched,
                        "still_missing": [],
                        "price": 0.0,
                        "complete": False,
                        "purged": True,
                        "message": f"{base}: {info.get('resolution', 'unresolved')} — removed from feed.",
                    }
                new_base = fallback_ticker.replace(".NS", "").replace(".BO", "")
                if new_base != base:
                    logger.info(
                        "repair price %s: retrying as %s (%s)",
                        base, new_base, info.get("resolution"),
                    )
                    r = await client.get(f"{market_url}/quote/{new_base}", timeout=8.0)
            if r.status_code == 200:
                q = r.json() if isinstance(r.json(), dict) else {}
                # Only merge non-None quote fields — never write pe_ratio=0 / volume=0 poison
                cleaned = {k: v for k, v in q.items() if v is not None}
                for k in ("price", "cmp", "ltp", "close", "last_price", "regularMarketPrice"):
                    px = _safe_float(cleaned.get(k))
                    if px > 0:
                        if MAX_UNIVERSE_PRICE > 0 and px > MAX_UNIVERSE_PRICE:
                            # This symbol was fed with "price" missing (not
                            # yet known to be over cap), and only now, on
                            # fetching a live quote, turns out to be over
                            # cap. Without purging here, every future
                            # repair cycle would burn another quote call
                            # re-discovering the exact same fact forever —
                            # the "price still missing after quote attempt"
                            # warning below used to fire every single time
                            # for a stock that will never legitimately have
                            # a storable price. Purge it now instead.
                            logger.info(
                                "repair price over cap %s — ₹%.2f > cap, purging from feed", base, px
                            )
                            try:
                                store.delete_symbol(base)
                            except Exception as e:
                                logger.warning("repair: purge over-cap %s failed: %s", base, e)
                            return {
                                "symbol": base,
                                "patched_fields": patched,
                                "still_missing": [],
                                "price": px,
                                "complete": False,
                                "purged": True,
                                "message": f"{base} is above ₹{MAX_UNIVERSE_PRICE:.0f} cap — removed from feed.",
                            }
                        current["price"] = px
                        current.setdefault("close", px)
                        current.setdefault("cmp", px)
                        current.setdefault("ltp", px)
                        # Real OHLCV from Yahoo 2d when present
                        for ok in ("previous_close", "day_high", "day_low", "day_change_pct"):
                            if cleaned.get(ok) is not None:
                                current[ok] = cleaned[ok]
                        if cleaned.get("volume") is not None:
                            try:
                                vol = int(float(cleaned["volume"]))
                                if vol > 0:
                                    current["volume"] = vol
                            except (TypeError, ValueError):
                                pass
                        patched.append("price")
                        break
            elif r.status_code in (401, 429):
                logger.warning("repair price %s HTTP %s — backing off", base, r.status_code)
                await asyncio.sleep(cooldown * 2)
        except Exception as e:
            logger.debug("repair price %s: %s", base, e)
        await asyncio.sleep(cooldown)

    if "rsi" in missing:
        # Prefer local 1mo RSI (no technical service / peer storm)
        try:
            from data_feed import compute_rsi_from_closes
            import yfinance as yf
            yf_ticker = resolve_ns_ticker(base)
            hist = (
                await asyncio.to_thread(lambda: yf.Ticker(yf_ticker).history(period="1mo"))
                if yf_ticker else None
            )
            if hist is not None and not hist.empty and "Close" in hist.columns:
                closes = hist["Close"].dropna().values
                rsi_local = compute_rsi_from_closes(closes, period=14)
                if rsi_local is not None:
                    current["rsi"] = rsi_local
                    patched.append("rsi")
                    missing.discard("rsi")
        except Exception as e:
            logger.debug("repair local rsi %s: %s", base, e)

    if "rsi" in missing and technical_url:
        try:
            r = await client.get(f"{technical_url}/analyze/{base}?lite=1", timeout=20.0)
            if r.status_code == 404:
                r = await client.get(f"{technical_url}/technical/{base}", timeout=20.0)
            if r.status_code == 200:
                t = r.json() if isinstance(r.json(), dict) else {}
                rsi = t.get("rsi")
                if rsi is not None and _safe_float(rsi) != 0:
                    current["rsi"] = _safe_float(rsi)
                    patched.append("rsi")
                if t.get("ema20") is not None:
                    current["ema20"] = t.get("ema20")
                if t.get("technical_score") is not None and current.get("technical_score") is None:
                    current["technical_score"] = t.get("technical_score")
                if t.get("macd_hist") is not None or t.get("macd") is not None:
                    current["macd_hist"] = t.get("macd_hist", t.get("macd"))
            elif r.status_code in (401, 429):
                logger.warning("repair rsi %s HTTP %s — backing off", base, r.status_code)
                await asyncio.sleep(cooldown * 2)
        except Exception as e:
            logger.debug("repair rsi %s: %s", base, e)
        await asyncio.sleep(cooldown)

    if ("pe_ratio" in missing or "roce" in missing) and fundamental_url:
        try:
            # skip_peers=1 — avoid 5–8 peer Yahoo fetches per stock (429 cascade)
            r = await client.get(
                f"{fundamental_url}/analyze/{base}?skip_peers=1&lite=1",
                timeout=35.0,
            )
            if r.status_code == 404:
                r = await client.get(
                    f"{fundamental_url}/fundamental/{base}?skip_peers=1",
                    timeout=35.0,
                )
            if r.status_code == 200:
                f = r.json() if isinstance(r.json(), dict) else {}
                metrics = f.get("metrics") if isinstance(f.get("metrics"), dict) else {}
                pe = f.get("pe_ratio", f.get("pe", metrics.get("pe_ratio", metrics.get("pe"))))
                roce = f.get("roce", metrics.get("roce"))
                if "pe_ratio" in missing and pe is not None and _safe_float(pe) != 0:
                    current["pe_ratio"] = _safe_float(pe)
                    patched.append("pe_ratio")
                if "roce" in missing and roce is not None and _safe_float(roce) != 0:
                    current["roce"] = _safe_float(roce)
                    patched.append("roce")
                if f.get("fundamental_score") is not None and current.get("fundamental_score") is None:
                    current["fundamental_score"] = f.get("fundamental_score")
                if f.get("sector") and not current.get("sector"):
                    current["sector"] = f.get("sector")
                if metrics:
                    cur_m = current.get("metrics") if isinstance(current.get("metrics"), dict) else {}
                    current["metrics"] = {**cur_m, **{k: v for k, v in metrics.items() if v is not None}}
            elif r.status_code in (401, 429):
                logger.warning("repair fund %s HTTP %s — backing off", base, r.status_code)
                await asyncio.sleep(cooldown * 2)
        except Exception as e:
            logger.debug("repair fund %s: %s", base, e)
        await asyncio.sleep(cooldown)

    if "sentiment_score" in missing and news_url:
        try:
            r = await client.get(f"{news_url}/analyze/{base}", timeout=20.0)
            if r.status_code == 200:
                n = r.json() if isinstance(r.json(), dict) else {}
                ns = n.get("news_score", n.get("sentiment_score"))
                if ns is not None:
                    current["sentiment_score"] = ns
                    current.setdefault("news_score", ns)
                    patched.append("sentiment_score")
                    missing.discard("sentiment_score")
            elif r.status_code in (401, 429):
                await asyncio.sleep(cooldown * 2)
        except Exception as e:
            logger.debug("repair sentiment %s: %s", base, e)
        await asyncio.sleep(cooldown)

    # ── Baseline seeds (Step 6): never leave audit stuck when upstreams are cold ──
    # Matches bulk feed defaults so Health Score recovers without 429 storms.
    if "rsi" in missing:
        # Neutral RSI seed if Yahoo/technical both failed
        current.setdefault("rsi", 50.0)
        current["rsi_seed"] = True
        patched.append("rsi")
        missing.discard("rsi")
        logger.info("repair %s: seeded baseline RSI=50", base)

    if "pe_ratio" in missing:
        current["pe_ratio"] = 22.5
        current["pe_seed"] = True
        patched.append("pe_ratio")
        missing.discard("pe_ratio")
        logger.info("repair %s: seeded baseline PE=22.5", base)

    if "roce" in missing:
        current["roce"] = 15.0
        current["roce_seed"] = True
        patched.append("roce")
        missing.discard("roce")
        logger.info("repair %s: seeded baseline ROCE=15", base)

    if "sentiment_score" in missing:
        current["sentiment_score"] = 0.65
        current["sentiment_seed"] = True
        current.setdefault("news_score", 0.65)
        patched.append("sentiment_score")
        missing.discard("sentiment_score")
        logger.info("repair %s: seeded baseline sentiment=0.65", base)

    # Price still missing and over-cap stocks: keep existing price if present
    # (MRF-style names already have live price from bulk; do not wipe them)
    if "price" in missing:
        px = _feed_resolved_price(current)
        if px > 0:
            missing.discard("price")
        else:
            # Last resort: do not invent a fake price
            logger.warning("repair %s: price still missing after quote attempt", base)

    current["symbol"] = base
    current["repair_patched"] = patched
    current["repair_updated_at"] = datetime.now(IST).isoformat()
    # Deduplicate patched list while preserving order
    seen_p = set()
    patched_unique = []
    for f in patched:
        if f not in seen_p:
            seen_p.add(f)
            patched_unique.append(f)
    patched = patched_unique

    store.put_symbol(base, current, ttl=DATA_FEED_TTL)
    still_missing = _feed_missing_fields(current)
    return {
        "symbol": base,
        "patched_fields": patched,
        "still_missing": still_missing,
        "price": _feed_resolved_price(current),
        "complete": len(still_missing) == 0,
        "message": (
            f"Repaired {base}: {', '.join(patched) or 'no changes'}"
            + (f" — still missing {still_missing}" if still_missing else " — complete")
        ),
    }






@app.post("/api/feed/repair-single/{symbol}")
@app.post("/data-feed/repair-single/{symbol}")
@app.post("/api/data-feed/repair-single/{symbol}")
@app.post("/feed/repair-single/{symbol}")
async def repair_single_stock(symbol: str):
    """Repair one symbol — fetch only missing fields, merge into Cache DB.

    Always returns 200 with status + patched_fields so the UI Repair button
    never sees a 422/timeout ghost. High-price names (e.g. MRF) keep their
    live price; missing RSI/PE/ROCE/sentiment are filled from upstreams or
    safe baselines so audit health recovers immediately.
    """
    import urllib.parse
    sym = urllib.parse.unquote(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not sym:
        return {"status": "error", "symbol": symbol, "message": "empty symbol", "complete": False}
    try:
        client = _get_http_client()
        result = await _patch_single_stock_feed(sym, client)
        return {"status": "success", "ok": True, **result}
    except Exception as e:
        logger.exception("repair_single_stock %s: %s", sym, e)
        # Never 500 the UI — report failure payload so button can show message
        return {
            "status": "error",
            "ok": False,
            "symbol": sym,
            "patched_fields": [],
            "still_missing": ["unknown"],
            "complete": False,
            "message": str(e)[:200],
        }


@app.post("/api/feed/repair-batch")
@app.post("/data-feed/repair-batch")
async def repair_batch_missing(limit: int = 10):
    """
    Rate-safe batch repair (default 10). 500ms spacing between symbols.
    """
    limit = max(1, min(int(limit or 10), 25))
    audit = await audit_missing_feed_data(limit=limit)
    targets = [item["symbol"] for item in (audit.get("incomplete_stocks") or [])[:limit]]
    client = _get_http_client()
    repaired = []
    for sym in targets:
        try:
            res = await _patch_single_stock_feed(sym, client)
            repaired.append(res)
        except Exception as e:
            repaired.append({"symbol": sym, "error": str(e)[:160], "complete": False})
        await asyncio.sleep(0.5)
    ok_n = sum(1 for r in repaired if r.get("complete") or r.get("patched_fields"))
    return {
        "status": "completed",
        "repaired_count": len(repaired),
        "successish_count": ok_n,
        "repaired": repaired,
        "repaired_symbols": [r.get("symbol") for r in repaired],
    }


# ── Refill All: one-shot background repair of every incomplete feed record ──
# (Data Feed Health page — "Auto-Repair Next 15" only ever touches the first
# 15 rows each click; this walks the WHOLE incomplete list in rate-safe
# batches so the user doesn't have to click Repair dozens of times.)
_REFILL_ALL_JOB: dict = {
    "status": "idle",       # idle | running | done | stopped | error
    "total": 0,
    "processed": 0,
    "ok_count": 0,
    "started_at": None,
    "finished_at": None,
    "message": "Idle",
    "last_symbol": None,
    "cancel_requested": False,
}


async def _run_refill_all_job(limit: int):
    global _REFILL_ALL_JOB
    try:
        audit = await audit_missing_feed_data(limit=max(limit, 5000))
        targets = [item["symbol"] for item in (audit.get("incomplete_stocks") or [])]
        targets = targets[:limit] if limit else targets
        _REFILL_ALL_JOB.update({
            "status": "running",
            "total": len(targets),
            "processed": 0,
            "ok_count": 0,
            "started_at": datetime.now(IST).isoformat(),
            "finished_at": None,
            "message": f"Repairing {len(targets)} symbols…",
            "cancel_requested": False,
        })
        if not targets:
            _REFILL_ALL_JOB.update({
                "status": "done",
                "message": "Nothing to repair — feed already healthy.",
                "finished_at": datetime.now(IST).isoformat(),
            })
            return

        client = _get_http_client()
        ok_n = 0
        # Small batches with a short pause between each — keeps us well under
        # upstream (Yahoo/TwelveData/NSE) rate limits over a long run instead
        # of hammering everything at once.
        BATCH = 5
        for i in range(0, len(targets), BATCH):
            if _REFILL_ALL_JOB.get("cancel_requested"):
                _REFILL_ALL_JOB.update({
                    "status": "stopped",
                    "message": f"Stopped by user after {_REFILL_ALL_JOB.get('processed', 0)}/{len(targets)}.",
                    "finished_at": datetime.now(IST).isoformat(),
                })
                return
            chunk = targets[i:i + BATCH]
            for sym in chunk:
                try:
                    res = await _patch_single_stock_feed(sym, client)
                    if res.get("complete") or res.get("patched_fields"):
                        ok_n += 1
                    _REFILL_ALL_JOB["last_symbol"] = sym
                except Exception as e:
                    logger.debug("refill-all repair %s: %s", sym, e)
                _REFILL_ALL_JOB["processed"] = _REFILL_ALL_JOB.get("processed", 0) + 1
                _REFILL_ALL_JOB["ok_count"] = ok_n
                _REFILL_ALL_JOB["message"] = (
                    f"{_REFILL_ALL_JOB['processed']}/{len(targets)} · last: {sym}"
                )
                await asyncio.sleep(0.5)
            # Brief pause between batches so we never sustain a hard hammer
            await asyncio.sleep(1.0)

        _REFILL_ALL_JOB.update({
            "status": "done",
            "message": f"Done — {ok_n}/{len(targets)} improved.",
            "finished_at": datetime.now(IST).isoformat(),
        })
    except Exception as e:
        logger.exception("refill-all job failed: %s", e)
        _REFILL_ALL_JOB.update({
            "status": "error",
            "message": str(e)[:200],
            "finished_at": datetime.now(IST).isoformat(),
        })


@app.post("/api/feed/repair-all")
@app.post("/data-feed/repair-all")
async def repair_all_missing(background_tasks: BackgroundTasks, limit: int = 5000):
    """Kick off a background job that repairs EVERY incomplete feed record,
    not just the next 15. Poll /api/feed/repair-all/status for progress."""
    if _REFILL_ALL_JOB.get("status") == "running":
        return {"ok": True, "already_running": True, **_REFILL_ALL_JOB}
    limit = max(1, min(int(limit or 5000), 5000))
    background_tasks.add_task(_run_refill_all_job, limit)
    return {"ok": True, "started": True, "message": "Refill All started in the background."}


@app.get("/api/feed/repair-all/status")
@app.get("/data-feed/repair-all/status")
async def repair_all_status():
    return dict(_REFILL_ALL_JOB)


@app.post("/api/feed/repair-all/stop")
@app.post("/data-feed/repair-all/stop")
async def repair_all_stop():
    _REFILL_ALL_JOB["cancel_requested"] = True
    return {"ok": True, "message": "Stop requested — will halt after the current batch."}


@app.post("/data-feed/run")
@app.post("/api/data-feed/run")
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
        clear_data_feed_stop()
        ok_n = ok0
        err_n = err0
        done_set = set(done0)
        client = _get_http_client()  # shared keepalive pool

        # ── PHASE 0: Chunked Yahoo bulk quotes (50/chunk, threads=True) ──
        # Replaces sequential /quote calls that took ~15 min with ~20s total.
        if start_at == 0 and not done_set:
            try:
                from data_feed import run_bulk_yahoo_price_feed_cached as run_bulk_yahoo_price_feed
                store.set_job(
                    status="running",
                    message=f"Bulk Yahoo quotes for {len(universe)} symbols (chunks of 50)…",
                    processed=0,
                    total=len(universe),
                    updated_at=datetime.now(IST).isoformat(),
                )
                # run_bulk is sync (yfinance) — offload to thread so event loop stays responsive
                bulk_result = await asyncio.to_thread(
                    run_bulk_yahoo_price_feed, universe, True
                )
                saved = int((bulk_result or {}).get("tracked_stocks") or 0)
                for sym in (bulk_result or {}).get("symbols") or []:
                    done_set.add(str(sym).upper().replace(".NS", "").replace(".BO", ""))
                ok_n = max(ok_n, saved)
                store.set_job(
                    status="running",
                    message=(
                        f"Bulk 5-field seed done: {saved}/{len(universe)} "
                        f"(price+RSI local; PE/ROCE/sentiment baseline)"
                    ),
                    processed=saved,
                    total=len(universe),
                    ok_count=ok_n,
                    updated_at=datetime.now(IST).isoformat(),
                    checkpoint={"cursor": 0, "done": list(done_set), "universe": universe},
                )
                logger.info("data-feed bulk phase: %s", bulk_result)
                # Skip sequential /analyze peer storms when bulk covered ≥80% of universe
                skip_fund = os.getenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK", "1").strip().lower() in (
                    "1", "true", "yes", "on",
                )
                if skip_fund and saved >= max(1, int(0.35 * len(universe))):
                    ts = datetime.now(IST).isoformat()
                    msg = (
                        f"Data feed bulk-complete for {saved} stocks at {ts} "
                        f"(local RSI + baseline PE/ROCE/sentiment; use Repair for real fundamentals)"
                    )
                    store.set_meta(
                        last_success_at=ts,
                        last_count=saved,
                        last_errors=0,
                        last_message=msg,
                        source="bulk_5field",
                        universe_size=len(universe),
                        partial=saved < len(universe),
                    )
                    store.set_job(
                        status="done",
                        processed=len(universe),
                        total=len(universe),
                        message=msg,
                        ok_count=saved,
                        error_count=0,
                        finished_at=ts,
                        checkpoint={"cursor": len(universe), "done": list(done_set), "universe": universe},
                    )
                    logger.info(msg)
                    return
            except Exception as e:
                logger.warning("data-feed bulk phase failed (continuing sequential fund fill): %s", e)

        # PHASE 1: optional concurrent fund fill (rate-safe). Skip when bulk already
        # covered enough OR DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK=1 and any seed exists.
        # Default ON: after bulk seed, do not sequential /fundamental/analyze (rate-limit storm).
        # Set DATA_FEED_SKIP_SEQUENTIAL_FUND=0 to force slow fund fill.
        _skip_seq = os.getenv("DATA_FEED_SKIP_SEQUENTIAL_FUND", "1").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _skip_seq and ok_n > 0:
            ts = datetime.now(IST).isoformat()
            msg = f"Data feed stopped after bulk seed ({ok_n} rows) — sequential fund skipped"
            store.set_job(status="done", processed=len(universe), total=len(universe),
                          message=msg, ok_count=ok_n, finished_at=ts)
            store.set_meta(last_success_at=ts, last_count=ok_n, last_message=msg,
                           universe_size=len(universe), partial=True)
            logger.info(msg)
            return

        if True:
            # Concurrent fund-only fill (no news storm) — max 3 in-flight
            fund_sem = asyncio.Semaphore(int(os.getenv("DATA_FEED_FUND_CONCURRENCY", "3")))

            async def _feed_one(base: str):
                async with fund_sem:
                    fund = None
                    events = None
                    try:
                        r = await client.get(f"{FUNDAMENTAL_URL}/analyze/{base}", timeout=25)
                        if r.status_code == 200:
                            fund = r.json()
                    except Exception:
                        pass
                    try:
                        r = await client.get(f"{EVENT_URL}/events/{base}", timeout=12)
                        if r.status_code == 200:
                            events = r.json()
                    except Exception:
                        pass
                    return base, fund, events

            for i in range(start_at, len(universe)):
                # Cooperative stop (process flag OR job flag) — ASAP
                jnow = store.job()
                if data_feed_stop_requested() or jnow.get("stop_requested") or jnow.get("status") in ("stopped", "stopping"):
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
                    base, fund, events = await _feed_one(base)
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
@app.post("/api/data-feed/stop")
async def data_feed_stop(force: bool = True):
    """Stop data feed and commit checkpoint immediately.

    Free-tier workers often die after sleep, so cooperative stop alone leaves
    status=running forever. We always force-commit from the last checkpoint.
    """
    # Hard stop first — worker sees this on next symbol (no Neon round-trip)
    try:
        request_data_feed_stop()
    except Exception:
        pass
    store = _feed_store()
    # Mark stopping so in-flight loop exits even if Event was cleared
    try:
        store.set_job(stop_requested=True, status="stopping", message="Stop requested — finishing current symbol…")
    except Exception:
        pass
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
@app.post("/api/data-feed/resume")
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


@app.get("/stockky-hot/premarket/status")
def stockky_hot_premarket_status():
    """Progress of the Hot Picks 'Premarket' bulk price pre-feed job."""
    job = hot_premarket_job_get(_redis_get)
    return {"ok": True, **job}


@app.post("/stockky-hot/premarket")
async def stockky_hot_premarket(background_tasks: BackgroundTasks):
    """Pre-feed prices for EVERY eligible stock in ONE bulk call, before
    running Search Hot Picks Stocks.

    Today, Hot Picks' own price-enrichment step (see stockky_hot_stocks's
    "Price enrichment" block) only ever READS from the shared data-feed
    store — it never fetches over the network — so any symbol whose feed
    row is stale/missing shows "₹—" on its card. This endpoint is the fix:
    it builds the full eligible universe the same way the main market scan
    does (_build_scan_universe — every eligible stock, not just Hot Picks'
    ~285-symbol catalyst-seeded shortlist) and pre-feeds it via the SAME
    bulk pipeline the Data Feed tab uses (run_bulk_yahoo_price_feed —
    bhavcopy first, then ONE POST /quotes/bulk call for whatever's left,
    never one symbol at a time), writing into the same store (Neon on
    Render, Oracle ADB on the Oracle deployment — store dialect-detects
    automatically). Once this finishes, click 'Search Hot Picks Stocks' —
    its price lookups will already be warm.
    """
    job = hot_premarket_job_get(_redis_get)
    if job.get("status") == "running":
        return {"ok": True, "already_running": True, **job}

    try:
        universe = _build_scan_universe()
    except Exception as e:
        return {"ok": False, "error": f"could not build universe: {str(e)[:160]}"}

    if not universe:
        return {"ok": False, "error": "scan universe is empty — nothing to pre-feed"}

    hot_premarket_job_set(
        _redis_set,
        _redis_get,
        status="running",
        processed=0,
        total=len(universe),
        started_at=datetime.now(IST).isoformat(),
        message=f"Pre-feeding {len(universe)} eligible stocks (bulk)…",
        finished_at=None,
    )

    def _run_premarket(syms: list):
        """Runs entirely outside the HTTP request — a full-universe bulk
        feed can take a while; background=true semantics match every
        other premarket job in this app (IPO, Surprise)."""
        try:
            from data_feed import run_bulk_yahoo_price_feed
            result = run_bulk_yahoo_price_feed(syms, merge_existing=True) or {}
            n = int(result.get("tracked_stocks") or 0)
            ts = datetime.now(IST).isoformat()
            hot_premarket_job_set(
                _redis_set,
                _redis_get,
                status="done",
                processed=n,
                total=len(syms),
                message=result.get("message") or f"Pre-fed {n}/{len(syms)} stocks",
                finished_at=ts,
            )
        except Exception as e:
            logger.exception("hot premarket bulk-feed failed")
            hot_premarket_job_set(
                _redis_set,
                _redis_get,
                status="error",
                message=str(e)[:200],
                finished_at=datetime.now(IST).isoformat(),
            )

    background_tasks.add_task(_run_premarket, universe)
    return {
        "ok": True,
        "started": True,
        "total": len(universe),
        "message": f"Premarket bulk pre-feed started for {len(universe)} eligible stocks",
    }


@app.post("/stockky-hot/run")
async def stockky_hot_run(background_tasks: BackgroundTasks, force: bool = True):
    """Start Hot Picks search with REAL progress (pipeline UI polls /stockky-hot/status).

    Progress used to be faked — total was hard-coded to 100 and processed jumped
    0 → 10 → 30 → (long silent await) → 90 → 100. Since the ETA is derived from
    elapsed/processed, a fabricated processed=10 two seconds in projected "a few
    seconds remaining" for a scan that takes minutes, and then nothing moved at
    all while stockky_hot_stocks() ran. Now total is the real universe size and
    processed is the real symbol index, reported from stockky_hot_stocks's
    existing progress_cb hook, so the countdown means something.
    """
    job = hot_job_get(_redis_get)
    if job.get("status") == "running":
        return {"ok": True, "already_running": True, **job}

    # Fresh run: drop any stop request left over from a previous scan, and make
    # sure the durable table exists before we have rows to write into it.
    try:
        from hotpicks_store import clear_hotpicks_stop, ensure_hotpicks_schema

        clear_hotpicks_stop()
        ensure_hotpicks_schema()
    except Exception as e:
        logger.debug("hotpicks pre-run setup skipped: %s", e)

    hot_job_set(
        _redis_set,
        _redis_get,
        status="running",
        processed=0,
        total=0,
        started_at=datetime.now(IST).isoformat(),
        message="Building catalyst universe…",
        estimated_remaining_sec=None,
        finished_at=None,
        current_symbol=None,
        stopped=False,
    )

    async def _run_hot():
        # Throttle: the scan can call back hundreds of times and each write is a
        # durable kv write. One write per symbol is fine for a ~50-symbol
        # universe, but cap it so a large universe cannot hammer the DB.
        state = {"last_write": 0.0, "last_processed": -1}
        min_interval = float(os.getenv("HOT_PROGRESS_MIN_INTERVAL_SEC", "1.0"))

        def _on_symbol(processed: int, total: int, symbol: str, batch: int = 0):
            """Called by stockky_hot_stocks before each symbol."""
            try:
                now = time.time()
                is_first = state["last_processed"] < 0
                is_last = total and processed >= total - 1
                if not is_first and not is_last and (now - state["last_write"]) < min_interval:
                    return
                state["last_write"] = now
                state["last_processed"] = processed
                hot_job_set(
                    _redis_set,
                    _redis_get,
                    processed=int(processed),
                    total=int(total or 0),
                    current_symbol=str(symbol),
                    message=f"Scanning {symbol} ({processed + 1}/{total})",
                )
            except Exception:
                pass

        try:
            # Clear short cache so force refresh
            try:
                if _redis:
                    _redis.delete(HOT_STOCKS_CACHE_KEY)
            except Exception:
                pass
            result = await stockky_hot_stocks(force=True, progress_cb=_on_symbol)
            ts = datetime.now(IST).isoformat()
            was_stopped = bool(isinstance(result, dict) and result.get("stopped_early"))
            total = int((result or {}).get("universe_size") or 0)
            done = int((result or {}).get("processed_symbols") or total)
            if isinstance(result, dict):
                result = {**result, "generated_at": result.get("generated_at") or ts, "persisted_at": ts}
                _redis_set(HOT_RESULT_KEY, result, ttl=20 * 3600)
            picks = sum(
                len((result or {}).get(k) or [])
                for k in ("news_driven", "results_driven", "bulk_insider_driven")
            )
            hot_job_set(
                _redis_set,
                _redis_get,
                status="stopped" if was_stopped else "done",
                processed=done,
                total=total or done,
                current_symbol=None,
                stopped=was_stopped,
                message=(
                    f"Stopped after {done}/{total} symbols — {picks} pick(s) kept"
                    if was_stopped
                    else f"Hot Picks ready at {ts} — {picks} pick(s)"
                ),
                finished_at=ts,
                estimated_remaining_sec=0,
            )
        except Exception as e:
            logger.exception("hot run failed")
            hot_job_set(
                _redis_set,
                _redis_get,
                status="error",
                current_symbol=None,
                estimated_remaining_sec=None,
                message=str(e)[:200],
            )
        finally:
            # Never leave the flag set — the next run must not stop instantly.
            try:
                from hotpicks_store import clear_hotpicks_stop

                clear_hotpicks_stop()
            except Exception:
                pass

    background_tasks.add_task(_run_hot)
    return {"ok": True, "started": True, "message": "Hot Picks search started"}


@app.post("/stockky-hot/stop")
def stockky_hot_stop():
    """Halt the running Hot Picks scan after the current symbol.

    Symbols already scored are ranked, cached and written to
    hotpicks_static_feed, so a stop is a "keep what you have", not a discard —
    same semantics as /surprise/ipo/stop and the data-feed repair stop.
    """
    try:
        from hotpicks_store import request_hotpicks_stop

        request_hotpicks_stop()
    except Exception as e:
        return {"ok": False, "detail": f"stop unavailable: {str(e)[:160]}"}
    job = hot_job_get(_redis_get)
    if job.get("status") != "running":
        return {"ok": True, "stopping": False, "message": "No Hot Picks scan is running", **job}
    hot_job_set(
        _redis_set,
        _redis_get,
        message="Stop requested — finishing current symbol…",
        stop_requested=True,
    )
    return {"ok": True, "stopping": True, "message": "Stopping after the current symbol"}


@app.get("/stockky-hot/table")
def stockky_hot_table(hours: int = 24):
    """Hot Picks from the durable table (default: last 24h).

    Serves hotpicks_static_feed directly — Neon on Render, Oracle ADB on the
    Oracle VM, identical JSON either way. This is what the tab paints instantly
    on open, before (or instead of) triggering a scan.
    """
    try:
        from hotpicks_store import hotpicks_db_payload, HOTPICKS_TABLE_HOURS
    except Exception as e:
        return {"ok": False, "rows": [], "count": 0, "hours": hours, "fresh": False,
                "detail": f"hotpicks store unavailable: {str(e)[:160]}"}
    try:
        window = int(hours or HOTPICKS_TABLE_HOURS)
    except Exception:
        window = 24
    window = max(1, min(window, 24 * 30))
    payload = hotpicks_db_payload(window)
    if not payload:
        return {
            "ok": True,
            "rows": [],
            "count": 0,
            "hours": window,
            "fresh": False,
            "detail": "No stored Hot Picks in this window yet — run a scan.",
        }
    rows = []
    for section in ("news_driven", "results_driven", "bulk_insider_driven"):
        rows.extend(payload.get(section) or [])
    return {"ok": True, **payload, "rows": rows}


@app.post("/stockky-hot/notify-top-picks")
async def api_hotpicks_notify_top_picks(top_n: int = Query(5, ge=1, le=20)):
    """
    Manual "Send Top 5 to Telegram" button for the Hot Picks tab — same
    pattern as /surprise/notify-top-picks (Surprise Momentum tab already
    had this; Hot Picks did not). Reuses whatever is currently cached
    (live scan result or the durable hotpicks_static_feed fallback) rather
    than triggering a fresh scan, so this responds instantly.
    """
    cached = _redis_get(HOT_STOCKS_CACHE_KEY)
    if not cached:
        try:
            from hotpicks_store import hotpicks_db_payload
            cached = hotpicks_db_payload()
        except Exception:
            cached = None

    def _has_picks(p) -> bool:
        if not isinstance(p, dict):
            return False
        return bool(
            (p.get("news_driven") or [])
            or (p.get("results_driven") or [])
            or (p.get("bulk_insider_driven") or [])
        )

    if not _has_picks(cached):
        return {
            "ok": False,
            "sent": False,
            "count": 0,
            "message": "No Hot Picks available right now — run a scan first.",
        }

    merged = (
        list(cached.get("bulk_insider_driven") or [])
        + list(cached.get("results_driven") or [])
        + list(cached.get("news_driven") or [])
    )
    order = {"BUY NOW": 0, "PREPARE TO BUY": 1, "DO NOT BUY": 2, "SELL": 3}
    merged.sort(key=lambda x: (order.get((x.get("decision") or "").upper(), 9), -(x.get("score") or 0)))
    top = merged[: max(1, min(int(top_n), 20))]
    if not top:
        return {"ok": False, "sent": False, "count": 0, "message": "No Hot Picks available right now."}

    lines = [f"🔥 *Stockky Hot Picks — Top {len(top)}*"]
    for i, s in enumerate(top, 1):
        sym = s.get("symbol")
        decision = s.get("decision") or "—"
        score = s.get("score")
        price = s.get("price") or s.get("close")
        section = (s.get("section") or "").replace("_", " ")
        price_str = f"₹{price}" if price else "price n/a"
        lines.append(f"{i}. {sym} — {decision} (score {score if score is not None else '—'}/100)\n   {price_str} · {section}")
    message = "\n".join(lines)

    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/notify",
            json={"title": "Stockky Hot Picks — Top Picks", "message": message, "channel": "telegram"},
            timeout=15,
        )
        try:
            detail = resp.json()
        except Exception:
            detail = {"status_code": resp.status_code}
        delivered = bool(isinstance(detail, dict) and detail.get("delivered"))
        return {
            "ok": True,
            "sent": delivered,
            "count": len(top),
            "symbols": [s.get("symbol") for s in top],
            "notification_result": detail,
        }
    except Exception as e:
        return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}


@app.get("/stockky-hot/audit")
def stockky_hot_audit():
    """Hot Picks feed health: backend in use, row counts, staleness, gaps."""
    try:
        from hotpicks_store import hotpicks_audit
    except Exception as e:
        return {"ok": False, "issues": [f"hotpicks store unavailable: {str(e)[:160]}"]}
    return hotpicks_audit()


@app.post("/stockky-hot/repair-batch")
def stockky_hot_repair_batch(limit: int = Query(15, ge=1, le=30), symbol: Optional[str] = Query(None)):
    """Price-only repair for hotpicks_static_feed rows — mirrors
    /api/surprise/repair-batch. Missing decision/score are reported by the
    audit above but intentionally not faked here; they only come from a
    fresh scoring pass (see hotpicks_repair_batch's docstring)."""
    try:
        from hotpicks_store import hotpicks_repair_batch
    except Exception as e:
        return {"status": "error", "error": f"hotpicks store unavailable: {str(e)[:160]}"}
    market_url = os.getenv("MARKET_DATA_URL", "")
    return hotpicks_repair_batch(limit=limit, symbol=symbol, market_data_url=market_url)




# ── OS signal hooks (Render deploy / free-tier SIGTERM) ─────────────────────
def _install_signal_handlers() -> None:
    """Ensure SIGTERM/SIGINT commit checkpoints even if uvicorn path is skipped."""
    import signal
    import threading

    _once = getattr(_install_signal_handlers, "_installed", False)
    if _once:
        return
    _install_signal_handlers._installed = True  # type: ignore[attr-defined]

    def _handle(signum, frame):
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.warning("Received %s — running graceful shutdown commit", name)
        try:
            _graceful_shutdown_commit(reason=f"signal_{name}")
        except Exception as e:
            logger.warning("signal shutdown commit failed: %s", e)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except Exception as e:
            logger.debug("signal %s install: %s", sig, e)


try:
    _install_signal_handlers()
except Exception as _sig_err:
    logger.debug("signal handlers: %s", _sig_err)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)