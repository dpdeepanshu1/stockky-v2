import math
"""
Market Data Service
--------------------
Single responsibility: fetch raw market data (price history, quote, company info)
for Indian equities from free public sources (yfinance) and serve over REST.
All data is cached with TTL that depends on market hours:
- During NSE trading hours (09:15-15:30 IST, Mon-Fri): TTL = 300 seconds (5 min)
- Outside: TTL = 21600 seconds (6 hours)

v2.2 – reduced retries and timeouts for faster responses.
"""
import os
import re
import urllib.parse
import time
import json
import logging
import math
import random
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List

import requests
import yfinance as yf

try:
    import rate_limiter as _rl
    _rl.patch_yfinance()
except Exception as _rl_e:
    logging.getLogger(__name__).warning("rate_limiter patch skipped: %s", _rl_e)
from upstash_redis import Redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import httpx
from circuit_breaker import get_breaker, all_snapshots
import gc
MAX_HISTORY_ROWS = int(os.environ.get('MAX_HISTORY_ROWS', '260'))  # ~1y daily, 512MB-safe
MAX_HISTORY_PERIOD = os.environ.get('MAX_HISTORY_PERIOD', '1y')

def _report_rate_limit(status: int, path: str = "", detail: str = "", symbol: str = "") -> None:
    """
    Dual-write rate-limit events:
      1) Direct Neon KV so the gateway Rate Limit Dashboard is not blind
      2) Best-effort POST to gateway /ops/rate-limits/event for in-process monitor
    """
    try:
        from circuit_breaker import record_rate_limit_hit
        record_rate_limit_hit(
            provider="market_data",
            status=int(status),
            path=path or "",
            detail=str(detail)[:200],
            symbol=symbol or "",
        )
    except Exception:
        pass
    try:
        gw = os.environ.get("API_GATEWAY_URL", "").rstrip("/")
        if not gw:
            return
        requests.post(
            f"{gw}/ops/rate-limits/event",
            json={
                "source": "market_data",
                "status": status,
                "path": path,
                "detail": str(detail)[:200],
                "symbol": symbol,
            },
            timeout=2,
        )
    except Exception:
        pass


# ── yfinance session patch ────────────────────────────────────────────────────
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

try:
    yf.set_tz_cache_location("/tmp/yfinance_tz")
except AttributeError:
    pass

# ── Helpers ────────────────────────────────────────────────────────────────────
def _normalize_de_ratio(val, sector=None):
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    sec = (str(sector or "")).lower()
    is_fin = any(x in sec for x in ("bank", "financial", "insurance"))
    if v > 50 and not is_fin:
        v = v / 100.0
    elif v > 200 and is_fin:
        v = v / 100.0
    return round(v, 2)


def _safe(val, decimals=2):
    try:
        f = float(val)
        if math.isnan(f) or not math.isfinite(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None

def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def _compute_growth(current, previous):
    if previous is None or previous == 0:
        return None
    try:
        g = ((float(current) - float(previous)) / float(previous)) * 100
        if g != g or abs(g) == float("inf"):  # NaN/Inf
            return None
        return g
    except Exception:
        return None

# Global yfinance concurrency guard (free-tier / Yahoo rate-limit safe).
# Caps concurrent Yahoo calls across quote + history + fundamentals so a
# parallel market scan does not stampede Yahoo and get empty responses.
import threading
_YFINANCE_MAX_CONCURRENT = int(os.getenv("YFINANCE_MAX_CONCURRENT", "1"))
_yf_semaphore = threading.Semaphore(_YFINANCE_MAX_CONCURRENT)
_YF_MIN_INTERVAL = float(os.getenv("YFINANCE_MIN_INTERVAL_SEC", "0.35"))
_yf_last_call = 0.0
_yf_lock = threading.Lock()

def _yf_rate_limited(func):
    """Run func under the yfinance semaphore + small spacing between calls."""
    global _yf_last_call
    with _yf_semaphore:
        with _yf_lock:
            now = time.time()
            gap = _YF_MIN_INTERVAL - (now - _yf_last_call)
            if gap > 0:
                time.sleep(gap)
            _yf_last_call = time.time()
        return func()

def _with_retry(func, max_retries=4, base_delay=1.0):
    """Retry with exponential backoff (Yahoo rate limits / free-tier).

    Circuit breaker opens after repeated Yahoo failures so the rest of the
    scan fails fast instead of stacking 70s timeouts.
    """
    br = get_breaker("yfinance", failure_threshold=6, recovery_timeout=120)
    if not br.allow():
        raise RuntimeError(f"yfinance circuit open; retry in {br.retry_after():.0f}s")
    last_err = None
    for attempt in range(max_retries):
        try:
            result = _yf_rate_limited(func)
            br.record_success()
            return result
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "too many" in msg or "rate limit" in msg:
                _set_cooldown("yfinance", _YF_COOLDOWN_SEC)
                br.record_failure(str(e))
                _report_rate_limit(429, path="yfinance", detail=str(e)[:200])
                raise
            if attempt == max_retries - 1:
                br.record_failure(str(e))
                raise
            wait = base_delay * (2 ** attempt)
            time.sleep(wait)
    if last_err:
        br.record_failure(str(last_err))
        raise last_err


def is_market_open() -> bool:
    """Return True if current time is within NSE trading hours (Mon-Fri, 09:15-15:30 IST)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def _sanitize_for_json(obj):
    """Replace NaN/Inf so FastAPI json.dumps never 500s (fundamentals etc.)."""
    import math
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        # json.dumps requires string (or int/float/bool/None) keys — a
        # pandas Timestamp or numpy scalar used as a dict key (e.g. from
        # `.to_dict()` on an indexed Series) raises the same
        # "not JSON serializable" class of error as an unhandled value
        # would. Stringify anything that isn't already a safe key type.
        return {
            (k if isinstance(k, (str, int, float, bool)) or k is None else str(k)): _sanitize_for_json(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    try:
        import numpy as np
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, (np.floating,)):
            f = float(obj)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, np.ndarray):
            return [_sanitize_for_json(x) for x in obj.tolist()]
    except Exception:
        pass
    try:
        import pandas as pd
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if obj is pd.NaT:
            return None
        if isinstance(obj, (pd.Series,)):
            return _sanitize_for_json(obj.to_dict())
    except Exception:
        pass
    import datetime as _dt
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    return obj


def get_cache_ttl() -> int:
    """Return TTL in seconds: 300 if market open, else 21600 (6 hours)."""
    return 300 if is_market_open() else 21600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-data-service")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

app = FastAPI(title="Stockky Market Data Service", version="2.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _start_yahoo_ws_feed():
    """
    Point 1 continued: one persistent WebSocket connection to Yahoo's live
    streaming endpoint (wss://streamer.finance.yahoo.com), subscribed once
    to the whole scan universe, replaces the vast majority of the
    per-symbol REST calls that were hitting yfinance/twelvedata/
    alphavantage/polygon rate limits (see the /quote/{symbol} waterfall —
    it now checks this feed first). This is a different Yahoo backend from
    the crumb-protected REST path, so it doesn't share that rate limit.
    """
    try:
        from surprise_premarket import default_universe_from_env
        import yahoo_ws_feed
        universe = default_universe_from_env()
        if universe:
            yahoo_ws_feed.start_feed_background(universe)
        else:
            logger.warning("yahoo_ws_feed: no universe configured (SURPRISE_UNIVERSE/SCAN_UNIVERSE), not starting")
    except Exception as e:
        logger.warning("yahoo_ws_feed startup skipped: %s", e)

# ── Root & Health endpoints (fix 404) ────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Market Data Service", "version": "2.2.0", "status": "running"}

@app.get("/health")
async def health():
    # Simple health check; optionally verify dependencies (Redis, etc.)
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/bhavcopy/universe")
def bhavcopy_universe(min_price: float = 0, limit: int = 3000):
    """
    §3 — Full real EQ/BE/BZ symbol list from the most recent fetchable
    bhavcopy session. Used as the universe-of-last-resort by api-gateway
    whenever NSE's live JSON securities API is unreachable.
    Filters through symbol_master status='active' when that table exists.
    """
    from bhavcopy import _fetch_bhav_day_parsed, _candidate_session_dates, _nse_client
    client = _nse_client()
    for d in _candidate_session_dates(n=6):
        parsed = _fetch_bhav_day_parsed(client, d)
        if not parsed:
            continue
        syms = []
        for s, row in parsed.items():
            if min_price and (row.get("close") or 0) < min_price:
                continue
            syms.append(s)
        # Filter through symbol_master when available
        try:
            from sqlalchemy import text as _text
            from db import get_engine as _get_engine
            engine = _get_engine()
            with engine.connect() as conn:
                rows = conn.execute(
                    _text("SELECT current_symbol FROM symbol_master WHERE status='active'")
                ).fetchall()
                active = {r[0] for r in rows}
                if active:
                    syms = [s for s in syms if s in active]
        except Exception:
            pass  # symbol_master not yet created — use full bhavcopy list
        return {
            "symbols": syms[:limit],
            "session_date": str(d),
            "count": len(syms[:limit]),
            "source": "bhavcopy",
        }
    return {"symbols": [], "session_date": None, "count": 0}


@app.get("/live-quote/{symbol}")
def live_quote(symbol: str):
    """
    §1 — Thin read off live_quotes table (populated by AngelOne WS feed).
    For real-trade-service and other services to consume without triggering
    any upstream API call.
    """
    sym = symbol.upper().replace(".NS", "").replace(".BO", "").strip()
    try:
        from sqlalchemy import text as _text
        from db import get_engine as _get_engine
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                _text(
                    "SELECT ltp, ohlc_json, volume, source, updated_at "
                    "FROM live_quotes WHERE symbol = :s"
                ),
                {"s": sym},
            ).fetchone()
            if row:
                import json as _json
                ohlc = {}
                try:
                    ohlc = _json.loads(row[1]) if row[1] else {}
                except Exception:
                    pass
                return {
                    "symbol": sym, "ltp": float(row[0] or 0),
                    "ohlc": ohlc, "volume": row[2],
                    "source": row[3], "updated_at": str(row[4]),
                }
    except Exception:
        pass
    # Fall through to normal quote endpoint on DB miss
    return {"symbol": sym, "ltp": None, "source": "miss"}



@app.get("/internal/yahoo-ws-status")
async def yahoo_ws_status():
    """Live-feed health — connected/subscribed count/last tick age. Polled
    by api-gateway's /ws jobs channel so the Data Feed / Surprise tabs can
    show whether quotes are coming from the WS push feed or falling back
    to REST."""
    try:
        import yahoo_ws_feed
        return yahoo_ws_feed.feed_status()
    except Exception as e:
        return {"connected": False, "error": str(e)[:200]}

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"}
    )

# ── Cache: memory-first (unlimited). Redis only if USE_REDIS=1 ─────────────────
USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")  # default OFF — Neon/memory only
# Kill-switches — any of these force Redis completely off (even if credentials exist)
if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False
if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False
# Hard safety: if not explicitly USE_REDIS=1, never touch Upstash (credentials alone are not enough)
if os.getenv("USE_REDIS", "0").strip() in ("", "0", "false", "False", "no", "NO"):
    USE_REDIS = False


FALLBACK_TTL_SECONDS = 30 * 24 * 60 * 60

# Global Yahoo/upstream cooldown after 429 (stops stampede + Redis write storm)
_YF_COOLDOWN_UNTIL = 0.0
_YF_COOLDOWN_SEC = float(os.getenv("YFINANCE_COOLDOWN_SEC", "180"))  # 3 min
_UPSTREAM_COOLDOWN = {}  # name -> until epoch

def _in_cooldown(name: str = "yfinance") -> bool:
    return time.time() < _UPSTREAM_COOLDOWN.get(name, 0)

def _set_cooldown(name: str = "yfinance", sec: float = None):
    global _YF_COOLDOWN_UNTIL
    until = time.time() + (sec if sec is not None else _YF_COOLDOWN_SEC)
    _UPSTREAM_COOLDOWN[name] = until
    if name == "yfinance":
        _YF_COOLDOWN_UNTIL = until
    logger.warning("%s cooldown until +%.0fs (rate limit / 429)", name, sec or _YF_COOLDOWN_SEC)


class _MemCache:
    def __init__(self, max_keys: int = 6000):
        self._d = {}
        self._lock = threading.Lock()
        self._max = max_keys

    def get(self, key: str):
        with self._lock:
            e = self._d.get(key)
            if not e:
                return None
            val, exp = e
            if exp is not None and time.time() > exp:
                self._d.pop(key, None)
                return None
            return val

    def set(self, key: str, value, ttl: int = None):
        with self._lock:
            if len(self._d) >= self._max and key not in self._d:
                # drop expired + some oldest keys
                now = time.time()
                for k in list(self._d.keys())[: max(50, self._max // 20)]:
                    v = self._d.get(k)
                    if not v or (v[1] is not None and v[1] < now):
                        self._d.pop(k, None)
                if len(self._d) >= self._max:
                    for k in list(self._d.keys())[:50]:
                        self._d.pop(k, None)
            exp = (time.time() + ttl) if ttl else None
            self._d[key] = (value, exp)

    def ttl(self, key: str) -> int:
        with self._lock:
            e = self._d.get(key)
            if not e:
                return -2
            _, exp = e
            if exp is None:
                return -1
            left = int(exp - time.time())
            return left if left > 0 else -2


_mem = _MemCache()
cache = None  # optional Upstash
try:
    if USE_REDIS and UPSTASH_URL and UPSTASH_TOKEN:
        cache = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
        cache.ping()
        logger.info("Connected to Upstash Redis (USE_REDIS=1)")
    else:
        cache = None  # hard-disable even if credentials present
        logger.info("Market-data cache: in-memory only (USE_REDIS=0) — no Upstash commands")
except Exception as e:
    logger.warning("Redis unavailable (%s). Memory-only cache.", e)
    cache = None


def _cache_ttl(key: str) -> int:
    t = _mem.ttl(key)
    if t != -2:
        return t
    if not cache:
        return -2
    try:
        tt = cache.ttl(key)
        return int(tt) if tt is not None else -2
    except Exception:
        return -2


def _should_soft_refresh(key: str, soft_window: int = 30) -> bool:
    """Soft refresh only when NOT rate-limited (avoids stampede during 429)."""
    if _in_cooldown("yfinance") or _in_cooldown("nse"):
        return False
    t = _cache_ttl(key)
    return 0 < t <= soft_window


def _cache_get(key: str):
    val = _mem.get(key)
    if val is not None:
        return val
    if not cache:
        return None
    try:
        raw = cache.get(key)
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        _mem.set(key, parsed, ttl=60)  # warm memory
        return parsed
    except Exception:
        return None


def _cache_set(key: str, value: dict, ttl: int = None):
    if ttl is None:
        ttl = get_cache_ttl()
    value = _sanitize_for_json(value)
    _mem.set(key, value, ttl=ttl)
    # HARD OFF: never write Upstash unless USE_REDIS explicitly enabled
    if not USE_REDIS or not cache:
        return
    try:
        cache.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.debug("redis set failed: %s", e)


def _fallback_get(key: str):
    val = _mem.get(f"fallback:{key}")
    if val is not None:
        return val
    if not cache:
        return None
    try:
        raw = cache.get(f"fallback:{key}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _fallback_set(key: str, value: dict):
    value = _sanitize_for_json(value)
    _mem.set(f"fallback:{key}", value, ttl=FALLBACK_TTL_SECONDS)
    if not USE_REDIS or not cache:
        return
    try:
        cache.setex(f"fallback:{key}", FALLBACK_TTL_SECONDS, json.dumps(value, default=str))
    except Exception:
        pass

# Yahoo Finance tickers for common NSE index display names.
# Without this, normalize_symbol("NIFTY NEXT 50") → "NIFTY NEXT 50.NS" → 404/503.
INDEX_YAHOO_MAP = {
    "NIFTY": "^NSEI",
    "NIFTY 50": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY NEXT 50": "^NSMIDCP",  # proxy; Yahoo ^NN50 sometimes unavailable
    "NIFTYNEXT50": "^NSMIDCP",
    "NIFTY NEXT50": "^NSMIDCP",
    "NIFTY BANK": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "BANK NIFTY": "^NSEBANK",
    "BANKNIFTY": "^NSEBANK",
    "NIFTY MIDCAP 100": "^NSEMDCP100",
    "NIFTY MIDCAP100": "^NSEMDCP100",
    "NIFTYMIDCAP100": "^NSEMDCP100",
    "NIFTY MIDCAP 150": "NIFTY_MIDCAP_150.NS",
    "NIFTY MIDCAP150": "NIFTY_MIDCAP_150.NS",
    "NIFTYMIDCAP150": "NIFTY_MIDCAP_150.NS",
    "NIFTY SMALLCAP 100": "NIFTYSMLCAP100.NS",
    "NIFTY SMALLCAP 250": "NIFTYSMLCAP250.NS",
    "NIFTY IT": "^CNXIT",
    "NIFTYIT": "^CNXIT",
    "SENSEX": "^BSESN",
    "BSE SENSEX": "^BSESN",
    "INDIA VIX": "^INDIAVIX",
    "INDIAVIX": "^INDIAVIX",
}
# Alternate Yahoo symbols to try if primary fails
INDEX_YAHOO_FALLBACKS = {
    "^NSMIDCP": ["^NSEI"],
    "NIFTY_MIDCAP_150.NS": ["^NSEMDCP100", "^NSEI"],
    "^NSEMDCP100": ["^NSEI"],
}



# Explicit map for multi-word / ambiguous NSE names (KFINTECH ≠ KPITTECH)
SMART_SYMBOL_MAP = {
    "KFIN TECHNOLOGIES": "KFINTECH",
    "KFINTECHNOLOGIES": "KFINTECH",
    "KPIT TECHNOLOGIES": "KPITTECH",
    "KPITTECHNOLOGIES": "KPITTECH",
    "BAJAJ HOLDINGS": "BAJAJHLDNG",
    "AIA ENGINEERING": "AIAENG",
    "360 ONE": "360ONE",
    "360ONE WAM": "360ONE",
    "HONASA CONSUMER": "HONASA",
    "PB FINTECH": "POLICYBZR",
    "MOTILAL OSWAL": "MOTILALOFS",
    "NAM INDIA": "NAM-INDIA",
    "ONE 97": "PAYTM",
    "ONE97": "PAYTM",
    "ZOMATO": "ETERNAL",
    "GMRINFRA": "GMRAIRPORT",
    "SRTRANSFIN": "SHRIRAMFIN",
    "MOTHERSUMI": "MOTHERSON",
    "CADILAHC": "ZYDUSLIFE",
    "MINDTREE": "LTIM",
    # ── Added 2026-08-26, kept in sync with api-gateway/symbol_aliases.py ──
    # (see that file's comment for the verified NSE circular dates)
    "TATAMOTORS": "TMPV",
    "LTIM": "LTM",
    # ── Reconciled with api-gateway/symbol_aliases.py:SYMBOL_RENAMES ──────────
    # These five were in api-gateway's rename table but NOT here, so a /quote
    # asked for PVR or ADANITRANS directly (i.e. not routed through the gateway's
    # resolver) queried the dead ticker and came back "symbol not found" — one of
    # the concrete sources of that error class. api-gateway/symbol_aliases.py is
    # the source of truth for NSE renames; this map is the market-data-service
    # copy and the two MUST be updated together. Verified once: no key disagrees
    # on its target between the two files.
    "PVR": "PVRINOX",
    "IBULHSGFIN": "SAMMAANCAP",
    "L&TFH": "LTF",
    "ADANITRANS": "ADANIENSOL",
    "NSPIRA": "NSIL",
    # 2026-08-24: added to api-gateway/symbol_aliases.py — reconciled here
    # too so a /quote/JUBILANT call resolves the same way whether it comes
    # through the gateway's resolver or hits this service directly.
    # "JUBILANT" was never a real NSE ticker (JUBLFOOD is).
    "JUBILANT": "JUBLFOOD",
}

# Genuinely delisted/merged-away symbols (NOT a rename — see
# api-gateway/symbol_aliases.py:KNOWN_DELISTED for the full explanation).
# A straight SMART_SYMBOL_MAP substitution would be wrong here because the
# conversion ratio isn't 1:1, so these get a hard skip instead of a mapped
# ticker: normalize_symbol() still returns "<SYM>.NS" for logging purposes,
# but callers that check is_known_delisted() first can avoid the network
# round-trip entirely and purge the row instead of "repairing" it forever.
KNOWN_DELISTED_SYMBOLS = {
    "TATAMTRDVR",  # merged into TATAMOTORS 2024-08-30 (7 ordinary : 10 DVR)
}


def is_known_delisted(symbol: str) -> bool:
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    return base in KNOWN_DELISTED_SYMBOLS


def sanitize_symbol(raw_symbol: str) -> str:
    """Decode URL params and map company names to canonical NSE tickers."""
    decoded = urllib.parse.unquote(str(raw_symbol or "")).upper().strip()
    decoded = decoded.replace(".NS", "").replace(".BO", "").strip()
    decoded = re.sub(r"\s+", " ", decoded)

    if decoded in SMART_SYMBOL_MAP:
        return SMART_SYMBOL_MAP[decoded]
    compact = decoded.replace(" ", "")
    if compact in SMART_SYMBOL_MAP:
        return SMART_SYMBOL_MAP[compact]

    # Conservative replacements only after smart map
    out = decoded
    out = out.replace(" TECHNOLOGIES", "TECH")
    out = out.replace(" TECHNOLOGY", "TECH")
    out = out.replace(" LIMITED", "")
    out = out.replace(" LTD", "")
    out = out.replace(" ", "")
    return out or decoded


def normalize_symbol(symbol: str) -> str:
    """Map equity and index symbols to Yahoo-compatible tickers.

    Equities get .NS if missing. Index *names* (with spaces) map to ^… tickers
    so history/quote never request "NIFTY NEXT 50.NS".
    Uses SMART_SYMBOL_MAP so KFIN TECHNOLOGIES → KFINTECH (not KPITTECH).
    """
    raw = (symbol or "").strip()
    if not raw:
        return raw
    # Decode %20 and map multi-word names before any other logic
    try:
        mapped = sanitize_symbol(raw)
        if mapped and " " not in mapped and mapped not in ("NIFTY",):
            # If sanitize produced a clean ticker (no spaces), use it as equity base
            if mapped.startswith("^"):
                return mapped
            # Index names still handled below via INDEX_YAHOO_MAP on original
            upper_check = urllib.parse.unquote(raw).upper().replace("_", " ").strip()
            if upper_check not in INDEX_YAHOO_MAP and not upper_check.startswith("NIFTY"):
                raw = mapped
    except Exception:
        pass
    upper = raw.upper().replace("_", " ")
    # Already a Yahoo index
    if raw.startswith("^"):
        return raw
    # Direct map (exact)
    key = upper.replace("  ", " ").strip()
    if key in INDEX_YAHOO_MAP:
        return INDEX_YAHOO_MAP[key]
    # Compact key without spaces
    compact = key.replace(" ", "")
    for k, v in INDEX_YAHOO_MAP.items():
        if k.replace(" ", "") == compact:
            return v
    # Equity path
    sym = raw.strip().upper().replace(".NS", "").replace(".BO", "").strip()
    if sym.startswith("^"):
        return sym
    if sym.startswith("NIFTY") or sym in ("SENSEX", "INDIAVIX", "BANKNIFTY"):
        if sym in INDEX_YAHOO_MAP:
            return INDEX_YAHOO_MAP[sym]
        compact = sym.replace(" ", "").replace("_", "")
        for k, v in INDEX_YAHOO_MAP.items():
            if k.replace(" ", "").replace("_", "") == compact:
                return v
        return "^NSEI" if "NIFTY" in sym else sym
    bare = SMART_SYMBOL_MAP.get(sym, sym)
    if bare.startswith("^"):
        return bare
    if not bare.endswith(".NS") and not bare.endswith(".BO"):
        bare = f"{bare}.NS"
    return bare


class QuoteResponse(BaseModel):
    """
    Quote-only schema. Fundamentals (pe_ratio, market_cap) stay Optional=None —
    never inject fake 0s that poison Neon merges / scanners / ML.
    """
    symbol: str
    name: Optional[str] = None
    price: Optional[float] = None
    cmp: Optional[float] = None
    previous_close: Optional[float] = None
    day_change_pct: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    volume: Optional[int] = None
    market_cap: Optional[float] = None  # fundamental service owns this
    pe_ratio: Optional[float] = None    # fundamental service owns this
    source: Optional[str] = "unknown"
    fetched_at: Optional[str] = None

    class Config:
        extra = "ignore"


class BulkQuoteRequest(BaseModel):
    """Request body for single-call bulk quotes (eliminates sequential 429 cascade)."""
    symbols: List[str]


def _clean_quote_dict(d: dict) -> dict:
    """Drop keys whose value is None so callers never JSON-merge nulls over real data."""
    return {k: v for k, v in (d or {}).items() if v is not None}


def _pad_quote_response(sym: str, data: Optional[dict] = None) -> dict:
    """
    Schema-safe quote dict WITHOUT inventing zeros.
    - Real OHLCV fields pass through only if present and valid
    - Missing fields stay None (not 0) so Neon merges keep prior real values
    - pe_ratio / market_cap never forced — owned by fundamental service
    """
    d = dict(data) if isinstance(data, dict) else {}
    base = (sym or d.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip() or "UNKNOWN"

    def _f(key):
        v = d.get(key)
        if v is None or v == "":
            return None
        try:
            f = float(v)
            if f != f:  # NaN
                return None
            return f
        except (TypeError, ValueError):
            return None

    def _i(key):
        v = d.get(key)
        if v is None or v == "":
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    price = _f("price")
    if price is None:
        price = _f("cmp")
    if price is None:
        price = _f("ltp")
    if price is None:
        price = _f("close")
    if price is None:
        price = _f("last_price")
    px = float(price) if price is not None and price > 0 else None

    prev = _f("previous_close")
    high = _f("day_high")
    low = _f("day_low")
    chg = _f("day_change_pct")
    vol = _i("volume")
    # Only accept positive volume — never treat missing as 0
    if vol is not None and vol < 0:
        vol = None

    fetched = d.get("fetched_at") or datetime.utcnow().isoformat()
    if not isinstance(fetched, str):
        fetched = str(fetched)

    out = {
        "symbol": base if not str(sym).endswith((".NS", ".BO")) else str(sym).upper(),
        "name": d.get("name") or base,
        "price": px,
        "cmp": px,
        "previous_close": prev,
        "day_change_pct": chg,
        "day_high": high,
        "day_low": low,
        "volume": vol,
        # Fundamentals: pass through only if explicitly provided (never invent)
        "market_cap": _f("market_cap"),
        "pe_ratio": _f("pe_ratio"),
        "source": d.get("source") or "unknown",
        "fetched_at": fetched,
    }
    return out


def _yahoo_tickers_for(symbol: str) -> list:
    """Yahoo candidates. Never turn ^NSEI into ^NSEI.NS. Apply renames."""
    raw = (symbol or "").strip()
    if not raw:
        return []
    try:
        mapped = normalize_symbol(raw)
    except Exception:
        mapped = raw.upper()
    if mapped.startswith("^"):
        return [mapped]
    base = mapped.upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return []
    if base.startswith("^"):
        return [base]
    bare = SMART_SYMBOL_MAP.get(base, base)
    if bare.startswith("^"):
        return [bare]
    # Index display names that slipped through
    if bare.startswith("NIFTY") or bare in ("SENSEX", "INDIAVIX", "BANKNIFTY"):
        idx = INDEX_YAHOO_MAP.get(bare) or INDEX_YAHOO_MAP.get(bare.replace("_", ""))
        if idx:
            return [idx]
        return ["^NSEI"] if "NIFTY" in bare else [bare]
    return [f"{bare}.NS", f"{bare}.BO"]


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "rate limit" in msg
        or "too many requests" in msg
        or "yfratelimiterror" in msg
        or "429" in msg
        or "invalid crumb" in msg
        or "unauthorized" in msg
        or "401" in msg
    )


def _yahoo_ohlcv_quote(symbol: str) -> Optional[dict]:
    """
    Real candle metrics via yfinance period=2d.
    Returns None on failure — never fake zeros.
    """
    if _in_cooldown("yfinance"):
        return None
    tickers = _yahoo_tickers_for(symbol)
    if not tickers:
        return None
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            latest = hist.iloc[-1]
            price = float(latest["Close"])
            if price != price or price <= 0:
                continue
            high = float(latest["High"]) if "High" in hist.columns else None
            low = float(latest["Low"]) if "Low" in hist.columns else None
            vol = None
            if "Volume" in hist.columns:
                try:
                    vol = int(float(latest["Volume"]))
                    if vol < 0:
                        vol = None
                except (TypeError, ValueError):
                    vol = None
            prev_close = price
            if len(hist) >= 2:
                try:
                    prev_close = float(hist.iloc[-2]["Close"])
                except (TypeError, ValueError, IndexError):
                    prev_close = price
            change_pct = None
            if prev_close and prev_close > 0:
                change_pct = round(((price - prev_close) / prev_close) * 100, 2)
            base = ticker.replace(".NS", "").replace(".BO", "")
            return {
                "symbol": base,
                "name": base,
                "price": price,
                "cmp": price,
                "previous_close": prev_close,
                "day_change_pct": change_pct,
                "day_high": high if high == high else None,
                "day_low": low if low == low else None,
                "volume": vol,
                "source": "yahoo",
                "yahoo_ticker": ticker,
                "fetched_at": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            if _is_rate_limit_error(e):
                _set_cooldown("yfinance")
                logger.warning("Yahoo rate-limited on %s — cooldown set", ticker)
                return None
            logger.debug("yahoo ohlcv %s: %s", ticker, e)
    return None


def _waterfall_yahoo_history_price(symbol: str) -> Optional[float]:
    """Primary free path: yfinance Close. Respects index tickers and cooldown."""
    if _in_cooldown("yfinance"):
        return None
    for ticker in _yahoo_tickers_for(symbol):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                px = float(hist["Close"].dropna().iloc[-1])
                if px > 0:
                    return px
        except Exception as e:
            if _is_rate_limit_error(e):
                _set_cooldown("yfinance")
                return None
            logger.debug("yahoo history %s: %s", ticker, e)
    return None


def _waterfall_equity_base(symbol: str) -> str:
    """Bare equity ticker for third-party APIs (no ^ indices, apply renames)."""
    raw = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if raw.startswith("^"):
        return ""  # skip paid APIs for indices when Yahoo failed
    try:
        mapped = normalize_symbol(raw)
        if mapped.startswith("^"):
            return ""
        bare = mapped.replace(".NS", "").replace(".BO", "").strip()
        return SMART_SYMBOL_MAP.get(bare, bare)
    except Exception:
        return SMART_SYMBOL_MAP.get(raw, raw)


def _waterfall_twelvedata_price(symbol: str) -> Optional[float]:
    if _in_cooldown("twelvedata"):
        return None
    key = TWELVE_DATA_API_KEY
    if not key:
        return None
    base = _waterfall_equity_base(symbol)
    if not base:
        return None
    # One primary candidate first — stop multi-suffix stampede on 429
    candidates = [f"{base}.NSE", base]
    for sym in candidates:
        try:
            url = f"https://api.twelvedata.com/price?symbol={sym}&apikey={key}"
            resp = httpx.get(url, timeout=4.0)
            if resp.status_code == 429:
                _set_cooldown("twelvedata", 120)
                logger.warning("TwelveData 429 — cooldown 120s")
                return None
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if isinstance(data, dict):
                    px = _safe(data.get("price"))
                    if px is not None and px > 0:
                        logger.info("TwelveData waterfall hit %s → ₹%.2f", sym, px)
                        return float(px)
        except Exception as e:
            logger.debug("TwelveData %s: %s", sym, e)
    return None


def _waterfall_polygon_price(symbol: str) -> Optional[float]:
    if _in_cooldown("polygon"):
        return None
    key = POLYGON_API_KEY
    if not key:
        return None
    base = _waterfall_equity_base(symbol)
    if not base:
        return None
    # Single attempt — India coverage is sparse; avoid 3x burn
    sym = base
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={key}"
        resp = httpx.get(url, timeout=4.0)
        if resp.status_code == 429:
            _set_cooldown("polygon", 120)
            return None
        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            results = (data or {}).get("results") or []
            if results:
                px = _safe(results[0].get("c"))
                if px is not None and px > 0:
                    logger.info("Polygon waterfall hit %s → ₹%.2f", sym, px)
                    return float(px)
    except Exception as e:
        logger.debug("Polygon %s: %s", sym, e)
    return None


def _waterfall_nse_direct_price(symbol: str) -> Optional[float]:
    """NSE's own quote-equity API — no key, no quota, and NOT gated behind
    Yahoo's crumb/cookie flow, so this keeps working during a yfinance
    "Invalid Crumb" outage. India-only coverage is exactly what we need here."""
    if _in_cooldown("nse_direct"):
        return None
    base = _waterfall_equity_base(symbol)
    if not base:
        return None
    try:
        from bhavcopy import _nse_client
        client = _nse_client()
        r = client.get(f"https://www.nseindia.com/api/quote-equity?symbol={base}")
        if r.status_code == 429:
            _set_cooldown("nse_direct", 90)
            return None
        if r.status_code == 200:
            data = r.json() if r.content else {}
            price_info = (data or {}).get("priceInfo") or {}
            px = _safe(price_info.get("lastPrice") or price_info.get("close"))
            if px is not None and px > 0:
                logger.info("NSE-direct waterfall hit %s → ₹%.2f", base, px)
                return float(px)
    except Exception as e:
        logger.debug("NSE-direct %s: %s", base, e)
    return None


def _waterfall_bhavcopy_price(symbol: str) -> Optional[float]:
    """Absolute last resort: official NSE bhavcopy EOD close.

    Not live, but a real yesterday's-close beats leaving price stuck at 0
    forever when every live source (Yahoo/TwelveData/AlphaVantage/Polygon/
    NSE-direct) is down or blocked."""
    if _in_cooldown("bhavcopy"):
        return None
    base = _waterfall_equity_base(symbol)
    if not base:
        return None
    try:
        from bhavcopy import eod_close_from_bhavcopy
        px = eod_close_from_bhavcopy(base)
        if px and px > 0:
            logger.info("Bhavcopy EOD waterfall hit %s → ₹%.2f", base, px)
            return float(px)
    except Exception as e:
        logger.debug("Bhavcopy price %s: %s", base, e)
        _set_cooldown("bhavcopy", 60)
    return None


def _waterfall_alphavantage_price(symbol: str) -> Optional[float]:
    """Emergency last resort — free tier ~25 req/day. One try only."""
    if _in_cooldown("alphavantage"):
        return None
    key = ALPHA_VANTAGE_API_KEY
    if not key:
        return None
    base = _waterfall_equity_base(symbol)
    if not base:
        return None
    # Prefer BSE style once — do not fire BSE+NSE+plain every time
    sym = f"{base}.BSE"
    try:
        url = (
            "https://www.alphavantage.co/query"
            f"?function=GLOBAL_QUOTE&symbol={sym}&apikey={key}"
        )
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 429:
            _set_cooldown("alphavantage", 300)
            return None
        if resp.status_code == 200 and resp.content:
            data = resp.json() if resp.content else {}
            gq = (data or {}).get("Global Quote") or (data or {}).get("globalQuote") or {}
            px = _safe(gq.get("05. price") or gq.get("05.price") or gq.get("price"))
            if px is not None and px > 0:
                logger.info("AlphaVantage waterfall hit %s → ₹%.2f", sym, px)
                return float(px)
            # Note rate limit messages in body
            note = str((data or {}).get("Note") or (data or {}).get("Information") or "")
            if "rate" in note.lower() or "call frequency" in note.lower():
                _set_cooldown("alphavantage", 300)
    except Exception as e:
        logger.debug("AlphaVantage %s: %s", base, e)
    return None


def get_realtime_price(symbol: str) -> Optional[float]:
    """
    Priority Waterfall for real-time price discovery.
    Stops executing immediately upon a successful fetch.
      1) Yahoo Finance (0 cost)
      2) NSE direct quote-equity (0 cost, India-only, survives Yahoo crumb outages)
      3) TwelveData (800/day)
      4) AlphaVantage (25/day — last resort)
      5) Polygon (sparse India coverage)
      6) NSE bhavcopy EOD close (last resort — not live, but never leaves price=0)
    """
    # 1. Primary: Yahoo
    try:
        q = _yahoo_ohlcv_quote(symbol)
        if q and q.get("price") and float(q["price"]) > 0:
            return float(q["price"])
    except Exception:
        pass
    try:
        px = _waterfall_yahoo_history_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    # 2. NSE direct — free, no key, unaffected by Yahoo's crumb/cookie gate
    try:
        px = _waterfall_nse_direct_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    # 3. TwelveData
    try:
        px = _waterfall_twelvedata_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    # 4. AlphaVantage (quota-scarce)
    try:
        px = _waterfall_alphavantage_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    # 5. Polygon
    try:
        px = _waterfall_polygon_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    # 6. Bhavcopy EOD close — absolute last resort, never leaves price stuck at 0
    try:
        px = _waterfall_bhavcopy_price(symbol)
        if px and px > 0:
            return float(px)
    except Exception:
        pass

    return None


@app.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    """
    Short-circuit waterfall quote path (never parallel-fan-out):

    0) Yahoo live WS feed (yahoo_ws_feed.py) — one persistent connection,
       zero HTTP request, zero rate-limit exposure. Hit rate depends on
       how long the feed has been subscribed/ticking for this symbol.
    1) Soft / durable cache (if warm)
    2) Yahoo OHLCV (primary, $0)
    3) Yahoo Ticker.info (still $0)
    4) TwelveData price (only if Yahoo failed)
    5) AlphaVantage (emergency, 25/day)
    6) Polygon (last resort)
    7) Last-good soft/durable fallback — never invent zeros

    Each stage stops the chain on the first valid price.
    """
    # Genuinely delisted/merged symbols (e.g. TATAMTRDVR, cancelled and
    # converted into TATAMOTORS 2024-08-30): fail fast with a clean 404
    # instead of burning the whole waterfall (yfinance -> TwelveData ->
    # AlphaVantage -> Polygon) only to fail every stage the same way, every
    # single repair cycle, forever. See KNOWN_DELISTED_SYMBOLS above.
    base_check = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if is_known_delisted(base_check):
        raise HTTPException(
            status_code=404,
            detail=f"{base_check} is delisted/merged — not a live NSE symbol (see symbol_aliases/KNOWN_DELISTED_SYMBOLS)",
        )

    sym = normalize_symbol(symbol)

    try:
        import yahoo_ws_feed
        live = yahoo_ws_feed.get_live_quote(sym)
        if live and live.get("price"):
            result = _pad_quote_response(sym, {
                **live,
                "name": sym,
                "fetched_at": datetime.utcnow().isoformat(),
            })
            result = _sanitize_for_json(result)
            result["source"] = "yahoo_ws"
            cache_key = f"quote:{sym}"
            _cache_set(cache_key, result)
            _fallback_set(cache_key, result)
            return result
    except Exception as e:
        logger.debug("yahoo_ws lookup %s: %s", sym, e)

    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)

    # Soft refresh window: serve stale while refreshing only if Yahoo is healthy
    if cached and _should_soft_refresh(cache_key, soft_window=45):
        soft_cached = cached
        cached = None
    else:
        soft_cached = cached

    if cached:
        return cached
    if soft_cached and _in_cooldown("yfinance"):
        return soft_cached

    # ── Primary: Yahoo clean OHLCV ──────────────────────────────────────────
    yahoo_full = None
    if not _in_cooldown("yfinance"):
        try:
            yahoo_full = _yahoo_ohlcv_quote(sym)
        except Exception as e:
            logger.debug("yahoo primary %s: %s", sym, e)

    if yahoo_full and yahoo_full.get("price") and float(yahoo_full["price"] or 0) > 0:
        result = _pad_quote_response(sym, yahoo_full)
        result = _sanitize_for_json(result)
        result["source"] = result.get("source") or "yahoo_clean"
        _cache_set(cache_key, result)
        _fallback_set(cache_key, result)
        return result

    # ── Light Yahoo Ticker.info fallback (still no third-party APIs) ────────
    try:
        ticker_sym = f"{sym}.NS" if not sym.endswith((".NS", ".BO")) else sym
        ticker = yf.Ticker(ticker_sym)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("last_price")
        if price and float(price) > 0:
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
            change_pct = None
            if prev_close and float(prev_close) > 0:
                change_pct = round(((float(price) - float(prev_close)) / float(prev_close)) * 100, 2)
            result = _pad_quote_response(sym, {
                "symbol": sym,
                "name": info.get("longName") or info.get("shortName") or sym,
                "price": float(price),
                "cmp": float(price),
                "previous_close": float(prev_close) if prev_close else None,
                "day_change_pct": change_pct,
                "day_high": info.get("dayHigh") or info.get("regularMarketDayHigh"),
                "day_low": info.get("dayLow") or info.get("regularMarketDayLow"),
                "volume": info.get("volume") or info.get("regularMarketVolume"),
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "source": "yahoo_info",
                "fetched_at": datetime.utcnow().isoformat(),
            })
            result = _sanitize_for_json(result)
            _cache_set(cache_key, result)
            _fallback_set(cache_key, result)
            return result
    except Exception as e:
        logger.debug("yahoo info fallback %s: %s", sym, e)

    # ── Short-circuit waterfall (only when Yahoo failed AND not cooling) ──
    # Skip paid APIs for pure index symbols; skip all when soft cache can serve.
    waterfall_price = None
    waterfall_source = None
    is_index = str(sym).startswith("^") or str(sym).upper().startswith("NIFTY")
    if soft_cached and isinstance(soft_cached, dict) and soft_cached.get("price") is not None:
        # Prefer stale-good over burning TwelveData/AV on bulk feed storms
        if _in_cooldown("yfinance") or _in_cooldown("twelvedata"):
            return _pad_quote_response(sym, soft_cached)
    if not is_index:
        # NSE-direct doesn't depend on yfinance/Yahoo cookies at all, so try
        # it even while yfinance is in cooldown (e.g. "Invalid Crumb" outage)
        # — it's the fastest way back to a real, live price.
        try:
            waterfall_price = _waterfall_nse_direct_price(sym)
            if waterfall_price and waterfall_price > 0:
                waterfall_source = "nse_direct"
        except Exception as e:
            logger.debug("nse-direct waterfall %s: %s", sym, e)

    if not is_index and not waterfall_price and not _in_cooldown("yfinance"):
        try:
            waterfall_price = _waterfall_twelvedata_price(sym)
            if waterfall_price and waterfall_price > 0:
                waterfall_source = "twelvedata"
        except Exception as e:
            logger.debug("twelvedata waterfall %s: %s", sym, e)

        if not waterfall_price:
            try:
                waterfall_price = _waterfall_alphavantage_price(sym)
                if waterfall_price and waterfall_price > 0:
                    waterfall_source = "alphavantage"
            except Exception as e:
                logger.debug("alphavantage waterfall %s: %s", sym, e)

        if not waterfall_price:
            try:
                waterfall_price = _waterfall_polygon_price(sym)
                if waterfall_price and waterfall_price > 0:
                    waterfall_source = "polygon"
            except Exception as e:
                logger.debug("polygon waterfall %s: %s", sym, e)

    if waterfall_price and waterfall_price > 0:
        result = _pad_quote_response(sym, {
            "symbol": sym,
            "name": sym,
            "price": float(waterfall_price),
            "cmp": float(waterfall_price),
            "source": waterfall_source or "waterfall",
            "fetched_at": datetime.utcnow().isoformat(),
        })
        result = _sanitize_for_json(result)
        _cache_set(cache_key, result)
        _fallback_set(cache_key, result)
        return result

    # ── Last-good soft / durable fallback (never invent zeros) ──────────────
    if soft_cached and isinstance(soft_cached, dict) and soft_cached.get("price") is not None:
        return _pad_quote_response(sym, soft_cached)
    fb = _fallback_get(cache_key)
    if fb and isinstance(fb, dict) and fb.get("price") is not None:
        return _pad_quote_response(sym, fb)

    # ── Absolute last resort: NSE bhavcopy EOD close ─────────────────────────
    # Every live source failed (common during a yfinance "Invalid Crumb"
    # outage combined with TwelveData/Polygon having no NSE coverage on the
    # free tier). A real EOD close still beats leaving the Data Feed Health
    # repair button stuck at "0 (missing)" forever.
    if not is_index:
        try:
            bhav_px = _waterfall_bhavcopy_price(sym)
            if bhav_px and bhav_px > 0:
                result = _pad_quote_response(sym, {
                    "symbol": sym,
                    "name": sym,
                    "price": float(bhav_px),
                    "cmp": float(bhav_px),
                    "source": "bhavcopy_eod",
                    "fetched_at": datetime.utcnow().isoformat(),
                })
                result = _sanitize_for_json(result)
                # Short TTL — this is EOD, not live; let the next live attempt override it soon.
                _cache_set(cache_key, result, ttl=120)
                _fallback_set(cache_key, result)
                return result
        except Exception as e:
            logger.debug("bhavcopy last-resort %s: %s", sym, e)

    result = _pad_quote_response(sym, {
        "symbol": sym,
        "name": sym,
        "price": None,
        "cmp": None,
        "source": "failed",
        "fetched_at": datetime.utcnow().isoformat(),
    })
    return _sanitize_for_json(result)



@app.post("/quotes/bulk")
def get_quotes_bulk(req: BulkQuoteRequest):
    """
    Single-call bulk quotes via yf.download for the entire requested universe.
    Replaces ticker-by-ticker loops that trigger free-tier 429 cascades.
    Returns padded quote dicts compatible with the single /quote/{symbol} shape.
    """
    if not req.symbols:
        return {"ok": False, "error": "No symbols", "quotes": []}

    yf_tickers = []
    symbol_map = {}  # yf ticker -> original base symbol (without .NS)
    seen = set()
    for sym in req.symbols:
        raw = (sym or "").strip()
        if not raw:
            continue
        mapped = normalize_symbol(raw)
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        yf_tickers.append(mapped)
        # Prefer clean base for response symbol
        base = mapped.replace(".NS", "").replace(".BO", "")
        if mapped.startswith("^"):
            base = mapped
        # Keep original request form if it was already clean
        orig_base = raw.upper().replace(".NS", "").replace(".BO", "").strip()
        symbol_map[mapped] = orig_base or base

    if not yf_tickers:
        return {"ok": False, "error": "No valid symbols after normalize", "quotes": []}

    try:
        from rate_limiter import acquire as rl_acquire, suggested_timeout as rl_timeout
        rl_acquire("yfinance", weight=len(yf_tickers))
        _ = rl_timeout(25.0, "yfinance")  # widened timeout applied via yf's own session below
    except Exception:
        pass

    try:
        data = yf.download(
            tickers=" ".join(yf_tickers),
            period="2d",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        logger.exception("yf.download bulk failed: %s", e)
        try:
            _report_rate_limit(429 if "429" in str(e) or "Too Many" in str(e) else 502, path="/quotes/bulk", detail=str(e)[:200])
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=str(e)[:300])

    results = []
    if data is None or (hasattr(data, "empty") and data.empty):
        return {"ok": True, "quotes": [], "note": "empty download"}

    # Ensure pandas is available (yfinance already depends on it)
    import pandas as pd

    # yf.download shapes:
    # - 1 ticker: columns are Open/High/Low/Close/Volume (no MultiIndex levels)
    # - N tickers: columns MultiIndex (ticker, OHLCV)
    try:
        is_multi = isinstance(data.columns, pd.MultiIndex) if hasattr(data, "columns") else False
    except Exception:
        is_multi = False

    def _extract_one(sub_df, ticker_key: str):
        try:
            if sub_df is None or (hasattr(sub_df, "empty") and sub_df.empty):
                return None
            sub = sub_df.dropna(how="all")
            if len(sub) < 1:
                return None
            latest = sub.iloc[-1]
            prev = sub.iloc[-2] if len(sub) >= 2 else latest

            def _cell(row, col):
                try:
                    if col not in row.index and col not in getattr(sub, "columns", []):
                        return None
                    v = row[col] if col in row.index else None
                    if v is None:
                        return None
                    f = float(v)
                    if f != f:  # NaN
                        return None
                    return f
                except Exception:
                    return None

            price = _cell(latest, "Close")
            if price is None or price <= 0:
                return None
            prev_close = _cell(prev, "Close")
            if prev_close is None or prev_close <= 0:
                prev_close = price
            chg = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
            day_high = _cell(latest, "High")
            day_low = _cell(latest, "Low")
            vol_raw = _cell(latest, "Volume")
            volume = int(vol_raw) if vol_raw is not None and vol_raw >= 0 else None

            original_sym = symbol_map.get(ticker_key, ticker_key.replace(".NS", "").replace(".BO", ""))
            quote = _pad_quote_response(original_sym, {
                "symbol": original_sym,
                "price": price,
                "previous_close": prev_close,
                "day_change_pct": round(chg, 2),
                "day_high": day_high,
                "day_low": day_low,
                "volume": volume,
                "source": "yahoo_bulk",
                "fetched_at": datetime.utcnow().isoformat(),
            })
            # Cache under both the yf key and the base symbol
            try:
                _cache_set(f"quote:{ticker_key}", quote)
                base_key = original_sym if not original_sym.startswith("^") else ticker_key
                _cache_set(f"quote:{normalize_symbol(base_key)}", quote)
            except Exception:
                pass
            return quote
        except Exception as ex:
            logger.debug("bulk extract failed for %s: %s", ticker_key, ex)
            return None

    if is_multi:
        # columns.levels[0] are the ticker keys
        try:
            tickers_in_df = list(data.columns.levels[0])
        except Exception:
            tickers_in_df = yf_tickers
        for ticker_key in tickers_in_df:
            try:
                sub_df = data[ticker_key]
            except Exception:
                continue
            q = _extract_one(sub_df, str(ticker_key))
            if q:
                results.append(q)
    else:
        # Single-ticker flat frame — map back to the only requested ticker
        ticker_key = yf_tickers[0] if yf_tickers else "UNKNOWN"
        q = _extract_one(data, ticker_key)
        if q:
            results.append(q)

    return {"ok": True, "quotes": _sanitize_for_json(results)}


@app.get("/history/{symbol}")
def get_history(
    symbol: str,
    period: str = Query("6mo", description="1mo, 3mo, 6mo, 1y, 2y, 5y"),
    interval: str = Query("1d", description="1d, 1wk, 1h"),
    force: bool = Query(False, description="Bypass cache for real-time sniper analysis"),
    days: Optional[int] = Query(
        None,
        description=(
            "When given, overrides `period` with an exact start=today-days "
            "window via yfinance's start=/end= instead of a named period "
            "bucket. For a stock that only listed N days ago, requesting "
            "period='1mo'/'3mo' asks Yahoo for a month+ of history that "
            "can't exist — some tickers handle that fine (just return what "
            "exists), others come back as 'possibly delisted; no price "
            "data found' instead of a short, valid range. Callers that "
            "know the real elapsed days (recent-IPO analysis, repair-RSI "
            "for a newly fed stock) should pass this instead of period."
        ),
    ),
):
    # Cap long periods on free-tier 512MB dynos
    _period_rank = {"1mo": 1, "3mo": 2, "6mo": 3, "1y": 4, "2y": 5, "5y": 6}
    if _period_rank.get(period, 4) > _period_rank.get(MAX_HISTORY_PERIOD, 4):
        period = MAX_HISTORY_PERIOD
    """OHLCV for equities and indices.

    Index display names (e.g. "NIFTY NEXT 50") are mapped via normalize_symbol
    to Yahoo tickers. If the primary ticker fails, INDEX_YAHOO_FALLBACKS are tried
    so prediction/sentiment never get stuck on 404/503 for known NSE indices.
    """
    sym = normalize_symbol(symbol)
    candidates = [sym]
    for _fb in INDEX_YAHOO_FALLBACKS.get(sym, []):
        if _fb not in candidates:
            candidates.append(_fb)
    # Also try ^NSEI as last resort for any NIFTY* request
    raw_u = (symbol or "").upper()
    if "NIFTY" in raw_u and "^NSEI" not in candidates:
        candidates.append("^NSEI")

    # `days` (when given) computes an exact start=/end= window instead of
    # snapping to a named period bucket — see the `days` param docstring
    # above for why. Clamp so it still respects MAX_HISTORY_PERIOD's byte
    # budget on free-tier dynos.
    start_date = None
    end_date = None
    if days is not None:
        _period_days_cap = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 366, "2y": 732, "5y": 1830}
        cap = _period_days_cap.get(MAX_HISTORY_PERIOD, 366)
        days = max(1, min(int(days), cap))
        end_date = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)  # inclusive of today
        start_date = end_date - timedelta(days=days)

    cache_key = f"history:{sym}:{period}:{interval}:{days or ''}"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    last_err = None
    for cand in candidates:
        try:
            ticker = yf.Ticker(cand)
            try:
                ticker._tz = "Asia/Kolkata"
            except Exception:
                pass
            if start_date is not None:
                df = _with_retry(
                    lambda t=ticker: t.history(
                        start=start_date, end=end_date, interval=interval, auto_adjust=True
                    ),
                    max_retries=3,
                    base_delay=0.8,
                )
            else:
                df = _with_retry(
                    lambda t=ticker: t.history(period=period, interval=interval, auto_adjust=True),
                    max_retries=3,
                    base_delay=0.8,
                )
            if df is None or df.empty:
                last_err = f"empty history for {cand}"
                logger.info("No history for candidate %s (requested %s)", cand, symbol)
                continue

            candles = []
            for idx, row in df.iterrows():
                if _safe(row["Close"]) is None:
                    continue
                try:
                    vol = int(row["Volume"]) if math.isfinite(float(row["Volume"])) else 0
                except Exception:
                    vol = 0
                candles.append({
                    "date": idx.strftime("%Y-%m-%d %H:%M"),
                    "open": _safe(row["Open"]),
                    "high": _safe(row["High"]),
                    "low": _safe(row["Low"]),
                    "close": _safe(row["Close"]),
                    "volume": vol,
                })

            if not candles:
                last_err = f"no valid candles for {cand}"
                del df
                gc.collect()
                continue

            if len(candles) > MAX_HISTORY_ROWS:
                candles = candles[-MAX_HISTORY_ROWS:]
            del df
            gc.collect()

            result = {
                "symbol": cand,
                "requested": (symbol or "").strip(),
                "period": period,
                "interval": interval,
                "candles": candles,
            }
            hist_ttl = 900 if is_market_open() else 21600
            _cache_set(cache_key, result, ttl=hist_ttl)
            if cand != sym:
                logger.info("History for %s served via fallback ticker %s", symbol, cand)
            return result
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            logger.warning("History candidate %s failed for %s: %s", cand, symbol, e)
            continue

    # All candidates failed
    detail = last_err or f"No history for {symbol}"
    # 404 for unknown symbols; 503 only when Yahoo looks transient
    if last_err and any(x in last_err.lower() for x in ("timeout", "429", "503", "connection", "temporarily")):
        _report_rate_limit(503, path=f"/history/{symbol}", detail=detail, symbol=symbol)
        raise HTTPException(status_code=503, detail=f"Temporary history unavailable for {symbol}: {detail}")
    raise HTTPException(status_code=404, detail=f"No history found for {symbol}: {detail}")

@app.get("/fundamentals/{symbol}")
def get_fundamentals_raw(
    symbol: str,
    force: bool = Query(False, description="Bypass cache for real-time sniper analysis"),
):
    try:
        # _sanitize_for_json wraps EVERY return path here, not just the
        # early cache-hit/cooldown branches inside _get_fundamentals_inner.
        # The fresh-computation success path there builds `result` from
        # raw pandas arithmetic (debt_to_equity, free_cashflow,
        # profit_margins, opm, current_ratio, interest_coverage, pe_growth
        # — all computed as numpy.float64/int64, not native Python floats)
        # and returned it unsanitized. json.dumps has no idea how to
        # serialize numpy.float64 (it isn't a subclass of float), so any
        # request that hit a fresh (non-cached) fundamentals fetch for a
        # well-covered stock crashed Starlette's response.render() —
        # that's the "must be str, bytes or bytearray" / json.encoder
        # traceback from NESTLEIND.NS in the logs. Sanitizing once here,
        # around every path _get_fundamentals_inner can return through,
        # fixes it regardless of which internal branch produced the result.
        return _sanitize_for_json(_get_fundamentals_inner(symbol, force=force))
    except Exception as e:
        logger.warning("fundamentals failed for %s: %s", symbol, e)
        return _sanitize_for_json({
            "symbol": str(symbol).upper().replace(".NS", "") + ".NS" if not str(symbol).endswith(".NS") else str(symbol),
            "error": "fundamentals_unavailable",
            "message": str(e)[:200],
            "pe_ratio": None,
            "roe": None,
        })

def _get_fundamentals_inner(symbol: str, force: bool = False):
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            return _sanitize_for_json(cached)
    if _in_cooldown("yfinance"):
        fb = _fallback_get(cache_key)
        if fb:
            return _sanitize_for_json(fb)
        return _sanitize_for_json({
            "symbol": sym,
            "error": "yfinance_cooldown",
            "message": "Yahoo rate-limited — serving empty fundamentals until cooldown ends",
            "pe_ratio": None,
            "roe": None,
            "revenue_growth": None,
        })

    result = {}
    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"

        info = {}
        try:
            info = _with_retry(lambda: ticker.info, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch info for {sym}: {e}")

        if info is None:
            info = {}

        financials = None
        balance = None
        cashflow = None
        try:
            financials = _with_retry(lambda: ticker.financials, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch financials for {sym}: {e}")
        try:
            balance = _with_retry(lambda: ticker.balance_sheet, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch balance sheet for {sym}: {e}")
        try:
            cashflow = _with_retry(lambda: ticker.cashflow, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch cashflow for {sym}: {e}")

        def _safe_info(key):
            val = info.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        financials_available = financials is not None and not financials.empty
        balance_available = balance is not None and not balance.empty
        cashflow_available = cashflow is not None and not cashflow.empty

        revenue_growth = None
        if financials_available and "Total Revenue" in financials.index:
            rev_series = financials.loc["Total Revenue"]
            if len(rev_series) >= 2:
                current_rev = rev_series.iloc[0]
                prev_rev = rev_series.iloc[1]
                revenue_growth = _compute_growth(current_rev, prev_rev)

        earnings_growth = None
        if financials_available and "Net Income" in financials.index:
            earnings_series = financials.loc["Net Income"]
            if len(earnings_series) >= 2:
                current_earn = earnings_series.iloc[0]
                prev_earn = earnings_series.iloc[1]
                earnings_growth = _compute_growth(current_earn, prev_earn)

        roe = None
        if "returnOnEquity" in info:
            roe = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("returnOnEquity"))) if _safe_info("returnOnEquity") else None
        elif balance_available and "Total Equity Gross Minority Interest" in balance.index:
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if financials_available and "Net Income" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                if equity != 0:
                    roe = (net_income / equity) * 100

        debt_to_equity = None
        if "debtToEquity" in info:
            debt_to_equity = _normalize_de_ratio(_safe_info("debtToEquity"), info.get("sector"))
        elif balance_available and "Total Debt" in balance.index and "Total Equity Gross Minority Interest" in balance.index:
            total_debt = balance.loc["Total Debt"].iloc[0]
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if equity != 0:
                debt_to_equity = total_debt / equity

        free_cashflow = None
        if "freeCashflow" in info:
            free_cashflow = _safe_info("freeCashflow")
        elif cashflow_available and "Free Cash Flow" in cashflow.index:
            free_cashflow = cashflow.loc["Free Cash Flow"].iloc[0]

        profit_margins = None
        if "profitMargins" in info:
            profit_margins = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("profitMargins")))
        else:
            if financials_available and "Net Income" in financials.index and "Total Revenue" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                revenue = financials.loc["Total Revenue"].iloc[0]
                if revenue != 0:
                    profit_margins = (net_income / revenue) * 100

        held_percent_institutions = None
        if "heldPercentInstitutions" in info:
            held_percent_institutions = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("heldPercentInstitutions")))
        elif "institutionalPercent" in info:
            held_percent_institutions = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("institutionalPercent")))

        pe_ratio = _safe_info("trailingPE") if "trailingPE" in info else _safe_info("peRatio")
        forward_pe = _safe_info("forwardPE")
        eps = _safe_info("trailingEps") or _safe_info("eps")
        market_cap = _safe_info("marketCap")
        dividend_yield = None
        if "dividendYield" in info:
            dividend_yield = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("dividendYield")))
        else:
            try:
                divs = ticker.dividends
                if divs is not None and not divs.empty:
                    last_price = _safe_info("regularMarketPrice") or _safe_info("last_price")
                    if last_price:
                        annual_div = float(divs.tail(4).sum())
                        dividend_yield = round(annual_div / last_price * 100, 2)
            except Exception:
                pass

        year_high = _safe_info("fiftyTwoWeekHigh")
        year_low = _safe_info("fiftyTwoWeekLow")
        fifty_day_average = _safe_info("fiftyDayAverage")
        two_hundred_day_average = _safe_info("twoHundredDayAverage")
        year_change_pct = _safe_info("52WeekChange")
        if year_change_pct is not None:
            year_change_pct = year_change_pct * 100

        sector = info.get("sector")
        industry = info.get("industry")

        pe_growth = pe_ratio / earnings_growth if (pe_ratio is not None and earnings_growth is not None and earnings_growth != 0) else None
        ev_ebitda = _safe_info("enterpriseToEbitda")
        price_to_book = _safe_info("priceToBook")
        roce = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("returnOnCapitalEmployed"))) if _safe_info("returnOnCapitalEmployed") else None
        opm = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("operatingMargins")))
        if opm is None and financials_available and "Operating Income" in financials.index and "Total Revenue" in financials.index:
            op_income = financials.loc["Operating Income"].iloc[0]
            revenue = financials.loc["Total Revenue"].iloc[0]
            opm = (op_income / revenue) * 100 if revenue != 0 else None

        current_ratio = None
        if balance_available and "Total Current Assets" in balance.index and "Total Current Liabilities" in balance.index:
            ca = balance.loc["Total Current Assets"].iloc[0]
            cl = balance.loc["Total Current Liabilities"].iloc[0]
            current_ratio = ca / cl if cl != 0 else None

        interest_coverage = None
        if financials_available:
            if "EBIT" in financials.index and "Interest Expense" in financials.index:
                ebit = financials.loc["EBIT"].iloc[0]
                interest = financials.loc["Interest Expense"].iloc[0]
                interest_coverage = ebit / interest if interest != 0 else None
            elif "Operating Income" in financials.index and "Interest Expense" in financials.index:
                ebit = financials.loc["Operating Income"].iloc[0]
                interest = financials.loc["Interest Expense"].iloc[0]
                interest_coverage = ebit / interest if interest != 0 else None

        promoter_holding = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("promoterHolding"))) if "promoterHolding" in info else None
        promoter_pledging = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("promoterPledging"))) if "promoterPledging" in info else None

        result = {
            "symbol": sym,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "market_cap": market_cap,
            "dividend_yield": dividend_yield,
            "year_change_pct": year_change_pct,
            "year_high": year_high,
            "year_low": year_low,
            "fifty_day_average": fifty_day_average,
            "two_hundred_day_average": two_hundred_day_average,
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "eps": eps,
            "roe": roe,
            "roce": roce,
            "debt_to_equity": debt_to_equity,
            "free_cashflow": free_cashflow,
            "profit_margins": profit_margins,
            "opm": opm,
            "current_ratio": current_ratio,
            "interest_coverage": interest_coverage,
            "held_percent_insiders": (lambda v: (v * 100) if v is not None else None)(_safe_info("heldPercentInsiders")),
            "held_percent_institutions": held_percent_institutions,
            "price_to_book": price_to_book,
            "pe_growth": pe_growth,
            "ev_ebitda": ev_ebitda,
            "promoter_holding": promoter_holding,
            "promoter_pledging": promoter_pledging,
            "sector": sector,
            "industry": industry,
        }

        logger.info(f"Fundamentals for {sym}: PE={pe_ratio}, ROE={roe}, Revenue growth={revenue_growth}")

    except Exception as e:
        logger.warning(f"YFinance failed for {sym}, falling back to NSE India: {e}")
        nse_data = _fetch_nse_fundamentals(sym)
        if nse_data:
            sec_info = nse_data.get("secInfo", {})
            result = {
                "symbol": sym,
                "pe_ratio": _safe(sec_info.get("pe")),
                "forward_pe": None,
                "market_cap": _safe(sec_info.get("marketCap")),
                "dividend_yield": _safe(sec_info.get("dividendYield")),
                "year_change_pct": None,
                "year_high": None,
                "year_low": None,
                "fifty_day_average": None,
                "two_hundred_day_average": None,
                "revenue_growth": None,
                "earnings_growth": None,
                "eps": None,
                "roe": _safe(sec_info.get("roe")),
                "roce": None,
                "debt_to_equity": _safe(sec_info.get("debtToEquity")),
                "free_cashflow": None,
                "profit_margins": None,
                "opm": None,
                "current_ratio": None,
                "interest_coverage": None,
                "held_percent_insiders": None,
                "held_percent_institutions": None,
                "price_to_book": None,
                "pe_growth": None,
                "ev_ebitda": None,
                "promoter_holding": None,
                "promoter_pledging": None,
                "sector": sec_info.get("sector"),
                "industry": sec_info.get("industry"),
            }
            logger.info(f"NSE India fallback fundamentals found for {sym}")

    if result and any(v is not None for v in [result.get("pe_ratio"), result.get("sector"), result.get("market_cap"), result.get("revenue_growth"), result.get("roe")]):
        _fallback_set(cache_key, result)

    if result:
        _cache_set(cache_key, result, ttl=86400)  # fundamentals change slowly, cache 24h
        return result

    stale = _fallback_get(cache_key)
    if stale:
        logger.info(f"Serving fallback fundamentals for {sym} (stale data)")
        stale = dict(stale)
        stale["stale"] = True
        _cache_set(cache_key, stale, ttl=1800)
        return stale

    raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {sym}")


# ── Surprise pre-market baselines (Neon surprise_static_feed) ───────────────
class SurprisePremarketRequest(BaseModel):
    symbols: Optional[list] = None


@app.post("/surprise/premarket")
def surprise_premarket_run(
    body: Optional[SurprisePremarketRequest] = None,
    symbols: Optional[str] = Query(None, description="Comma-separated symbols"),
    background: bool = Query(True, description="Run in background and poll /surprise/premarket/status"),
):
    """
    Pre-compute static baselines for surprise scanner (run ~08:55 IST or manual).
    Default background=true so Render does not hit the 100s gateway timeout.
    Poll GET /surprise/premarket/status for progress %.
    """
    from surprise_premarket import (
        precalculate_surprise_baselines,
        default_universe_from_env,
        get_premarket_progress,
    )
    import threading

    syms: list = []
    if body and body.symbols:
        syms = [str(s).strip() for s in body.symbols if str(s).strip()]
    elif symbols:
        syms = [x.strip() for x in symbols.split(",") if x.strip()]
    if not syms:
        syms = default_universe_from_env()

    prog = get_premarket_progress()
    if prog.get("is_running"):
        return {
            "ok": True,
            "accepted": False,
            "already_running": True,
            "message": "Premarket already running — poll /surprise/premarket/status",
            "progress": prog,
        }

    if background:
        def _job():
            try:
                precalculate_surprise_baselines(syms)
            except Exception as e:
                logger.exception("background premarket: %s", e)

        threading.Thread(target=_job, daemon=True, name="surprise-premarket").start()
        return {
            "ok": True,
            "accepted": True,
            "background": True,
            "symbols": len(syms),
            "message": "Premarket started — poll /surprise/premarket/status",
            "progress": get_premarket_progress(),
        }

    result = precalculate_surprise_baselines(syms)
    return result


@app.get("/surprise/premarket")
def surprise_premarket_get(
    symbols: Optional[str] = Query(None, description="Comma-separated symbols"),
    background: bool = Query(True),
):
    """GET variant for cron curl simplicity."""
    return surprise_premarket_run(body=None, symbols=symbols, background=background)


@app.get("/surprise/premarket/status")
def surprise_premarket_status():
    """Progress for manual UI button (percent, ETA, current symbol)."""
    from surprise_premarket import get_premarket_progress

    return get_premarket_progress()



@app.get("/surprise/static")
def surprise_static_list(limit: int = 50):
    """Peek rows in surprise_static_feed (debug / health)."""
    from surprise_premarket import _db_url

    url = _db_url()
    if not url:
        return {"ok": False, "error": "no_database_url", "rows": []}
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with eng.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
                        "dist_52w_pct, sector, is_liquid, updated_at "
                        "FROM surprise_static_feed ORDER BY updated_at DESC LIMIT :lim"
                    ),
                    {"lim": max(1, min(limit, 500))},
                )
                .mappings()
                .all()
            )
        eng.dispose()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("updated_at") is not None:
                d["updated_at"] = str(d["updated_at"])
            out.append(d)
        return {"ok": True, "count": len(out), "rows": out}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}



if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)



# ── Official NSE Bhavcopy + quote delivery % (free) ─────────────────────────
@app.get("/delivery/{symbol}")
def get_delivery_pct(symbol: str):
    """
    Accurate delivery % for NSE symbols (free tier).
    Order: Redis cache → NSE quote-equity → official bhavcopy archives → neutral fallback.
    Never fails the request; always returns a structured payload with `source`.
    """
    from bhavcopy import get_delivery

    sym = normalize_symbol(symbol).replace(".NS", "").replace(".BO", "")
    cache_key = f"delivery:{sym}"
    cached = _cache_get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("delivery_pct") is not None:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached

    result = get_delivery(sym)
    result["from_cache"] = False
    # Cache real sources longer outside market hours via existing TTL helper
    ttl = get_cache_ttl()
    if result.get("source") == "fallback_neutral":
        ttl = min(ttl, 900)  # retry sooner when we only had a placeholder
    _cache_set(cache_key, result, ttl=ttl)
    return result


@app.get("/delivery/{symbol}/refresh")
def refresh_delivery_pct(symbol: str):
    """Force-refresh delivery (bypass cache) — useful after market close."""
    from bhavcopy import get_delivery

    sym = normalize_symbol(symbol).replace(".NS", "").replace(".BO", "")
    cache_key = f"delivery:{sym}"
    result = get_delivery(sym)
    result["from_cache"] = False
    _cache_set(cache_key, result, ttl=get_cache_ttl())
    return result