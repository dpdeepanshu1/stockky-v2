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
import logging
import difflib
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Set, Dict, Union

import httpx
import yfinance as yf
import feedparser
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from upstash_redis import Redis

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

# ── Redis ──────────────────────────────────────────────────────────────────────
_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
    logger.info("Connected to Upstash Redis")
except Exception as e:
    logger.warning("Redis unavailable: %s", e)

WATCHLIST_KEY       = "stockky:watchlist"
SEARCHED_KEY        = "stockky:searched_symbols"
SCAN_UNIVERSE_KEY   = "stockky:scan_universe"
IPO_CACHE_KEY       = "stockky:ipos:recent"
KNOWN_SYMBOLS_KEY   = "stockky:known_symbols"
SCAN_TASK_PREFIX    = "stockky:scan_task:"
MARKET_MOVERS_CACHE_PREFIX = "stockky:market_movers:"
INDICES_CACHE_KEY   = "stockky:indices"
INDICES_LAST_KNOWN  = "stockky:indices_last_known"

FUNDAMENTAL_CACHE_PREFIX = "stockky:fundamental:"
EVENT_CACHE_PREFIX = "stockky:event:"
LAST_FULL_SCAN_KEY = "stockky:last_full_scan"
LAST_FULL_SCAN_TTL = int(os.getenv("LAST_FULL_SCAN_TTL", "900"))  # 15 min default
DECIDE_CACHE_PREFIX = "stockky:decide_cache:"
DECIDE_CACHE_TTL_OPEN = int(os.getenv("DECIDE_CACHE_TTL_OPEN", "300"))   # 5 min market open
DECIDE_CACHE_TTL_CLOSED = int(os.getenv("DECIDE_CACHE_TTL_CLOSED", "21600"))  # 6 h closed
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
def _redis_get(key: str):
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None

def _redis_set(key: str, value, ttl: int = None):
    if not _redis:
        return
    try:
        data = json.dumps(value, default=str)
        if ttl:
            _redis.setex(key, ttl, data)
        else:
            _redis.set(key, data)
    except Exception as e:
        logger.warning("Redis set failed: %s", e)

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
    data = _fetch_from_nse_api("equity-stockIndices?index=SECURITIES%20IN%20NSE", "nse:all_securities")
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
    indices = ["NIFTY%2050", "NIFTY%20NEXT%2050", "NIFTY%20MIDCAP%20100"]
    all_symbols = []
    for idx in indices:
        data = _fetch_from_nse_api(f"equity-stockIndices?index={idx}", f"nse:index_{idx}")
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
    data = _fetch_from_nse_api("ipo?type=listed", IPO_CACHE_KEY, ttl=86400)
    symbols = []
    if data and isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                sym = item.get("symbol") or item.get("secCode")
                if sym:
                    symbols.append(sym.upper())
    if not symbols:
        symbols = ["JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO", "RAILTEL", "IRFC", "MVELECTRO"]
    return symbols

def _get_momentum_movers() -> List[str]:
    movers = []
    try:
        nifty_symbols = _get_nifty_indices()[:50]
        performances = []
        for sym in nifty_symbols:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                hist = ticker.history(period="5d", interval="1d")
                if hist.empty or len(hist) < 2:
                    continue
                week_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
                performances.append((sym, float(week_change)))
            except Exception:
                continue
        performances.sort(key=lambda x: x[1], reverse=True)
        movers = [s for s, _ in performances[:10]] + [s for s, _ in performances[-10:]]
    except Exception as e:
        logger.warning("Could not fetch momentum movers: %s", e)
    return movers

def _get_news_mentioned_symbols() -> List[str]:
    mentioned = []
    try:
        feed = feedparser.parse(
            "https://news.google.com/rss/search?q=NSE+stock+bulk+deal+earnings+results&hl=en-IN&gl=IN&ceid=IN:en"
        )
        text = " ".join(e.title for e in feed.entries[:30]).upper()
        all_symbols = _get_all_nse_securities()
        for sym in all_symbols[:300]:
            if sym in text:
                mentioned.append(sym)
    except Exception as e:
        logger.warning("Could not parse news for symbols: %s", e)
    return mentioned[:15]

def _get_event_symbols() -> List[str]:
    try:
        resp = httpx.get(f"{EVENT_URL}/symbols_with_events", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data.get("symbols", [])
            elif isinstance(data, list):
                return data
    except Exception as e:
        logger.warning(f"Could not fetch event symbols: {e}")
    return []

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

    result = clean[:300]
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=21600)
    logger.info(f"Scan universe built: {len(result)} symbols")
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
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=5)
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
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached.get("metrics"), cached.get("fallback", False)

    try:
        resp = await client.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            metrics = data.get("metrics")
            fallback_used = data.get("fallback_used", False)
            _redis_set(cache_key, {"metrics": metrics, "fallback": fallback_used}, ttl=21600)
            return metrics, fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")
    return {}, True

async def _fetch_events_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached

    try:
        resp = await client.get(f"{EVENT_URL}/events/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                _redis_set(cache_key, data, ttl=21600)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {e}")
    return None

async def _fetch_news_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    try:
        resp = await client.get(f"{NEWS_URL}/analyze/{symbol}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"News fetch failed for {symbol}: {e}")
    return None

async def _fetch_prediction_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[float], Optional[str]]:
    try:
        resp = await client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
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
MAX_PARALLEL_WORKERS = int(os.getenv("MAX_PARALLEL_SCAN_WORKERS", "18"))
MAX_RETRIES = 1
RETRY_BACKOFF = 1.0

def _is_market_open_ist() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (t.hour > 9 or (t.hour == 9 and t.minute >= 15)) and (t.hour < 15 or (t.hour == 15 and t.minute <= 30))

def _decide_cache_ttl() -> int:
    return DECIDE_CACHE_TTL_OPEN if _is_market_open_ist() else DECIDE_CACHE_TTL_CLOSED

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

async def _wake_required_services(client: httpx.AsyncClient = None) -> dict:
    """Wake free-tier services before scan. Market-data gets a warm yfinance touch + double ping."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=20)
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
                    await asyncio.sleep(1.5)
                    r2 = await client.get(f"{base}/health", timeout=10)
                    ok = r.status_code == 200 or r2.status_code == 200
                    return name, {"ok": ok, "status": r2.status_code if r2 else r.status_code, "warmed": True}
                r = await client.get(f"{base}/health", timeout=12)
                return name, {"ok": r.status_code == 200, "status": r.status_code}
            except Exception as e:
                return name, {"ok": False, "error": str(e)[:120]}
        pairs = await asyncio.gather(*(ping(n, cfg["url"]) for n, cfg in SYSTEM_SERVICES.items()))
        results = dict(pairs)
    finally:
        if own_client:
            await client.aclose()
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
                    decision_resp = await client.get(f"{DECISION_URL}/decide/{symbol}", timeout=90)
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
    limits = httpx.Limits(max_keepalive_connections=200, max_connections=200)
    cancelled = False
    cancel_key = SCAN_TASK_PREFIX + task_id + ":cancel"
    async with httpx.AsyncClient(timeout=240, limits=limits) as client:
        # Wake services first on free-tier so cold starts don't serialize into every symbol
        if WAKE_BEFORE_SCAN:
            try:
                wake_results = await _wake_required_services(client)
                logger.info("Pre-scan wake: %s", {k: v.get("ok") for k, v in wake_results.items()})
                # Extra wait when market-data was cold so first history calls succeed
                wait = max(WAKE_WAIT_SECONDS, 10.0)
                if not (wake_results.get("market-data") or {}).get("ok"):
                    wait = max(wait, 18.0)
                await asyncio.sleep(min(wait, 25.0))
            except Exception as e:
                logger.warning("Pre-scan wake failed (continuing): %s", e)

        # asyncio.ensure_future wraps each coroutine as a real Task —
        # bare coroutines (what `_analyze_one_symbol_ultra(...)` returns
        # before being scheduled) don't have .done()/.cancel() at all.
        # The cancellation code below used to call those on the bare
        # coroutines directly, which raised an unhandled AttributeError
        # the instant a cancel was requested — silently killing this
        # entire background task before it ever reached the code that
        # writes the finalized "done" status, which is exactly why
        # Stop Scan appeared to hang forever with no summary.
        tasks = [
            asyncio.ensure_future(_analyze_one_symbol_ultra(sym, client, sem, lite=lite))
            for sym in universe
        ]

        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result.get("decision") == "ERROR":
                    errors.append({"symbol": result.get("symbol"), "error": result.get("error", "Unknown error")})
                else:
                    results.append(result)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Task failed: {e}")
            processed += 1
            elapsed = round(time.time() - start_time, 1)

            # Separate Redis key from the progress-status one below, so a
            # cancel request can never be silently overwritten by the
            # periodic progress write two lines down — that write used to
            # replace the entire stored dict (including cancel_requested)
            # with a fresh one that didn't have that key at all.
            if processed % 3 == 0:
                if _redis_get(cancel_key):
                    cancelled = True

            if processed % 5 == 0 or processed == total or cancelled:
                _redis_set(SCAN_TASK_PREFIX + task_id, {
                    "status": "running",
                    "total": total,
                    "processed": processed,
                    "elapsed": elapsed,
                    "result": None,
                    "error": None,
                }, ttl=3600)

            if cancelled:
                logger.info(f"Scan {task_id} cancelled after {processed}/{total} symbols — finalizing with partial results")
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break

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

    _redis_set(SCAN_TASK_PREFIX + task_id, {
        "status": "done",
        "total": total,
        "processed": processed,
        "elapsed": elapsed_final,
        "result": final_result,
        "error": None,
    }, ttl=3600)

    # Cache full scan result so repeated "Run market scan" within TTL is instant
    if not cancelled and results:
        _redis_set(LAST_FULL_SCAN_KEY, {
            "task_id": task_id,
            "result": final_result,
            "scanned_at": final_result["scanned_at"],
            "universe_size": final_result["universe_size"],
        }, ttl=LAST_FULL_SCAN_TTL)

    _send_scan_notification(final_result.get("recommendations", []), final_result["verdict"], final_result["scanned"], final_result["universe_size"])

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

@app.get("/health")
def health():
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
            async with httpx.AsyncClient(timeout=timeout) as client:
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
    async with httpx.AsyncClient(timeout=5) as client:
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
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol_to_use}",
            params={"already_owned": already_owned},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()
        result = _normalize_decision_response(raw, symbol_to_use)

        if result.get("close") is None:
            price = _fetch_price_from_quote(symbol_to_use)
            if price is not None:
                result["close"] = price
                if result.get("support") is None:
                    result["support"] = round(price * 0.95, 2)
                if result.get("resistance") is None:
                    result["resistance"] = round(price * 1.05, 2)

        _merge_fundamentals(result, symbol_to_use)

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
            _redis_set(cache_key, {"metrics": metrics, "fallback": fallback_used}, ttl=21600)
            normalized["fundamental_metrics"] = metrics if metrics else {}
            normalized["fundamental_fallback"] = fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")

def _fetch_news(symbol: str) -> Optional[dict]:
    try:
        resp = httpx.get(f"{NEWS_URL}/analyze/{symbol}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
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
                _redis_set(cache_key, data, ttl=21600)
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
    async with httpx.AsyncClient(timeout=240) as client:
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
        use_lite = SCAN_LITE_DEFAULT if lite is None else bool(lite)

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

@app.get("/scan/status/{task_id}")
def get_scan_status(task_id: str):
    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Task not found or expired")
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
    """Requests cancellation of a running scan. run_scan_parallel checks
    a dedicated cancel key (not the shared progress dict, which gets
    periodically overwritten and would silently wipe this flag) every
    3rd completion, and once seen, stops collecting further results and
    finalizes the task as 'done' with whatever was actually scored so far."""
    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Task not found or expired")
    if data.get("status") != "running":
        return {"status": "already_finished", "task_status": data.get("status")}
    _redis_set(SCAN_TASK_PREFIX + task_id + ":cancel", True, ttl=3600)
    return {"status": "cancel_requested", "processed_so_far": data.get("processed", 0), "total": data.get("total", 0)}

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
        resp = httpx.post(
            f"{NOTIFICATION_URL}/config",
            json=update.model_dump(exclude_none=True),
            timeout=10,
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
    """Proxy CallMeBot test / manual call."""
    try:
        # Prefer query message; allow JSON body override on POST
        msg = message
        try:
            # body may be empty
            pass
        except Exception:
            pass
        resp = httpx.post(
            f"{NOTIFICATION_URL}/call/me",
            params={"message": msg},
            timeout=25,
        )
        resp.raise_for_status()
        return resp.json()
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
        async with httpx.AsyncClient(timeout=45) as client:
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
        async with httpx.AsyncClient(timeout=60) as client:
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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{TRAINING_URL}/training-score/{symbol}")
            resp.raise_for_status()
            return resp.json()
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
        )
    )
    timeout = 180.0 if heavy else 60.0
    try:
        body = await request.body()
        fwd_headers = {"Accept": "application/json"}
        ct = request.headers.get("content-type")
        if ct:
            fwd_headers["Content-Type"] = ct

        target_url = f"{TRAINING_URL.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            # httpx decompresses; do not ask upstream for identity only — default is fine
        ) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                content=body if body else None,
                params=request.query_params,
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)