"""
API Gateway
------------
Single entry point for the React frontend.
v2.5.16 – uses IST for fetched_at timestamp (hh:mm:ss AM/PM).
"""
import os
import re
import math
import json
import time
import asyncio
import gc
import logging
import difflib
import uuid
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from starlette.concurrency import run_in_threadpool
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Optional, Set, Dict, Union, Any

import httpx
import yfinance as yf
from symbol_aliases import resolve_ns_ticker
import ms_passthrough  # Group 349: market-structure block passthrough (never raises)

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
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response, WebSocket, WebSocketDisconnect, Query, Depends
from admin_guard import require_admin as _require_admin  # group 453
import cors_policy as _cors_policy  # group 454
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
from metrics import metrics, label_limiter
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

# 2026-10-04 (log noise): httpx logs every outbound request at INFO and uvicorn logs every /health probe
# (container healthchecks + the browser's api.ping()) -- together most of the log volume. httpx -> WARNING
# (HTTPX_LOG_LEVEL=INFO restores it); successful /health access lines are dropped (ACCESS_LOG_HEALTH=1 restores
# them; a non-200 /health still logs).
def _quiet_noisy_loggers() -> None:
    import re as _re
    import logging as _lg
    import os as _os
    _lvl = getattr(_lg, (_os.getenv("HTTPX_LOG_LEVEL") or "WARNING").strip().upper(), _lg.WARNING)
    if not isinstance(_lvl, int):
        _lvl = _lg.WARNING
    for _n in ("httpx", "httpcore"):
        _lg.getLogger(_n).setLevel(_lvl)
    if (_os.getenv("ACCESS_LOG_HEALTH") or "").strip().lower() in ("1", "true", "yes"):
        return
    _pat = _re.compile(r'"GET \S*/health(\?\S*)? HTTP/[\d.]+" 200')

    class _NoHealthAccess(_lg.Filter):
        def filter(self, record):  # noqa: A003
            try:
                return not _pat.search(record.getMessage())
            except Exception:  # noqa: BLE001
                return True

    _acc = _lg.getLogger("uvicorn.access")
    if not any(getattr(f, "_stockky_health_filter", False) for f in _acc.filters):
        _flt = _NoHealthAccess()
        _flt._stockky_health_filter = True
        _acc.addFilter(_flt)


_quiet_noisy_loggers()

# ---- IST timezone ----
IST = ZoneInfo("Asia/Kolkata")

def _env_url(name: str, default: str, rstrip: bool = True) -> str:
    """URL setting from the environment with a blank-safe fallback.

    os.getenv(name, default) only falls back when the variable is UNSET, so an empty or
    whitespace-only value (a blank Render dashboard variable, `NAME=` in an env_file) overrode a working
    default and every request went to "/path". Blank / whitespace-only (and, with rstrip, slash-only)
    values now use `default`; padded values are trimmed.
    """
    raw = (os.getenv(name) or "").strip()
    if rstrip:
        raw = raw.rstrip("/")
    return raw or (default.rstrip("/") if rstrip else default)


# ---- Service URLs (env-driven; defaults match config/service_urls.py) ----
_DP = _env_url("DECISION_PREDICTION_URL", "https://decision-prediction-service.onrender.com", rstrip=False)
_AI = _env_url("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com", rstrip=False)
_NS = _env_url("NOTIFICATION_SCHEDULER_URL", "https://notification-scheduler-service-x8vc.onrender.com/notification", rstrip=False)

DECISION_URL = _env_url("DECISION_URL", f"{_DP.rstrip('/')}/decision", rstrip=False)
NOTIFICATION_URL = _env_url("NOTIFICATION_URL", _NS.rstrip('/') if _NS.rstrip('/').endswith('notification') else f"{_NS.rstrip('/')}/notification", rstrip=False)
NEWS_URL = _env_url("NEWS_URL", f"{_AI.rstrip('/')}/news", rstrip=False)
MARKET_DATA_URL = _env_url("MARKET_DATA_URL", "http://market-data-service:8001", rstrip=False)
TECHNICAL_URL = _env_url("TECHNICAL_URL", f"{_AI.rstrip('/')}/technical", rstrip=False)
FUNDAMENTAL_URL = _env_url("FUNDAMENTAL_URL", f"{_AI.rstrip('/')}/fundamental", rstrip=False)
# 2026-09-11: base URL for the overnight-orchestrator proxy routes below —
# same service as NOTIFICATION_URL, different sub-app mount (scheduler/
# main.py is mounted at /scheduler, not /notification — see that service's
# main.py).
_NS_BASE = _NS.rstrip('/')
if _NS_BASE.endswith("/notification"):
    _NS_BASE = _NS_BASE[: -len("/notification")]
SCHEDULER_URL = _env_url("SCHEDULER_URL", f"{_NS_BASE}/scheduler", rstrip=False)
EVENT_URL = _env_url("EVENT_URL", f"{_AI.rstrip('/')}/event", rstrip=False)
PREDICTION_URL = _env_url("PREDICTION_URL", f"{_DP.rstrip('/')}/prediction", rstrip=False)

# ---- Market Sentiment & Training ----
MARKET_SENTIMENT_URL = _env_url("MARKET_SENTIMENT_URL", f"{_AI.rstrip('/')}/sentiment", rstrip=False)
TRAINING_URL = _env_url("TRAINING_URL", f"{_DP.rstrip('/')}/training", rstrip=False)

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

# group318: FastAPI deprecated @app.on_event. These two decorators keep what on_event did (handlers run in the order they
# are declared; an async handler is awaited, a sync one is called inline; a failing startup handler aborts boot) but run
# them from a lifespan. The decorated function is returned unchanged, so tests can still call it directly.
import contextlib as _contextlib
import inspect as _inspect

_STARTUP_HOOKS: list = []
_SHUTDOWN_HOOKS: list = []


def _on_startup(fn):
    _STARTUP_HOOKS.append(fn)
    return fn


def _on_shutdown(fn):
    _SHUTDOWN_HOOKS.append(fn)
    return fn


@_contextlib.asynccontextmanager
async def _lifespan(_app):
    for _h in list(_STARTUP_HOOKS):
        _r = _h()
        if _inspect.isawaitable(_r):
            await _r
    yield
    for _h in list(_SHUTDOWN_HOOKS):
        _r = _h()
        if _inspect.isawaitable(_r):
            await _r


app = FastAPI(title="Stockky API Gateway", version="2.5.16", lifespan=_lifespan)
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


@_on_startup
async def _boot_forensics_startup():
    """Session 72 (#2): classify why the previous process ended (OOM SIGKILL vs clean restart vs recreate)."""
    try:
        import boot_forensics
        boot_forensics.record_boot("api-gateway")
    except Exception as _bf:
        logger.debug("boot_forensics unavailable: %s", _bf)


@_on_shutdown
async def _boot_forensics_shutdown():
    try:
        import boot_forensics
        boot_forensics.mark_clean_shutdown()
    except Exception:
        pass


@_on_startup
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
    # 2026-09-09 fix: reset circuit breakers on every startup so a half-open/open
    # state from a previous container run (or a cold-start timeout storm) does not
    # permanently block the api-gateway from reaching market-data-service.
    # Safe: breakers re-open themselves if the downstream is genuinely down.
    try:
        from circuit_breaker import reset_all_breakers
        names = reset_all_breakers()
        logger.info("Startup: reset %d circuit breaker(s): %s", len(names), names)
    except Exception as e:
        logger.warning("Startup warning (circuit-breaker reset, non-fatal): %s", e)
    # group 185: say so at boot when the hand-kept NSE holiday set has run out (this year, or next year from 15 Nov).
    try:
        from nse_holidays import holiday_coverage_warning
        _hol_msg = holiday_coverage_warning()
        if _hol_msg:
            logger.warning(_hol_msg)
    except Exception as e:
        logger.debug("holiday coverage check skipped (non-fatal): %s", e)


@_on_shutdown
async def _graceful_shutdown():
    """FastAPI/uvicorn SIGTERM path: commit work, stop loops, close clients."""
    global _shared_http_client, _quote_loop_task
    logger.info("Graceful shutdown starting…")
    try:
        phases = _graceful_shutdown_commit(reason="process_shutdown")
        logger.info("Graceful shutdown commit: %s", phases)
    except Exception as e:
        logger.warning("Graceful shutdown commit failed: %s", e)
    # group346: durable kv writes queued from the event loop (write-behind) must reach Oracle before we exit
    try:
        if _kv_cache is not None and hasattr(_kv_cache, "flush_pending"):
            _drained = await asyncio.to_thread(_kv_cache.flush_pending, 10.0)
            logger.info("kv write-behind flushed on shutdown: %s", _drained)
    except Exception as e:
        logger.warning("kv write-behind flush failed: %s", e)
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


# --- CORS (group 454: origin allowlist via GATEWAY_CORS_ORIGINS; unset = wildcard as before; see cors_policy.py) ---
_CORS_ORIGINS = _cors_policy.configured_origins()
if _cors_policy.is_wildcard(_CORS_ORIGINS):
    _CORS_ORIGINS = ["*"]
    logger.warning("CORS: GATEWAY_CORS_ORIGINS is not set - any website origin may call the gateway from a browser. Set it to your site's origin(s).")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,   # the app authenticates with a Bearer header, never cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# E88: batch M (gateway_support/bound_routes_m.py); built once here, before its first use below.
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware  # noqa: E402
from gateway_support import MainRef as _MainRef  # noqa: E402
from gateway_support.bound_routes_m import build_bound_routes_m as _build_bound_routes_m  # noqa: E402
_bound_routes_m = _build_bound_routes_m(_MainRef(globals()))
# E88: moved to gateway_support/bound_routes_m.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
add_cors_header = _bound_routes_m.add_cors_header
app.add_middleware(_BaseHTTPMiddleware, dispatch=add_cors_header)   # same registration the middleware decorator makes

# E88: moved to gateway_support/bound_routes_m.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
universal_exception_handler = _bound_routes_m.universal_exception_handler
app.add_exception_handler(Exception, universal_exception_handler)   # same registration the exception-handler decorator makes

# ── Redis (OFF by default — USE_REDIS=1 to enable Upstash) ─────────────────────
# CRITICAL: Never import or ping Upstash unless USE_REDIS=1.
# Residual REDIS_URL / UPSTASH_* env vars on Render must not cause a handshake.
_redis = None
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes") or \
   os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    _USE_REDIS = False

if _USE_REDIS:
    url = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
    tok = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if url and tok:
        try:
            from upstash_redis import Redis as _UpstashRedis
            _redis = _UpstashRedis(url=url, token=tok)
            _redis.ping()
            logger.info("Connected to Upstash Redis (USE_REDIS=1)")
        except Exception as e:
            logger.warning("Redis unavailable (falling back to memory + durable DB): %s", e)
            _redis = None
    else:
        logger.info("USE_REDIS=1 but UPSTASH credentials missing — memory + durable DB only")
else:
    logger.info(
        "Gateway Redis disabled (USE_REDIS=0 / DISABLE_REDIS) — "
        "in-memory + durable-DB cache only; no Upstash handshake"
    )

# In-memory fallback so scan status / universe work without Redis
_mem_kv = {}
_mem_kv_exp = {}
import time as _time_mod



WATCHLIST_KEY       = "stockky:watchlist"
SEARCHED_KEY        = "stockky:searched_symbols"
SCAN_UNIVERSE_KEY         = "stockky:scan_universe"
# Separate long-lived stale-serve safety net — written with a 4h TTL every
# time a fresh universe is built, never cleared by /scan/universe/cache,
# so real-trade-service always has something to serve on a cold restart even
# if the 30-min live key has expired.  Prefix "stockky:scan_universe" keeps
# it in kv_cache's _DURABLE_PREFIXES so it persists in Neon across restarts.
SCAN_UNIVERSE_STALE_KEY   = "stockky:scan_universe:stale_fallback"
IPO_CACHE_KEY       = "stockky:ipos:recent"
KNOWN_SYMBOLS_KEY   = "stockky:known_symbols"
SCAN_TASK_PREFIX    = "stockky:scan_task:"
# 2026-09-18 fix — see the BUG FIX docstring at the top of
# _get_momentum_movers() for the full root-cause writeup: that function had
# no result cache of its own, so every caller (there are ~20 call sites)
# paid for a full fresh recompute, including an uncached whole-market
# AngelOne quote sweep. This is what was making /scan/universe (and other
# movers-dependent endpoints) slow enough to trip real-trade-service's
# candidate-fetch ReadTimeout, especially in the first few minutes after a
# redeploy when several callers (the stockky-hot warm-up job, dashboard
# polls, candidate_engine) all land in the same short window.
MOMENTUM_MOVERS_CACHE_KEY = "stockky:momentum_movers"
MOMENTUM_MOVERS_CACHE_TTL = 90  # seconds
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
STATIC_PARAM_TTL = int(((os.getenv("STATIC_PARAM_TTL") or "").strip() or "86400"))  # 24 hours
LAST_FULL_SCAN_KEY = "stockky:last_full_scan"
LAST_FULL_SCAN_TTL = int(((os.getenv("LAST_FULL_SCAN_TTL") or "").strip() or "86400"))  # 24h — survive refresh / stop partial
DECIDE_CACHE_PREFIX = "stockky:decide_cache:"
DECIDE_CACHE_TTL_OPEN = int(((os.getenv("DECIDE_CACHE_TTL_OPEN") or "").strip() or "300"))   # 5 min market open
DECIDE_CACHE_TTL_CLOSED = int(((os.getenv("DECIDE_CACHE_TTL_CLOSED") or "").strip() or "21600"))  # 6 h closed
BATCH_RESULT_CACHE_PREFIX = "stockky:batch_result:"
BATCH_RESULT_CACHE_ENABLED = ((os.getenv("BATCH_RESULT_CACHE") or "").strip() or "true").lower() in ("1", "true", "yes")
SCAN_LITE_DEFAULT = os.getenv("SCAN_LITE_DEFAULT", "false").lower() in ("1", "true", "yes")
WAKE_BEFORE_SCAN = ((os.getenv("WAKE_BEFORE_SCAN") or "").strip() or "true").lower() in ("1", "true", "yes")
WAKE_WAIT_SECONDS = float(((os.getenv("WAKE_WAIT_SECONDS") or "").strip() or "12"))

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
    # Hero MotoCorp's NSE symbol is HEROMOTOCO. "HEROMOTORS" (and the typos around it) is not a
    # symbol, but it is what people type; without this a typed "HERO MOTERS" fuzzy-matched
    # whatever near-miss had been recorded as "searched" and analysed that.
    "HEROMOTORS": "HEROMOTOCO",
    "HEROMOTERS": "HEROMOTOCO",
    "HEROMOTER": "HEROMOTOCO",
    "HEROMOTO": "HEROMOTOCO",
    "HEROMOTOR": "HEROMOTOCO",
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

def _sync_scan_task_mirror(key: str, value, ttl: int = None) -> None:
    """Keep ONLY currently-running scan-task statuses mirrored in `_mem_kv`.

    `_redis_set` returns right after a successful `kv_cache.set`, so with kv_cache importable (the normal
    production state) scan statuses never reached `_mem_kv` -- but `_graceful_shutdown_commit` and
    `/scan/stop-all` find running scans by walking `_mem_kv`, so they saw none and the persisted status
    stayed "running" until its TTL. This mirrors a scan-task key while its status is "running" and drops it
    on any other status, so `_mem_kv` stays bounded to live scans. Entries past their TTL are pruned here
    too, so a scan that died without writing a final status is not re-cancelled forever. Never raises."""
    try:
        if not isinstance(key, str) or not key.startswith(SCAN_TASK_PREFIX) or key.endswith(":cancel"):
            return
        now = _time_mod.time()
        for k in [k for k in list(_mem_kv) if str(k).startswith(SCAN_TASK_PREFIX)
                  and (_mem_kv_exp.get(k) or now + 1) < now]:
            _mem_kv.pop(k, None)
            _mem_kv_exp.pop(k, None)
        if isinstance(value, dict) and value.get("status") == "running":
            _mem_kv[key] = dict(value)
            if ttl:
                _mem_kv_exp[key] = now + int(ttl)
            else:
                _mem_kv_exp.pop(key, None)
        else:
            _mem_kv.pop(key, None)
            _mem_kv_exp.pop(key, None)
    except Exception:
        pass


def _redis_set(key: str, value, ttl: int = None):
    """Memory always; Neon for durable prefixes; Redis only if USE_REDIS=1."""
    if _kv_cache is not None:
        try:
            _kv_cache.set(key, value, ttl=ttl)
            _sync_scan_task_mirror(key, value, ttl)
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
    """The stored searched-symbol list, self-cleaned (group101, items 11 and 28).

    Old entries could be anything a past version stored: padded/lowercase/.NS spellings, non-strings,
    misspelt names (HEROMOTORS) recorded before group93 stopped recording failed lookups, and symbols that
    have since been delisted (AAKASH, ANNAPURNA). Every read now normalises and runs the list through
    `_filter_equities` (the same delisted / index / non-equity / rename gate as the scan universe), and
    writes the cleaned list back once when it differs, so the stored key heals itself. Never raises: a
    failed cleanup or write-back returns what could be cleaned and leaves the key as it was."""
    raw = _redis_get(SEARCHED_KEY) or []
    if not isinstance(raw, list):
        return []
    normed = [str(x).strip().upper().replace(".NS", "").replace(".BO", "").strip()
              for x in raw if isinstance(x, str) and x.strip()]
    # A typed name that has a curated one-to-one alias (HEROMOTORS -> HEROMOTOCO, TATAMOTORS -> TMPV) is stored
    # as its real symbol; list-valued aliases are ambiguous and left as they are.
    normed = [SYMBOL_ALIASES[x] if isinstance(SYMBOL_ALIASES.get(x), str) else x for x in normed]
    try:
        cleaned = _filter_equities(normed)
    except Exception as e:  # pragma: no cover - _filter_equities itself never raises
        logger.debug("searched-list cleanup failed: %s", e)
        return normed
    if cleaned != raw:
        try:
            _redis_set(SEARCHED_KEY, cleaned[-200:])
            if len(cleaned) != len(raw):
                logger.info("searched symbols: cleaned stored list (%d -> %d entries)", len(raw), len(cleaned))
        except Exception as e:
            logger.debug("searched-list write-back failed: %s", e)
    return cleaned


async def _load_searched_safe() -> List[str]:
    """``_load_searched`` in a worker thread; a failure degrades to [] so a
    side lookup can't fail a /scan/universe call whose universe built fine."""
    try:
        got = await asyncio.to_thread(_load_searched)
    except Exception as e:
        logger.warning("scan/universe: searched-list lookup failed: %s", e)
        return []
    return got if isinstance(got, list) else []

def _add_searched(symbol: str):
    searched = _load_searched()
    sym = (symbol or "").strip().upper().replace(".NS", "").replace(".BO", "").strip()
    if sym and sym not in searched:
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

# 2026-10-06 (group 178): while NSE blocks this VM's IP (403 on every board, see the group 96 note for
# quote-equity) each caller of _fetch_from_nse_api still paid for a request, a fresh cookie bootstrap and a retry,
# for each of the four movers boards, on every movers recompute -- more traffic into the block, nothing gained.
# After a 401/403/429 that survives the one session-refresh retry, ALL NSE api fetches pause for
# NSE_API_BLOCK_SECONDS (default 600; 0 = never pause; blank/invalid = default). During the pause a stale cached
# value is still served and otherwise None (the callers already fall through to the other sources); one warning
# marks the start. A 200 ends the pause. 404/5xx/exceptions never start one.
_nse_api_block_until = 0.0
_nse_api_block_skipped = 0


def _nse_api_block_seconds() -> float:
    raw = (os.getenv("NSE_API_BLOCK_SECONDS") or "").strip()
    try:
        v = float(raw) if raw else 600.0
    except ValueError:
        return 600.0
    return v if v >= 0 else 600.0


# group346 (log review #16): the block pause above covers 401/403/429 only. Boards that answer 404 (retired endpoint),
# 5xx (503 on bulk/block deals) or fail at the transport level were asked again on every recompute. One endpoint that
# fails this way is now left alone for NSE_ENDPOINT_FAIL_PAUSE_S (default 900 s), doubling per further failure up to
# NSE_ENDPOINT_FAIL_MAX_S (default 3600 s); a 200 clears it. Other endpoints are unaffected. 0 disables.
_nse_endpoint_fail: dict = {}   # endpoint -> [consecutive_failures, paused_until_epoch]


def _nse_endpoint_env(name: str, default: float) -> float:
    try:
        v = float((os.getenv(name) or "").strip() or default)
        return v if v >= 0 else default
    except ValueError:
        return default


def _nse_endpoint_paused(endpoint: str) -> bool:
    ent = _nse_endpoint_fail.get(endpoint)
    return bool(ent and ent[1] > time.time())


def _nse_endpoint_note_failure(endpoint: str, why: str) -> None:
    base = _nse_endpoint_env("NSE_ENDPOINT_FAIL_PAUSE_S", 900.0)
    if base <= 0:
        return
    cap = max(base, _nse_endpoint_env("NSE_ENDPOINT_FAIL_MAX_S", 3600.0))
    ent = _nse_endpoint_fail.setdefault(endpoint, [0, 0.0])
    ent[0] += 1
    pause = min(cap, base * (2 ** (ent[0] - 1)))
    ent[1] = time.time() + pause
    logger.warning("NSE API %s failed (%s) - not asking this endpoint again for %ds (failure #%d)",
                   endpoint, why, int(pause), ent[0])


def _fetch_from_nse_api(endpoint: str, cache_key: str, ttl: int = 21600):
    global _nse_api_block_until, _nse_api_block_skipped
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    if _nse_api_block_until and time.time() < _nse_api_block_until:
        _nse_api_block_skipped += 1
        return cached if cached else None
    if _nse_endpoint_paused(endpoint):
        return cached if cached else None
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
            _nse_api_block_until = 0.0
            _nse_endpoint_fail.pop(endpoint, None)
            data = resp.json()
            if isinstance(data, dict):
                _redis_set(cache_key, data, ttl)
                return data
        else:
            _pause = _nse_api_block_seconds()
            if resp.status_code in (401, 403, 429) and _pause > 0:
                _nse_api_block_until = time.time() + _pause
                logger.warning(
                    "NSE API %s returned %s after a session refresh — pausing all NSE api fetches for %ds "
                    "(skipped %d calls during the previous pause)",
                    endpoint, resp.status_code, int(_pause), _nse_api_block_skipped,
                )
                _nse_api_block_skipped = 0
            elif resp.status_code == 404 or resp.status_code >= 500:
                _nse_endpoint_note_failure(endpoint, f"HTTP {resp.status_code}")
            else:
                logger.warning(f"NSE API {endpoint} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Failed to fetch {endpoint}: {e}")
        _nse_endpoint_note_failure(endpoint, type(e).__name__)
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

# 2026-09-04 (session19 FUT-contamination fix, entry #95): F&O futures/
# options contracts (e.g. "APLAPOLLO29SEP26FUT", "ANGELONE29SEP26FUT",
# "BANKNIFTY29SEP2648000CE") leaked into the equity movers list because no
# filter here — or in symbol_aliases.py — ever checked for the NSE F&O
# naming convention (base symbol + dd-MMM-yy contract date + FUT/strike+CE
# or PE). Same fast-path treatment as _INDEX_PSEUDO_TOKENS below: checked
# BEFORE falling through to symbol_aliases.is_non_equity_instrument() (which
# now also carries this same regex, see that module) so the guard holds
# even in the except-fallback branch further down that returns the raw
# symbol unfiltered if the symbol_aliases import itself fails.
_DERIVATIVE_CONTRACT_RE = re.compile(
    r"\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}"
    r"(FUT|\d+(\.\d+)?(CE|PE))$"
)

# 2026-10-04 (startup-log audit): ETFs / index funds trade in the NSE cash segment as plain "EQ"
# series, so AngelOne's whole-market scrip master (the source of /angelone/movers) includes them,
# and illiquid ones with a stale previous close show up as >=5% "movers" (COMMOIETF, NV20IETF,
# NIFTY1 were then quoted + news-analysed by every stockky-hot run). They are not single-stock
# picks. Conservative suffix/prefix patterns only: *ETF, *BEES (NIFTYBEES, GOLDBEES, ...), SBI's
# SETF* series, and NIFTY/BANKNIFTY index-style tickers. Applied ONLY in the universe builders
# (_clean_equity_symbol); symbol_aliases.is_non_equity_instrument is intentionally untouched so a
# user can still run /stock/<ETF> by hand.
# group 188 (item 9): also liquid / gilt funds (LIQUIDPLUS, LIQUIDSBI, HDFCLIQUID, GSEC10YEAR), SILVERADD / GOLDCASE
# style names, and the ETF names that carry no ETF-looking word (BANKBETA, MONQ50, AONESILVER, GROWWMETAL, ...).
# Same patterns as market-data-service/instrument_filter.py and position-stocks-service/feed/instrument_filter.py.
_ETF_INDEX_FUND_SYMBOL_RE = re.compile(
    r"(?:ETF|BEES)$|^SETF[A-Z0-9]|^(?:NIFTY|BANKNIFTY)\d*$"
    r"|LIQUID|GSEC|^(?:SILVER|GOLD)(?:ADD|CASE)$"
    r"|^(?:BANKBETA|MONQ50|AONESILVER|GROWWMETAL|MASPTOP50|MAFANG|MON100|MAHKTECH|MOM100|MOM50|MIDSMALL|N100)$"
)


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
    if _DERIVATIVE_CONTRACT_RE.search(s):
        return None
    if _ETF_INDEX_FUND_SYMBOL_RE.search(s):
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
    # 2026-10-04 (log-audit item 22): tell "unreachable" (no usable response at
    # all) apart from "reachable but empty" (HTTP 200 with a body that carries no
    # symbol rows). The old text said "unreachable" for both, which was misleading
    # when NSE answered 200 with zero rows.
    _nse_reachable = isinstance(data, dict)
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
            if _nse_reachable:
                logger.warning(
                    "NSE live API answered but returned 0 securities rows — using "
                    "bhavcopy universe fallback (%d symbols)", len(symbols)
                )
            else:
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
        #
        # BUG FIX (2026-09-03, Groww "Volume shockers"/"Intraday Screener"
        # screenshots session): this list was frozen at large, long-listed
        # NIFTY names only. Several of TODAY's real high-percentage movers
        # from the user's screenshots — GROWW (Billionbrains Garage
        # Ventures), PINELABS, URBANCO (Urban Company), SWIGGY, OLAELEC
        # (Ola Electric Mobility), ATHERENERG (Ather Energy), FIRSTCRY
        # (Brainbees Solutions), VENTIVE (Ventive Hospitality), SHREEJISPG
        # (Shreeji Shipping Global), NETWEB (Netweb Technologies India) —
        # are all 2024-2025-listed names that did not exist when this list
        # was first written, so in the absolute-worst-case path (NSE live
        # AND bhavcopy both down) the scan universe couldn't have surfaced
        # any of them even by luck. Tickers verified against NSE listing
        # circulars/exchange data before adding — this is a safety-net
        # list, not the primary universe source, so it only matters when
        # everything else has already failed, but it should still be a
        # real sample of what's actually trading, not a frozen 2023 list.
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
            # Recently-listed, high-turnover names (added 2026-09-03) —
            # confirmed NSE symbols, not guesses.
            "GROWW", "PINELABS", "URBANCO", "SWIGGY", "OLAELEC",
            "ATHERENERG", "FIRSTCRY", "VENTIVE", "SHREEJISPG", "NETWEB",
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
    # Same equity filter as every other universe source: index/derivative/renamed/
    # delisted names are dropped (or mapped) here, not only in the final clean pass.
    symbols = _filter_equities(symbols)  # cleans + dedupes, preserving order
    if not symbols:
        # §3: try ipo_scanner's independent calendar source before dropping to static list
        try:
            from ipo_scanner import fetch_ipoalerts_calendar
            alt_rows = fetch_ipoalerts_calendar()
            symbols = _filter_equities([r.get("symbol") for r in (alt_rows or []) if r.get("symbol")])
            if symbols:
                logger.info("_get_recent_ipos: ipoalerts fallback returned %d symbols", len(symbols))
        except Exception as e:
            logger.warning("ipoalerts fallback for recent-ipos failed: %s", e)

    if not symbols:
        # BUG FIX (2026-09-03): this fallback (used only when both the
        # live public-past-issues endpoint AND the ipoalerts fallback
        # fail) had gone stale — it was missing most of the actual
        # 2025-2026 IPO cohort. Confirmed real NSE symbols added below
        # (verified against listing circulars, not guessed) so the IPO
        # Tracker's worst-case path still surfaces genuinely recent
        # listings instead of an outdated, unverifiable list.
        symbols = [
            "JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO",
            "RAILTEL", "IRFC", "MVELECTRO",
            "GROWW", "PINELABS", "URBANCO", "VENTIVE", "SHREEJISPG",
            "FIRSTCRY", "SWIGGY", "OLAELEC", "ATHERENERG", "NETWEB",
        ]
        logger.warning("_get_recent_ipos: using static fallback list (%d names)", len(symbols))
    return symbols

# ── Rotating coverage cursor for the general-pool momentum-mover fallback ───
# (2026-09-01) A plain random.sample() every cycle gives no guarantee that
# any particular symbol outside the NIFTY indices is ever actually checked —
# purely by chance it could be skipped for dozens of cycles running. This
# cursor instead walks the (stably sorted) general_pool in fixed-size slices,
# wrapping around and reshuffling the visit order only once a full lap
# completes, so every symbol gets examined exactly once every
# ceil(len(general_pool) / _GENERAL_POOL_SAMPLE_SIZE) cycles — a bounded,
# known worst case instead of an open-ended one.
_GENERAL_POOL_SAMPLE_SIZE = 150
_general_pool_cursor_lock = threading.Lock()
_general_pool_order: List[str] = []
_general_pool_order_key: Optional[tuple] = None
_general_pool_pos = 0


def _next_general_pool_slice(general_pool: List[str], sample_size: int) -> List[str]:
    global _general_pool_order, _general_pool_order_key, _general_pool_pos
    if not general_pool:
        return []
    with _general_pool_cursor_lock:
        # Fingerprint the whole pool, not just (len, first, last): a pool of the same
        # size and endpoints with swapped interior symbols must reshuffle too.
        key = (len(general_pool), hash(tuple(general_pool)))
        if _general_pool_order_key != key:
            # Pool composition changed (fresh NSE securities list, or first
            # call this process) — reshuffle once and restart the cursor.
            # Randomizing the ORDER (not the per-call sample) still avoids
            # always starting from the same A-prefix names, while keeping
            # full-lap coverage guaranteed.
            _general_pool_order = list(general_pool)
            random.shuffle(_general_pool_order)
            _general_pool_order_key = key
            _general_pool_pos = 0
        n = len(_general_pool_order)
        size = min(sample_size, n)
        start = _general_pool_pos % n
        end = start + size
        if end <= n:
            out = _general_pool_order[start:end]
        else:
            out = _general_pool_order[start:] + _general_pool_order[: end - n]
        _general_pool_pos = end % n
        return out


_angelone_movers_warned_at = [0.0]


# ── group189 (item 12): single-flight for the momentum-movers computation ───────────────────────────
# The 2026-10-06 boot log ran the whole NSE gainers / losers / volume-gainers / NIFTY 500 / "momentum_movers step1"
# sequence twice at the same moment: the result cache is only filled when a computation FINISHES, so two callers
# that both arrive while it is empty (the startup pre-warm and the first /scan/universe, say) each paid for a full
# pass (4 NSE board fetches, the AngelOne whole-market sweep, the bulk yfinance fallback). Now the first caller
# computes and later callers that arrive meanwhile wait for its answer. A follower that waits longer than
# MOMENTUM_MOVERS_JOIN_WAIT_S (default 45) or finds the leader failed computes for itself, as before; a re-entrant
# call from the computing thread never waits on itself. MOMENTUM_MOVERS_SINGLE_FLIGHT=0 restores one pass per caller.
_MM_FLIGHT_LOCK = threading.Lock()
_mm_flight: Optional["_MomentumFlight"] = None


class _MomentumFlight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[List[str]] = None
        self.ok = False
        self.thread_id = threading.get_ident()


def _momentum_single_flight_cfg() -> tuple:
    on = (os.getenv("MOMENTUM_MOVERS_SINGLE_FLIGHT") or "").strip().lower() not in ("0", "false", "no", "off")
    try:
        wait = float((os.getenv("MOMENTUM_MOVERS_JOIN_WAIT_S") or "").strip() or "45")
    except ValueError:
        wait = 45.0
    if wait != wait or wait < 0:
        wait = 45.0
    return on, wait


# group203: set once a movers pass has produced a non-empty list in this process. Until then (cold start) a
# /scan/universe caller waits SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S instead of the full movers deadline.
_MOVERS_EVER_READY = False


def _note_movers_ready(out) -> None:
    global _MOVERS_EVER_READY
    if isinstance(out, list) and out:
        _MOVERS_EVER_READY = True


def _get_momentum_movers() -> List[str]:
    """Real-time movers (see _compute_momentum_movers). Concurrent callers share one computation."""
    global _mm_flight
    cached = _redis_get(MOMENTUM_MOVERS_CACHE_KEY)
    if isinstance(cached, list) and cached:
        _note_movers_ready(cached)
        return cached
    on, wait = _momentum_single_flight_cfg()
    if not on:
        out = _compute_momentum_movers()
        _note_movers_ready(out)
        return out
    with _MM_FLIGHT_LOCK:
        flight = _mm_flight
        leader = flight is None
        if leader:
            flight = _mm_flight = _MomentumFlight()
    if not leader:
        if flight.thread_id == threading.get_ident():
            return _compute_momentum_movers()          # re-entrant call from the computing thread
        if flight.done.wait(wait) and flight.ok and flight.result is not None:
            logger.debug("momentum_movers: joined the pass already in flight")
            return list(flight.result)
        return _compute_momentum_movers()              # leader failed or is too slow: do it ourselves
    try:
        out = _compute_momentum_movers()
        flight.result = list(out) if isinstance(out, list) else None
        flight.ok = isinstance(out, list)
        _note_movers_ready(out)
        return out
    finally:
        with _MM_FLIGHT_LOCK:
            if _mm_flight is flight:
                _mm_flight = None
        flight.done.set()


def _compute_momentum_movers() -> List[str]:
    """Real-time movers: NSE gainers/losers/most-active + ≥5% day/week moves.

    BUG FIX (2026-09-18): this function had no result cache of its own —
    every one of its ~20 call sites across this file (scan/universe,
    stockky_hot_stocks' background warm-up loop, dashboard endpoints,
    build_scan_universe, etc.) triggered a full fresh recompute each time:
    4 NSE board fetches (each individually cached at ttl=900s inside
    _fetch_from_nse_api, but still 4 round trips per call), one UNCACHED
    AngelOne whole-market quote sweep (~2000-2700 symbols — several real
    seconds even when it succeeds), and sometimes a bulk yfinance fallback.
    Live logs taken right after a redeploy show this recomputing 5+ times
    within a few minutes — once per caller that happened to land in that
    window — each one re-paying the AngelOne sweep cost from scratch. That
    redundant load is what was making /scan/universe (and anything else
    that calls this) slow enough to trip real-trade-service's
    candidate-fetch ReadTimeout, even though api-gateway's own /health
    stayed instant throughout (a cheap route with none of this work).
    Caching the whole result for MOMENTUM_MOVERS_CACHE_TTL seconds means a
    burst of callers in the same window shares one computation instead of
    each paying for their own — mirrors the per-endpoint NSE caching
    already used inside this function, just applied to the function's
    overall output too.
    """
    cached = _redis_get(MOMENTUM_MOVERS_CACHE_KEY)
    if isinstance(cached, list) and cached:
        return cached

    movers: set[str] = set()

    # 1) NSE live boards (best free real-time source when reachable — this
    # is the ONLY source below that is genuinely full-market: gainers/
    # losers/volume-gainers boards are not restricted to any index. If
    # this source is silently blocked/empty, everything downstream
    # degrades to the large-cap-biased sources 2-4, which structurally
    # cannot surface most real smallcap/midcap volume-shocker moves — so
    # its per-endpoint yield is now logged explicitly (31-Aug-2026) so a
    # future silent NSE-block is diagnosable from logs instead of only
    # showing up as a generic "Momentum movers collected: N" line that
    # can't distinguish "genuinely quiet market" from "NSE blocked us".
    nse_live_before = len(movers)
    for endpoint, key in (
        ("live-analysis-variations?index=gainers", "nse:gainers"),
        ("live-analysis-variations?index=losers", "nse:losers"),
        ("live-analysis-variations?index=volume-gainers", "nse:vol_gainers"),
        ("equity-stock-indices?index=NIFTY%20500", "nse:nifty500_idx"),
    ):
        pre = len(movers)
        n_rows = 0          # group202: rows the board returned
        n_small = 0         # ... of which a real move under 2% (valid, just not a mover)
        try:
            data = _fetch_from_nse_api(endpoint, key, ttl=900)
            if data is None:
                logger.warning("NSE movers %s: no data (blocked/unreachable?)", endpoint)
            rows = []
            if isinstance(data, dict):
                rows = data.get("data") or data.get("NIFTY") or []
                if isinstance(rows, dict):
                    rows = rows.get("data") or []
            if isinstance(rows, list):
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    n_rows += 1
                    sym = (item.get("symbol") or item.get("symbolName") or "").upper()
                    sym = _clean_equity_symbol(sym)
                    if not sym:
                        continue
                    # Prefer names with meaningful move when field present
                    # First field that is actually present — a numeric 0 / 0.0 is a real
                    # (flat) value, not a missing one.
                    chg = None
                    for _k in ("pChange", "perChange", "change_pct"):
                        _v = item.get(_k)
                        if _v is not None and _v != "":
                            chg = _v
                            break
                    try:
                        chg_f = float(chg) if chg is not None else None
                    except (TypeError, ValueError):
                        chg_f = None
                    if chg_f is None or abs(chg_f) >= 2.0:
                        movers.add(sym.replace("&", "").replace("-", "") if False else sym)
                    else:
                        n_small += 1
        except Exception as e:
            logger.debug("NSE movers %s: %s", endpoint, e)
        # group202: "+0 symbols" used to look the same whether NSE returned nothing or a full board of
        # quiet stocks; the row counts tell the two apart.
        logger.info("NSE movers %s: +%d symbols (rows=%d, under 2%% move=%d)",
                    endpoint, len(movers) - pre, n_rows, n_small)
    logger.info(
        "momentum_movers step1 (NSE live boards, full-market): %d symbols",
        len(movers) - nse_live_before,
    )

    # 1b) AngelOne whole-market LTP sweep (2026-09-04, corrected).
    #
    # This is the fix for the yfinance seed scan in step 4 below being a
    # rate-limit/latency risk of its own: yf.download() on a ~180-symbol
    # rotating seed, every cycle it ran, from a cloud IP, with no official
    # bulk-pull rate-limit contract from Yahoo — a slower-onset version of
    # the exact same "unauthenticated cloud IP hitting a market-data
    # provider" problem NSE's Akamai block already causes in step 1.
    #
    # NOTE: an earlier version of this step called AngelOne's
    # marketData/v1/gainersLosers endpoint directly — that turned out to be
    # F&O-derivatives-scoped only (confirmed against AngelOne's own SmartAPI
    # docs), not a cash-equity board, so it's been replaced. market-data-
    # service's /angelone/movers now sweeps the FULL NSE-EQ token universe
    # (~2000 symbols, via angelone_scrip_master + the same authenticated
    # quote batch endpoint already used for REAL-mode trading) and computes
    # day_change_pct itself from ltp vs previous close — genuinely
    # whole-market, not F&O-scoped, not NSE-block-exposed, not
    # Yahoo-rate-limit-exposed. Runs ALWAYS (unconditionally, like step 1),
    # not gated behind step 1 failing, so it's a genuine co-primary source.
    payload = None
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/angelone/movers", timeout=25.0)
        if resp.status_code == 200:
            payload = resp.json()
            pre = len(movers)
            for row in (payload.get("data") or []):
                if not isinstance(row, dict):
                    continue
                sym = _clean_equity_symbol((row.get("symbol") or "").upper())
                if sym:
                    movers.add(sym)
            logger.info(
                "AngelOne movers: +%d symbols (status=%s, universe=%s, quotes_fetched=%s)",
                len(movers) - pre, payload.get("status"),
                payload.get("universe_size"), payload.get("quotes_fetched"),
            )
        else:
            logger.debug("AngelOne movers: HTTP %d", resp.status_code)
        # 2026-10-06 (group 178): a status=error / not_configured answer (e.g. AngelOne login 403) came back as
        # HTTP 200 and was ignored here with no log line at all. One warning per 10 minutes now says what
        # market-data-service reported.
        if resp.status_code == 200 and isinstance(payload, dict) and payload.get("status") not in (None, "ok"):
            _now_m = time.time()
            if _now_m - _angelone_movers_warned_at[0] >= 600:
                _angelone_movers_warned_at[0] = _now_m
                _why = str(payload.get("error") or payload.get("reason") or "no detail")[:200]
                # group341: a boot-time "still loading" answer is expected, not a fault
                (logger.info if "still loading" in _why else logger.warning)(
                    "AngelOne movers unavailable (status=%s): %s — whole-market movers fall back to the "
                    "other sources",
                    payload.get("status"), _why,
                )
    except Exception as e:
        logger.debug("AngelOne movers fetch failed: %s", e)

    # 2) Gateway's own gainers/losers/most-active (wiring fix, 30-Aug session):
    # this used to make an HTTP round-trip to MARKET_DATA_URL for
    # /market/top-gainers, /market/top-losers, /market/most-active — but
    # those three routes are defined on THIS service (api-gateway), not
    # market-data-service. The call always 404'd against the wrong host and
    # was silently swallowed by the try/except, so this source contributed
    # zero symbols on every run. Calling the local functions directly is
    # both correct and faster (no network round-trip for data already in
    # this process) — and now also benefits from the nifty50-fetch lock +
    # ThreadPoolExecutor fix below, since these all read through
    # _get_nifty50_data().
    try:
        for getter in (market_top_gainers, market_top_losers, market_most_active):
            try:
                payload = getter()
                for item in (payload or {}).get("data") or []:
                    if isinstance(item, dict):
                        raw_sym = item.get("symbol") or item.get("ticker") or ""
                        # Same delisted/index/rename gate as step 1 above —
                        # these boards weren't routed through it before.
                        sym = _clean_equity_symbol(raw_sym)
                        if sym:
                            movers.add(sym)
            except Exception:
                continue
    except Exception as e:
        logger.debug("gateway movers: %s", e)

    # 3) Gateway's own /market endpoints (yfinance-backed) when available
    #
    # BUG FIX (2026-09-01): the guard here was `"_get_nifty50_data" in dir()`
    # — dir() with no arguments returns names in the CURRENT LOCAL SCOPE,
    # not the module's globals, and _get_nifty50_data is never assigned as
    # a local variable inside this function (it's only ever called). So
    # this condition was always False, `data` was always `[]`, and this
    # entire source has contributed zero movers since it was added —
    # silently, since the surrounding try/except also swallows the dead
    # branch without a trace. _get_nifty50_data is a plain module-level
    # function defined earlier in this same file (already called directly,
    # unguarded, elsewhere — see market_top_gainers/losers/most_active
    # above) — call it directly; the existing try/except already protects
    # this whole block from any real failure.
    try:
        data = _get_nifty50_data()
        for row in data or []:
            if not isinstance(row, dict):
                continue
            sym = _clean_equity_symbol(row.get("symbol") or "")
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
    #
    # BUG FIX (31-Aug-2026): the old line was
    #   seed = (_get_nifty_indices() + _get_all_nse_securities()[:120])[:80]
    # `_get_nifty_indices()` alone is already ~250-450 symbols long (NIFTY
    # 50 + NEXT 50 + MIDCAP100 + MIDCAP150 + SMALLCAP100, concatenated IN
    # THAT ORDER). Truncating the *combined* list to the first 80 elements
    # therefore kept almost nothing but NIFTY 50 / NEXT 50 large-caps —
    # the midcap/smallcap tail of _get_nifty_indices() and the entire
    # _get_all_nse_securities()[:120] slice were concatenated on but
    # essentially never reached. That is why real volume-shocker sessions
    # (see 31-Aug-2026 Groww "Volume shockers" screenshots: Ashoka
    # Buildcon, Diffusion Engineers, Aym Syntex, Manali Petrochemicals,
    # Bodal Chemicals, Q-Line Biotech, Modison, Kapston Services, John
    # Cockerill, RPG Life Sciences, Veranda Learning... none of them
    # NIFTY50/NEXT50 names) never reached this fallback scan at all, and
    # since sources 1-3 above are themselves large-cap-biased or
    # NSE-live-API-dependent (frequently 401/403-blocked from datacenter
    # IPs — see _fetch_from_nse_api), this was the *only* remaining path
    # that could have surfaced them, and it was structurally incapable of
    # doing so.
    #
    # Fix: sample explicitly from each market-cap bucket instead of
    # truncating one concatenated list. Mid/smallcap gets the larger
    # share, since that is where the actually-missed movers are, and the
    # general-universe slice is RANDOMIZED (not an alphabetical prefix)
    # so successive scan cycles rotate across the ~2000-symbol NSE list
    # instead of always re-checking the same A-D names.
    nse_live_contributed = len(movers) - nse_live_before
    if nse_live_contributed == 0:
        # Step 1 (the only genuinely full-market source) contributed nothing
        # this cycle. That's either a genuinely quiet market or NSE is
        # blocking this session — the two look identical downstream (a thin
        # `movers` set) unless flagged explicitly here.
        logger.warning(
            "momentum_movers: NSE live boards (gainers/losers/volume-gainers/"
            "nifty500) contributed 0 symbols this cycle — likely blocked "
            "(401/403) rather than a genuinely quiet market. Falling back "
            "entirely to the rotating general-pool sample below, which only "
            "covers a fraction of NSE per cycle."
        )

    if len(movers) < 40:
        nifty_idx_full = _get_nifty_indices()  # NIFTY50+NEXT50+MID100+MID150+SMALL100
        largecap_seed = nifty_idx_full[:20]     # small largecap slice — cheap sanity coverage
        midsmall_seed = nifty_idx_full[20:]     # NEXT50 tail + MID/SMALLCAP — highest value
        random.shuffle(midsmall_seed)
        all_sec = _get_all_nse_securities()
        general_pool = sorted(s for s in all_sec if s not in set(nifty_idx_full))
        # 2026-09-01 fix: this used to be a fresh random.sample() every call,
        # so coverage of the ~2000-symbol general pool was purely probabilistic
        # — a given symbol (e.g. one of the Groww "Volume shockers" names that
        # isn't in any NIFTY index) could go unsampled for many cycles in a
        # row by bad luck, while others got re-checked repeatedly for no
        # benefit. A persistent rotating cursor instead guarantees the WHOLE
        # general_pool is covered exactly once every
        # ceil(len(general_pool) / _GENERAL_POOL_SAMPLE_SIZE) cycles, so real
        # coverage gaps shrink to a bounded, known cycle count instead of
        # "maybe never." Sample size/order otherwise unchanged, so this adds
        # no extra yfinance load per cycle.
        general_sample = _next_general_pool_slice(general_pool, _GENERAL_POOL_SAMPLE_SIZE)
        seed = list(dict.fromkeys(largecap_seed + midsmall_seed[:80] + general_sample))[:180]
        logger.info(
            "momentum_movers fallback seed: %d largecap + %d mid/smallcap-index + "
            "%d general (rotating cursor, full pool=%d) = %d total (nse_live_movers_so_far=%d)",
            len(largecap_seed), min(80, len(midsmall_seed)), len(general_sample),
            len(general_pool), len(seed), len(movers),
        )
        # BUG FIX (2026-09-01, round 15): this used to call yf.Ticker(...).history()
        # once PER SYMBOL, sequentially — up to 180 blocking network round-trips
        # in-process. That's what made GET /scan/universe time out at 20s (the
        # diagnostic script's own client timeout; unthrottled the loop actually
        # runs for minutes). It also bypassed market-data-service's shared
        # yfinance rate limiter/circuit breaker entirely, adding an uncoordinated
        # burst of Yahoo calls on top of whatever market-data-service was already
        # throttling. Fix: delegate to the same POST /quotes/bulk endpoint the
        # rest of this codebase already uses for exactly this problem (see
        # bulk_yahoo_download_prices / run_bulk_yahoo_price_feed) — ONE
        # yf.download() call for the whole seed, routed through the existing
        # rate limiter, with kv-cache/live-feed short-circuiting for anything
        # already fresh. Trade-off: /quotes/bulk only carries today's
        # day_change_pct, not 5-day history, so the week_chg>=5% leg is dropped
        # here — day_chg alone is still the dominant volume-shock signal.
        try:
            from data_feed import bulk_yahoo_download_prices
            bulk_quotes = bulk_yahoo_download_prices(seed)
        except Exception as e:
            logger.warning("momentum_movers bulk quote fetch failed: %s", e)
            bulk_quotes = {}
        for sym in seed:
            q = bulk_quotes.get(sym)
            if not isinstance(q, dict):
                continue
            try:
                day_chg = float(q.get("day_change_pct"))
            except (TypeError, ValueError):
                continue
            if abs(day_chg) >= 5.0:
                movers.add(sym)

    out = sorted(movers)
    logger.info("Momentum movers collected: %s symbols", len(out))
    try:
        _redis_set(MOMENTUM_MOVERS_CACHE_KEY, out, ttl=MOMENTUM_MOVERS_CACHE_TTL)
    except Exception as e:
        logger.debug("momentum movers cache set failed (non-fatal): %s", e)
    return out

# Tickers that are also ordinary English words / exchange names. The news scan
# upper-cases the whole feed text, so e.g. "consumer demand", "chemical sector",
# "oil prices", "clean energy" or "BSE Sensex" matched the tickers CONSUMER /
# CHEMICAL / OIL / CLEAN / BSE and pushed them into the hot-picks universe (seen
# in the 2026-10-04 startup log: /news/analyze/CONSUMER, CHEMICAL, CONS, CLEAN,
# AFFORDABLE, APOLLO, OIL). For these the mention only counts when the ORIGINAL
# text spells the ticker in ALL CAPS (how a ticker is written in a headline), not
# when it is a lower/Title-case dictionary word.
_NEWS_AMBIGUOUS_WORD_SYMBOLS = frozenset({
    "CONSUMER", "CHEMICAL", "CHEMICALS", "CLEAN", "CONS", "AFFORDABLE",
    "APOLLO", "OIL", "FACT", "GATEWAY", "ELEVATE", "ENDURANCE", "CELEBRITY",
})
# Never taken from news text at all: "BSE"/"NSE" in these feeds almost always
# means the exchange (the queries themselves contain them), not the listed stock.
_NEWS_EXCLUDED_SYMBOLS = frozenset({"BSE", "NSE"})
# ETF / index-instrument tickers (COMMOIETF, NV20IETF, NIFTY1, ...) are not
# single-stock news targets.
_NEWS_ETF_INDEX_SYMBOL_RE = _ETF_INDEX_FUND_SYMBOL_RE


def _news_symbol_is_ambiguous(sym: str) -> bool:
    return sym in _NEWS_AMBIGUOUS_WORD_SYMBOLS


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
        raw_text = " ".join(text_parts)
        text = raw_text.upper()
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
            if sym in _NEWS_EXCLUDED_SYMBOLS or _NEWS_ETF_INDEX_SYMBOL_RE.search(sym):
                continue
            # Whole-token match for every symbol length: "IDEA" must not match "IDEAS" and
            # "TITAN" must not match "TITANIUM", while punctuation next to a ticker
            # ("TCS," / "(ITC)") still counts as a boundary.
            haystack = raw_text if _news_symbol_is_ambiguous(sym) else text
            if re.search(r"(?<![A-Za-z0-9&])" + re.escape(sym) + r"(?![A-Za-z0-9&])", haystack):
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

    # Wiring audit follow-up (30-Aug session, round 2): this "extra" block
    # called {MARKET_DATA_URL}/events/active-symbols, which — like the
    # /market/bulk-deals and /market/near-52w-high paths fixed earlier in
    # this same audit — does not exist on market-data-service or anywhere
    # else in the codebase. Always 404'd, silently caught, contributed
    # nothing. The /symbols_with_events call above (against the actual
    # event microservice) is the real, working source for this function;
    # removed the dead fallback rather than leave a second copy of the
    # same class of bug in the repo. A market-wide "active symbols with
    # corporate actions" endpoint would need to be built on market-data-
    # service or event-service first — that's a feature gap, not a fix.

    out = []
    seen = set()
    for s in symbols:
        # Delisted/index/rename gate — EVENT_URL's corporate-events feed can
        # reference a symbol that's since delisted or merged; nothing here
        # previously checked that before it entered the scan universe.
        su = _clean_equity_symbol(s)
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
        # Route through _clean_equity_symbol (delisted/index/rename gate) —
        # this source previously only normalized case/suffix, which is why
        # a delisted or merged-away symbol on NSE's bulk/block-deals board
        # could reach the scan universe untouched. See the delisted-gate
        # comment in _build_scan_universe() for the full root-cause writeup.
        s = _clean_equity_symbol(sym)
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

    # Wiring fix (30-Aug session): sources 2 and 3 used to call
    # {MARKET_DATA_URL}/market/bulk-deals, /market/block-deals,
    # /nse/bulk-deals, and {EVENT_URL}/bulk_deals. None of these routes
    # are implemented on market-data-service or the event microservice —
    # neither host exposes a market-wide bulk/block-deals list (the event
    # service only has bulk_deals nested inside its per-symbol
    # /events/{symbol} response, which requires already knowing the
    # symbol — not useful for *discovering* symbols). Both blocks always
    # 404'd and were silently caught, burning up to ~40s of timeout
    # budget for zero rows on every run. Removed rather than faked; the
    # NSE-direct source above is the only real source until/unless one of
    # those services actually grows a market-wide bulk-deals endpoint
    # (that's a feature addition, not a wiring fix — flagging for
    # separate follow-up, not silently working around it here).

    logger.info("Bulk/block deal symbols: %s", len(out))
    return out[:100]


def _get_52w_extreme_symbols() -> List[str]:
    """Symbols near 52-week high/low or with sharp multi-day moves (surprise raise)."""
    out: List[str] = []
    seen = set()

    def _add(sym: str):
        # Same delisted/index/rename gate as _get_bulk_deal_symbols._add —
        # this 52w/gainers source had the identical gap.
        s = _clean_equity_symbol(sym)
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    # Wiring fix (30-Aug session): this loop used to hit six paths on
    # MARKET_DATA_URL. Four of them (/market/near-52w-high,
    # /market/near-52w-low, /market/52-week-high, /market/52-week-low)
    # are not implemented anywhere in the codebase — not on
    # market-data-service, not on api-gateway, nowhere — so they always
    # 404'd. The other two (/market/top-gainers, /market/most-active) ARE
    # real, but they live on api-gateway itself, not market-data-service,
    # so calling them via MARKET_DATA_URL also always 404'd. Net effect:
    # this entire block contributed zero symbols on every single run,
    # silently, with the fallback below doing all the real work. Fixed by
    # calling the two real, locally-available functions directly (no
    # network round-trip) and dropping the four endpoints that don't
    # exist — implementing a real 52-week-high/low board is a genuine
    # feature gap, not a wiring bug, and is a separate piece of work.
    for getter in (market_top_gainers, market_most_active):
        try:
            payload = getter()
            for item in (payload or {}).get("data") or []:
                if isinstance(item, dict):
                    chg = item.get("change_pct") or item.get("pChange") or item.get("pctChange")
                    try:
                        chg_f = abs(float(chg)) if chg is not None else None
                    except (TypeError, ValueError):
                        chg_f = None
                    if chg_f is None or chg_f >= 3.0:
                        _add(item.get("symbol") or item.get("Symbol") or item.get("ticker"))
        except Exception as e:
            logger.debug("52w/movers %s: %s", getter.__name__, e)

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
SCAN_UNIVERSE_TARGET = int(((os.getenv("SCAN_UNIVERSE_TARGET") or "").strip() or "500"))
SCAN_UNIVERSE_HARD_CAP = int(((os.getenv("SCAN_UNIVERSE_HARD_CAP") or "").strip() or "5000"))


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
    if not isinstance(row, dict):
        return False  # nothing to read a price from (the by-name check above already ran)
    for k in ("price", "close", "cmp", "ltp", "last_price", "current_price"):
        # Guarded per key: one unparseable value must not hide a valid price listed after it.
        try:
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
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px > MAX_UNIVERSE_PRICE
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


# ── group196: the "background rebuild" after a stale-served universe never rebuilt anything ─────────────
# _build_scan_universe() serves the durable stale copy (SCAN_UNIVERSE_STALE_KEY) whenever the live key is cold, and
# its comment promised "background rebuild will follow". Nothing ever did: the startup warm, the cached=true route's
# fire-and-forget rebuild and every later caller all went through the SAME function, which found the stale copy
# (kv get_stale ignores the TTL, and only a real build rewrites that key) and returned it again. So once one stale copy
# existed the universe was only re-served, never rebuilt, and the first /scan/universe after a restart ran on the
# previous session's names. Now a stale serve schedules ONE real rebuild (single-flight, daemon thread); the rebuild
# skips the live/stale shortcuts. SCAN_UNIVERSE_STALE_REFRESH=0 restores the old serve-only behaviour.
_UNIVERSE_REFRESH_LOCK = threading.Lock()
_universe_refresh_ctx = threading.local()
SCAN_UNIVERSE_MIN_REBUILD_SYMBOLS = 50     # a rebuild thinner than this never overwrites the stored universe


def _stale_universe_refresh_enabled() -> bool:
    return (os.getenv("SCAN_UNIVERSE_STALE_REFRESH") or "").strip().lower() not in ("0", "false", "no", "off")


def _build_scan_universe_fresh():
    """A REAL rebuild: skips the live-cache and stale-copy shortcuts of _build_scan_universe().

    Single-flight: returns None at once when another real rebuild is already running."""
    if not _UNIVERSE_REFRESH_LOCK.acquire(blocking=False):
        return None
    _universe_refresh_ctx.on = True
    try:
        return _build_scan_universe()
    finally:
        _universe_refresh_ctx.on = False
        _UNIVERSE_REFRESH_LOCK.release()


def _schedule_scan_universe_refresh() -> bool:
    """Start a background real rebuild unless one is running or the switch is off. Never raises."""
    try:
        if not _stale_universe_refresh_enabled() or _UNIVERSE_REFRESH_LOCK.locked():
            return False

        def _run() -> None:
            try:
                _build_scan_universe_fresh()
            except Exception as e:
                logger.warning("scan_universe: background rebuild failed (non-fatal): %s", e)

        threading.Thread(target=_run, name="scan-universe-refresh", daemon=True).start()
        return True
    except Exception as e:
        logger.debug("scan_universe: background rebuild not scheduled: %s", e)
        return False


def _build_scan_universe_forced() -> List[str]:
    """force_refresh=true: a REAL rebuild for this caller (group196b).

    The force_refresh paths drop the live key and call _build_scan_universe(), which then found the durable stale copy and
    re-served it, so "force refresh" never refreshed the universe. A real rebuild that is already running (single
    flight, `None`) or that returns nothing falls back to the plain call, which waits on / serves the normal way."""
    out = _build_scan_universe_fresh()
    return out if out else _build_scan_universe()


# ── Build scan universe ──────────────────────────────────────────────────────
def _build_scan_universe() -> List[str]:
    # ── 2026-09-18 fix: stale-serve on cold-start ─────────────────────────
    # Before this fix _build_scan_universe() had a 30-min in-market TTL on
    # SCAN_UNIVERSE_KEY.  After a container restart the memory cache is wiped
    # and Neon's copy may also be expired, so every first call of the day paid
    # the full synchronous rebuild cost (~20-30s) — long enough to trip
    # real-trade-service's 25s client timeout and lose the whole first cycle.
    #
    # Fix: if the normal (TTL-respecting) cache miss occurs, try a *stale* read
    # from Neon (kv_cache.kv_get_stale — ignores expires_at). If we get
    # anything back, serve it immediately AND set SCAN_UNIVERSE_STALE_SERVED so
    # the background pre-warm in _warm_momentum_movers_cache() (startup hook)
    # knows to kick off a real rebuild.  The stale result is always < 6h old
    # (off-market TTL) so the data is perfectly usable for the first cycle.
    # Real-trade-service gets a fast response; the rebuild runs concurrently.
    #
    # The stale copy is also written with a very long TTL (4h) every time a
    # fresh universe is built (see the _redis_set call near the end of this
    # function) under SCAN_UNIVERSE_STALE_KEY, kept separate from the short-TTL
    # live key so a hard cache-clear never wipes the safety net.

    refresh = bool(getattr(_universe_refresh_ctx, "on", False))      # group196: real rebuild, no shortcuts
    cached = None if refresh else _redis_get(SCAN_UNIVERSE_KEY)
    if cached and isinstance(cached, list) and len(cached) > 0:
        # Always re-apply the delisted/index gate too, not just price — a symbol
        # can get added to KNOWN_DELISTED (symbol_aliases.py) *after* this cache
        # was written, and without this the stale cache would keep serving it
        # for its full TTL instead of dropping it on the next rebuild.
        cached = _filter_equities(cached)
        # Always re-apply ≤₹5000 gate so a stale cache cannot reintroduce high-ticket names
        return _filter_symbols_under_max_price(cached)

    # Live cache miss — try stale Neon read before paying full rebuild cost
    if _kv_cache is not None and not refresh:
        try:
            stale = _kv_cache.get_stale(SCAN_UNIVERSE_STALE_KEY)
            if stale and isinstance(stale, list) and len(stale) > 0:
                stale = _filter_equities(stale)
                stale = _filter_symbols_under_max_price(stale)
                if len(stale) >= 50:  # sanity: must be a real universe, not a stub
                    logger.info(
                        "scan_universe: live cache cold — serving %d-symbol stale copy "
                        "from the durable DB (background rebuild will follow)",
                        len(stale),
                    )
                    # Write it back into the live cache with a short TTL so the
                    # next caller inside the same cycle doesn't also hit Neon,
                    # AND so the background rebuild naturally overwrites it soon.
                    _redis_set(SCAN_UNIVERSE_KEY, stale, ttl=300)
                    _schedule_scan_universe_refresh()      # group196: the promised background rebuild
                    return stale
        except Exception as _stale_e:
            logger.debug("scan_universe stale-read failed (non-fatal): %s", _stale_e)

    universe = set()
    try:
        all_stocks = _get_all_nse_securities()
        if all_stocks:
            # BUG FIX (31-Aug-2026): _get_all_nse_securities() returns NSE's
            # "SECURITIES IN NSE" list in NSE's own (effectively alphabetical)
            # order. Slicing the first SCAN_UNIVERSE_TARGET elements off that
            # list — as this used to do — meant the non-dynamic base of the
            # scan universe was always the same ~500 early-alphabet names
            # (A-D-ish) every single cycle, structurally starving anything
            # from later in the alphabet of a slot unless a dynamic source
            # (movers/news/bulk-deals/etc.) happened to surface it. Same bug
            # class already fixed in _get_momentum_movers()'s fallback seed
            # (see the 31-Aug-2026 note there) — fixed here the same way:
            # shuffle a copy before truncating, so the base slice rotates
            # across the full ~2000-symbol NSE list cycle to cycle instead
            # of always favoring early letters.
            shuffled_stocks = list(all_stocks)
            random.shuffle(shuffled_stocks)
            universe.update(shuffled_stocks[:SCAN_UNIVERSE_TARGET])
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

    # ── Delisted/merged gate — applied ONCE here for every source ──────────
    # ROOT CAUSE (why delisted names were reaching the scan universe):
    # _get_all_nse_securities() and _get_nifty_indices() already ran every
    # symbol through _clean_equity_symbol() (which checks is_known_delisted()
    # from symbol_aliases.py), but that gate was applied per-source, not
    # centrally. _get_bulk_deal_symbols(), _get_52w_extreme_symbols() and
    # _get_event_symbols() built their output with a bare uppercase/suffix
    # normalizer (_add() in each function) and never called
    # _clean_equity_symbol() at all — so a delisted or merged-away ticker
    # surfacing through NSE's bulk-deal board, a 52-week-high/gainers scrape,
    # or the corporate-events feed sailed straight into `universe` with zero
    # eligibility check. _get_momentum_movers() had the same gap in its
    # steps 2-3 (gateway's own gainers/losers/most-active, and the
    # yfinance seed's `movers.add(sym)` calls). Watchlist/searched entries
    # were never checked either. This loop is the single point every source
    # funnels through before scoring, so gating HERE (via _clean_equity_symbol,
    # the same function _get_all_nse_securities already trusts) closes the
    # gap for all current sources and any future one, instead of patching
    # each source's _add() helper individually and risking the next new
    # source repeating the same omission.
    clean = []
    seen = set()
    dropped_delisted = 0
    for s in universe:
        c = _clean_equity_symbol(s)
        if c is None:
            if str(s or "").strip():
                dropped_delisted += 1
            continue
        if c not in seen:
            seen.add(c)
            clean.append(c)
    if dropped_delisted:
        logger.info(
            "Scan universe: dropped %d delisted/merged/invalid symbols before build",
            dropped_delisted,
        )

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
        # Same alphabetical-bias bug as the base-universe slice above — pad
        # candidates were pulled off _get_all_nse_securities() in NSE's
        # native order, so a "thin sources" cycle always padded with the
        # same early-alphabet names first. Shuffle before taking the pad
        # slice for the same rotating-coverage reason.
        try:
            pad = [s for s in _get_all_nse_securities() if s not in set(ordered)]
        except Exception as _pe:
            # A failing securities lookup must not discard the universe that
            # was already built — just skip the padding.
            logger.warning("universe pad skipped: securities lookup failed: %s", _pe)
            pad = []
        random.shuffle(pad)
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
        # Group 141: an NSE weekday holiday is a closed day too (30 min TTL would rebuild the universe
        # all day for nothing). A failed calendar lookup keeps the weekday-only answer.
        try:
            if is_weekday and is_nse_holiday(now.date()):
                is_weekday = False
        except Exception:
            pass
        mins = now.hour * 60 + now.minute
        market_open = is_weekday and (9 * 60 + 15) <= mins <= (15 * 60 + 30)
        ttl = 1800 if market_open else 21600  # 30m vs 6h
    except Exception:
        ttl = 3600
    result = _filter_symbols_under_max_price(result)
    if refresh and len(result) < SCAN_UNIVERSE_MIN_REBUILD_SYMBOLS:
        # group196: a thin rebuild (NSE blocked, sources empty) must not replace the stored universe
        logger.warning("scan_universe: background rebuild produced only %d symbol(s) - kept the stored universe",
                       len(result))
        return result
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=ttl)
    # 2026-09-18 fix: also write a long-lived stale-fallback copy so a restart
    # that finds the 30-min live key expired can still serve something fast
    # instead of doing a full synchronous rebuild.  4h TTL covers any realistic
    # gap between a forced restart and the next scheduled background rebuild.
    # This key is in kv_cache._DURABLE_PREFIXES ("stockky:scan_universe"
    # prefix) so it lands in Neon and survives process restarts.
    try:
        _redis_set(SCAN_UNIVERSE_STALE_KEY, result, ttl=14400)  # 4h
    except Exception as _se:
        logger.debug("scan_universe stale-key write failed (non-fatal): %s", _se)
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
    except Exception as e:
        logger.debug("known-symbols: _get_all_nse_securities lookup failed: %s", e)
    # Each source is guarded on its own so one failing source degrades the set
    # instead of making symbol resolution (and every route using it) raise.
    for _src in (_get_nifty_indices, _load_watchlist, _load_searched,
                 _get_recent_ipos, _get_momentum_movers):
        try:
            combined.update(_src())
        except Exception as e:
            logger.debug("known-symbols: %s failed: %s", getattr(_src, "__name__", _src), e)
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

def _squash_symbol(text: str) -> str:
    """Upper-case, drop .NS/.BO and every whitespace character ("hero moters" -> "HEROMOTERS").
    NSE symbols never contain spaces, so a space is always a name typed as words. Hyphens and
    '&' are kept: BAJAJ-AUTO and M&M are real symbols."""
    return "".join((text or "").upper().replace(".NS", "").replace(".BO", "").split())


# A fuzzy (typo) correction is applied silently only when it is this close AND clearly ahead of
# the runner-up; anything weaker is returned to the user as "did you mean" instead of analysing
# a different company than the one they meant.
FUZZY_AUTO_CORRECT_MIN_RATIO = 0.90
FUZZY_AUTO_CORRECT_MIN_GAP = 0.05
FUZZY_SUGGEST_CUTOFF = 0.6


def _symbol_suggestions(typed: str, known, first: Optional[str] = None, limit: int = 3) -> List[str]:
    """Closest known symbols to `typed`, best first; `first` (an already-found match) leads."""
    out: List[str] = [first] if first else []
    try:
        for m in difflib.get_close_matches(typed, list(known or ()), n=limit + 1, cutoff=FUZZY_SUGGEST_CUTOFF):
            if m not in out:
                out.append(m)
    except Exception as e:
        logger.debug("symbol suggestions failed for %s: %s", typed, e)
    return out[:limit]


def _fuzzy_correction_is_confident(typed: str, match: str, known) -> bool:
    """True when `match` is a close enough, unambiguous correction of `typed`."""
    best = difflib.SequenceMatcher(None, typed, match).ratio()
    if best < FUZZY_AUTO_CORRECT_MIN_RATIO:
        return False
    for other in difflib.get_close_matches(typed, list(known or ()), n=3, cutoff=FUZZY_SUGGEST_CUTOFF):
        if other != match and best - difflib.SequenceMatcher(None, typed, other).ratio() < FUZZY_AUTO_CORRECT_MIN_GAP:
            return False
    return True


def _resolve_symbol(misspelled: str) -> Optional[str]:
    if not misspelled:
        return None
    symbol = _squash_symbol(misspelled)
    if not symbol:
        return None
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

# ── Degraded (decision engine down) payload ──────────────────────────────
def _degraded_hold(symbol: str, error: str, summary: str, flag: str, note: str) -> dict:
    """Neutral HOLD returned by /stock/{symbol} when the decision engine errors or is unreachable.

    Carries the same neutral score / price fields the normal path always has (50 for the sub-scores,
    0 combined, None for close/entry/target/stop) so a UI reading `technical_score` or `close` gets a
    neutral value instead of `undefined`. `data_insufficient: True` + the low-quality flag still mark it
    as not a real analysis."""
    return {
        "ok": True,
        "symbol": symbol,
        "decision": "HOLD",
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
        "reasons": {"technical": ["Data unavailable"], "fundamental": ["Data unavailable"]},
        "data_insufficient": True,
        "error": error,
        "natural_language_summary": summary,
        "data_quality": {"level": "low", "flags": [flag], "note": note},
    }


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
# Moved to pick_ranking.py (group 320, first step of splitting this file); names re-exported here unchanged.
from pick_ranking import (  # noqa: E402,F401
    VALUE_PRICE_CAP, VALUE_BONUS_MAX, VALUE_MIN_FUNDAMENTAL_FOR_BONUS,
    _value_adjusted_score, _select_top_picks, _board_rank_key,
)

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


# group153 (market-data overload): this helper used to send one GET /quote/{sym} per symbol
# (8 at a time), so every hot-picks / scan chunk turned into N market-data /quote waterfalls
# even though market-data already serves AngelOne ticks in bulk - and two overlapping callers
# (hot-picks pass 2 + a scan chunk) asked for the same symbols again. Now:
#   * the symbol list is de-duplicated by base symbol;
#   * lists of GATEWAY_BULK_QUOTE_MIN (15) or more are priced with chunked POST /quotes/bulk
#     first; only the symbols bulk could not price use the old per-symbol path;
#   * bulk prices are kept for GATEWAY_BULK_QUOTE_CACHE_S (8 s) so an overlapping caller reuses
#     them instead of asking again, and a bulk row whose fetched_at is older than
#     GATEWAY_BULK_QUOTE_MAX_AGE_S (20 s) is ignored (sent per-symbol instead).
# GATEWAY_BULK_QUOTE_MIN=0 turns the bulk path off (old behaviour, minus the de-duplication).
_GW_BULK_PX_CACHE: dict = {}   # base -> (monotonic_ts, price)
from datetime import timezone as _dt_timezone


def _gw_env_num(name: str, default: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _gw_quote_px(data) -> Optional[float]:
    """First positive price field of a market-data quote dict (same order as the per-symbol path)."""
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
    return None


def _gw_bulk_row_fresh(row: dict, max_age_s: float) -> bool:
    """True when the bulk row's fetched_at parses and is at most max_age_s old (UTC, naive or aware)."""
    raw = row.get("fetched_at")
    if not raw or not isinstance(raw, str):
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt_timezone.utc)
    return 0 <= (datetime.now(_dt_timezone.utc) - ts).total_seconds() <= max_age_s


def _gw_quotes_closed() -> bool:
    """group233: True on weekends, NSE holidays and outside 09:15-16:00 IST, when no live price can change.
    GATEWAY_BULK_QUOTE_CLOSED_AWARE=0 turns the closed-market handling off."""
    if ((os.getenv("GATEWAY_BULK_QUOTE_CLOSED_AWARE") or "").strip() or "1").lower() in ("0", "false", "no", "off"):
        return False
    try:
        return _market_session_phase_ist() in ("closed", "holiday")
    except Exception:  # noqa: BLE001
        return False


async def _fetch_prices_bulk_first(bases: list, client: httpx.AsyncClient) -> dict:
    """Price `bases` through chunked POST /quotes/bulk. Returns {BASE: float} for the ones it could
    price; a failing chunk just leaves its symbols for the per-symbol path (never raises)."""
    out: dict = {}
    now = time.monotonic()
    cache_s = _gw_env_num("GATEWAY_BULK_QUOTE_CACHE_S", 8.0)
    max_age = _gw_env_num("GATEWAY_BULK_QUOTE_MAX_AGE_S", 20.0)
    if _gw_quotes_closed():
        # group233: market-data answers a closed market from the last close, whose fetched_at is hours old.
        # Accept it (and reuse it for a while) instead of throwing it away and asking per symbol.
        max_age = max(max_age, _gw_env_num("GATEWAY_BULK_QUOTE_CLOSED_MAX_AGE_S", 7 * 86400.0))
        cache_s = max(cache_s, _gw_env_num("GATEWAY_BULK_QUOTE_CLOSED_CACHE_S", 600.0))
    chunk = max(1, int(_gw_env_num("GATEWAY_BULK_QUOTE_CHUNK", 50)))
    timeout_s = _gw_env_num("GATEWAY_BULK_QUOTE_TIMEOUT_S", 12.0)
    need = []
    for b in bases:
        hit = _GW_BULK_PX_CACHE.get(b)
        if cache_s > 0 and hit and now - hit[0] <= cache_s:
            out[b] = hit[1]
        else:
            need.append(b)
    sem = asyncio.Semaphore(2)

    async def one_chunk(part: list):
        async with sem:
            try:
                r = await client.post(
                    f"{MARKET_DATA_URL.rstrip('/')}/quotes/bulk",
                    json={"symbols": part},
                    timeout=timeout_s,
                )
                if r.status_code != 200:
                    return
                body = r.json()
                rows = body.get("quotes") if isinstance(body, dict) else None
                wanted = set(part)
                for q in rows or []:
                    if not isinstance(q, dict):
                        continue
                    base = str(q.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
                    if base not in wanted or not _gw_bulk_row_fresh(q, max_age):
                        continue
                    px = _gw_quote_px(q)
                    if px:
                        out[base] = px
                        _GW_BULK_PX_CACHE[base] = (time.monotonic(), px)
            except Exception as e:
                logger.debug("gateway bulk quotes chunk failed (%d symbols): %s", len(part), e)

    await asyncio.gather(*(one_chunk(need[i:i + chunk]) for i in range(0, len(need), chunk)),
                         return_exceptions=True)
    if len(_GW_BULK_PX_CACHE) > 5000:       # bounded: drop entries past the TTL
        cutoff = time.monotonic() - max(cache_s, 1.0)
        for k in [k for k, v in _GW_BULK_PX_CACHE.items() if v[0] < cutoff]:
            _GW_BULK_PX_CACHE.pop(k, None)
    return out


async def _fetch_prices_bulk_async(symbols: list, client: httpx.AsyncClient, *,
                                   bulk_min: Optional[int] = None, per_symbol_fallback: bool = True) -> dict:
    """Concurrent short quotes for a scan chunk → {BASE: float}.

    group233: bulk_min overrides GATEWAY_BULK_QUOTE_MIN for this call (the hot-picks price pass has fewer than
    15 missing symbols, so it always took the per-symbol path); per_symbol_fallback=False leaves a symbol that
    bulk could not price unpriced instead of sending one GET /quote for it. With the market closed the
    per-symbol leftover is skipped anyway: market-data already tried the cached close, the last-good row and
    the bhavcopy close for every symbol in the bulk call."""
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
                px = _gw_quote_px(r.json())
                if px:
                    out[base] = px
            except Exception:
                return

    # A None / blank / non-string symbol used to be requested as ".../quote/None" and stored
    # under "". Skip it instead of asking the market-data service for nothing. group153: the same
    # stock listed twice (or as SYM and SYM.NS) is requested once.
    todo, seen = [], set()
    for s_ in symbols:
        if not isinstance(s_, str) or not s_.strip():
            continue
        base = s_.upper().replace(".NS", "").replace(".BO", "").strip()
        if base in seen:
            continue
        seen.add(base)
        todo.append(s_)

    if bulk_min is None:
        bulk_min = int(_gw_env_num("GATEWAY_BULK_QUOTE_MIN", 15))
    bulk_ran = False
    if bulk_min > 0 and len(todo) >= bulk_min:
        bases = [t.upper().replace(".NS", "").replace(".BO", "").strip() for t in todo]
        try:
            out.update(await _fetch_prices_bulk_first(bases, client))
            bulk_ran = True
        except Exception as e:
            logger.debug("gateway bulk-first pricing failed, using per-symbol path: %s", e)
        todo = [t for t, b in zip(todo, bases) if b not in out]

    if not per_symbol_fallback or (bulk_ran and _gw_quotes_closed()):
        if todo:
            logger.info("gateway prices: %d symbol(s) left unpriced after bulk (no per-symbol /quote)", len(todo))
        return out
    await asyncio.gather(*(one(s_) for s_ in todo), return_exceptions=True)
    return out

async def _fetch_fundamental_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[dict], bool]:
    """Prefer Data Feed → short Redis cache → upstream. Write-through to Data Feed on upstream hit."""
    # 1) Data Feed (durable 12–24h) — any useful feed row skips upstream
    try:
        fed = await asyncio.to_thread(_feed_store().get_symbol, symbol)  # group303: Oracle read off the event loop
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
            # absent AND explicit None both mean "unknown" -> treat as fallback (True)
            _fb = fed.get("fallback_used")
            return reconstructed, True if _fb is None else bool(_fb)
    except Exception as e:
        logger.debug("data feed fundamental read: %s", e)

    # 2) Short Redis cache
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict) and (cached.get("full") or cached.get("metrics") is not None):
        # Stale-while-revalidate: if we're inside the soft TTL window (close to
        # hard expiry), kick off a background refresh now so the cache is warm
        # by the time it actually expires, instead of only ever refreshing
        # reactively once a request finds the key already gone.
        if soft_ttl_should_refresh(_redis, cache_key):
            if try_refresh_lock(_redis, symbol, ttl_sec=5):
                asyncio.create_task(_background_refresh_fundamental(symbol, client, cache_key))
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
        return await _refresh_fundamental_upstream(symbol, client, cache_key)
    finally:
        # Always release, not just on a 200 — otherwise a non-200 upstream
        # response (or an exception raised before this point) held the lock
        # for its full 5s TTL and needlessly staled concurrent requests.
        release_refresh_lock(_redis, symbol)


async def _background_refresh_fundamental(symbol: str, client: httpx.AsyncClient, cache_key: str) -> None:
    """Fire-and-forget stale-while-revalidate wrapper: refreshes upstream then
    always releases the lock this call acquired, whatever the outcome."""
    try:
        await _refresh_fundamental_upstream(symbol, client, cache_key)
    finally:
        release_refresh_lock(_redis, symbol)


async def _refresh_fundamental_upstream(symbol: str, client: httpx.AsyncClient, cache_key: str) -> tuple[Optional[dict], bool]:
    """Fetch fundamental data from upstream and write-through to the short
    Redis cache + Data Feed. Caller is responsible for the refresh-lock
    lifecycle (acquire before calling; release in a finally so it happens
    even on a non-200 response or a raised exception)."""
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
                existing = await asyncio.to_thread(_feed_store().get_symbol, symbol) or {}  # group303
                if existing:
                    for k in ("bulk_deals", "recent_insider_transactions", "earnings_surprise",
                              "next_earnings_date", "event_summary", "has_positive_catalyst",
                              "recent_event_score"):
                        if existing.get(k) is not None and payload.get(k) in (None, [], ""):
                            payload[k] = existing.get(k)
                await asyncio.to_thread(_feed_store().put_symbol, symbol, payload, DATA_FEED_TTL)  # group303: 1 read + 3 writes + index = ~5 Oracle round trips
            except Exception as e:
                logger.debug("data feed write-through fund: %s", e)
            if data.get("fundamental_score") is not None:
                out = dict(data)
                out["from_data_feed"] = False
                return out, fallback_used
            return metrics, fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {str(e) or type(e).__name__}")
    return {}, True

async def _fetch_events_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    """Prefer Data Feed → short Redis → upstream. Write-through on upstream hit."""
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"

    # 1) Data Feed first (even empty lists mean "was fed" — avoid upstream spam)
    try:
        fed = await asyncio.to_thread(_feed_store().get_symbol, symbol)  # group303
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
                    existing = await asyncio.to_thread(_feed_store().get_symbol, symbol)  # group303
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
                    await asyncio.to_thread(_feed_store().put_symbol, symbol, payload, DATA_FEED_TTL)  # group303
                except Exception as e:
                    logger.debug("data feed write-through events: %s", e)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {str(e) or type(e).__name__}")
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
        logger.warning(f"News fetch failed for {symbol}: {str(e) or type(e).__name__}")
    return None

async def _fetch_prediction_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[float], Optional[str]]:
    try:
        resp = await _cb_get(client, "prediction", f"{PREDICTION_URL}/predict/{symbol}", timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("model_loaded"):
                return data.get("prediction_score"), data.get("note")
    except Exception as e:
        logger.warning(f"Prediction lookup failed for {symbol}: {str(e) or type(e).__name__}")
    return None, None

# ── Hinglish & GenAI summary ──────────────────────────────────────────────
# ── Gemini-powered summary (optional — falls back to the Hinglish template
# below if GEMINI_API_KEY isn't set, or if the call fails/times out/gets
# truncated) ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip() or None
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
# Free-tier Gemini has its own RPM limit, independent of MAX_PARALLEL_WORKERS
# (which bounds calls to decision-engine, not Gemini) — a scan running 10
# concurrent symbol analyses shouldn't also fire 10 concurrent Gemini calls.
GEMINI_SEMAPHORE = asyncio.Semaphore(int(((os.getenv("GEMINI_MAX_CONCURRENT") or "").strip() or "3")))
GEMINI_MAX_OUTPUT_TOKENS = int(((os.getenv("GEMINI_MAX_OUTPUT_TOKENS") or "").strip() or "400"))
# After a 429 from Gemini, stop calling it for a while and serve the template straight away.
# Without this every /stock call made a Gemini request that was certain to 429 again (same
# approach as decision-prediction-service's _gemini_cooldown_until, which uses 10 minutes).
# A Retry-After header on the 429 wins over the default, clamped to 30s..1h.
def _gemini_cooldown_default() -> float:
    try:
        v = float(((os.getenv("GEMINI_COOLDOWN_SECONDS") or "").strip() or "600"))
    except ValueError:
        v = 600.0
    return v if v > 0 else 600.0

GEMINI_COOLDOWN_SECONDS = _gemini_cooldown_default()
_gemini_cooldown_until = 0.0  # epoch seconds; Gemini is skipped while time.time() is below this


def _gemini_cooldown_remaining() -> float:
    return max(0.0, _gemini_cooldown_until - time.time())


def _start_gemini_cooldown(resp, symbol) -> None:
    """Called on a 429. Logs once per cooldown window (not once per request)."""
    global _gemini_cooldown_until
    secs = GEMINI_COOLDOWN_SECONDS
    try:
        ra = (getattr(resp, "headers", None) or {}).get("retry-after")
        if ra is not None and str(ra).strip():
            secs = min(3600.0, max(30.0, float(str(ra).strip())))
    except (TypeError, ValueError, AttributeError):
        pass
    already = _gemini_cooldown_remaining() > 0
    _gemini_cooldown_until = max(_gemini_cooldown_until, time.time() + secs)
    if not already:
        logger.warning(f"Gemini 429 for {symbol} — skipping Gemini for {secs:.0f}s, using template")
        try:
            rate_limit_monitor.record(source="gemini", status=429, path="generateContent", symbol=str(symbol or ""))
        except Exception:
            pass

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
    if _gemini_cooldown_remaining() > 0:
        return _generate_summary(data)  # in cooldown after a 429: no request, no log line
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
        if resp.status_code == 429:
            _start_gemini_cooldown(resp, data.get("symbol"))
            return _generate_summary(data)
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
def _scan_notify_num(v):
    """Float for a message field, or None when it is missing / not a usable number."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _send_scan_notification(recommendations: list, verdict: str, scanned: int, universe_size: int):
    if not recommendations:
        message = f"📊 Market Scan Complete\n\nScanned {scanned} stocks. No strong BUY signals today.\nVerdict: {verdict}"
    else:
        lines = [f"📊 *Top {len(recommendations)} Picks from Market Scan*", ""]
        for i, r in enumerate(recommendations[:5], 1):
            # Message building runs outside the try below, so a string close / non-dict
            # entry_range must degrade to "field omitted", never raise out of the scan-complete path.
            if not isinstance(r, dict):
                continue
            symbol = r.get("symbol")
            decision = r.get("decision")
            combined_score = r.get("combined_score")
            close = _scan_notify_num(r.get("close"))
            target = _scan_notify_num(r.get("target"))
            stop_loss = _scan_notify_num(r.get("stop_loss"))
            entry = r.get("entry_range")
            entry = entry if isinstance(entry, dict) else {}
            entry_low = _scan_notify_num(entry.get("low"))
            entry_high = _scan_notify_num(entry.get("high"))
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
        buy_now = [r for r in (recommendations or []) if isinstance(r, dict) and r.get("decision") == "BUY NOW"]
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

def _cleanup_scan_resources(pool, client, not_done, deadline_seconds):
    """Background-thread cleanup for /scan/watchlist stragglers.

    Gives whatever didn't finish inside the request's own deadline a bounded
    extra grace period to complete naturally (their results still only
    write to caches, which the *next* /scan/watchlist call benefits from),
    then force-releases the thread pool and HTTP client either way so a
    slow watchlist scan can never leak connections/threads across calls.
    """
    grace = float(((os.getenv("WATCHLIST_SCAN_GRACE_SECONDS") or "").strip() or "30"))
    try:
        if not_done:
            wait(not_done, timeout=grace)
    except Exception:
        pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass


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
MAX_PARALLEL_WORKERS = int(((os.getenv("MAX_PARALLEL_SCAN_WORKERS") or "").strip() or "8"))  # free-tier safe
SCAN_BATCH_SIZE = int(((os.getenv("SCAN_BATCH_SIZE") or "").strip() or "8"))  # aligned with workers; bulk Neon removes DB N+1

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


def _wake_pings_enabled() -> bool:
    """Render-era "wake the free-tier dyno" pings. On an always-on host (the Oracle VM: ORACLE_DSN set) they are
    pure noise -- every stockky-hot batch sent 6 /health?warm=true calls. WAKE_PINGS=1 forces them on, =0 off;
    unset -> off when ORACLE_DSN is set, on otherwise (Render/Neon deployments keep the old behaviour)."""
    v = (os.getenv("WAKE_PINGS") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return not (os.getenv("ORACLE_DSN") or "").strip()


async def _wake_required_services(client: httpx.AsyncClient = None) -> dict:
    """Wake free-tier services before scan. Market-data gets a warm yfinance touch + double ping."""
    if not _wake_pings_enabled():
        return {n: {"ok": True, "status": None, "warmed": False, "skipped": True} for n in SYSTEM_SERVICES}
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
                        feed_row = await asyncio.to_thread(_feed_store().get_symbol, base_sym)  # group303
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
                                close_px = (await asyncio.to_thread(_fetch_price_from_quote, symbol))
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
                            # First rung holding a usable number wins. A real 0 is a
                            # valid score; only None / blank / non-numeric / non-finite
                            # values fall through to the next rung.
                            score = 50.0
                            for _rung in ("combined_score", "fundamental_score", "technical_score"):
                                _v = fr.get(_rung)
                                if _v is None or isinstance(_v, bool):
                                    continue
                                try:
                                    _f = float(_v)
                                except (TypeError, ValueError):
                                    continue
                                if _f != _f or _f in (float("inf"), float("-inf")):
                                    continue
                                score = _f
                                break
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
                            # Group 349: flat structure summary for the decision service's shadow policy (None keys dropped below)
                            "structure_summary": {k: v for k, v in feed_row.items() if isinstance(k, str) and k.startswith("ms_")} or None,
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
                    # Only cache a real engine answer. A non-dict / empty body is
                    # replaced by the placeholder default row, which must not be
                    # served from cache for the whole TTL.
                    if isinstance(raw, dict) and raw:
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
                        price = (await asyncio.to_thread(_fetch_price_from_quote, symbol))
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

                    # Take results straight from gather(return_exceptions=True)
                    # instead of calling task.result() afterwards: .result()
                    # re-raises a child's CancelledError (a BaseException that
                    # `except Exception` does not catch) and would abort the
                    # whole scan for one cancelled enrichment call. Any child
                    # outcome that is an exception, cancellation included, now
                    # falls back to the same default as a failed call. If the
                    # OUTER task is cancelled, gather itself raises here, so
                    # real cancellation still propagates.
                    task_out = {}
                    if tasks:
                        _gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
                        task_out = dict(zip(tasks.keys(), _gathered))

                    if "fund" in tasks:
                        fund_res = task_out.get("fund")
                        if isinstance(fund_res, BaseException):
                            fund_res = ({}, True)
                        if isinstance(fund_res, tuple):
                            fund_metrics, fund_fallback = fund_res
                        else:
                            fund_metrics, fund_fallback = {}, True
                        if fund_metrics:
                            normalized["fundamental_metrics"] = fund_metrics
                            normalized["fundamental_fallback"] = fund_fallback

                    if "event" in tasks:
                        event_data = task_out.get("event")
                        if isinstance(event_data, BaseException):
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
                        news_data = task_out.get("news")
                        if isinstance(news_data, BaseException):
                            news_data = None
                        if news_data:
                            # A news payload with no score must not wipe a score the
                            # feed / decision engine already supplied.
                            if news_data.get("news_score") is not None:
                                normalized["news_score"] = news_data.get("news_score")
                            reasons = normalized.get("reasons", {})
                            if news_data.get("reasons"):
                                reasons["news"] = news_data["reasons"]
                                normalized["reasons"] = reasons

                    if "pred" in tasks:
                        pred_res = task_out.get("pred")
                        if isinstance(pred_res, BaseException):
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
                _mark_thin_technical(normalized)
                return normalized

            except CircuitOpenError as e:
                # Fail fast — no retry storm when dependency circuit is open
                metrics.inc("stockky_scan_circuit_skip_total", dependency=e.name)
                logger.warning("Scan skip %s: %s", symbol, e)
                await asyncio.sleep(1.5)
                price = None
                try:
                    price = (await asyncio.to_thread(_fetch_price_from_quote, symbol))
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

def _score_sort_key(row) -> float:
    """Sort key for a result row's combined_score. An absent OR explicit-null /
    non-numeric score sorts as 0 instead of raising TypeError mid-scan."""
    if not isinstance(row, dict):
        return 0.0
    v = _hot_float(row.get("combined_score"))
    return v if v is not None else 0.0


async def run_scan_parallel(task_id: str, universe: List[str], lite: bool = False):
    """Run the scan; whatever happens, never leave ``_SCAN_IN_PROGRESS`` stuck on
    (it pauses the WS quote upstream) or the task record stuck at "running"."""
    global _SCAN_IN_PROGRESS
    try:
        return await _run_scan_parallel_impl(task_id, universe, lite)
    except Exception as e:
        logger.error("scan %s crashed: %s", task_id, e, exc_info=True)
        try:
            _cur = _redis_get(SCAN_TASK_PREFIX + task_id)
            # A crash AFTER the result was saved (e.g. a failing notification)
            # must not overwrite a finished task record.
            if isinstance(_cur, dict) and _cur.get("status") not in (None, "running"):
                return None
            _redis_set(SCAN_TASK_PREFIX + task_id, {
                "status": "error",
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "message": "Scan failed unexpectedly — run it again.",
                "result": None,
                "partial": True,
            }, ttl=3600)
        except Exception:
            pass
    finally:
        _SCAN_IN_PROGRESS = False


async def _run_scan_parallel_impl(task_id: str, universe: List[str], lite: bool = False):
    global _SCAN_IN_PROGRESS
    start_time = time.time()
    _SCAN_IN_PROGRESS = True
    try:
        set_activity_paused(False)
        _SCAN_CANCEL_FLAGS.discard("__ALL__")
    except Exception:
        pass

    # Prioritize watchlist / searched so useful picks surface early without shrinking universe
    universe = await asyncio.to_thread(_prioritize_universe, universe)  # group361: off the event loop
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

    _status(0, message="Starting — DB data-feed preferred; upstream only for live fields")
    logger.info("Scan %s started universe=%s lite=%s", task_id, total, lite)

    sem = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    cancel_key = SCAN_TASK_PREFIX + task_id + ":cancel"
    client = _get_http_client()  # shared keepalive pool

    # Data Feed coverage FIRST — ONE Neon bulk query (not 300 sequential hits)
    feed_hit = 0
    prefetched_feeds: dict = {}
    try:
        _status(0, message="Bulk-loading DB data-feed (single query)…")
        from data_feed import get_all_stock_feeds
        bases = [s.upper().replace(".NS", "").replace(".BO", "").strip() for s in universe]
        prefetched_feeds = await asyncio.to_thread(get_all_stock_feeds, bases) or {}  # group361: off the event loop
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
        _status(0, message=f"DB bulk feed {feed_hit}/{total} — starting batches")
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
            logger.warning("Pre-scan wake failed/timeout (continuing DB-first): %s", e)
    else:
        logger.info(
            "Skipping long pre-scan wake (DB feed coverage %.0f%%) — proceed to batches",
            feed_ratio * 100,
        )

    # Seed per-symbol batch cache from last partial scan so 60/300 style
    # resumes don't re-score already completed names (Neon/redis durable).
    try:
        last = _redis_get(LAST_FULL_SCAN_KEY) if BATCH_RESULT_CACHE_ENABLED else None
        if last and isinstance(last, dict):
            prev = (last.get("result") or {})
            # The last-scan record lives 24h, but seeding re-caches each row with a FRESH
            # TTL. A row from a scan older than that TTL must not be served as fresh, so
            # skip seeding then. A missing/unparseable scanned_at keeps the old behaviour.
            try:
                _scanned_raw = last.get("scanned_at") or prev.get("scanned_at")
                if _scanned_raw:
                    _scanned_dt = datetime.fromisoformat(str(_scanned_raw))
                    if _scanned_dt.tzinfo is None:
                        _scanned_dt = _scanned_dt.replace(tzinfo=IST)
                    if (datetime.now(IST) - _scanned_dt).total_seconds() > _decide_cache_ttl():
                        prev = {}
            except Exception:
                pass
            prev_all = prev.get("all_results") or []
            seeded = 0
            for row in prev_all:
                if not isinstance(row, dict):
                    continue
                sym = (row.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "")
                if not sym or row.get("decision") == "ERROR":
                    continue
                # A lite row (score ladder, no fundamentals/news) must not be
                # served by a FULL scan as if it were a full result.
                if row.get("lite") and not lite:
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
        "Scan full universe=%s batch_size=%s workers=%s (DB data-feed preferred)",
        total, batch_size, MAX_PARALLEL_WORKERS,
    )
    _status(0, message=f"Processing {total} symbols — DB feed first, upstream for live fields only")

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

    _warm_bucket = [0]

    async def _on_batch_end(progress):
        # Keep free-tier services awake during long full-universe scans.
        # Skip warm when this scan is mostly cache hits (no upstream pressure).
        total_c = progress.cache_hits + progress.cache_misses
        hit_rate = (progress.cache_hits / total_c) if total_c else 0.0
        if hit_rate >= 0.85:
            return
        # Warm once each time processed crosses another multiple of 20. (`processed % 20 == 0`
        # only matched when the batch size divides 20, e.g. batch 8 never hit 20/60/100.)
        _bucket = progress.processed // 20
        if _bucket > _warm_bucket[0] and not progress.cancelled:
            _warm_bucket[0] = _bucket
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

    results.sort(key=_score_sort_key, reverse=True)

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
            # A DO NOT BUY row is never promoted to PREPARE TO BUY on score alone
            # (it used to be, which put "Top N Picks" under a "DO NOT BUY ANY
            # STOCK TODAY" verdict). An empty board still falls back to the
            # overall ranking below, with each row's true decision.
            if decision in ("DO NOT BUY", "SELL"):
                continue
            if decision in ("BUY NOW", "PREPARE TO BUY") or (sc or 0) >= min_sc:
                scored.append({**r, "_hz_score": sc, "horizon_focus": horizon_key})
        scored.sort(key=_board_rank_key, reverse=True)
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
        "watchlist_size": len(await asyncio.to_thread(_load_watchlist)),  # group361: off the event loop
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
        # BUG FIX (session 55, event-loop-blocking-I/O audit): _send_scan_notification()
        # does a blocking _wake_notification_service() httpx.get (up to 5s) plus
        # one or two blocking httpx.post calls (up to 15s + 20s) — called inline
        # here, on the event loop, at the end of every full market scan. Since
        # this coroutine (run_scan_parallel) runs on the same event loop that
        # serves every other request this process handles, an unresponsive/slow
        # notification-scheduler-service could stall the whole API for up to ~40s
        # right after each scan. Same blocking-I/O-on-event-loop bug class as
        # circuit_breaker.py/rate_limit_monitor.py/batch_worker.py/the two fixes
        # above in this file — moved off the event loop via asyncio.to_thread.
        await asyncio.to_thread(
            _send_scan_notification,
            # Only actionable rows are announced as "Top N Picks"; a board that
            # is just the never-empty fallback gets the "No strong BUY signals"
            # message instead of contradicting a DO NOT BUY verdict.
            [r for r in final_result.get("recommendations", [])
             if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")],
            final_result["verdict"],
            final_result["scanned"],
            final_result["universe_size"],
        )

# ── Cached Market Movers Data ──────────────────────────────────────────────
# Fix (30 Aug 2026): guards _get_nifty50_data's cold-cache fetch so
# concurrent callers (top-gainers/top-losers/most-active all hit a cold
# cache within milliseconds of each other on a fresh day) don't each
# independently kick off their own full 50-symbol yfinance fetch. See the
# function docstring below for the failure this caused.
_nifty50_fetch_lock = threading.Lock()

# SESSION 33c fix: last successful movers fetch, kept independent of the
# per-day cache_key above so a pre-open/closed-market request always has
# something real to fall back to instead of an empty list. Mirrors the
# indices endpoint's INDICES_LAST_KNOWN pattern (get_market_indices, above).
MARKET_MOVERS_LAST_KNOWN = "stockky:market_movers_last_known"
# group111: when the last-known list was saved (IST, "YYYY-MM-DD HH:MM"). Starts with the last-known key,
# so kv_cache's durable-prefix rule already covers it.
MARKET_MOVERS_LAST_KNOWN_AT = "stockky:market_movers_last_known_at"


def _mark_movers_stale(rows: List[dict]) -> List[dict]:
    """Copies of last-known mover rows with `stale: True` (group110), plus `stale_since` (group111: when the
    list was saved) when that timestamp is known. The stored list is never mutated, and non-dict entries are
    passed through untouched. Never raises."""
    since = None
    try:
        v = _redis_get(MARKET_MOVERS_LAST_KNOWN_AT)
        if isinstance(v, str) and v.strip():
            since = v.strip()
    except Exception:
        since = None
    out = []
    for r in rows:
        if isinstance(r, dict):
            r = {**r, "stale": True}
            if since:
                r["stale_since"] = since
        out.append(r)
    return out


def _movers_fallback_when_yahoo_empty() -> List[dict]:
    """Rows for the dashboard Movers panel when the open-session yfinance fetch returned nothing.

    1) AngelOne's whole-market sweep (market-data-service /angelone/movers: authenticated broker quotes,
       not NSE- or Yahoo-exposed). It lists only moves of 5% or more and carries no volume, so rows have
       symbol/price/change/change_pct and `source: "angelone"`; "most active" has nothing to rank in
       this mode and stays empty. Not cached here (market-data caches it).
    2) The last-known list from the previous good fetch (up to 7 days old, same as the pre-open path).
    3) [] when neither exists. Never raises."""
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/angelone/movers", timeout=25.0)
        if resp.status_code == 200:
            payload = resp.json() or {}
            rows = []
            for r in (payload.get("data") or []):
                if not isinstance(r, dict):
                    continue
                sym = _clean_equity_symbol((r.get("symbol") or "").upper())
                ltp = _hot_float(r.get("ltp"))
                pct = _hot_float(r.get("pct_change"))
                if not sym or ltp is None or pct is None or ltp <= 0 or pct <= -100.0:
                    continue
                prev = ltp / (1.0 + pct / 100.0)
                rows.append({
                    "symbol": sym,
                    "price": round(ltp, 2),
                    "change": round(ltp - prev, 2),
                    "change_pct": round(pct, 2),
                    "source": "angelone",
                })
            if rows:
                logger.warning(
                    "Movers: yfinance returned nothing during the open session - serving %d AngelOne movers (>=5%%)",
                    len(rows),
                )
                return rows
    except Exception as e:
        logger.debug("Movers AngelOne fallback failed: %s", e)
    try:
        last_known = _redis_get(MARKET_MOVERS_LAST_KNOWN)
        if last_known and isinstance(last_known, list) and len(last_known) > 0:
            logger.warning(
                "Movers: yfinance returned nothing during the open session - serving last-known movers (stale)"
            )
            return _mark_movers_stale(last_known)
    except Exception:
        pass
    return []


# Group 122: once-per-window throttle state for the "no last-known movers" line.
_MOVERS_EMPTY_LOG_WINDOW_SEC = 600.0
_MOVERS_EMPTY_LOG_AT: Dict[str, float] = {}


def _movers_rows_from_market_data(symbols: List[str]) -> List[dict]:
    """group275: movers rows for `symbols` from market-data-service POST /quotes/bulk (Dhan -> AngelOne -> yfinance
    is decided there), instead of one yfinance 1m-history call per symbol. Returns the rows it could build; a symbol
    with no fresh price or no previous close is simply left out so the caller can try yfinance for it. Never raises.
    GATEWAY_MOVERS_VIA_MARKET_DATA=0 turns it off."""
    if _gw_env_num("GATEWAY_MOVERS_VIA_MARKET_DATA", 1.0) <= 0 or not symbols:
        return []
    out: List[dict] = []
    chunk = max(1, int(_gw_env_num("GATEWAY_BULK_QUOTE_CHUNK", 50)))
    max_age = _gw_env_num("GATEWAY_MOVERS_QUOTE_MAX_AGE_S", 90.0)
    timeout_s = _gw_env_num("GATEWAY_BULK_QUOTE_TIMEOUT_S", 12.0)
    wanted_all = {str(x).upper().replace(".NS", "").replace(".BO", "").strip() for x in symbols if x}
    for i in range(0, len(symbols), chunk):
        part = [str(x).upper().replace(".NS", "").replace(".BO", "").strip() for x in symbols[i:i + chunk] if x]
        if not part:
            continue
        try:
            r = httpx.post(f"{MARKET_DATA_URL.rstrip('/')}/quotes/bulk", json={"symbols": part}, timeout=timeout_s)
            if r.status_code != 200:
                continue
            for q in ((r.json() or {}).get("quotes") or []):
                if not isinstance(q, dict):
                    continue
                base = str(q.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
                if base not in wanted_all or not _gw_bulk_row_fresh(q, max_age):
                    continue
                px = _gw_quote_px(q)
                prev = _hot_float(q.get("previous_close"))
                if not px or not prev or prev <= 0:
                    continue
                hi = _hot_float(q.get("day_high")) or px
                lo = _hot_float(q.get("day_low")) or px
                try:
                    vol = int(float(q.get("volume") or 0))
                except (TypeError, ValueError):
                    vol = 0
                out.append({
                    "symbol": base,
                    "price": round(px, 2),
                    "change": round(px - prev, 2),
                    "change_pct": round((px - prev) / prev * 100, 2),
                    "volume": vol,
                    "high": round(hi, 2),
                    "low": round(lo, 2),
                })
        except Exception as e:  # noqa: BLE001
            logger.debug("movers bulk quotes chunk failed (%d symbols): %s", len(part), e)
    return out


def _get_nifty50_data() -> List[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{MARKET_MOVERS_CACHE_PREFIX}{today}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, list) and len(cached) > 0:
        logger.info("Serving cached market movers data for %s", today)
        return cached

    # SESSION 33c fix (audit finding): this function used to go straight to
    # a ~150-symbol yfinance fetch on ANY cache miss, using
    # `ticker.history(period="1d", interval="1m")` per symbol — i.e.
    # TODAY's 1-minute intraday candles. Before the 09:15 IST session
    # opens (the "preopen"/08:30-09:15 warm window this dashboard itself
    # labels "PRE-OPEN"), NO symbol has any 1-minute bar for today yet, so
    # every single one of those ~150 requests is *guaranteed* to return an
    # empty DataFrame -> hist.empty -> None -> data == []. Because an empty
    # list is falsy, the cache-read check above (`if cached and ...`)
    # never treats it as a hit, so this expensive full fetch re-ran on
    # EVERY poll of /market/top-gainers /-losers /-most-active during the
    # entire pre-open window — hammering Yahoo with ~150 requests per poll
    # for something that cannot possibly succeed yet. That request volume
    # is a plausible contributor to get_market_indices()'s intermittent
    # "Index data temporarily unavailable" 503s (same yfinance backend,
    # same container IP, more likely to get rate-limited/throttled).
    # Fix: outside the "open" session phase, skip the guaranteed-empty
    # fetch entirely and serve the last successful day's data (clearly
    # marked stale) instead — same last-known-fallback shape
    # get_market_indices already uses.
    phase = _market_session_phase_ist()
    if phase != "open":
        last_known = _redis_get(MARKET_MOVERS_LAST_KNOWN)
        if last_known and isinstance(last_known, list) and len(last_known) > 0:
            logger.info(
                "Market session phase=%s — serving last-known movers instead of "
                "a guaranteed-empty pre-open/closed yfinance 1m-interval fetch",
                phase,
            )
            return _mark_movers_stale(last_known)
        # Group 122: the three Movers routes and the momentum-movers collector each
        # hit this branch in the same pass, so the line printed 4x per pass (and on
        # every poll). Log it at INFO once per window per phase; repeats go to DEBUG.
        _msg = ("Market session phase=%s and no last-known movers cached yet — "
                "returning empty rather than a guaranteed-empty fetch")
        _now_mono = time.monotonic()
        _last = _MOVERS_EMPTY_LOG_AT.get(phase)
        if _last is None or _now_mono - _last >= _MOVERS_EMPTY_LOG_WINDOW_SEC:
            _MOVERS_EMPTY_LOG_AT[phase] = _now_mono
            logger.info(_msg, phase)
        else:
            logger.debug(_msg, phase)
        return []

    # Fix (30 Aug 2026): test_20260830_134442.log — market/top-gainers and
    # market/top-losers both timed out at 25.00s, back to back, right after
    # /scan/watchlist also timed out. Two things were going on:
    #   1. This function had no lock: top-gainers and top-losers both saw a
    #      cache miss within the same request burst and each independently
    #      started its own full 50-symbol sequential yfinance fetch —
    #      doubling the work for the exact same "today" cache entry.
    #   2. The fetch itself was a plain sequential for-loop over 50 tickers
    #      with no bound on total time, so on a cold cache it could already
    #      run long even alone.
    # The lock below makes the second caller wait for the first fetch and
    # reuse its result instead of duplicating it; the ThreadPoolExecutor
    # below (bounded by the same MAX_PARALLEL_WORKERS ceiling used
    # elsewhere in this file for yfinance-backed work) bounds the fetch
    # itself to roughly 1/Nth of the old sequential time.
    with _nifty50_fetch_lock:
        # Re-check: another thread may have just finished fetching while
        # we were waiting for the lock.
        cached = _redis_get(cache_key)
        if cached and isinstance(cached, list) and len(cached) > 0:
            logger.info("Serving cached market movers data for %s (post-lock)", today)
            return cached

        logger.info("Fetching fresh market movers data (market-data bulk first, yfinance for the rest) for %s", today)
        # BUG (31-Aug-2026): `_get_nifty_indices()[:50]` — despite this
        # function's name/callers implying general "market movers" — was
        # *always* exactly the NIFTY 50 constituents, because
        # _get_nifty_indices() concatenates NIFTY 50 first, then NEXT 50 /
        # MIDCAP100 / MIDCAP150 / SMALLCAP100 after it; slicing the first
        # 50 off the front never reaches anything past NIFTY 50 itself.
        # /market/top-gainers, /market/top-losers, /market/most-active
        # (and momentum_movers step 2/3) were therefore large-cap-only by
        # construction and could never surface a smallcap/midcap
        # volume-shocker like the ones in the 31-Aug-2026 Groww
        # screenshots. Mix in a shuffled sample of the NEXT50/MID/SMALL
        # tail too — keep the full NIFTY 50 (cheap, always relevant) and
        # add a bounded, randomized slice of the rest so this reflects
        # more than large-caps without blowing up fetch time (this stays
        # cached once/day behind the lock above).
        nifty_idx_all = _get_nifty_indices()
        nifty50_only = nifty_idx_all[:50]
        rest = nifty_idx_all[50:]
        random.shuffle(rest)
        nifty_symbols = list(dict.fromkeys(nifty50_only + rest[:100]))

        def _fetch_one(sym):
            try:
                yf_ticker = resolve_ns_ticker(sym)
                if not yf_ticker:
                    return None
                ticker = yf.Ticker(yf_ticker)
                hist = ticker.history(period="1d", interval="1m")
                if hist.empty:
                    return None
                latest = hist.iloc[-1]
                # Previous SESSION close when yfinance exposes it; otherwise the
                # day's first bar (open if present, else its close). It used to
                # be the first 1-minute bar's close, and volume/high/low were the
                # LAST minute only, so "most active" ranked one minute of volume.
                prev_close = None
                try:
                    prev_close = _hot_float(ticker.fast_info.get("previous_close"))
                except Exception:
                    prev_close = None
                if not prev_close or prev_close <= 0:
                    first = hist.iloc[0]
                    prev_close = _hot_float(first["Open"]) if "Open" in hist.columns else None
                    if not prev_close or prev_close <= 0:
                        prev_close = _hot_float(first["Close"])
                if not prev_close or prev_close <= 0:
                    return None
                change_pct = (latest["Close"] - prev_close) / prev_close * 100
                return {
                    "symbol": sym,
                    "price": round(latest["Close"], 2),
                    "change": round(latest["Close"] - prev_close, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": int(hist["Volume"].sum()),
                    "high": round(hist["High"].max(), 2),
                    "low": round(hist["Low"].min(), 2),
                }
            except Exception as e:
                logger.warning(f"Could not fetch {sym}: {e}")
                return None

        # group275: market-data first (a few bulk calls); yfinance only for what it could not price.
        data = _movers_rows_from_market_data(nifty_symbols)
        _have = {d["symbol"] for d in data}
        _left = [sy for sy in nifty_symbols if str(sy).upper().replace(".NS", "").replace(".BO", "").strip() not in _have]
        if data:
            logger.info("Movers: market-data priced %d/%d symbols, %d left for yfinance",
                        len(data), len(nifty_symbols), len(_left))
        # group279: the per-symbol yfinance fetch is the last resort and can be switched off
        # (GATEWAY_DIRECT_YFINANCE_FALLBACK=0); symbols market-data could not price are then left out.
        if _left and not _direct_yf_ok():
            logger.info("Movers: direct yfinance is off (GATEWAY_DIRECT_YFINANCE_FALLBACK=0) - %d symbols "
                        "market-data could not price are left out", len(_left))
            _left = []
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as ex:
            for res in ex.map(_fetch_one, _left):
                if res:
                    data.append(res)
        _redis_set(cache_key, data, ttl=86400)
        if data:
            # SESSION 33c fix: keep a rolling last-known copy so the next
            # pre-open/closed-market window (see the phase check above) has
            # real data to serve instead of an empty list.
            _redis_set(MARKET_MOVERS_LAST_KNOWN, data, ttl=7 * 86400)
            try:
                _redis_set(MARKET_MOVERS_LAST_KNOWN_AT, datetime.now().strftime("%Y-%m-%d %H:%M"), ttl=7 * 86400)
            except Exception:
                pass            # the timestamp is only a label; never fail the fetch over it
            return data
    # group109 (log-audit item 20): the yfinance fetch above came back empty DURING the open session
    # (Yahoo blocked / rate limited from the VM, item 6). That used to leave the dashboard Movers panel
    # empty even though last-known data existed and AngelOne's whole-market sweep was reachable.
    # Done outside the fetch lock so a slow fallback never blocks other callers.
    return _movers_fallback_when_yahoo_empty()

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
# E83: batch H (gateway_support/bound_routes_h.py); built once here, before its first include below.
from gateway_support import MainRef as _MainRef  # noqa: E402
from gateway_support.bound_routes_h import build_bound_routes_h as _build_bound_routes_h  # noqa: E402
_bound_routes_h = _build_bound_routes_h(_MainRef(globals()))
# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
root = _bound_routes_h.root
app.include_router(_bound_routes_h.root_router)



# E84: batch I (gateway_support/bound_routes_i.py): GET /quote/{symbol}, its own factory because the handler's last fallback calls yf.Ticker (listed in the yfinance guard).
from gateway_support.bound_routes_i import build_bound_routes_i as _build_bound_routes_i  # noqa: E402
_bound_routes_i = _build_bound_routes_i(_MainRef(globals()))
# E84: moved to gateway_support/bound_routes_i.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
proxy_quote = _bound_routes_i.proxy_quote
app.include_router(_bound_routes_i.proxy_quote_router)


# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
from gateway_support import MainRef as _MainRef  # noqa: E402
from gateway_support.bound_routes import build_bound_routes as _build_bound_routes  # noqa: E402
_bound_routes = _build_bound_routes(_MainRef(globals()))
circuits_status = _bound_routes.circuits_status
app.include_router(_bound_routes.circuits_router)


# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_rate_limits = _bound_routes.ops_rate_limits
app.include_router(_bound_routes.rate_limits_router)


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_rate_limits_event = _bound_routes_h.ops_rate_limits_event
app.include_router(_bound_routes_h.ops_rate_limits_event_router)


# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
metrics_endpoint = _bound_routes.metrics_endpoint
app.include_router(_bound_routes.metrics_router)


# Alert cool-down (best-effort, per process): the same set of problems is notified at most once
# per OPS_ALERT_COOLDOWN_SEC (default 15 min; 0 disables). A NEW problem (different open-circuit
# set, or the error-rate condition appearing) alerts immediately; the state is cleared when the
# system is healthy again, and only recorded after a successful delivery so a failed send retries.
_OPS_ALERT_STATE: Dict[str, Any] = {}
_ops_alert_clock = time.monotonic


def _ops_alert_cooldown_sec() -> float:
    try:
        return max(0.0, float(((os.getenv("OPS_ALERT_COOLDOWN_SEC") or "").strip() or "900")))
    except (TypeError, ValueError):
        return 900.0


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_check_alert = _bound_routes_h.ops_check_alert
app.include_router(_bound_routes_h.ops_check_alert_router)


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_refresh_static_params = _bound_routes_h.ops_refresh_static_params
app.include_router(_bound_routes_h.ops_refresh_static_params_router)





# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_history = _bound_routes_h.market_history
app.include_router(_bound_routes_h.market_history_router)


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_db_status = _bound_routes_h.ops_db_status
app.include_router(_bound_routes_h.ops_db_status_router)



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
            if out["ok"] and hasattr(_kv_cache, "get"):
                # status() only reports a cached flag; do a real read so Neon compute is actually touched.
                try:
                    _kv_cache.get("__neon_keepalive__")
                except Exception as e:
                    out["ok"] = False
                    out["error"] = str(e)[:200]
            return out
        _kv_cache.get("__neon_keepalive__")
        out["ok"] = True
        out["neon_connected"] = True
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
from gateway_support.bound_routes_b import build_bound_routes_b as _build_bound_routes_b  # noqa: E402
_bound_routes_b = _build_bound_routes_b(_MainRef(globals()))
ops_neon_keepalive = _bound_routes_b.ops_neon_keepalive
app.include_router(_bound_routes_b.ops_neon_keepalive_router)

# E77: third batch (gateway_support/bound_routes_c.py); built once here, before its first include below.
from gateway_support.bound_routes_c import build_bound_routes_c as _build_bound_routes_c  # noqa: E402
_bound_routes_c = _build_bound_routes_c(_MainRef(globals()))
# E78: batch D (gateway_support/bound_routes_d.py); built once here, before its first include below.
from gateway_support.bound_routes_d import build_bound_routes_d as _build_bound_routes_d  # noqa: E402
_bound_routes_d = _build_bound_routes_d(_MainRef(globals()))
# E79: batch E (gateway_support/bound_routes_e.py); built once here, before its first include below.
from gateway_support.bound_routes_e import build_bound_routes_e as _build_bound_routes_e  # noqa: E402
_bound_routes_e = _build_bound_routes_e(_MainRef(globals()))
# E80: batch F (gateway_support/bound_routes_f.py); built once here, before its first include below.
from gateway_support.bound_routes_f import build_bound_routes_f as _build_bound_routes_f  # noqa: E402
_bound_routes_f = _build_bound_routes_f(_MainRef(globals()))
# E81: batch G (gateway_support/bound_routes_g.py); built once here, before its first include below.
from gateway_support.bound_routes_g import build_bound_routes_g as _build_bound_routes_g  # noqa: E402
_bound_routes_g = _build_bound_routes_g(_MainRef(globals()))


# E85: batch J (gateway_support/bound_routes_j.py); built once here, before its first include below.
from gateway_support import MainRef as _MainRef  # noqa: E402
from gateway_support.bound_routes_j import build_bound_routes_j as _build_bound_routes_j  # noqa: E402
_bound_routes_j = _build_bound_routes_j(_MainRef(globals()))
# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_wake_db_all = _bound_routes_j.ops_wake_db_all
app.include_router(_bound_routes_j.ops_wake_db_all_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_idle_tick = _bound_routes_f.ops_idle_tick
app.include_router(_bound_routes_f.ops_idle_tick_router)




# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_qstash_tick = _bound_routes_d.ops_qstash_tick
app.include_router(_bound_routes_d.ops_qstash_tick_router)


# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_qstash_publish = _bound_routes.ops_qstash_publish
app.include_router(_bound_routes.qstash_publish_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
wake_all_services = _bound_routes_e.wake_all_services
app.include_router(_bound_routes_e.wake_all_services_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
momentum_movers_route = _bound_routes_e.momentum_movers_route
app.include_router(_bound_routes_e.momentum_movers_route_router)


def _durable_cache_configured() -> bool:
    """True when a persistent cache backend is wired up: legacy Upstash Redis OR the Neon/Postgres/Oracle
    kv_cache database. Config only — never opens a connection, so it is safe for a liveness-style probe.

    /ready used to test `_redis` alone, but the Redis client is None by default and the Oracle VM
    (UPSTASH_* blank, ORACLE_DSN set) keeps everything in the kv_cache database, so /ready answered 503
    forever on a perfectly healthy deployment."""
    if _redis:
        return True
    try:
        neon_url = getattr(_kv_cache, "_neon_url", None)
        return bool(callable(neon_url) and neon_url())
    except Exception:
        return False


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
health = _bound_routes_f.health
app.include_router(_bound_routes_f.health_router)

# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ready = _bound_routes.ready
app.include_router(_bound_routes.ready_router)

# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
system_health = _bound_routes_j.system_health
app.include_router(_bound_routes_j.system_health_router)

# ── Wake all services ──────────────────────────────────────────────────
# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
wake_all_services_probe = _bound_routes_f.wake_all_services_probe
app.include_router(_bound_routes_f.wake_all_services_probe_router)

# ── Watchlist endpoints ──────────────────────────────────────────────────────
# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_watchlist = _bound_routes_e.get_watchlist
app.include_router(_bound_routes_e.get_watchlist_router)

def _drop_cache_keys(*keys) -> None:
    """Best-effort delete of cache keys from EVERY layer (kv/Neon, in-process,
    legacy Redis). It used to touch only the Redis client, which is None by
    default, so a "force refresh" / watchlist edit never cleared the real
    cached universe or last scan."""
    for key in keys:
        if _kv_cache is not None:
            try:
                _kv_cache.delete(key)
            except Exception:
                pass
        try:
            _mem_kv.pop(key, None)
            _mem_kv_exp.pop(key, None)
        except Exception:
            pass
        if _redis:
            try:
                _redis.delete(key)
            except Exception:
                pass


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
set_watchlist = _bound_routes_h.set_watchlist
app.include_router(_bound_routes_h.set_watchlist_router)

# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
add_to_watchlist = _bound_routes_h.add_to_watchlist
app.include_router(_bound_routes_h.add_to_watchlist_router)

# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
remove_from_watchlist = _bound_routes_e.remove_from_watchlist
app.include_router(_bound_routes_e.remove_from_watchlist_router)

# ── Added from first version: /events/{symbol} endpoint ─────────────────
# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_symbol_events = _bound_routes_e.get_symbol_events
app.include_router(_bound_routes_e.get_symbol_events_router)

# ── Searched symbols ────────────────────────────────────────────────────────
# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_searched_symbols = _bound_routes_e.get_searched_symbols
app.include_router(_bound_routes_e.get_searched_symbols_router)

# ── Stock decision ──────────────────────────────────────────────────────────
def _stock_budget_sec(name: str, default: float, lo: float, hi: float) -> float:
    """Blank / non-numeric / out-of-range env values fall back to `default` (never raise at import)."""
    raw = (os.getenv(name) or "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        return default
    if v != v or v < lo or v > hi:          # NaN or outside the allowed range
        return default
    return v


# /stock/{symbol} time budget. The decide call used to have a hard 20 s read timeout: the first
# analysis after a boot (cold caches, competing with the start-up warm-ups and the hot-picks scan)
# ran past it, the gateway gave up with a neutral HOLD, and the decision service's finished work was
# thrown away. Now: a longer per-call read timeout, ONE retry that can use what the first attempt
# warmed, and an overall deadline (kept under the browser's 120 s request timeout) that also bounds
# the enrichment fan-out and the Gemini summary.
STOCK_DECIDE_TIMEOUT_SEC = _stock_budget_sec("STOCK_DECIDE_TIMEOUT_SEC", 60.0, 5.0, 170.0)
STOCK_OVERALL_DEADLINE_SEC = _stock_budget_sec("STOCK_OVERALL_DEADLINE_SEC", 100.0, 10.0, 170.0)
STOCK_DECIDE_RETRY_MIN_SEC = _stock_budget_sec("STOCK_DECIDE_RETRY_MIN_SEC", 15.0, 1.0, 120.0)
_STOCK_SUMMARY_RESERVE_SEC = 3.0    # below this much budget left, skip Gemini and use the template


# Technical-analysis reasons that mean the indicators were computed from a minimal or fallback price
# history (a single quote/bhavcopy bar, a market-data fallback, or fewer than ~30 daily bars) rather than
# a full history. The technical service already writes these into reasons["technical"]; nothing used to
# turn them into a visible note, so a neutral 50 from one bar looked like a real technical read.
_THIN_TECHNICAL_MARKERS = (
    "limited history",
    "history temporarily thin",
    "price history unavailable",
    "fallback quote",
    "fallback technical",
    "technical built from market-data fallback",
    "market-data fallback",
    "full technicals retry",
    "ema trend: insufficient data",
    "macd: insufficient data",
)
THIN_TECHNICAL_FLAG = "Technicals on minimal price history"


def _thin_technical_history(result: dict, tech_reasons) -> bool:
    """True when the technical pillar rests on a minimal / fallback bar (see _THIN_TECHNICAL_MARKERS),
    or the decision service's own pillar map says the technical pillar is not live
    (data_quality.pillars.technical is False). The payload's overall `data_insufficient` is NOT used:
    it can be set for non-technical reasons."""
    dq = result.get("data_quality") if isinstance(result, dict) else None
    pillars = dq.get("pillars") if isinstance(dq, dict) else None
    if isinstance(pillars, dict) and pillars.get("technical") is False:
        return True
    blob = " ".join(str(r) for r in (tech_reasons or [])).lower()
    return any(m in blob for m in _THIN_TECHNICAL_MARKERS)


def _mark_thin_technical(result: dict) -> dict:
    """Scan / Hot Picks rows: add `technical_thin` (bool), the same test /stock/{symbol} uses for its
    "Technicals on minimal price history" data-quality flag (group105, log-audit item 12).

    Label only: it changes no score, decision or ranking, and it is set only on full analyses (rows built from
    the Data Feed fast path carry no technical reasons, so they get no field). Never raises."""
    try:
        if isinstance(result, dict):
            reasons = result.get("reasons")
            tech = reasons.get("technical") if isinstance(reasons, dict) else None
            result["technical_thin"] = bool(_thin_technical_history(result, tech or []))
    except Exception:
        pass
    return result


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stock_structure = _bound_routes_c.stock_structure
app.include_router(_bound_routes_c.stock_structure_router)


_CHART_TFS = ("1m", "5m", "15m", "30m", "1h", "1d")


# E87: batch L (gateway_support/bound_routes_l.py); built once here, before its first include below.
from gateway_support.bound_routes_l import build_bound_routes_l as _build_bound_routes_l  # noqa: E402
_bound_routes_l = _build_bound_routes_l(_MainRef(globals()))
# E87: moved to gateway_support/bound_routes_l.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stock_chart_context = _bound_routes_l.stock_chart_context
app.include_router(_bound_routes_l.stock_chart_context_router)


# E87: moved to gateway_support/bound_routes_l.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_stock_decision = _bound_routes_l.get_stock_decision
app.include_router(_bound_routes_l.get_stock_decision_router)


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
        logger.warning(f"Fundamental fetch failed for {symbol}: {str(e) or type(e).__name__}")

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
        logger.warning(f"News fetch failed for {symbol}: {str(e) or type(e).__name__}")
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
        logger.warning(f"Events fetch failed for {symbol}: {str(e) or type(e).__name__}")
    return None

# ── Legacy synchronous scan ──────────────────────────────────────────────────
# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
run_scan_post = _bound_routes_h.run_scan_post
app.include_router(_bound_routes_h.run_scan_post_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
run_scan = _bound_routes_f.run_scan
app.include_router(_bound_routes_f.run_scan_router)



# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
scan_batch = _bound_routes_d.scan_batch
app.include_router(_bound_routes_d.scan_batch_router)

# ── Async scan endpoints ──────────────────────────────────────────────────
# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
start_scan = _bound_routes_j.start_scan
app.include_router(_bound_routes_j.start_scan_router)

# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_last_scan = _bound_routes_e.get_last_scan
app.include_router(_bound_routes_e.get_last_scan_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_scan_status = _bound_routes_e.get_scan_status
app.include_router(_bound_routes_e.get_scan_status_router)



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

# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stream_market_scan = _bound_routes_j.stream_market_scan
app.include_router(_bound_routes_j.stream_market_scan_router)




def _structure_metric(name: str, value: float = 1.0, **labels: Any) -> None:
    """group357: fail-safe counter (a broken metrics registry or a zero count never raises into a route)."""
    try:
        if value:
            metrics.inc(name, float(value), **labels)
    except Exception:  # noqa: BLE001
        pass


# E54: main-owned helpers the sniper module needs, bound at call time (see gateway_routers/sniper.py).
import gateway_support  # noqa: E402


async def _cb_get_hook(*args, **kwargs):
    return await _cb_get(*args, **kwargs)  # looked up in main at call time: patching main._cb_get still works


def _structure_metric_hook(*args, **kwargs):
    return _structure_metric(*args, **kwargs)


gateway_support.bind(cb_get=_cb_get_hook, structure_metric=_structure_metric_hook, technical_url=TECHNICAL_URL)
# E54: moved to gateway_routers/sniper.py (code unchanged)
from gateway_routers.sniper import _sniper_attach_structure, _sniper_observe  # noqa: E402,F401


# E53: moved to gateway_routers/scan_extras.py (code unchanged); included here so route order is unchanged.
from gateway_routers.scan_extras import buys_edge_router as _buys_edge_router, find_buys_edge, find_buys_edge_evaluate  # noqa: E402,F401
app.include_router(_buys_edge_router)


# E54: moved to gateway_routers/sniper.py (code unchanged); included here so route order is unchanged.
from gateway_routers.sniper import sniper_router as _sniper_router, find_actionable_buys  # noqa: E402,F401
app.include_router(_sniper_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
cancel_scan = _bound_routes_c.cancel_scan
app.include_router(_bound_routes_c.cancel_scan_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
scan_stop_all = _bound_routes_c.scan_stop_all
app.include_router(_bound_routes_c.scan_stop_all_router)


# ── Watchlist-only scan ──────────────────────────────────────────────────
# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
scan_watchlist = _bound_routes_j.scan_watchlist
app.include_router(_bound_routes_j.scan_watchlist_router)

# ── Market routes ────────────────────────────────────────────────────────────
# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_session = _bound_routes_b.market_session
app.include_router(_bound_routes_b.market_session_router)


def _safe_pct(change, base) -> float:
    """change / base * 100, or 0.0 when the base is missing, non-positive or the
    result is not finite (a zero previous close used to give inf, which then
    poisoned the caches and made FastAPI fail to JSON-encode the response)."""
    try:
        c = float(change)
        b = float(base)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(c) or not math.isfinite(b) or b <= 0:
        return 0.0
    out = c / b * 100
    return out if math.isfinite(out) else 0.0


def _json_finite(obj) -> bool:
    """True when every float inside obj is finite (i.e. it is JSON-encodable)."""
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_json_finite(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_json_finite(v) for v in obj)
    return True


def _rank_market_rows(rows, key: str, reverse: bool, n: int = 10) -> list:
    """Top-n rows by a numeric key. A malformed row (not a dict, or a missing /
    non-numeric / non-finite key) is skipped instead of failing the endpoint."""
    scored = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        v = _hot_float(r.get(key))
        if v is None:
            continue
        scored.append((v, r))
    scored.sort(key=lambda t: t[0], reverse=reverse)
    return [r for _, r in scored[:n]]


def _movers_response(sorted_data: list) -> dict:
    """{data, count}, plus `stale: true` when any returned row came from the last-known list (group110)."""
    out = {"data": sorted_data, "count": len(sorted_data)}
    stale_rows = [r for r in sorted_data if isinstance(r, dict) and r.get("stale")]
    if stale_rows:
        out["stale"] = True
        since = next((r.get("stale_since") for r in stale_rows if r.get("stale_since")), None)
        if since:
            out["stale_since"] = since      # group111: when the last-known list was saved
    return out


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_top_gainers = _bound_routes_e.market_top_gainers
app.include_router(_bound_routes_e.market_top_gainers_router)

# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_top_losers = _bound_routes_e.market_top_losers
app.include_router(_bound_routes_e.market_top_losers_router)

# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_most_active = _bound_routes_e.market_most_active
app.include_router(_bound_routes_e.market_most_active_router)

def _trending_yf_row(sym):
    """E86: the last-resort direct-yfinance price for one trending symbol (moved unchanged out of market_trending's nested helper).
    Returns (price, change, change_pct), or None where the handler used to `continue` (fallback off, symbol not resolvable, empty history).
    Exceptions propagate to the caller's per-symbol try/except, exactly as before."""
    if not _direct_yf_ok():
        return None
    yf_ticker = resolve_ns_ticker(sym)
    if not yf_ticker:
        return None
    ticker = yf.Ticker(yf_ticker)
    hist = ticker.history(period="1d")
    if hist.empty:
        return None
    price = round(hist["Close"].iloc[-1], 2)
    change = round(hist["Close"].iloc[-1] - hist["Open"].iloc[-1], 2)
    _open = float(hist["Open"].iloc[-1])
    # A zero/invalid Open used to give inf, which FastAPI cannot
    # JSON-encode: one bad bar turned the whole route into a 500.
    change_pct = round(change / _open * 100, 2) if (math.isfinite(_open) and _open > 0) else None
    return price, change, change_pct


# E86: batch K (gateway_support/bound_routes_k.py); built once here, before its first include below.
from gateway_support import MainRef as _MainRef  # noqa: E402
from gateway_support.bound_routes_k import build_bound_routes_k as _build_bound_routes_k  # noqa: E402
_bound_routes_k = _build_bound_routes_k(_MainRef(globals()))
# E86: moved to gateway_support/bound_routes_k.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
market_trending = _bound_routes_k.market_trending
app.include_router(_bound_routes_k.market_trending_router)

def _direct_yf_ok() -> bool:
    """group278: may this service call yfinance itself as a LAST resort when market-data-service has nothing?
    GATEWAY_DIRECT_YFINANCE_FALLBACK=0 enforces the contract that prices come only through market-data (which
    already walks Dhan -> AngelOne -> yfinance). Default on, so nothing changes until it is turned off."""
    from yf_policy import direct_yf_ok     # group279: the same switch is read by the scanners and premarket
    return direct_yf_ok()


def _indices_via_md_on() -> bool:
    """group278: /market/indices reads ^NSEI / ^BSESN from market-data /history first (INDICES_VIA_MARKET_DATA, default on)."""
    return (os.getenv("INDICES_VIA_MARKET_DATA") or "").strip().lower() not in ("0", "false", "no", "off")


def _md_index_frame(symbol: str, period: str):
    """group278: daily OHLC of an index from market-data-service /history as a DataFrame shaped like
    yf.Ticker(...).history() (DatetimeIndex, Open/High/Low/Close/Volume), or None when market-data has nothing.
    Never raises. market-data decides Dhan -> AngelOne -> yfinance."""
    try:
        import pandas as pd
        from urllib.parse import quote as _urlquote
        md_period = "5d" if period in ("1d", "5d") else period
        resp = httpx.get(f"{MARKET_DATA_URL}/history/{_urlquote(symbol, safe='')}",
                         params={"period": md_period, "interval": "1d"}, timeout=12)
        if resp.status_code != 200:
            return None
        candles = (resp.json() or {}).get("candles") or []
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.columns = [str(c).capitalize() for c in df.columns]
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["Close"]).sort_index()
        if df.empty:
            return None
        if period == "1d":
            df = df.iloc[-1:]          # yfinance period="1d" is the latest session only
        return df
    except Exception as e:  # noqa: BLE001
        logger.debug("index history via market-data %s: %s", symbol, e)
        return None


class _IndexTicker:
    """group278: the part of yf.Ticker that /market/indices uses (.history(period=...)), served by market-data
    first and, only when that has nothing and _direct_yf_ok(), by yfinance as before."""

    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, period: str = "1d", **kw):
        if _indices_via_md_on():
            df = _md_index_frame(self.symbol, period)
            if df is not None:
                return df
        if _direct_yf_ok():
            return yf.Ticker(self.symbol).history(period=period, **kw)
        import pandas as pd
        return pd.DataFrame()


def _prev_session_change(ticker, last_price) -> Optional[dict]:
    """Change of ``last_price`` against the PREVIOUS SESSION's close, or None.

    Group 221 (2026-10-07). /market/indices fetches ``history(period="1d")``, which is a single
    row, so its ``len(hist) > 1`` previous-close branch never runs and ``nifty.change_pct`` is really
    "vs today's open". That is left as it is (the regime score and the dashboard were tuned on it);
    this reads the real previous close separately so a gate can see a gap-down day that has been
    flat since the open. Fail-soft: any problem -> None, never an exception.
    """
    try:
        closes = ticker.history(period="5d")["Close"].dropna()
        if len(closes) < 2:
            return None
        row = -2
        try:   # last row dated before today (IST) = a finished session, so it IS the previous close
            last_day = closes.index[-1].date()
            if last_day < datetime.now(IST).date():
                row = -1
        except Exception:
            row = -2   # no usable index dates (tests, odd frames): the second-last row
        prev = float(closes.iloc[row])
        last = float(last_price)
        if not (math.isfinite(prev) and math.isfinite(last)) or prev <= 0 or last <= 0:
            return None
        return {"prev_close": round(prev, 2),
                "change_pct": round(_safe_pct(last - prev, prev), 2)}
    except Exception as e:
        logger.debug("prev-session change unavailable: %s", e)
        return None


# ── IMPROVED /market/indices with IST time ──────────────────────────────
# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_market_indices = _bound_routes_f.get_market_indices
app.include_router(_bound_routes_f.get_market_indices_router)

# ── Universe preview + ≤ ₹5000 pre-filter ─────────────────────────────────
# Session 72 (open-issue #3): /scan/universe must never run past its callers'
# 25s client timeout. _get_momentum_movers() is a SYNC helper that on a cold
# cache does 4 NSE fetches + a blocking 25s httpx.get to market-data +
# optionally yfinance — it was called straight from this async handler, i.e.
# ON THE EVENT LOOP: a cold call froze the whole gateway (including /health)
# for tens of seconds. It now runs in a worker thread under a deadline; on
# timeout the thread keeps running (it warms MOMENTUM_MOVERS_CACHE_KEY for the
# next caller) and this request returns without movers, flagged partial.
SCAN_UNIVERSE_MOVERS_DEADLINE_S = float(((os.getenv("SCAN_UNIVERSE_MOVERS_DEADLINE_S") or "").strip() or "12"))
SCAN_UNIVERSE_BUILD_DEADLINE_S = float(((os.getenv("SCAN_UNIVERSE_BUILD_DEADLINE_S") or "").strip() or "20"))


def _scan_universe_cold_movers_deadline() -> float:
    """group203: SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S (default 5; 0 = off = always the full deadline)."""
    try:
        v = float(((os.getenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S") or "").strip() or "5"))
    except ValueError:
        return 5.0
    return v if v == v and v >= 0 else 5.0


def _movers_deadline_now() -> float:
    full = SCAN_UNIVERSE_MOVERS_DEADLINE_S
    cold = _scan_universe_cold_movers_deadline()
    if _MOVERS_EVER_READY or cold <= 0:
        return full
    return min(full, cold)


async def _movers_with_deadline() -> tuple[List[str], bool]:
    """(movers, partial). partial=True when the deadline hit before movers were ready.

    group203: before any movers pass has finished in this process (cold start) the wait is shortened, so a
    cached=true caller is not held for the full deadline while the startup warm-up is still running."""
    task = asyncio.ensure_future(asyncio.to_thread(_get_momentum_movers))
    deadline = _movers_deadline_now()
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=deadline), False
    except asyncio.TimeoutError:
        logger.warning("scan/universe: momentum movers not ready within %.0fs — returning without them "
                       "(computation continues in the background and will warm the cache)",
                       deadline)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return [], True
    except Exception as _me:
        logger.warning("scan/universe: momentum movers failed: %s", _me)
        return [], True


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_scan_universe = _bound_routes_c.get_scan_universe
app.include_router(_bound_routes_c.get_scan_universe_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_universe = _bound_routes_c.get_universe
app.include_router(_bound_routes_c.get_universe_router)

# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
clear_universe_cache = _bound_routes_f.clear_universe_cache
app.include_router(_bound_routes_f.clear_universe_cache_router)

# ── Notification endpoints ──────────────────────────────────────────────────
# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
notifications_health = _bound_routes_b.notifications_health
app.include_router(_bound_routes_b.notifications_health_router)

# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_notification_config = _bound_routes_b.get_notification_config
app.include_router(_bound_routes_b.get_notification_config_router)

# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
set_notification_config = _bound_routes_g.set_notification_config
app.include_router(_bound_routes_g.set_notification_config_router)

# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
delete_notification_channel = _bound_routes_g.delete_notification_channel
app.include_router(_bound_routes_g.delete_notification_channel_router)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
notifications_call_me = _bound_routes_g.notifications_call_me
app.include_router(_bound_routes_g.notifications_call_me_router)

# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
test_notification_channels = _bound_routes_g.test_notification_channels
app.include_router(_bound_routes_g.test_notification_channels_router)

# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
send_picks_to_telegram = _bound_routes_j.send_picks_to_telegram
app.include_router(_bound_routes_j.send_picks_to_telegram_router)

# ============================================================================
# Training Service Proxy Routes
# ============================================================================

# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
training_status = _bound_routes_b.training_status
app.include_router(_bound_routes_b.training_status_router)

# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
trigger_training = _bound_routes_g.trigger_training
app.include_router(_bound_routes_g.trigger_training_router)

# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
get_training_score = _bound_routes_b.get_training_score
app.include_router(_bound_routes_b.get_training_score_router)

# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
training_other_proxy = _bound_routes_h.training_other_proxy
app.include_router(_bound_routes_h.training_other_proxy_router)




# ── Stockky 🔥 Stocks – curated news / results / bulk / insider driven list ──
HOT_STOCKS_CACHE_KEY = "stockky:hot_stocks"
# Market hours: short cache (2–5 min) so UI stays light but not spammy.
# Off-hours: until next market open (see _hot_stocks_ttl).
HOT_STOCKS_TTL_OPEN_MIN = int(((os.getenv("HOT_STOCKS_TTL_OPEN_MIN") or "").strip() or "120"))    # 2 min
HOT_STOCKS_TTL_OPEN_MAX = int(((os.getenv("HOT_STOCKS_TTL_OPEN_MAX") or "").strip() or "300"))    # 5 min
HOT_STOCKS_TTL_OPEN_DEFAULT = int(((os.getenv("HOT_STOCKS_TTL_OPEN") or "").strip() or "180"))    # 3 min default when open


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
    if not _wake_pings_enabled():
        return
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


def _build_hot_conviction_extra(decide_full, last_decision):
    """Assemble the conviction fields a Hot Picks card renders — the trade
    levels (Entry/Target/Stop/Horizon) and the per-pillar score row
    (Tech/Fund/News/Pred/Mkt/Train) — from whatever decision record is already
    cached for the symbol. Prefers the full decide-cache, falls back to the
    lighter last_decision seed. Read-only; no network calls.

    Returns ONLY non-None fields so splatting it into a hot item never clobbers
    a real value with a null. This is the enrichment whose absence made every
    one of those fields show "—" on the cards.
    """
    out: dict = {}
    keys = (
        "combined_score", "technical_score", "fundamental_score", "news_score",
        "prediction_score", "market_score", "training_score",
        "entry_range", "target", "stop_loss", "holding_period",
        "holding_period_estimate", "confidence", "technical_thin",
    )
    # last_decision first, decide_full last → the fuller record wins.
    for src in (last_decision, decide_full):
        if not isinstance(src, dict):
            continue
        for k in keys:
            v = src.get(k)
            if v is not None and v != "N/A":
                out[k] = v
    # Derive a Horizon label when we have entry+target but no explicit estimate.
    if "holding_period_estimate" not in out:
        try:
            entry = out.get("entry_range")
            entry_px = None
            if isinstance(entry, dict):
                entry_px = entry.get("low") or entry.get("high")
            tgt = out.get("target")
            if entry_px and tgt:
                out["holding_period_estimate"] = _estimate_holding_period(
                    float(entry_px), float(tgt), ""
                )
        except Exception:
            pass
    return out


def _env_int(name: str, default: int, fallback_name: str = None) -> int:
    """int(os.environ[name]) with a safe default for unset/blank/non-numeric."""
    raw = os.getenv(name)
    if (raw is None or str(raw).strip() == "") and fallback_name:
        raw = os.getenv(fallback_name)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _hot_base(sym) -> str:
    """Canonical bare symbol: upper-case FIRST, then drop a .NS/.BO suffix.

    (Stripping before upper-casing left ``aaa.ns`` as ``AAA.NS``, which was
    then treated as a different symbol from ``AAA``.)
    """
    t = str(sym or "").strip().upper()
    for suf in (".NS", ".BO"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t


def _drop_dead_hot_symbols(seen: dict) -> list:
    """Remove confirmed-delisted and non-equity symbols from `seen` (normalised
    symbol -> original spelling) in place; return the dropped keys. Uses only the
    static KNOWN_DELISTED / non-equity rules, never renames a symbol, and keeps
    everything if symbol_aliases cannot be imported."""
    try:
        from symbol_aliases import is_known_delisted, is_non_equity_instrument
    except Exception:
        return []
    dead = [k for k in list(seen) if is_known_delisted(k) or is_non_equity_instrument(k)]
    for k in dead:
        seen.pop(k, None)
    return dead


def _hot_float(v):
    """float(v) or None when v is missing / non-numeric / non-finite."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# Positive catalyst wording — whole words/phrases only, so "bulk of staff"
# or "record loss" can't promote a name on a substring accident.
_CATALYST_POS_RE = re.compile(
    r"\b(?:surg(?:e|es|ed|ing)|jump(?:s|ed|ing)?|rall(?:y|ies|ied|ying)|beats?"
    r"|order wins?|wins?|bulk deals?|stake|upgrad(?:e|es|ed)"
    r"|record (?:profits?|highs?|revenues?|sales|earnings|orders?|growth|quarter))\b"
)
# Negative wording vetoes the promotion (e.g. "falls as it wins no orders").
_CATALYST_NEG_RE = re.compile(
    r"\b(?:loss(?:es)?|fall(?:s|ing)?|fell|drop(?:s|ped)?|plunge[sd]?|slump(?:s|ed)?"
    r"|layoffs?|laid off|downgrad(?:e|es|ed)|no orders?|wins? no|fraud|probe|penalty|default)\b"
)


def _has_catalyst_language(summary) -> bool:
    text = str(summary or "").lower()
    if not text:
        return False
    return bool(_CATALYST_POS_RE.search(text)) and not _CATALYST_NEG_RE.search(text)


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
                    s = _hot_base(r.get("symbol"))
                    if not s:
                        continue
                    if dec in ("BUY NOW", "PREPARE TO BUY"):
                        scan_syms.append(s)
                        try:
                            # Persist the FULL conviction picture (trade levels +
                            # per-pillar scores), not just decision/score/reasons.
                            # The Hot Picks cards read these back to render Entry/
                            # Target/Stop/Horizon and the Tech/Fund/News/Pred/Mkt/
                            # Train row — storing only 3 fields here is exactly why
                            # those showed "—".
                            _seed_dec = {
                                "decision": r.get("decision"),
                                "score": r.get("combined_score") or r.get("score"),
                                "reasons": r.get("reasons") or [],
                            }
                            for _k in (
                                "combined_score", "technical_score", "fundamental_score",
                                "news_score", "prediction_score", "market_score",
                                "training_score", "entry_range", "target", "stop_loss",
                                "holding_period", "holding_period_estimate", "confidence",
                                "technical_thin",
                            ):
                                _v = r.get(_k)
                                if _v is not None:
                                    _seed_dec[_k] = _v
                            _redis_set(
                                f"stockky:last_decision:{s}",
                                _seed_dec,
                                ttl=86400,
                            )
                        except Exception:
                            pass
    except Exception as e:
        logger.debug("hot seed from scan: %s", e)

    watch = await asyncio.to_thread(_load_watchlist) or []  # group361: off the event loop
    momentum = []
    news_syms = []
    event_syms = []
    try:
        momentum = (await asyncio.to_thread(_get_momentum_movers)) or []
    except Exception as e:
        logger.warning("hot momentum: %s", e)
    try:
        news_syms = (await asyncio.to_thread(_get_news_mentioned_symbols)) or []
    except Exception as e:
        logger.warning("hot news: %s", e)
    try:
        event_syms = (await asyncio.to_thread(_get_event_symbols)) or []
    except Exception as e:
        logger.warning("hot events: %s", e)

    # Catalyst sources first so they are never crowded out by scan seed
    _raw_universe = (
        list(momentum)[:80]
        + list(news_syms)[:60]
        + list(event_syms)[:80]
        + list(scan_syms)
        + list(watch)
        + list(_load_searched() or [])[:40]
        + list(await asyncio.to_thread(_get_recent_ipos) or [])[:20]  # group361: off the event loop
    )
    # De-duplicate on the NORMALISED symbol ("AAA" == "AAA.NS" == "aaa.ns"),
    # keeping the first spelling seen, so one name is never fetched twice.
    _seen_u: dict = {}
    for _u in _raw_universe:
        _k = _hot_base(_u)
        if _k and _k not in _seen_u:
            _seen_u[_k] = _u
    # Log-audit item 28 (group 99): this universe merges the watchlist, searched
    # symbols, news and events WITHOUT the delisted/non-equity gate that
    # _clean_equity_symbol applies to the scan universe, so confirmed-dead names
    # (AAKASH, ANNAPURNA) were fetched every run and 404'd on every upstream.
    _dropped_dead = _drop_dead_hot_symbols(_seen_u)
    if _dropped_dead:
        logger.info("stockky-hot universe: dropped %s delisted/non-equity symbol(s): %s",
                    len(_dropped_dead), ",".join(sorted(_dropped_dead)[:20]))
    universe = list(_seen_u.values())
    if not universe:
        universe = list(_get_nifty_indices() or [])[:80]
    # Full catalyst universe by default. Optional max_symbols only if caller caps.
    # Heavy work is processed in batches (CATALYST_BATCH_SIZE / HOT_BATCH_SIZE) with warm between batches.
    if max_symbols is not None:
        lim = max(10, int(max_symbols))
        universe = universe[:lim]
    else:
        lim = len(universe)
    batch_size = max(5, _env_int("HOT_BATCH_SIZE", 25, fallback_name="CATALYST_BATCH_SIZE"))
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
                base = _hot_base(sym)
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

                # Fuller conviction record for the card's trade levels + pillar
                # scores. Prefer the full decide-cache; fall back to the light
                # last_decision seed. Read-only KV lookups (no network).
                _decide_full = None
                try:
                    _decide_full = _redis_get(f"{DECIDE_CACHE_PREFIX}{base}")
                except Exception:
                    _decide_full = None
                conviction_extra = _build_hot_conviction_extra(_decide_full, decision_data)

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
                _nscore_raw = (news_data or {}).get("news_score")
                # A non-numeric news score is treated as "no score" instead of
                # dropping the symbol from every section (results rows too).
                _nscore_f = _hot_float(_nscore_raw)
                nscore = _nscore_raw if _nscore_f is not None else None
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
                if _nscore_f is not None and int(_nscore_f) >= 62 and hc >= 2:
                    catalyst_bits.append(f"Positive news flow (score {nscore}, {hc} headlines)")
                if news_summary and _has_catalyst_language(news_summary):
                    catalyst_bits.append("Catalyst language in news summary")
                if has_bulk_insider and bulk:
                    catalyst_bits.append("Bulk/block deal activity")
                if has_results and event_data and event_data.get("earnings_surprise"):
                    _es = event_data.get("earnings_surprise")
                    if isinstance(_es, dict):
                        try:
                            sp = float(_es.get("surprise_pct") or 0)
                            if sp > 0:
                                catalyst_bits.append(f"Positive earnings surprise {sp}%")
                        except (TypeError, ValueError):
                            catalyst_bits.append("Earnings/results event")
                    else:
                        # truthy non-dict (e.g. "beat"): a results event, but
                        # no parsable surprise — don't drop the whole symbol.
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
                news_ok = hc >= 2 and _nscore_f is not None and _nscore_f >= 55
                if news_ok and (actionable or has_results or has_bulk_insider or from_scan or hc >= 4):
                    news_driven.append({
                        **conviction_extra,
                        "symbol": base,
                        "decision": decision,
                        "score": score,
                        "news_score": (nscore if nscore is not None else conviction_extra.get("news_score")),
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
                        **conviction_extra,
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
                        **conviction_extra,
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
    # Pass 1: feed store (bhavcopy/Oracle) — cheap in-memory/KV reads.
    # Pass 2: AngelOne LTP waterfall via market-data-service /quote/{sym}
    #   for any symbol still at price=0 after pass 1.  Market-data-service
    #   already resolves AngelOne→yfinance→NSE in its /quote waterfall
    #   (decision #16), so a single concurrent batch of GET /quote/{sym}
    #   calls here gets live LTP during market hours and prevents hot-picks
    #   cards from showing "₹—" for symbols that are in-universe but whose
    #   feed-store row is stale (e.g. freshly-listed names, catalyst names
    #   outside the ≤₹5000 data-feed universe).
    try:
        _hot_store = _feed_store()
    except Exception:
        _hot_store = None
    # Pass 1: feed store
    if _hot_store is not None:
        for _bucket in (news_driven, results_driven, bulk_insider_driven):
            for _item in _bucket:
                try:
                    _row = await asyncio.to_thread(_hot_store.get_symbol, _item.get("symbol") or "")  # group303
                    _px = _feed_resolved_price(_row) if _row else 0.0
                    if _px > 0:
                        _item["price"] = _px
                        _item["close"] = _px
                except Exception:
                    pass
    # Pass 2: AngelOne / yfinance waterfall for anything still missing a price.
    # Collect all unpinned symbols, batch-fetch via _fetch_prices_bulk_async
    # (concurrent GET /quote/{sym} on market-data-service — AngelOne first).
    _still_missing = [
        _item
        for _bucket in (news_driven, results_driven, bulk_insider_driven)
        for _item in _bucket
        if not ((_hot_float(_item.get("price")) or 0) > 0 or (_hot_float(_item.get("close")) or 0) > 0)
    ]
    if _still_missing:
        _miss_syms = list({_item.get("symbol") or "" for _item in _still_missing if _item.get("symbol")})
        try:
            # group233: always bulk (even for < 15 symbols) and never one GET /quote per symbol.
            _live_prices: dict = await _fetch_prices_bulk_async(
                _miss_syms, client, bulk_min=1, per_symbol_fallback=False)
            if _live_prices:
                for _item in _still_missing:
                    _sym = _item.get("symbol") or ""
                    _lpx = _live_prices.get(_sym) or _live_prices.get(_sym.replace(".NS", "").replace(".BO", ""))
                    # One unparsable live price must not stop the pass for every
                    # later item: parse per item and skip just the bad one.
                    _lpx_f = _hot_float(_lpx)
                    if _lpx_f is not None and _lpx_f > 0:
                        _item["price"] = _lpx_f
                        _item["close"] = _lpx_f
                logger.info(
                    "stockky-hot price pass-2 (AngelOne/yf waterfall): %s/%s symbols resolved",
                    sum(1 for s in _miss_syms if _live_prices.get(s)),
                    len(_miss_syms),
                )
        except Exception as _pe:
            logger.debug("stockky-hot price pass-2 failed (non-fatal): %s", _pe)

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
        ttl = min(ttl, int(((os.getenv("HOT_PARTIAL_CACHE_TTL") or "").strip() or "300")))
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
        stored = await asyncio.to_thread(hotpicks_db_upsert, payload)  # group356: Oracle MERGE + commit, off the event loop
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

SYMBOL_LIST_CAP = 200


def _clean_symbol_list(raw, cap: int = SYMBOL_LIST_CAP) -> Optional[List[str]]:
    """Validate a caller-supplied symbol list.

    Accepts a list/tuple/set of strings or a comma/semicolon-separated string;
    drops non-strings and blanks, upper-cases, de-duplicates (keeping order)
    and caps the size. Returns None when nothing usable remains, so the caller
    falls back to its default universe.
    """
    if isinstance(raw, str):
        items = [x for x in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return None
    out: List[str] = []
    seen = set()
    for it in items:
        if not isinstance(it, str):
            continue
        t = it.strip().upper()
        if not t or len(t) > 32 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= cap:
            break
    return out or None


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_run_premarket_feed = _bound_routes_d.api_run_premarket_feed
app.include_router(_bound_routes_d.api_run_premarket_feed_router)



# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_repair_batch = _bound_routes_d.api_surprise_repair_batch
app.include_router(_bound_routes_d.api_surprise_repair_batch_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_audit = _bound_routes_c.api_surprise_audit
app.include_router(_bound_routes_c.api_surprise_audit_router)


SURPRISE_SCAN_DEADLINE_S = float(((os.getenv("SURPRISE_SCAN_DEADLINE_S") or "").strip() or "20"))

# 2026-10-06 (group 175): single-flight for the default full-universe surprise scan. A scan that outlived the
# 20 s deadline kept running in the background, but every later request (the Surprise tab, the real-trade-service
# candidate poll) started ANOTHER full scan over the same ~hundreds of symbols, so slow scans piled up and got
# slower (log: "exceeded 20s -- served last computed result (age 252s)"). While one is in flight, new callers for
# the same default scan now join it. Explicit `symbols` and `force_reload` calls are never shared.
# SURPRISE_SCAN_SINGLE_FLIGHT=0 restores one scan per request.
_surprise_scan_inflight: Optional["asyncio.Future"] = None


def _surprise_single_flight_enabled() -> bool:
    return (os.getenv("SURPRISE_SCAN_SINGLE_FLIGHT") or "").strip().lower() not in ("0", "false", "no", "off")


def _start_surprise_scan(engine, *, client, sym_list, force_reload: bool, cached: bool) -> "asyncio.Future":
    global _surprise_scan_inflight

    def _new():
        return asyncio.ensure_future(engine.scan(
            client=client,
            market_data_url=MARKET_DATA_URL,
            symbols=sym_list,
            force_reload_static=bool(force_reload),
            cached=bool(cached),
        ))

    if sym_list or force_reload or not _surprise_single_flight_enabled():
        return _new()
    cur = _surprise_scan_inflight
    try:
        if cur is not None and not cur.done() and cur.get_loop() is asyncio.get_running_loop():
            logger.debug("surprise/scan: joining the scan already in flight")
            return cur
    except Exception:  # noqa: BLE001 - never let the sharing check break a scan
        pass
    task = _new()
    _surprise_scan_inflight = task

    def _clear(t, _g=globals()):
        if _g.get("_surprise_scan_inflight") is t:
            _g["_surprise_scan_inflight"] = None

    task.add_done_callback(_clear)
    return task


def _surprise_closed_serve_enabled() -> bool:
    return (os.getenv("SURPRISE_CLOSED_SERVE_LAST") or "").strip().lower() not in ("0", "false", "no", "off")


async def _surprise_closed_market_result(engine, limit):
    """group302: when the market is closed (or a holiday) hand back the last saved Surprise result instead of
    starting a 970-symbol sweep nobody can use (2026-10-10, a Saturday: opening the tab started a full scan over a
    31-hour-old result: 0/0 on screen, a busy gateway, nothing new to learn). The engine only had this shortcut for
    cached=true callers and only when its result was newer than the last close. Returns None when a live scan should
    run (market not closed, nothing saved yet, or the caller forced it with refresh=true / force_reload=true /
    explicit symbols). SURPRISE_CLOSED_SERVE_LAST=0 restores the old behaviour."""
    try:
        if not _surprise_closed_serve_enabled():
            return None
        phase = _market_session_phase_ist()
        if phase not in ("closed", "holiday"):
            return None
        if getattr(engine, "_last_result", None) is None:
            loader = (getattr(engine, "_load_last_result_stale_from_durable_cache", None)
                      or getattr(engine, "_load_last_result_from_durable_cache", None))
            if callable(loader):
                await asyncio.to_thread(loader)
        last = getattr(engine, "_last_result", None)
        if last is None:
            return None
        age = max(0.0, time.time() - float(getattr(engine, "_last_scan_ts", 0.0) or 0.0))
        result = dict(last)
        result["from_cache"] = True
        result["market_closed"] = True
        result["market_closed_cache"] = True
        result["market_phase"] = phase
        result["cache_age_sec"] = round(age, 1)
        result["message"] = "Market closed - showing the last saved scan. Use Refresh Scan to run a fresh one."
        if limit is not None and isinstance(result.get("stocks"), list):
            result["stocks"] = result["stocks"][: max(0, int(limit))]
        return result
    except Exception as e:  # noqa: BLE001 - never let this shortcut break a scan
        logger.debug("surprise closed-market serve skipped: %s", e)
        return None


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_scan = _bound_routes_c.api_surprise_scan
app.include_router(_bound_routes_c.api_surprise_scan_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_scan_stream = _bound_routes_j.api_surprise_scan_stream
app.include_router(_bound_routes_j.api_surprise_scan_stream_router)



# E53: moved to gateway_routers/scan_extras.py (code unchanged); included here so route order is unchanged.
from gateway_routers.scan_extras import surprise_static_router as _surprise_static_router, api_surprise_static  # noqa: E402,F401
app.include_router(_surprise_static_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_notify_top_picks = _bound_routes_j.api_surprise_notify_top_picks
app.include_router(_bound_routes_j.api_surprise_notify_top_picks_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_scan_router as _ipo_scan_router, api_ipo_scan  # noqa: E402,F401
app.include_router(_ipo_scan_router)


# E49: moved to gateway_routers/surprise_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.surprise_jobs import ipo_status_router as _ipo_status_router, api_ipo_scan_status  # noqa: E402,F401
app.include_router(_ipo_status_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_list_router as _ipo_list_router, api_ipo_list  # noqa: E402,F401
app.include_router(_ipo_list_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_audit_router as _ipo_audit_router, api_ipo_audit  # noqa: E402,F401
app.include_router(_ipo_audit_router)


# E49: moved to gateway_routers/surprise_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.surprise_jobs import ipo_stop_router as _ipo_stop_router, api_ipo_stop  # noqa: E402,F401
app.include_router(_ipo_stop_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_add_router as _ipo_add_router, IpoAddRequest, api_ipo_add  # noqa: E402,F401
app.include_router(_ipo_add_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_repair_router as _ipo_repair_router, api_ipo_repair_batch  # noqa: E402,F401
app.include_router(_ipo_repair_router)


# E52: moved to gateway_routers/ipo_routes.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ipo_routes import ipo_purge_router as _ipo_purge_router, api_ipo_purge_non_equity  # noqa: E402,F401
app.include_router(_ipo_purge_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_ipo_notify_top_picks = _bound_routes_d.api_ipo_notify_top_picks
app.include_router(_bound_routes_d.api_ipo_notify_top_picks_router)


# E49: moved to gateway_routers/surprise_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.surprise_jobs import premarket_status_router as _premarket_status_router, api_surprise_premarket_status  # noqa: E402,F401
app.include_router(_premarket_status_router)


# E86: the handler used to create this with `global`; it now reads/writes it as ref._SURPRISE_SCHEMA_CONFIRMED, so it is defined here (False = schema not yet confirmed this process).
_SURPRISE_SCHEMA_CONFIRMED = False
# E86: moved to gateway_support/bound_routes_k.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_surprise_premarket_proxy = _bound_routes_k.api_surprise_premarket_proxy
app.include_router(_bound_routes_k.api_surprise_premarket_proxy_router)


# E49: moved to gateway_routers/surprise_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.surprise_jobs import surprise_stop_router as _surprise_stop_router, api_surprise_stop  # noqa: E402,F401
app.include_router(_surprise_stop_router)


async def _redis_get_bounded(key: str):
    """Read `key` WITHOUT blocking the event loop and WITHOUT an unbounded wait.

    2026-10-21 (correction of the earlier plain `await asyncio.to_thread(_redis_get, ...)`): kv_cache already bounds
    durable reads made from the loop thread (group359: KV_LOOP_READ_TIMEOUT_S, default 1.5 s, then a miss), but that
    bound still FREEZES the loop for the wait (two reads = the 3.2 s stall the watchdog caught). A bare to_thread
    removed the freeze but also the bound: a slow Oracle read then held /stockky-hot for the full Oracle time, and
    real-trade's 25 s timeout feeds its "api-gateway DOWN" circuit breaker. Here the read runs on a worker thread
    (the loop stays free) and the caller waits at most HOT_KV_READ_TIMEOUT_S (default = KV_LOOP_READ_TIMEOUT_S, 1.5 s);
    on timeout the answer is a miss, and the worker keeps going and warms the memory cache for the next call."""
    try:
        _t = float(((os.getenv("HOT_KV_READ_TIMEOUT_S") or os.getenv("KV_LOOP_READ_TIMEOUT_S") or "").strip() or "1.5"))
        if not (_t == _t and _t >= 0.1):
            _t = 1.5
    except (TypeError, ValueError):
        _t = 1.5
    try:
        return await asyncio.wait_for(asyncio.to_thread(_redis_get, key), timeout=_t)
    except asyncio.TimeoutError:
        logger.info("stockky-hot: kv read %s exceeded %.1fs - treated as a miss (worker keeps warming the cache)", key, _t)
        return None


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_endpoint = _bound_routes_d.stockky_hot_endpoint
app.include_router(_bound_routes_d.stockky_hot_endpoint_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
catalyst_alert_status = _bound_routes_f.catalyst_alert_status
app.include_router(_bound_routes_f.catalyst_alert_status_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
catalyst_alert_scan = _bound_routes_j.catalyst_alert_scan
app.include_router(_bound_routes_j.catalyst_alert_scan_router)


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
        msg = json.dumps({**payload, "channel": channel}, default=str)
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
        if not _direct_yf_ok():
            raise ValueError("direct yfinance fallback is off (GATEWAY_DIRECT_YFINANCE_FALLBACK=0)")
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
    """Push quotes for WS-watched symbols and evaluate price alerts.

    Quote pushes only happen while clients are connected AND watching symbols
    AND the session is live. Price alerts are server-side rules that don't
    depend on any browser being connected, so the alert pass now runs every
    live-session tick regardless of clients/watched symbols (it used to be
    skipped by every idle ``continue``).
    Power-off / activity pause: sleep only — zero upstream calls.
    Off-hours / weekend / holiday: idle sleep (no upstream spam, no alerts).
    """
    while True:
        try:
            if activity_paused() or scan_in_progress() or not _QUOTE_LOOP_ENABLED:
                await asyncio.sleep(30)
                continue
            phase = _market_session_phase_ist()
            # Only work while session is live (preopen/open/post)
            if phase not in ("preopen", "open", "post"):
                await asyncio.sleep(90)
                continue
        except Exception as e:
            logger.debug("quote loop gate: %s", e)
            await asyncio.sleep(20)
            continue

        # ── quote push: only with connected clients that watch symbols ──
        symbols = []
        try:
            # No connected clients → do not hit market-data for quotes at all
            symbols = ws_manager.all_watched_symbols() if getattr(ws_manager, "active", None) else []
            # Cap concurrent watched symbols to protect Yahoo/NSE / free-tier
            for sym in list(symbols)[:12]:
                # One failing symbol/broadcast must not abort the rest of the tick.
                try:
                    q = await asyncio.to_thread(_resolve_quote_price, sym)
                    if q:
                        await ws_manager.broadcast(f"quote:{sym}", {
                            **q,
                            "type": "quote",
                        })
                        try:
                            metrics.inc("stockky_ws_quote_push_total")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug("quote loop %s: %s", sym, e)
                    continue
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug("quote loop: %s", e)
        # Real-time price alerts (15-min cooldown per rule)
        # BUG FIX (session 55, event-loop-blocking-I/O audit): evaluate_price_alerts()
        # does a blocking kv_get() (and, on any trigger, a blocking kv_set() to
        # persist the cooldown); the httpx.post() notification call below was
        # also synchronous. Both run via asyncio.to_thread so a slow DB or slow
        # notification service can't stall every other request.
        try:
            from data_feed import evaluate_price_alerts
            triggered = await asyncio.to_thread(evaluate_price_alerts)
            for t in triggered[:5]:
                try:
                    await asyncio.to_thread(_wake_notification_service)
                    msg = (
                        f"⚡ {t.get('symbol')} ₹{t.get('current_price')} "
                        f"({t.get('direction')} target ₹{t.get('target_price')})"
                    )
                    await asyncio.to_thread(
                        httpx.post,
                        f"{NOTIFICATION_URL}/notify",
                        json={"title": f"Price Alert · {t.get('symbol')}", "message": msg, "channel": "all"},
                        timeout=8,
                    )
                    await ws_manager.broadcast("alerts", {
                        "type": "price_alert",
                        **{k: t.get(k) for k in ("symbol", "current_price", "target_price", "direction", "note", "id")},
                    })
                except Exception as e_alert:   # group 451: was `pass` - a price alert that could not be sent vanished without a trace (its cooldown is already used)
                    logger.warning("price alert for %s was NOT delivered: %s: %s", t.get("symbol"), type(e_alert).__name__, e_alert)
        except Exception as e:
            logger.debug("quote loop alerts: %s", e)
        # Open with quotes to push: 8s; preopen/post — or nothing to push (no
        # clients / no watched symbols): slower, so an idle loop stays cheap.
        phase = _market_session_phase_ist()
        await asyncio.sleep(8 if (phase == "open" and symbols) else 20)


def _ensure_quote_loop():
    global _quote_loop_task
    try:
        loop = asyncio.get_event_loop()
        if _quote_loop_task is None or _quote_loop_task.done():
            _quote_loop_task = loop.create_task(_quote_broadcast_loop())
    except Exception as e:
        logger.debug("quote loop start: %s", e)


def _ws_symbol_list(raw) -> List[str]:
    """Coerce a client-supplied ``symbols`` value to a list of strings.

    A bare string becomes a one-item list; non-iterables and non-string items
    are dropped instead of raising inside the socket loop.
    """
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        return [x for x in raw if isinstance(x, str)]
    return []


# E88: moved to gateway_support/bound_routes_m.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
websocket_endpoint = _bound_routes_m.websocket_endpoint
app.include_router(_bound_routes_m.websocket_endpoint_router)


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
        import structure_refresh as _sr
        out["structure_refresh"] = _sr.get_job()
    except Exception as e:
        logger.debug("jobs snapshot structure_refresh: %s", e)

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
    for key in ("data_feed", "refill_additional", "structure_refresh", "surprise_premarket", "ipo_scan"):
        entry = snap.get(key)
        if not isinstance(entry, dict):
            continue
        # The premarket progress reports ``is_running`` (no ``status``), so
        # honour that flag too or it never speeds up the push interval.
        if entry.get("status") in ("running", "computing", "started") or entry.get("is_running") is True:
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
                **snap,
                # Own keys last: a job source returning ``type`` / ``ts``
                # must not override the broadcast's envelope.
                "type": "jobs_snapshot",
                "ts": datetime.now(IST).isoformat(),
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


# group302: runtime tuning that runs first at boot.
#  - a larger default worker pool: asyncio.to_thread() (the surprise scan, movers sweep, scan-universe build, DB
#    reads) shares ONE default executor of min(32, cpu+4) threads (about 6 on this 2 vCPU box). The boot warms alone
#    could keep all of them busy, so every other to_thread call (the health-adjacent DB reads, audits) queued behind
#    them. GATEWAY_THREAD_POOL (default 32, 0 = leave the default) raises it; the threads are idle most of the time.
#  - the event-loop stall watchdog (loop_watchdog.py): logs the exact blocking line when the loop stalls > 3 s.
@_on_startup
async def _gateway_runtime_tuning():
    try:
        _n = int(((os.getenv("GATEWAY_THREAD_POOL") or "").strip() or "32"))
        if _n > 0:
            import concurrent.futures as _cf
            asyncio.get_running_loop().set_default_executor(
                _cf.ThreadPoolExecutor(max_workers=_n, thread_name_prefix="gw-worker")
            )
            logger.info("Startup: default thread pool set to %d workers (GATEWAY_THREAD_POOL)", _n)
    except Exception as e:  # noqa: BLE001
        logger.warning("Startup warning (thread pool tuning, non-fatal): %s", e)
    try:
        import loop_watchdog
        if loop_watchdog.start():
            logger.info("Startup: event-loop stall watchdog running (GET /ops/loop-lag)")
    except Exception as e:  # noqa: BLE001
        logger.debug("loop watchdog not started: %s", e)


# E49: moved to gateway_routers/ops_status.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ops_status import lag_router as _lag_router, ops_loop_lag, ops_db_latency  # noqa: E402,F401
app.include_router(_lag_router)


# group302: the boot warms (momentum movers -> scan universe, and the surprise scan) used to start together a few
# seconds after boot, on top of the first page loads. They now go one after another: movers/universe first (after
# GATEWAY_BOOT_WARM_DELAY_S, default 8 s open / GATEWAY_BOOT_WARM_CLOSED_DELAY_S, default 90 s when the market is
# closed - nobody needs them then and the UI is usually loading), the surprise warm waits for them to finish
# (at most GATEWAY_BOOT_WARM_SURPRISE_WAIT_S, default 120 s).
_boot_warm_movers_done: Optional["asyncio.Event"] = None


def _boot_warm_event() -> "asyncio.Event":
    global _boot_warm_movers_done
    if _boot_warm_movers_done is None:
        _boot_warm_movers_done = asyncio.Event()
    return _boot_warm_movers_done


def _boot_warm_env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(((os.getenv(name) or "").strip() or default)))
    except ValueError:
        return float(default)


def _boot_warm_movers_delay_sec() -> float:
    try:
        phase = _market_session_phase_ist()
    except Exception:  # noqa: BLE001
        phase = "open"
    if phase in ("closed", "holiday"):
        return _boot_warm_env_float("GATEWAY_BOOT_WARM_CLOSED_DELAY_S", 90.0)
    return _boot_warm_env_float("GATEWAY_BOOT_WARM_DELAY_S", 8.0)


# ── Startup cache pre-population ──────────────────────────────────────────
@_on_startup
async def startup_event():
    """Non-blocking indices warm — never delays app readiness."""
    try:
        try:
            await asyncio.to_thread(_redis.delete, INDICES_CACHE_KEY)  # group302: may be a durable DB call, keep it off the loop
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


# ── Startup surprise-scan cache pre-warm ────────────────────────────────────
# 2026-09-03 fix: SurpriseStockEngine.scan()'s cached=true fast path (used
# by real-trade-service's candidate_engine on every pipeline cycle, see
# _SOURCES["surprise"]) only has anything to serve once _last_result has
# been populated by at least one full scan. Right after a fresh deploy/
# restart, _last_result is None, so the very first cached=true call of the
# day is guaranteed to fall through to a full live scan — which can still
# exceed candidate_engine's 25s client timeout on a cold process (no warm
# quote cache, cold DB connection pool), producing exactly one
# "candidate fetch .../surprise/scan?cached=true... failed: ReadTimeout"
# right after every restart even though SURPRISE_CACHE_MAX_AGE_SEC=220s
# (this same file, surprise_scanner.py) already fixed the *recurring*
# version of this timeout for steady-state operation. Firing the first
# scan here, in the background, means _last_result is warm by the time the
# real pipeline cycle asks for it — same pattern as the indices warm just
# above, but as a genuine background task (create_task, not awaited)
# since a full surprise-universe scan can legitimately take longer than
# the 8s budget used for indices, and must never delay app readiness.
# group120 (startup quote burst): the warm above used to run a FULL quote sweep (one /quote call per
# liquid static symbol, ~1,000 of them) the instant the process started, every time, while
# market-data-service was itself still booting and the momentum/universe warms were running beside
# it. Three changes, all in this hook:
#   1. wait SURPRISE_BOOT_WARM_DELAY_SEC (default 20, 0 = old behaviour) so the other warms and
#      market-data-service settle first;
#   2. outside market hours (closed / holiday) restore the last durable result and skip the sweep:
#      the cached=true fast path only honours a result younger than SURPRISE_CACHE_MAX_AGE_SEC
#      (220s), so a night-time warm could never serve the next morning's first cycle anyway;
#   3. otherwise call scan(cached=True) so a restart seconds after a good scan reuses the durable
#      copy instead of sweeping again (restarts in quick succession were already observed).
def _surprise_boot_warm_delay_sec() -> float:
    try:
        return max(0.0, float(((os.getenv("SURPRISE_BOOT_WARM_DELAY_SEC") or "").strip() or "20")))
    except ValueError:
        return 20.0


# group222: the skip above only covered "closed"/"holiday". A boot at 08:37 IST is "preopen", so the full
# ~1,000-symbol /quote sweep still ran (2026-10-07 boot log: several hundred /quote calls within a minute,
# half of them falling through AngelOne-first to Yahoo). The sweep result is only honoured for
# SURPRISE_CACHE_MAX_AGE_SEC (220 s), so one that finishes more than ~5 minutes before 09:15 cannot serve the
# first cycle anyway. Pre-open boots earlier than SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC (default 300) before the
# open now take the same restore-and-skip path; later ones still warm. 0 restores the old behaviour.
def _surprise_boot_warm_preopen_lead_sec() -> float:
    try:
        return max(0.0, float(((os.getenv("SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC") or "").strip() or "300")))
    except ValueError:
        return 300.0


def _seconds_to_market_open_ist() -> float:
    """Seconds from now until today's 09:15 IST (<= 0 once it has passed)."""
    now = datetime.now(IST)
    return (now.replace(hour=9, minute=15, second=0, microsecond=0) - now).total_seconds()


def _surprise_boot_warm_skip_reason() -> Optional[str]:
    """'closed' / 'not open yet' when the boot quote sweep should be skipped (a saved result is restored
    instead), else None. Never raises."""
    try:
        phase = _market_session_phase_ist()
        if phase in ("closed", "holiday"):
            return "closed"
        if phase == "preopen":
            lead = _surprise_boot_warm_preopen_lead_sec()
            if lead > 0 and _seconds_to_market_open_ist() > lead:
                return "not open yet"
    except Exception:  # noqa: BLE001
        pass
    return None


@_on_startup
async def _warm_surprise_scan_cache():
    async def _warm():
        try:
            delay = _surprise_boot_warm_delay_sec()
            if delay > 0:
                await asyncio.sleep(delay)
            # group302: let the movers/universe warm finish first (bounded wait) so the two never overlap.
            try:
                await asyncio.wait_for(_boot_warm_event().wait(), timeout=_boot_warm_env_float("GATEWAY_BOOT_WARM_SURPRISE_WAIT_S", 120.0))
            except asyncio.TimeoutError:
                logger.info("Startup: surprise warm no longer waiting for the movers warm")
            from surprise_scanner import surprise_engine
            _skip_why = _surprise_boot_warm_skip_reason()
            if _skip_why:
                # group132: ONE stale read, tried first. group131 ran the plain read first, but kv_cache's
                # plain read DELETES an expired row, so the stale read that followed never found it. The
                # stale read accepts expired rows and removes nothing (freshness is age-checked in scan()).
                # An engine without it (older build) falls back to the plain read as before.
                loader = (getattr(surprise_engine, "_load_last_result_stale_from_durable_cache", None)
                          or getattr(surprise_engine, "_load_last_result_from_durable_cache", None))
                if callable(loader):
                    await asyncio.to_thread(loader)
                if getattr(surprise_engine, "_last_result", None) is not None:
                    logger.info("Startup: market %s — restored the last surprise/scan result, skipped the boot quote sweep", _skip_why)
                    return
            client = _get_http_client()
            # group356 (14 Oct 10:38 boot log: TWO full scans finished, 216 rows in 56 s and 192 rows in 67 s, the second one
            # degraded with over_budget=102): this warm called engine.scan() directly, so it never joined the single-flight added
            # in group 175 - the first /surprise/scan request that arrived during the warm started a second full sweep.
            # It now goes through _start_surprise_scan(), so a request that arrives mid-warm joins THIS scan (and this warm joins
            # one a request already started).
            await _start_surprise_scan(surprise_engine, client=client, sym_list=None, force_reload=False, cached=True)
            logger.info("Startup: surprise/scan cache pre-warmed (first pipeline cycle will hit cached=true)")
        except Exception as e:
            logger.warning("Startup warning (surprise-scan warm, non-fatal): %s", e)

    try:
        asyncio.create_task(_warm())
    except Exception as e:
        logger.debug("surprise-scan warm task not scheduled: %s", e)


# ── Startup momentum-movers cache pre-warm ──────────────────────────────────
# 2026-09-18 fix, same pattern/reason as _warm_surprise_scan_cache above:
# _get_momentum_movers() now caches its result (MOMENTUM_MOVERS_CACHE_KEY,
# see its own docstring), but right after a fresh deploy/restart that cache
# is empty, so the first caller of the day — which in practice is usually
# real-trade-service's candidate_engine hitting /scan/universe within
# seconds of both services coming up together — still pays the full
# uncached cost (NSE boards + an AngelOne whole-market quote sweep) and can
# trip its own client-side ReadTimeout. Firing one computation here, as a
# genuine background task (not awaited, never delays app readiness — same
# reasoning as the surprise-scan warm above), means the cache is already
# populated by the time the real pipeline's first cycle asks for it.
@_on_startup
async def _warm_momentum_movers_cache():
    async def _warm():
        try:
            await _warm_inner()
        finally:
            _boot_warm_event().set()

    async def _warm_inner():
        try:
            _d = _boot_warm_movers_delay_sec()
            if _d > 0:
                await asyncio.sleep(_d)
            await asyncio.to_thread(_get_momentum_movers)
            logger.info("Startup: momentum-movers cache pre-warmed (first /scan/universe call will be fast)")
        except Exception as e:
            logger.warning("Startup warning (momentum-movers warm, non-fatal): %s", e)
            return
        # _build_scan_universe() has its own durable cache (SCAN_UNIVERSE_KEY,
        # 30m in market hours / 6h off-hours) that can independently be cold
        # after a restart even once momentum movers is warm — it also pulls
        # NSE securities/indices, bulk-deal symbols, 52w-extreme symbols, and
        # news-mentioned symbols. Warm it right after so /scan/universe's
        # OTHER dependency is ready too, not just the movers half.
        try:
            # group196: a cold live key + a stored stale copy used to make this warm just re-serve the stale copy
            _live = await asyncio.to_thread(_redis_get, SCAN_UNIVERSE_KEY)  # group302: durable read, off the loop
            if isinstance(_live, list) and _live:
                await asyncio.to_thread(_build_scan_universe)
            else:
                await asyncio.to_thread(_build_scan_universe_fresh)
            logger.info("Startup: scan-universe cache pre-warmed (first /scan/universe call will be fast)")
        except Exception as e:
            logger.warning("Startup warning (scan-universe warm, non-fatal): %s", e)

    try:
        asyncio.create_task(_warm())
    except Exception as e:
        logger.debug("momentum-movers/scan-universe warm task not scheduled: %s", e)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_circuit_reset = _bound_routes_g.ops_circuit_reset
app.include_router(_bound_routes_g.ops_circuit_reset_router)


# E49: moved to gateway_routers/ops_status.py (code unchanged); included here so route order is unchanged.
from gateway_routers.ops_status import circuit_router as _circuit_router, ops_circuit_status  # noqa: E402,F401
app.include_router(_circuit_router)


# ── Overnight orchestrator proxy (2026-09-11) ────────────────────────────────
# Thin proxy to notification-scheduler-service's /scheduler/overnight/*
# routes (see that service's scheduler/overnight_orchestrator.py) so the
# frontend Settings page only ever needs to know about api-gateway, the
# same way it already reaches every other backend feature — it never needs
# its own separate stored URL for the scheduler service the way real-trade
# does (that one's a different trust boundary; this one isn't).
def _overnight_proxy_body(r):
    """Relay the scheduler's JSON body, but never present an upstream 4xx/5xx as a plain 200.

    A failed upstream used to come back as its error body with HTTP 200, so the Settings page could not
    tell "scheduler down" from a normal reply. Same {"ok": False, "error": ...} envelope as the
    exception path, with the upstream body kept under `detail`."""
    body = r.json()
    status = getattr(r, "status_code", 200)
    if isinstance(status, int) and status >= 400:
        return {"ok": False, "error": f"scheduler returned HTTP {status}", "detail": body}
    return body


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_overnight_status = _bound_routes_b.ops_overnight_status
app.include_router(_bound_routes_b.ops_overnight_status_router)


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_overnight_get_config = _bound_routes_b.ops_overnight_get_config
app.include_router(_bound_routes_b.ops_overnight_get_config_router)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_overnight_set_config = _bound_routes_g.ops_overnight_set_config
app.include_router(_bound_routes_g.ops_overnight_set_config_router)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_overnight_run = _bound_routes_g.ops_overnight_run
app.include_router(_bound_routes_g.ops_overnight_run_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_keepalive = _bound_routes_f.ops_keepalive
app.include_router(_bound_routes_f.ops_keepalive_router)

# ═══════════════════════════════════════════════════════════════════════════
# Data Feed — slow fields (12–24h) for free-tier rate-limit relief
# ═══════════════════════════════════════════════════════════════════════════



# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_power_off = _bound_routes_g.ops_power_off
app.include_router(_bound_routes_g.ops_power_off_router)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_resume_activity = _bound_routes_g.ops_resume_activity
app.include_router(_bound_routes_g.ops_resume_activity_router)


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
ops_activity_status = _bound_routes_b.ops_activity_status
app.include_router(_bound_routes_b.ops_activity_status_router)



# E49: moved to gateway_routers/feed_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.feed_jobs import bulk_cache_router as _bulk_cache_router, api_bulk_quote_cache  # noqa: E402,F401
app.include_router(_bulk_cache_router)


# E48: the price-alert routes moved to gateway_routers/price_alerts.py (code unchanged). Included here, where they used to be, so route order is the
# same; the handler names are re-exported so `main.api_list_price_alerts` and `gw.api_add_price_alert(...)` keep working.
from gateway_routers.price_alerts import (  # noqa: E402
    router as _price_alerts_router,
    api_list_price_alerts,
    api_add_price_alert,
    api_delete_price_alert,
)
app.include_router(_price_alerts_router)
# E51: scheduler alert switches for the calibration page (admin-guarded proxy, gateway_routers/scheduler_control.py).
from gateway_routers.scheduler_control import scheduler_control_router as _scheduler_control_router, scheduler_control_proxy  # noqa: E402,F401
app.include_router(_scheduler_control_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_evaluate_price_alerts = _bound_routes_c.api_evaluate_price_alerts
app.include_router(_bound_routes_c.api_evaluate_price_alerts_router)


# E81: moved to gateway_support/bound_routes_g.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
hard_reset_database = _bound_routes_g.hard_reset_database
app.include_router(_bound_routes_g.hard_reset_database_router)



def _bulk_feed_include_index() -> bool:
    """group426: BULK_FEED_INCLUDE_INDEX (default on, blank-safe): also price symbols already in the feed index, not only the scan universe."""
    v = ((os.getenv("BULK_FEED_INCLUDE_INDEX") or "").strip() or "1").lower()
    return v not in ("0", "false", "no", "off")


# E86: moved to gateway_support/bound_routes_k.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
start_bulk_feed = _bound_routes_k.start_bulk_feed
app.include_router(_bound_routes_k.start_bulk_feed_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
refresh_prepare_to_buy = _bound_routes_f.refresh_prepare_to_buy
app.include_router(_bound_routes_f.refresh_prepare_to_buy_router)



_STRUCTURE_REFRESH_START_LOCK = threading.Lock()


def _start_structure_refresh(force: bool = False, dry_run: bool = False, limit: Optional[int] = None) -> Dict[str, Any]:
    """group360: start the post-close `structure_only` refresh of the feed rows' ms_* keys on a daemon thread (blocking planning reads: call this
    from a worker thread). One run at a time; a second caller gets `already_running`. `dry_run` only reports what would be refreshed.
    STRUCTURE_REFRESH_ENABLED=0 answers `disabled`. Never raises into the route or the QStash tick."""
    if not _STRUCTURE_REFRESH_START_LOCK.acquire(blocking=False):
        return {"ok": True, "already_running": True, "status": "running", "message": "A structure refresh is already starting"}
    try:
        return _start_structure_refresh_locked(force, dry_run, limit)
    except Exception as e:  # noqa: BLE001
        logger.exception("structure refresh start failed: %s", e)
        return {"ok": False, "status": "error", "error": str(e)[:200]}
    finally:
        _STRUCTURE_REFRESH_START_LOCK.release()


def _start_structure_refresh_locked(force: bool, dry_run: bool, limit: Optional[int]) -> Dict[str, Any]:
    import structure_refresh as _sr
    if not _sr.enabled():
        return {"ok": False, "status": "disabled", "message": "STRUCTURE_REFRESH_ENABLED=0"}
    job = _sr.get_job()
    if job.get("status") == "running":
        return {"ok": True, "already_running": True, **job}
    store = _feed_store()
    if dry_run:   # a dry run has to answer with the selection, so it plans before returning
        syms, tally, expected = _sr.plan(store, force=bool(force), limit=limit)
        plan_info = {"selection": tally, "expected_bar_date": str(expected), "total": len(syms)}
        return {"ok": True, "dry_run": True, "status": "dry_run", "would_refresh": len(syms), "symbols": syms[:50], **plan_info}
    # group 486: planning reads EVERY feed row (a slow store read). It used to run before this function returned, so the POST blocked for the whole read
    # and the daily_close job timed out at 60 s on 2026-10-21 although the docstring says "returns at once". Plan inside the worker thread instead.
    _sr._set_job(status="running", message="Structure refresh: planning (reading the feed rows)", processed=0, total=0, force=bool(force), selection={})

    def _worker() -> None:
        try:
            syms, tally, expected = _sr.plan(store, force=bool(force), limit=limit)
            if not syms:
                _sr._set_job(status="done", message="Nothing to do: every feed row already has the latest completed daily structure",
                             processed=0, total=0, selection=tally, expected_bar_date=str(expected))
                return
            _sr._set_job(status="running", message=f"Structure refresh starting for {len(syms)} symbols", processed=0, total=len(syms),
                         selection=tally, expected_bar_date=str(expected))
            with httpx.Client() as client:
                _sr.run(syms, store, _sr.make_fetcher(client), force=bool(force), tally=tally)
        except Exception as e:  # noqa: BLE001
            logger.exception("structure_refresh worker: %s", e)
            try:
                _sr._set_job(status="error", message=str(e)[:240])
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_worker, name="structure-refresh", daemon=True).start()
    return {"ok": True, "status": "running", "message": "Structure refresh started (planning in the background); poll /data-feed/structure-refresh/status"}


_STRUCTURE_CATCHUP_TASK: Optional["asyncio.Task"] = None


async def _structure_refresh_catchup_loop() -> None:
    """group378: safety net for the post-close structure refresh. Every STRUCTURE_REFRESH_CATCHUP_INTERVAL_S (default 30 min), when the market
    is closed and no refresh is running, start the normal refresh; it selects only rows with no / an old structure, so it is a no-op once
    the feed is current. Covers a missed QStash / GitHub trigger and a restart after 16:00 IST. Never raises."""
    import structure_refresh as _sr
    try:
        await asyncio.sleep(_sr.catchup_initial_delay_s())
    except asyncio.CancelledError:
        return
    while True:
        try:
            if _sr.catchup_wanted(_market_session_phase_ist(), _sr.get_job().get("status")):
                res = await asyncio.to_thread(_start_structure_refresh, False, False, None)
                logger.info("structure refresh catch-up: %s (%s)", res.get("status"), res.get("message") or res.get("selection") or "")
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.debug("structure refresh catch-up tick failed (ignored): %s", e)
        try:
            await asyncio.sleep(_sr.catchup_interval_s())
        except asyncio.CancelledError:
            return


@_on_startup
async def _start_structure_refresh_catchup():
    global _STRUCTURE_CATCHUP_TASK
    try:
        import structure_refresh as _sr
        if not _sr.catchup_enabled():
            logger.info("Startup: structure refresh catch-up off (STRUCTURE_REFRESH_ENABLED / STRUCTURE_REFRESH_CATCHUP)")
            return
        if _STRUCTURE_CATCHUP_TASK is None or _STRUCTURE_CATCHUP_TASK.done():
            _STRUCTURE_CATCHUP_TASK = asyncio.create_task(_structure_refresh_catchup_loop(), name="structure-refresh-catchup")
            logger.info("Startup: structure refresh catch-up loop started (every %ds when the market is closed)", int(_sr.catchup_interval_s()))
    except Exception as e:  # noqa: BLE001
        logger.warning("Startup warning (structure refresh catch-up, non-fatal): %s", e)


@_on_shutdown
async def _stop_structure_refresh_catchup():
    t = _STRUCTURE_CATCHUP_TASK
    if t is not None and not t.done():
        t.cancel()


# E87: moved to gateway_support/bound_routes_l.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_structure_refresh_status = _bound_routes_l.data_feed_structure_refresh_status
app.include_router(_bound_routes_l.data_feed_structure_refresh_status_router)


# E87: moved to gateway_support/bound_routes_l.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_structure_refresh = _bound_routes_l.data_feed_structure_refresh
app.include_router(_bound_routes_l.data_feed_structure_refresh_router)


# E49: moved to gateway_routers/feed_jobs.py (code unchanged); included here so route order is unchanged.
from gateway_routers.feed_jobs import feed_job_router as _feed_job_router, data_feed_structure_refresh_stop, data_feed_refill_status  # noqa: E402,F401
app.include_router(_feed_job_router)


# E86: moved to gateway_support/bound_routes_k.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_refill_additional = _bound_routes_k.data_feed_refill_additional
app.include_router(_bound_routes_k.data_feed_refill_additional_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_meta = _bound_routes_e.data_feed_meta
app.include_router(_bound_routes_e.data_feed_meta_router)


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_status = _bound_routes_f.data_feed_status
app.include_router(_bound_routes_f.data_feed_status_router)


def _data_feed_normalize_enabled() -> bool:
    return (os.getenv("DATA_FEED_STATUS_NORMALIZE") or "").strip().lower() not in ("0", "false", "no", "off")


def _normalize_data_feed_status(store, job: dict, meta: dict, fed_count: int, last_ok):
    """group302: make /data-feed/status tell one consistent story.

    - "Last success" is a real feed run, never a boot heal. Older boots stamped the restart time into the meta;
      when the stored message is a boot heal and the job has a finish time, that finish time is the real one
      (and the meta is repaired once).
    - elapsed / ETA exist only while a job is running (a dead job's 8-day-old started_at used to print 675174 s).
    - one count: the stocks held in the feed. meta.stock_count (only ever written as 0 by a hard reset) and
      meta.last_count follow it; `partial` is only true when the last run really stopped short of its universe.
    DATA_FEED_STATUS_NORMALIZE=0 returns the raw job/meta as before.
    """
    job = dict(job or {})
    meta = dict(meta or {})
    if _data_feed_normalize_enabled():
        try:
            status = str(job.get("status") or "idle").lower()
            if status != "running":
                job["elapsed_sec"] = 0
                job["estimated_remaining_sec"] = 0
            fin = job.get("finished_at")
            if (
                status != "running"
                and fin
                and last_ok
                and str(meta.get("last_message") or "").startswith("Boot heal")
                and str(last_ok) > str(fin)
            ):
                last_ok = fin
                meta["last_success_at"] = fin
                try:
                    store.set_meta(last_success_at=fin)
                except Exception:  # noqa: BLE001 - repair is best effort
                    pass
            meta["stock_count"] = fed_count
            meta["last_count"] = fed_count
            _proc = int(job.get("processed") or 0)
            _tot = int(job.get("total") or 0)
            if status != "running" and _tot > 0 and _proc >= _tot:
                meta["partial"] = False
            job["universe_size"] = int(meta.get("universe_size") or _tot or 0)
        except Exception as e:  # noqa: BLE001 - never let tidying break the status call
            logger.debug("data-feed status normalize skipped: %s", e)
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
AUDIT_TTL_SEC = float(((os.getenv("AUDIT_TTL_SEC") or "").strip() or "20"))
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


# E55: moved to gateway_support/bound_routes.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
audit_missing_feed_data = _bound_routes.audit_missing_feed_data
app.include_router(_bound_routes.audit_missing_router)


def _compute_feed_audit(limit: int) -> dict:
    """The actual (synchronous, DB/store-backed) audit walk — see the
    run_in_threadpool BUG FIX note on audit_missing_feed_data above for why
    this lives in its own plain function instead of running inline there."""
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


# E80: moved to gateway_support/bound_routes_f.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
purge_over_cap_feed_symbols = _bound_routes_f.purge_over_cap_feed_symbols
app.include_router(_bound_routes_f.purge_over_cap_feed_symbols_router)


# Single-segment data-feed ACTION names that are registered POST-only above. A GET on one of them
# used to fall into the `/data-feed/{symbol}` catch-all below and come back as a 200
# `{"ok": False, "detail": "No data feed entry"}` for a bogus "symbol" (harmless, but misleading: a
# browser/curl GET looked like it had "worked"). They now get the 405 the method deserves. The match
# is exact and lowercase, so real (uppercase) NSE symbols can never collide with it.
_FEED_POST_ONLY_ACTIONS = frozenset({
    "hard-reset", "start-bulk-feed", "refresh-prepare-to-buy", "refill-additional", "structure-refresh",
    "purge-over-cap", "update", "batch", "update-batch", "repair-batch", "repair-all",
    "run", "stop", "resume",
})


# E87: moved to gateway_support/bound_routes_l.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_symbol = _bound_routes_l.data_feed_symbol
app.include_router(_bound_routes_l.data_feed_symbol_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_update_single = _bound_routes_d.data_feed_update_single
app.include_router(_bound_routes_d.data_feed_update_single_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_update_batch = _bound_routes_d.data_feed_update_batch
app.include_router(_bound_routes_d.data_feed_update_batch_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_update_batch_refresh = _bound_routes_d.data_feed_update_batch_refresh
app.include_router(_bound_routes_d.data_feed_update_batch_refresh_router)


# ── Surgical Data Repair (audit + non-destructive patch) ───────────────────
# _REQUIRED_FEED_FIELDS defined above (before audit-missing route)

REPAIR_COOLDOWN_SEC = float(((os.getenv("REPAIR_COOLDOWN_SEC") or "").strip() or "0.5"))


def _safe_float(val, default: float = 0.0) -> float:
    """Parse floats safely including NSE comma formats; never raises.
    NaN and +/-infinity (including the strings "nan" / "inf") are not usable numbers and return `default`."""
    if val is None or val == "":
        return default
    try:
        if isinstance(val, (int, float)):
            f = float(val)
            return f if math.isfinite(f) else default  # NaN / inf guard
        s = str(val).replace(",", "").replace(" ", "").strip()
        if not s or s.upper() in ("-", "NA", "N/A", "NONE", "NULL"):
            return default
        f = float(s)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError, OverflowError):
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
        # Flat value first, then the nested metrics value for the SAME key. A flat 0 / "-" / junk used to
        # win (`0 not in (None, "")`), so a usable metrics value for that key was never consulted.
        for raw in (data.get(k), m.get(k)):
            try:
                s = str(raw or "").replace(",", "").replace(" ", "").strip()
                if not s or s.upper() in ("-", "NA", "N/A"):
                    continue
                v = float(s)
                if v > 0 and math.isfinite(v):
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
    current = dict(await asyncio.to_thread(store.get_symbol, base) or {})  # group303

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
            await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
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
            await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
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

    market_url = ((os.getenv("MARKET_DATA_URL") or "").strip() or MARKET_DATA_URL or "").strip().rstrip("/")
    technical_url = ((os.getenv("TECHNICAL_URL") or "").strip() or TECHNICAL_URL or "").strip().rstrip("/")
    fundamental_url = ((os.getenv("FUNDAMENTAL_URL") or "").strip() or FUNDAMENTAL_URL or "").strip().rstrip("/")
    news_url = ((os.getenv("NEWS_URL") or "").strip() or NEWS_URL or "").strip().rstrip("/")

    # Genuinely delisted/merged symbols (TATAMTRDVR etc.) and known non-NSE
    # tickers: purge immediately instead of burning a /quote round trip that
    # can only ever fail — same rationale as the known-high-price short
    # circuit above. This is checked before the "price" fetch below so a
    # symbol like TATAMTRDVR never even reaches market-data-service.
    try:
        from symbol_aliases import is_known_delisted as _is_known_delisted
        from symbol_aliases import is_learned_delisted as _is_learned_delisted
        from symbol_aliases import MAX_FAILURE_STREAK as _MAX_FAILURE_STREAK
        from symbol_aliases import resolve_with_fallback as _resolve_with_fallback
    except Exception:
        _is_known_delisted = lambda _s: False  # noqa: E731
        _is_learned_delisted = lambda _s: False  # noqa: E731
        _MAX_FAILURE_STREAK = 5
        _resolve_with_fallback = None
    if _is_known_delisted(base):
        try:
            await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
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

    # Self-learned delisting (rate_limiter.py's yfinance monkeypatch already
    # gave up on this symbol after MAX_FAILURE_STREAK consecutive failures —
    # see symbol_aliases.record_resolution_failure/is_learned_delisted). This
    # is exactly why Repair used to return 200 "success" with patched_fields
    # empty and no error, forever: nothing purged the row, so it just kept
    # re-attempting a fetch the yfinance layer had already flagged as
    # hopeless. Purge here too, but with a distinct message (lower-confidence
    # than a manually-confirmed KNOWN_DELISTED entry, since it's inferred
    # from repeated failures rather than a verified corporate action).
    if _is_learned_delisted(base):
        try:
            await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
        except Exception as e:
            logger.warning("repair: purge learned-delisted %s failed: %s", base, e)
        return {
            "symbol": base,
            "patched_fields": [],
            "still_missing": [],
            "price": _feed_resolved_price(current),
            "complete": False,
            "purged": True,
            "message": f"{base} failed to resolve on {_MAX_FAILURE_STREAK}+ consecutive attempts across all price sources — removed from feed as probably delisted (self-learned, not manually confirmed).",
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
                        await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
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
                                await asyncio.to_thread(store.delete_symbol, base)  # group361: off the event loop
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
                        # Price aliases: keep a stored value only if it is a usable (positive) number.
                        # setdefault kept a stored 0 / "" / None, leaving the aliases unusable next to the
                        # freshly repaired price.
                        for _alias in ("close", "cmp", "ltp"):
                            if _safe_float(current.get(_alias)) <= 0:
                                current[_alias] = px
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
            hist = None
            # group279: market-data /history first (Dhan -> AngelOne -> yfinance there); direct yfinance only when
            # that has nothing and GATEWAY_DIRECT_YFINANCE_FALLBACK allows it. REPAIR_RSI_VIA_MARKET_DATA=0 = old path.
            if yf_ticker:
                from yf_policy import env_on, md_daily_frame
                if env_on("REPAIR_RSI_VIA_MARKET_DATA", True):
                    hist = await asyncio.to_thread(md_daily_frame, MARKET_DATA_URL, base, "1mo")
                if (hist is None or hist.empty) and _direct_yf_ok():
                    hist = await asyncio.to_thread(lambda: yf.Ticker(yf_ticker).history(period="1mo"))
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
                    # Without this the baseline-seed block below still fired and
                    # stamped rsi_seed=True on a real technical-service value.
                    missing.discard("rsi")
                if t.get("ema20") is not None:
                    current["ema20"] = t.get("ema20")
                if t.get("technical_score") is not None and current.get("technical_score") is None:
                    current["technical_score"] = t.get("technical_score")
                if t.get("macd_hist") is not None or t.get("macd") is not None:
                    current["macd_hist"] = t.get("macd_hist", t.get("macd"))
                try:  # Group 349: seed the flat ms_* summary keys into the feed row (only non-None values)
                    _msk = ms_passthrough.feed_keys(t)
                    if _msk:
                        current.update(_msk)
                        patched.append("market_structure")
                except Exception:  # noqa: BLE001
                    pass
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
                    missing.discard("pe_ratio")   # keep the real PE out of the seed block
                if "roce" in missing and roce is not None and _safe_float(roce) != 0:
                    current["roce"] = _safe_float(roce)
                    patched.append("roce")
                    missing.discard("roce")       # keep the real ROCE out of the seed block
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
                    if current.get("news_score") is None:   # a stored None is "absent"; a stored 0 is a real score
                        current["news_score"] = ns
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
        # A stored 0 / None RSI is "unusable", not a value: seed over it too
        # (setdefault used to keep the stored zero and leave the row incomplete).
        if not current.get("rsi"):
            current["rsi"] = 50.0
        current["rsi_seed"] = True
        patched.append("rsi")
        missing.discard("rsi")
        logger.info("repair %s: seeded baseline RSI=50", base)

    if "pe_ratio" in missing:
        current["pe_ratio"] = 22.5
        current["pe_seed"] = True
        # Group 145: data_feed.merge_feed_payload only recognises "pe_ratio_seed" (the name bulk uses).
        # The repair wrote only "pe_seed", so its 22.5 was not protected from overwriting a real stored
        # PE and was not labelled as a seed. Set both; "pe_seed" stays for anything already reading it.
        current["pe_ratio_seed"] = True
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
        if current.get("news_score") is None:
            current["news_score"] = 0.65
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
    current["repair_updated_at"] = datetime.now(IST).isoformat()
    # Deduplicate patched list while preserving order
    seen_p = set()
    patched_unique = []
    for f in patched:
        if f not in seen_p:
            seen_p.add(f)
            patched_unique.append(f)
    patched = patched_unique
    # Persist the DE-DUPLICATED list (it used to be assigned before the
    # de-dupe, so the stored copy kept duplicates the API response didn't).
    current["repair_patched"] = patched

    # group303: this commit used to run on the event loop (loop-watchdog saw a 4.0s stall in oracledb commit during repair-all)
    await asyncio.to_thread(store.put_symbol, base, current, DATA_FEED_TTL)
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






# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
repair_single_stock = _bound_routes_c.repair_single_stock
app.include_router(_bound_routes_c.repair_single_stock_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
repair_batch_missing = _bound_routes_c.repair_batch_missing
app.include_router(_bound_routes_c.repair_batch_missing_router)


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
    try:
        # Mark the job running (and reset the cancel flag) BEFORE the audit: the audit can take a
        # while, and a Stop that lands during it used to be wiped by the post-audit reset, so the
        # whole run proceeded. This also makes /repair-all report already_running during the audit.
        _REFILL_ALL_JOB.update({
            "status": "running",
            "total": 0,
            "processed": 0,
            "ok_count": 0,
            "started_at": datetime.now(IST).isoformat(),
            "finished_at": None,
            "message": "Auditing incomplete feed records…",
            "last_symbol": None,
            "cancel_requested": False,
        })
        audit = await audit_missing_feed_data(limit=max(limit, 5000))
        targets = [item["symbol"] for item in (audit.get("incomplete_stocks") or [])]
        targets = targets[:limit] if limit else targets
        _REFILL_ALL_JOB.update({
            "total": len(targets),
            "message": f"Repairing {len(targets)} symbols…",
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


# E83: moved to gateway_support/bound_routes_h.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
repair_all_missing = _bound_routes_h.repair_all_missing
app.include_router(_bound_routes_h.repair_all_missing_router)


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
repair_all_status = _bound_routes_b.repair_all_status
app.include_router(_bound_routes_b.repair_all_status_router)


# E76: moved to gateway_support/bound_routes_b.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
repair_all_stop = _bound_routes_b.repair_all_stop
app.include_router(_bound_routes_b.repair_all_stop_router)


# E86: moved to gateway_support/bound_routes_k.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_run = _bound_routes_k.data_feed_run
app.include_router(_bound_routes_k.data_feed_run_router)


# E77: moved to gateway_support/bound_routes_c.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_stop = _bound_routes_c.data_feed_stop
app.include_router(_bound_routes_c.data_feed_stop_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
data_feed_resume = _bound_routes_d.data_feed_resume
app.include_router(_bound_routes_d.data_feed_resume_router)


# ── Hot Picks on-demand job (no auto-load spam) ─────────────────────────────

# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_status = _bound_routes_e.stockky_hot_status
app.include_router(_bound_routes_e.stockky_hot_status_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_result = _bound_routes_e.stockky_hot_result
app.include_router(_bound_routes_e.stockky_hot_result_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_premarket_status = _bound_routes_e.stockky_hot_premarket_status
app.include_router(_bound_routes_e.stockky_hot_premarket_status_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_premarket = _bound_routes_j.stockky_hot_premarket
app.include_router(_bound_routes_j.stockky_hot_premarket_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_run = _bound_routes_j.stockky_hot_run
app.include_router(_bound_routes_j.stockky_hot_run_router)


# E79: moved to gateway_support/bound_routes_e.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_stop = _bound_routes_e.stockky_hot_stop
app.include_router(_bound_routes_e.stockky_hot_stop_router)


# E49: moved to gateway_routers/hot_picks.py (code unchanged); included here so route order is unchanged.
from gateway_routers.hot_picks import hot_table_router as _hot_table_router, stockky_hot_table  # noqa: E402,F401
app.include_router(_hot_table_router)


# E85: moved to gateway_support/bound_routes_j.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
api_hotpicks_notify_top_picks = _bound_routes_j.api_hotpicks_notify_top_picks
app.include_router(_bound_routes_j.api_hotpicks_notify_top_picks_router)


# E49: moved to gateway_routers/hot_picks.py (code unchanged); included here so route order is unchanged.
from gateway_routers.hot_picks import hot_audit_router as _hot_audit_router, stockky_hot_audit  # noqa: E402,F401
app.include_router(_hot_audit_router)


# E78: moved to gateway_support/bound_routes_d.py (body unchanged, main.py names read via MainRef at call time); included here so route order is unchanged.
stockky_hot_repair_batch = _bound_routes_d.stockky_hot_repair_batch
app.include_router(_bound_routes_d.stockky_hot_repair_batch_router)




# ── OS signal hooks (Render deploy / free-tier SIGTERM) ─────────────────────
def _install_signal_handlers() -> None:
    """Ensure SIGTERM/SIGINT commit checkpoints even if uvicorn path is skipped."""
    import signal

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
except Exception as _sig_err:  # pragma: no cover - defensive, handler install already swallows its own errors
    logger.debug("signal handlers: %s", _sig_err)

if __name__ == "__main__":  # pragma: no cover - uvicorn entrypoint, never imported by tests
    import uvicorn
    port = int(((os.getenv("PORT") or "").strip() or 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)