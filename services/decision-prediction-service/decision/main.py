"""
Decision Engine Service v0.7.6
Changes:
- Fetches market sentiment from API Gateway's /market/indices endpoint (fast and reliable)
- Always includes the live market_score in the response
- Added retry and logging
- Speed: in-process + Redis decide cache, bulk /decide/batch endpoint (free-tier friendly)
- Multi-horizon scoring (short/mid/long) via horizons.py
- v0.7.5: Expanded shared httpx pool (150 keepalive / 400 max connections, 45s timeout)
  to eliminate PoolTimeout and ReadTimeout under concurrent free-tier scan load
- v0.7.6: Short-circuit path — when gateway/Neon already supplies RSI, PE, technical_score,
  fundamental_score, news_score etc., skip the corresponding HTTP calls to analysis-intelligence.
  New POST /decide/evaluate accepts a payload and prefers supplied data (eliminates ~90% of
  internal traffic during market scans on free tier).
"""
import os
import json
import asyncio
import gc
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from horizons import multi_horizon_decide
from circuit_breaker import get_breaker, CircuitOpenError, all_snapshots

def _safe_int(val, default=50):
    try:
        if val is None:
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default

def _safe_float(val, default=None):
    try:
        if val is None:
            return default
        f = float(val)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

# ---- Service URLs (env-driven; aligned with config/service_urls.py) ----
_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
_DP = os.getenv("DECISION_PREDICTION_URL", "https://decision-prediction-service.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI.rstrip('/')}/technical")
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_AI.rstrip('/')}/fundamental")
NEWS_URL = os.getenv("NEWS_URL", f"{_AI.rstrip('/')}/news")
EVENT_URL = os.getenv("EVENT_URL", f"{_AI.rstrip('/')}/event")
PREDICTION_URL = os.getenv("PREDICTION_URL", f"{_DP.rstrip('/')}/prediction")
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "https://api-gateway-puwd.onrender.com")
TRAINING_SERVICE_URL = os.getenv("TRAINING_SERVICE_URL", f"{_DP.rstrip('/')}/training")

EARNINGS_RISK_DAYS = 3
EARNINGS_BOOST_DAYS = 7

# ── Decide cache (avoids re-running full fan-out for same symbol within TTL) ──
IST = ZoneInfo("Asia/Kolkata")
DECIDE_CACHE_TTL_OPEN = int(os.getenv("DECIDE_CACHE_TTL_OPEN", "600"))
DECIDE_CACHE_TTL_CLOSED = int(os.getenv("DECIDE_CACHE_TTL_CLOSED", "43200"))
BATCH_MAX_SYMBOLS = int(os.getenv("DECIDE_BATCH_MAX", "25"))
BATCH_CONCURRENCY = int(os.getenv("DECIDE_BATCH_CONCURRENCY", "8"))

_decide_mem_cache: dict = {}  # symbol -> (expires_ts, payload)
_redis = None
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    _USE_REDIS = False
try:
    import kv_cache as _kv
except Exception:
    _kv = None  # type: ignore
if _USE_REDIS:
    try:
        from upstash_redis import Redis
        _url = os.getenv("UPSTASH_REDIS_REST_URL")
        _tok = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if _url and _tok:
            _redis = Redis(url=_url, token=_tok)
            _redis.ping()
            logger.info("Decision-engine Upstash Redis ON (USE_REDIS=1)")
    except Exception as e:
        logger.warning("Decision-engine Redis unavailable: %s", e)
        _redis = None
else:
    logger.info("Decision-engine Redis OFF — memory + optional Neon decide cache")

def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (t.hour > 9 or (t.hour == 9 and t.minute >= 15)) and (t.hour < 15 or (t.hour == 15 and t.minute <= 30))

def _cache_ttl() -> int:
    return DECIDE_CACHE_TTL_OPEN if _is_market_open() else DECIDE_CACHE_TTL_CLOSED

def _cache_get_decide(symbol: str):
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    now = time_module_time()
    entry = _decide_mem_cache.get(sym)
    if entry and entry[0] > now:
        return entry[1]
    if _redis:
        try:
            raw = _redis.get(f"decide:{sym}")
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                _decide_mem_cache[sym] = (now + _cache_ttl(), data)
                return data
        except Exception:
            pass
    if _kv is not None:
        try:
            data = _kv.get(f"stockky:decide_cache:{sym}")
            if data is not None:
                _decide_mem_cache[sym] = (now + _cache_ttl(), data)
                return data
        except Exception:
            pass
    return None

def _is_weak_decide_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return True
    reasons = payload.get("reasons") or {}
    blob = " ".join(
        str(x)
        for k in ("technical", "fundamental", "market")
        for x in (reasons.get(k) or [])
    ).lower()
    if "error processing" in blob or "temporarily unavailable" in blob:
        return True
    if payload.get("combined_score") in (0, None) and payload.get("close") is None:
        return True
    if payload.get("data_insufficient") and payload.get("close") is None:
        return True
    return False


def _cache_set_decide(symbol: str, payload: dict):
    # Never cache failed/weak payloads — forces live API on next Analyse
    if _is_weak_decide_payload(payload):
        return
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    ttl = _cache_ttl()
    _decide_mem_cache[sym] = (time_module_time() + ttl, payload)
    if _redis:
        try:
            _redis.setex(f"decide:{sym}", ttl, json.dumps(payload, default=str))
        except Exception:
            pass
    if _kv is not None:
        try:
            _kv.set(f"stockky:decide_cache:{sym}", payload, ttl=ttl)
        except Exception:
            pass

def time_module_time():
    import time as _t
    return _t.time()

app = FastAPI(title="Stockky Decision Engine", version="0.7.6")

# Shared downstream client — avoid per-request TLS to analysis/training/market-data
# Expanded pool + longer timeouts for free-tier Render (prevents PoolTimeout / ReadTimeout
# when scanning many symbols and fanning out to technical/fundamental/news/event/prediction)
_HTTP_LIMITS = httpx.Limits(max_keepalive_connections=150, max_connections=400)
_HTTP_TIMEOUT = httpx.Timeout(45.0, connect=15.0)
_shared_http: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is not None and not _shared_http.is_closed:
        return _shared_http
    _shared_http = httpx.AsyncClient(limits=_HTTP_LIMITS, timeout=_HTTP_TIMEOUT, follow_redirects=True)
    return _shared_http


@app.on_event("startup")
async def _start_http_pool():
    global _shared_http
    _shared_http = httpx.AsyncClient(limits=_HTTP_LIMITS, timeout=_HTTP_TIMEOUT, follow_redirects=True)
    logger.info("Decision shared httpx pool started (limits=150 keepalive / 400 max, timeout=45s)")


@app.on_event("shutdown")
async def _stop_http_pool():
    global _shared_http
    if _shared_http is not None and not _shared_http.is_closed:
        await _shared_http.aclose()
        _shared_http = None

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"},
    )


class Decision(str, Enum):
    BUY_NOW = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    HOLD = "HOLD"
    WAIT = "WAIT"
    DO_NOT_BUY = "DO NOT BUY"
    SELL = "SELL"


@app.get("/")
def root():
    return {"service": "Stockky Decision Engine", "version": "0.7.6", "status": "running",
            "features": ["decide_cache", "decide_batch"]}


@app.get("/health")
def health(warm: bool = False):
    """warm=true pings downstream URLs so free-tier dynos stay responsive."""
    warmed = {}
    if warm:
        import httpx as _hx
        for name, url in [
            ("technical", f"{TECHNICAL_URL}/health"),
            ("fundamental", f"{FUNDAMENTAL_URL}/health"),
            ("news", f"{NEWS_URL}/health"),
            ("event", f"{EVENT_URL}/health"),
        ]:
            try:
                r = _hx.get(url, timeout=8)
                warmed[name] = r.status_code == 200
            except Exception as e:
                warmed[name] = False
    return {
        "status": "ok",
        "service": "decision-engine-service",
        "warmed": warmed or None,
        "circuits": all_snapshots(),
    }


@app.get("/circuits")
def circuits_status():
    return {"circuits": all_snapshots()}


# ── Fetch helpers ──────────────────────────────────────────────────
async def _fetch_optional(client: httpx.AsyncClient, url: str, label: str):
    """Fetch optional pillar with circuit breaker (fail fast when dependency is down)."""
    breaker = get_breaker(f"decision:{label.lower()}", failure_threshold=8, recovery_timeout=45)
    if not breaker.allow():
        logger.warning("%s circuit OPEN — skip (retry in %.0fs)", label, breaker.retry_after())
        return None
    # Fundamentals / prediction need more than 5s on free-tier cold start
    timeout = httpx.Timeout(35.0 if label.lower() in ("fundamental", "prediction", "technical") else 20.0, connect=8.0)
    try:
        resp = await client.get(url, timeout=timeout)
        if resp.status_code >= 400:
            detail = (resp.text or "")[:180].replace("\n", " ")
            breaker.record_failure(f"HTTP {resp.status_code}")
            logger.warning("%s unavailable: HTTP %s %s", label, resp.status_code, detail)
            return None
        data = resp.json()
        breaker.record_success()
        return data
    except Exception as e:
        msg = str(e) or type(e).__name__
        breaker.record_failure(msg)
        logger.warning("%s unavailable: %s", label, msg)
        return None


# ── Market Sentiment fetch from API Gateway ──────────────────────
_sentiment_cache = {"ts": 0.0, "data": None}
_SENTIMENT_TTL = float(os.getenv("MARKET_SENTIMENT_TTL_SEC", "120"))


async def get_market_sentiment() -> dict:
    """Fetch live market sentiment from the API Gateway's /market/indices endpoint."""
    import time as _t
    now = _t.time()
    if _sentiment_cache["data"] is not None and (now - _sentiment_cache["ts"]) < _SENTIMENT_TTL:
        return _sentiment_cache["data"]
    for attempt in range(2):
        try:
            client = _get_http_client()
            if True:
                # Use the API Gateway's endpoint – it always returns a score
                resp = await client.get(f"{API_GATEWAY_URL}/market/indices?force_refresh=false")
                if resp.status_code == 200:
                    data = resp.json()
                    score = data.get("market_score", 50)
                    logger.info(f"Market sentiment fetched from API Gateway: {score}")
                    _sentiment_cache["ts"] = _t.time()
                    _sentiment_cache["data"] = {"market_score": score, "source": "api_gateway"}
                    return {"market_score": score, **data}
                else:
                    logger.warning(f"API Gateway returned {resp.status_code} (attempt {attempt+1})")
        except Exception as e:
            logger.warning("Market sentiment fetch attempt %s failed: %s: %s", attempt+1, type(e).__name__, e or "(empty)")
            if attempt == 0:
                await asyncio.sleep(0.5)
    # Fallback
    logger.warning("All market sentiment fetches failed, using neutral 50")
    neutral = {"market_score": 50, "classification": "NEUTRAL", "trend": "Neutral"}
    _sentiment_cache["ts"] = _t.time()
    _sentiment_cache["data"] = neutral
    return neutral


# ── Training Intelligence fetch ──────────────────────────────────────
async def get_training_score(symbol: str) -> dict:
    try:
        client = _get_http_client()
        resp = await client.get(f"{TRAINING_SERVICE_URL}/training-score/{symbol}", timeout=15.0)
        if resp.status_code == 200:
            data = resp.json()
            return data
        else:
            logger.warning(f"Training score for {symbol} returned {resp.status_code}")
    except Exception as e:
        logger.warning("Could not fetch training score for %s: %s: %s", symbol, type(e).__name__, e or "(empty)")
    # Cold-start resilience: neutral training signal, no invented edge
    return {
        "symbol": symbol,
        "training_score": 50,
        "live_win_rate": None,
        "win_rate": None,
        "t1_success_probability": None,
        "t5_success_probability": None,
        "historical_similarity": None,
        "similar_setups": [],
        "cold_start": True,
        "note": "Insufficient evaluated history — thresholds stay at baseline until live outcomes accumulate.",
    }


# ── Event signal extraction ────────────────────────────────────
def _extract_event_signals(events: dict | None) -> dict:
    if not events or not isinstance(events, dict):
        return {
            "event_score_delta": 0, "event_risk": False, "event_reasons": [],
            "earnings_days_out": None, "event_score": 50.0,
        }

    delta = 0
    reasons = []
    event_risk = False
    earnings_days_out = None
    now = datetime.utcnow()

    # Earnings proximity
    next_earnings = events.get("next_earnings_date")
    if next_earnings:
        try:
            earnings_dt = datetime.fromisoformat(str(next_earnings)[:10])
            days_out = (earnings_dt - now).days
            earnings_days_out = days_out
            if 0 <= days_out <= EARNINGS_RISK_DAYS:
                event_risk = True
                reasons.append(f"⚠ Earnings in {days_out}d ({next_earnings[:10]}) — hold off, high volatility risk")
                delta -= 5
            elif 0 < days_out <= EARNINGS_BOOST_DAYS:
                delta += 8
                reasons.append(f"📈 Earnings in {days_out}d — pre-results momentum window")
            elif days_out < 0 and days_out >= -30:
                reasons.append(f"📊 Recent earnings ({abs(days_out)}d ago)")
        except (ValueError, TypeError):
            pass

    # Earnings surprise
    earnings_surprise = events.get("earnings_surprise")
    if earnings_surprise and isinstance(earnings_surprise, dict):
        surprise_pct = earnings_surprise.get("surprise_pct")
        if surprise_pct is not None:
            if surprise_pct > 5:
                delta += 10
                reasons.append(f"📈 Earnings surprise: +{surprise_pct:.1f}% beat — short-term momentum fuel")
            elif surprise_pct > 0:
                delta += 5
                reasons.append(f"📈 Mild earnings beat: +{surprise_pct:.1f}%")
            elif surprise_pct < -5:
                delta -= 8
                reasons.append(f"📉 Earnings surprise: {surprise_pct:.1f}% miss")

    # Analyst upgrades/downgrades
    analyst_actions = events.get("recent_analyst_actions") or []
    for action in analyst_actions[:2]:
        act = str(action.get("action", "")).lower()
        grade = str(action.get("to_grade", "")).lower()
        firm = action.get("firm", "")
        if act in ("upgrade", "upgraded") or grade in ("buy", "strong buy", "outperform", "overweight"):
            delta += 6
            reasons.append(f"📈 Analyst upgrade: {firm} → {grade}")
            break
        elif act in ("downgrade", "downgraded") or grade in ("sell", "underperform", "underweight"):
            delta -= 6
            reasons.append(f"📉 Analyst downgrade: {firm} → {grade}")
            break

    # Insider transactions
    insider_txns = events.get("recent_insider_transactions") or []
    for txn in insider_txns[:2]:
        txn_type = str(txn.get("transaction", "")).lower()
        shares = txn.get("shares") or 0
        if "buy" in txn_type or "purchase" in txn_type:
            if shares and shares > 1000:
                delta += 5
                reasons.append(f"🏦 Insider buying: {txn.get('insider', 'insider')} bought {shares:,} shares")
                break
        elif "sell" in txn_type and "sale" in txn_type:
            delta -= 3
            reasons.append(f"🏦 Insider selling: {txn.get('insider', 'insider')} sold shares")
            break

    # Bulk/Block deals — strong short-term signal (Manorama-type moves)
    bulk_deals = events.get("bulk_deals") or []
    if bulk_deals:
        buyish = False
        for d in bulk_deals[:5]:
            side = str(d.get("buy_sell") or d.get("side") or d.get("transaction") or "").lower()
            if "buy" in side or side in ("b", "purchase"):
                buyish = True
                break
        if buyish:
            delta += 10
            reasons.append("📦 Bulk/Block BUY detected — short-term demand spike risk/reward")
        else:
            delta += 6
            reasons.append("📦 Bulk/Block deal detected")

    # FII/DII net flow
    fii_flow = events.get("fii_dii_net_flow")
    if fii_flow and isinstance(fii_flow, dict):
        net = fii_flow.get("net")
        if net is not None:
            if net > 0:
                delta += 3
                reasons.append(f"📈 FII/DII net inflow positive")
            elif net < 0:
                delta -= 3
                reasons.append(f"📉 FII/DII net outflow negative")

    return {
        "event_score_delta": max(-18, min(18, delta)),
        "event_risk": event_risk,
        "event_reasons": reasons,
        "earnings_days_out": earnings_days_out,
        # Event scoring pipeline fix: the event/main.py service now computes
        # a proper nature-based 0-100 event_score (event_depth.compute_event_score)
        # — earnings beat vs miss, bonus/buyback vs dilutive rights issue,
        # insider buy vs sell, rating up/downgrade, bulk-deal direction,
        # regulatory action, all recency-decayed. Prefer that directly so
        # horizon scoring uses the same number the event box displays,
        # instead of a second, disconnected delta-only calculation. Fall
        # back to converting the local -18..+18 delta only for older cached
        # event payloads that predate the upstream event_score field.
        "event_score": (
            float(events.get("event_score"))
            if events.get("event_score") is not None
            else max(0.0, min(100.0, 50.0 + max(-18, min(18, delta)) * 2.5))
        ),
    }


# ── Market Sentiment Adjustment ──────────────────────────────
def _market_sentiment_adjustment(market_score: int) -> tuple:
    if market_score >= 70:
        return (8, f"📈 Very strong bullish market sentiment (+8)")
    elif market_score >= 60:
        bonus = int((market_score - 60) / 10 * 8)
        return (bonus, f"📈 Positive market sentiment (+{bonus})")
    elif market_score <= 30:
        return (-8, f"📉 Very strong bearish market sentiment (-8)")
    elif market_score <= 40:
        penalty = int((40 - market_score) / 10 * 8)
        return (-penalty, f"📉 Negative market sentiment (-{penalty})")
    else:
        return (0, f"➖ Neutral market sentiment (no adjustment)")


# ── Combined score (with market sentiment as a component) ──────────
def _combined_score(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    training_score: int,
    market_score: int,
    event_delta: int = 0,
    market_adjustment: int = 0,
) -> float:
    news = news_score if news_score is not None else 50
    pred = prediction_score if prediction_score is not None else 50

    # Primary combined score leans short-term: ~65% news+events path
    # (news pillar + event_delta), ~35% technical/fundamental/pred/train/market.
    weights = {
        "t": 0.12,
        "f": 0.08,
        "n": 0.45,
        "p": 0.08,
        "m": 0.05,
        "train": 0.07,
    }

    total = (
        technical_score * weights["t"] +
        fundamental_score * weights["f"] +
        news * weights["n"] +
        pred * weights["p"] +
        market_score * weights["m"] +
        training_score * weights["train"]
    )

    # event_delta already encodes bulk/insider/results — amplify for short-term
    total += (event_delta * 1.35) + market_adjustment
    return round(max(0, min(100, total)), 1)


# ── Decision logic ──────────────────────────────────────────
def _live_win_rate_threshold_shift(live_win_rate, live_n: int = 0) -> float:
    """Closed-loop: positive shift = harder BUY bars when live edge is weak.

    Full strength from ~25 evaluated samples; partial from 8+.
    """
    if live_win_rate is None:
        return 0.0
    try:
        wr = float(live_win_rate)
        n = int(live_n or 0)
    except (TypeError, ValueError):
        return 0.0
    if n < 8:
        return 0.0
    if wr > 1.5:
        wr = wr / 100.0
    wr = max(0.0, min(1.0, wr))
    # Baseline expected edge ~55%. Below → raise bars; above → ease slightly.
    delta = (0.55 - wr) * 20.0
    # Confidence scale: 8 samples → 40%, 25+ → 100%
    conf = min(1.0, max(0.4, (n - 8) / 17.0 + 0.4))
    delta *= conf
    return max(-6.0, min(8.0, delta))


def _decide(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    trend_strength: str,
    volume_surge: bool,
    dist_to_resistance_pct: float | None,
    event_risk: bool,
    already_owned: bool,
    combined: float,
    data_insufficient: bool = False,
    live_win_rate=None,
    live_win_rate_n: int = 0,
) -> Decision:
    if data_insufficient:
        if news_score is not None and news_score >= 60:
            return Decision.WAIT
        return Decision.DO_NOT_BUY

    if already_owned and combined < 35:
        return Decision.SELL
    if already_owned and 35 <= combined < 60:
        return Decision.HOLD

    # Soft safety signals (penalties / boosts) — NOT hard vetoes.
    news_penalty = 0
    if news_score is not None and news_score < 35:
        news_penalty = 8
    model_penalty = 0
    if prediction_score is not None and prediction_score < 45:
        model_penalty = 5
    resistance_penalty = 0
    if dist_to_resistance_pct is not None and dist_to_resistance_pct <= 1:
        resistance_penalty = 4

    adj = combined - news_penalty - model_penalty - resistance_penalty

    # Closed-loop: live win-rate shifts decision thresholds
    bar_shift = _live_win_rate_threshold_shift(live_win_rate, live_win_rate_n)
    buy_bar = 68.0 + bar_shift
    prepare_bar = 54.0 + bar_shift * 0.7

    if adj >= buy_bar and technical_score >= 50 and fundamental_score >= 40:
        return Decision.PREPARE_TO_BUY if event_risk else Decision.BUY_NOW
    if adj >= prepare_bar or (fundamental_score >= 55 and technical_score >= 50 and adj >= 50 + bar_shift * 0.5):
        return Decision.PREPARE_TO_BUY
    if adj >= 60 + bar_shift * 0.5 and technical_score >= 55:
        return Decision.PREPARE_TO_BUY

    if already_owned and combined >= 60:
        return Decision.HOLD

    return Decision.DO_NOT_BUY


def _is_long_term_hold_candidate(
    fundamental_score: int,
    technical_score: int,
    event_risk: bool,
    data_insufficient: bool,
) -> bool:
    """Separate from the short-term entry-timing decision above —a stock
    can be a strong long-term hold candidate even when short-term
    technicals don't currently justify BUY NOW/PREPARE TO BUY (price
    just temporarily weak on an otherwise sound company), and vice versa
    (a short-term breakout on a fundamentally mediocre company isn't a
    long-term hold candidate). Kept as an additive flag rather than a
    new Decision value so nothing that filters on
    decision in (BUY_NOW, PREPARE_TO_BUY) — training-service, scanner.py,
    the frontend's actionable-picks list — needs to change to recognize it.
    """
    if data_insufficient:
        return False
    return fundamental_score >= 70 and technical_score >= 40 and not event_risk


def _long_term_hold_estimate(fundamental_score: int) -> dict:
    """6-18 month horizon, loosely scaled by fundamental conviction —
    this is a soft heuristic label, not a model prediction, same
    honesty caveat as the short-term holding_period_estimate api-gateway
    computes for BUY NOW/PREPARE TO BUY."""
    min_months = 6
    max_months = 6 + round((fundamental_score - 70) / 30 * 12)  # up to +12 months at fundamental_score=100
    max_months = max(min_months + 3, min(max_months, 18))
    start = datetime.now(timezone.utc).date()
    end_min = start + timedelta(days=min_months * 30)
    end_max = start + timedelta(days=max_months * 30)
    return {
        "min_months": min_months,
        "max_months": max_months,
        "expected_by_earliest": end_min.isoformat(),
        "expected_by_latest": end_max.isoformat(),
        "label": f"{min_months}-{max_months} months (review by {end_min.strftime('%b %Y')}\u2013{end_max.strftime('%b %Y')})",
    }


# ── Record prediction to Training Service ────────────────────────────
async def record_prediction_for_training(
    symbol: str,
    decision: str,
    confidence: float,
    price: float,
    entry_range: dict,
    target: float,
    stop_loss: float,
    market_sentiment: dict,
    features: dict,
    event_data: dict | None = None,
    fundamental_metrics: dict | None = None,
):
    move_pct = abs(target - price) / price * 100 if target and price else None
    if move_pct is not None:
        min_weeks = max(2, round(move_pct / 5))
        max_weeks = max(min_weeks + 1, round(move_pct / 2.5))
        holding_period_str = f"{min(min_weeks, 8)}-{min(max_weeks, 12)} weeks"
    else:
        holding_period_str = "N/A"

    payload = {
        "symbol": symbol,
        "decision": decision,
        "confidence": "High" if confidence >= 75 else "Medium" if confidence >= 55 else "Low",
        "price": price,
        "combined_score": confidence,
        "technical_score": features.get("technical", 50),
        "fundamental_score": features.get("fundamental", 50),
        "news_score": features.get("news"),
        "prediction_score": features.get("prediction"),
        "market_score": features.get("market", 50),
        "market_sentiment_adjustment": features.get("market_adjustment", 0),
        "training_score": features.get("training", 50),
        "event_risk": features.get("event_risk", False),
        "entry_range_low": entry_range.get("low") if entry_range else None,
        "entry_range_high": entry_range.get("high") if entry_range else None,
        "target": target,
        "stop_loss": stop_loss,
        "holding_period": holding_period_str,
        "support": features.get("support"),
        "resistance": features.get("resistance"),
        "sector": None,
        "valuation": "fair",
        "market_mood": market_sentiment.get("classification", "NEUTRAL"),
        "nifty_change_pct": market_sentiment.get("nifty_change_pct"),
        "sensex_change_pct": market_sentiment.get("sensex_change_pct"),
        "rsi": features.get("rsi"),
        "macd": features.get("macd"),
        "ema": features.get("ema"),
        "volume_ratio": features.get("volume_ratio"),
        "debt_to_equity": fundamental_metrics.get("debt_to_equity") if fundamental_metrics else None,
        "roe": fundamental_metrics.get("roe") if fundamental_metrics else None,
        "roce": fundamental_metrics.get("roce") if fundamental_metrics else None,
        "feature_snapshot": features,
    }

    try:
        client = _get_http_client()
        if True:
            response = await client.post(
                f"{TRAINING_SERVICE_URL}/api/predictions",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            if response.status_code in (200, 201):
                logger.info(f"Prediction recorded for {symbol}: {response.json().get('prediction_id')}")
            else:
                logger.warning(f"Failed to record prediction: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Error recording prediction for {symbol}: {e}")



def _assess_data_quality(
    technical: dict,
    fundamental: dict,
    news,
    events,
    prediction,
    training,
    data_insufficient: bool,
) -> dict:
    """Score how many decision pillars are live vs fallback/missing (free-tier honesty)."""
    training = training if isinstance(training, dict) else {}
    fundamental = fundamental if isinstance(fundamental, dict) else {}
    technical = technical if isinstance(technical, dict) else {}
    live_n = int(training.get("live_win_rate_n") or training.get("evaluated_n") or 0)
    provisional_flag = bool(training.get("live_win_rate_provisional") or training.get("provisional"))
    # Closed-loop incomplete until enough T+1/T+5 outcomes
    sparse_loop = live_n < 8
    fallback_fund = bool(fundamental.get("fallback_used"))
    pillars = {
        "price": bool(technical and technical.get("close") is not None),
        "technical": bool(
            technical
            and not technical.get("data_insufficient")
            and technical.get("technical_score") is not None
        ),
        "fundamental": bool(fundamental and not fallback_fund and fundamental.get("fundamental_score") is not None),
        "news": bool(news and isinstance(news, dict) and news.get("news_score") is not None),
        "events": bool(events and isinstance(events, dict)),
        "prediction": bool(
            prediction and isinstance(prediction, dict) and prediction.get("model_loaded")
        ),
        "training": bool(training and training.get("training_score") is not None),
    }
    live = sum(1 for v in pillars.values() if v)
    total = len(pillars)
    core_ok = pillars["price"] and pillars["technical"]
    actionable_ok = core_ok and live >= 3 and not data_insufficient and not sparse_loop and not fallback_fund
    quality = "high" if live >= 5 and core_ok and not sparse_loop else "medium" if live >= 3 and core_ok else "low"
    # Provisional = not enough evaluated outcomes OR thin pillars OR fund fallback
    provisional = bool(
        provisional_flag
        or sparse_loop
        or quality == "low"
        or data_insufficient
        or (fallback_fund and live < 5)
    )
    return {
        "pillars": pillars,
        "live_count": live,
        "total_pillars": total,
        "quality": quality,
        "actionable_ok": actionable_ok,
        "core_ok": core_ok,
        "provisional": provisional,
        "live_win_rate_n": live_n,
        "fallback_fundamental": fallback_fund,
        "sparse_closed_loop": sparse_loop,
        "block_buy_now": provisional,
    }


def _apply_data_quality_gate(
    decision: Decision,
    quality: dict,
    already_owned: bool,
    technical: dict = None,
) -> Decision:
    """
    Free-tier honesty gate with momentum override for sniper setups:
    - Provisional / low-n / fallback / thin data → normally never emit BUY NOW
    - Exception: strong technical (score >= 65) + volume_surge may keep BUY NOW
      even on provisional data so exceptional setups are not starved
    - Otherwise BUY NOW demoted to WAIT / PREPARE / DO_NOT_BUY per quality
    """
    if already_owned or decision not in (Decision.BUY_NOW, Decision.PREPARE_TO_BUY):
        return decision

    provisional = bool(quality.get("provisional") or quality.get("block_buy_now"))
    live_n = int(quality.get("live_win_rate_n") or 0)
    core_ok = bool(quality.get("core_ok"))
    live_count = int(quality.get("live_count") or 0)
    quality_label = quality.get("quality") or "low"

    tech_dict = technical if isinstance(technical, dict) else {}
    tech_score = int(tech_dict.get("technical_score") or 50)
    vol_surge = bool(tech_dict.get("volume_surge", False))

    # Provisional block with momentum override for exceptional technical setups
    if decision == Decision.BUY_NOW and provisional:
        if core_ok and tech_score >= 65 and vol_surge:
            # Sniper exception: strong technical + volume surge may pass
            return Decision.BUY_NOW
        if core_ok and live_count >= 3 and quality_label != "low":
            # Data looks OK but closed-loop still sparse → allow PREPARE, not BUY NOW
            return Decision.PREPARE_TO_BUY
        return Decision.WAIT

    if decision == Decision.BUY_NOW and not quality.get("actionable_ok"):
        if core_ok and (live_count >= 2 or tech_score >= 60):
            return Decision.WAIT
        return Decision.DO_NOT_BUY

    if decision == Decision.PREPARE_TO_BUY and quality_label == "low" and not core_ok:
        return Decision.DO_NOT_BUY

    return decision



async def _fallback_technical_from_market_data(symbol: str) -> dict:
    """When technical microservice is cold, build minimal technicals from market-data quote/history."""
    out = {
        "technical_score": 50,
        "trend_strength": "unknown",
        "volume_surge": False,
        "close": None,
        "support": None,
        "resistance": None,
        "reasons": ["Technical built from market-data fallback"],
        "data_insufficient": False,
    }
    try:
        client = _get_http_client()
        if True:
            try:
                await client.get(f"{MARKET_DATA_URL}/health", params={"warm": "true"})
            except Exception:
                pass
            close = None
            try:
                qr = await client.get(f"{MARKET_DATA_URL}/quote/{symbol}")
                if qr.status_code == 200:
                    close = (qr.json() or {}).get("price")
            except Exception:
                pass
            candles = []
            try:
                hr = await client.get(f"{MARKET_DATA_URL}/history/{symbol}", params={"period": "6mo", "interval": "1d"})
                if hr.status_code == 200:
                    candles = (hr.json() or {}).get("candles") or []
            except Exception:
                pass
            closes = [c.get("close") for c in candles if c.get("close") is not None]
            highs = [c.get("high") for c in candles if c.get("high") is not None]
            lows = [c.get("low") for c in candles if c.get("low") is not None]
            if close is None and closes:
                close = closes[-1]
            out["close"] = float(close) if close is not None else None
            if lows:
                out["support"] = round(float(min(lows[-20:])), 2)
            elif close:
                out["support"] = round(float(close) * 0.97, 2)
            if highs:
                out["resistance"] = round(float(max(highs[-20:])), 2)
            elif close:
                out["resistance"] = round(float(close) * 1.03, 2)
            # Simple trend score from last ~20 vs prior
            if len(closes) >= 25:
                recent = sum(closes[-10:]) / 10
                prior = sum(closes[-25:-15]) / 10
                chg = (recent / prior - 1) * 100 if prior else 0
                score = max(20, min(80, int(50 + chg * 3)))
                out["technical_score"] = score
                out["trend_strength"] = "strong" if chg > 3 else "weak" if chg < -3 else "neutral"
                out["reasons"] = [f"Fallback technical from market history ({len(closes)} bars); trend {out['trend_strength']}"]
            elif out["close"]:
                out["reasons"] = [f"Fallback quote ₹{out['close']}; full technicals retry recommended"]
                out["data_insufficient"] = len(closes) < 5
            else:
                out["data_insufficient"] = True
                out["reasons"] = ["Market-data fallback could not obtain price"]
    except Exception as e:
        out["reasons"] = [f"Market-data fallback failed: {e}"]
        out["data_insufficient"] = True
    return out


# ── Short-circuit helpers (prefer gateway / Neon payload over HTTP) ─────────
def _derive_technical_from_payload(payload: dict) -> dict:
    """Build a technical pillar dict from prefetched fields (RSI, scores, price)."""
    rsi = _safe_float(payload.get("rsi"), 52.0)
    close = _safe_float(payload.get("close") or payload.get("price") or payload.get("ltp") or payload.get("cmp"))
    stored = payload.get("technical_score")
    if stored is not None:
        tech_score = _safe_int(stored, 50)
    else:
        # Lightweight RSI-based score (mirrors instant_scanner heuristics)
        if 45 <= rsi <= 65:
            tech_score = 62
        elif 35 <= rsi < 45 or 65 < rsi <= 72:
            tech_score = 55
        elif rsi < 30:
            tech_score = 68  # oversold bounce potential
        elif rsi > 75:
            tech_score = 38  # overbought
        else:
            tech_score = 50
    return {
        "technical_score": tech_score,
        "trend_strength": payload.get("trend_strength") or "neutral",
        "volume_surge": bool(payload.get("volume_surge", False)),
        "close": close,
        "support": _safe_float(payload.get("support")),
        "resistance": _safe_float(payload.get("resistance")),
        "rsi": rsi,
        "macd": payload.get("macd") or payload.get("macd_hist"),
        "ema20": payload.get("ema20") or payload.get("ema"),
        "reasons": ["Short-circuit: technical derived from gateway/Neon payload (no HTTP)"],
        "from_payload": True,
        "data_insufficient": close is None,
    }


def _derive_fundamental_from_payload(payload: dict) -> dict:
    """Build a fundamental pillar dict from PE / stored score / metrics."""
    pe = _safe_float(payload.get("pe_ratio") or payload.get("pe"), 22.0)
    stored = payload.get("fundamental_score")
    if stored is not None:
        fund_score = _safe_int(stored, 50)
    else:
        if 8 <= pe <= 28:
            fund_score = 62
        elif 28 < pe <= 40:
            fund_score = 48
        elif 0 < pe < 8:
            fund_score = 55
        elif pe > 50:
            fund_score = 35
        else:
            fund_score = 50
    metrics = payload.get("metrics") or payload.get("fundamental_metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "fundamental_score": fund_score,
        "valuation": payload.get("valuation") or "fair",
        "sector": payload.get("sector"),
        "industry": payload.get("industry"),
        "quality_score": payload.get("quality_score"),
        "metrics": metrics,
        "reasons": ["Short-circuit: fundamental derived from gateway/Neon payload (no HTTP)"],
        "from_payload": True,
        "fallback_used": False,
    }


def _has_usable_prefetched(payload: dict | None) -> bool:
    """True when payload has enough fields to skip at least one expensive HTTP call."""
    if not isinstance(payload, dict) or not payload:
        return False
    keys = (
        "technical_score", "fundamental_score", "news_score",
        "rsi", "pe_ratio", "pe", "sentiment_score", "market_score",
        "close", "price", "ltp",
    )
    return any(payload.get(k) is not None for k in keys)


# ── Main route ────────────────────────────────────────────────────
@app.get("/decide/{symbol}")
async def decide(
    symbol: str,
    already_owned: bool = False,
    background_tasks: BackgroundTasks = None,
    force: bool = False,
    # Optional short-circuit query params (gateway can pass Neon values)
    rsi: float | None = None,
    pe_ratio: float | None = None,
    technical_score: int | None = None,
    fundamental_score: int | None = None,
    news_score: int | None = None,
    close: float | None = None,
    skip_http: bool = False,
):
    # Build prefetched bag from query params when present
    prefetched = None
    if any(v is not None for v in (rsi, pe_ratio, technical_score, fundamental_score, news_score, close)) or skip_http:
        prefetched = {
            k: v for k, v in {
                "rsi": rsi,
                "pe_ratio": pe_ratio,
                "technical_score": technical_score,
                "fundamental_score": fundamental_score,
                "news_score": news_score,
                "close": close,
            }.items() if v is not None
        }
        if skip_http:
            prefetched["skip_http"] = True
    return await _decide_impl(symbol, already_owned=already_owned, background_tasks=background_tasks, force=force, prefetched=prefetched)


@app.post("/decide/evaluate")
async def decide_evaluate(request: Request, background_tasks: BackgroundTasks = None):
    """
    Short-circuit evaluate: prefer payload data from API Gateway / Neon cache.
    Eliminates most internal HTTP calls to analysis-intelligence-service during scans.
    Body example:
      {
        "symbol": "RELIANCE",
        "rsi": 54.2,
        "pe_ratio": 24.1,
        "technical_score": 61,
        "fundamental_score": 58,
        "news_score": 55,
        "sentiment_score": 52,
        "close": 2450.5,
        "already_owned": false,
        "force": false
      }
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body required")
    symbol = (body.get("symbol") or "").strip().upper().replace(".NS", "").replace(".BO", "")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    already_owned = bool(body.get("already_owned", False))
    force = bool(body.get("force", False))
    return await _decide_impl(symbol, already_owned=already_owned, background_tasks=background_tasks, force=force, prefetched=body)


async def _decide_impl(
    symbol: str,
    already_owned: bool = False,
    background_tasks: BackgroundTasks = None,
    force: bool = False,
    prefetched: dict | None = None,
):
    # Speed: serve from decide cache unless force=true
    if not force:
        cached = _cache_get_decide(symbol)
        if cached and isinstance(cached, dict) and cached.get("decision") and not _is_weak_decide_payload(cached):
            cached = dict(cached)
            cached["from_cache"] = True
            return cached
    try:
        client = _get_http_client()
        use_short = _has_usable_prefetched(prefetched)
        skip_all_http = bool(prefetched and prefetched.get("skip_http"))

        # Decide which pillars we can satisfy from payload vs must fetch
        need_technical = not (use_short and (
            prefetched.get("technical_score") is not None or prefetched.get("rsi") is not None
        ))
        need_fundamental = not (use_short and (
            prefetched.get("fundamental_score") is not None
            or prefetched.get("pe_ratio") is not None
            or prefetched.get("pe") is not None
        ))
        need_news = not (use_short and prefetched.get("news_score") is not None)
        # Events + prediction are cheaper to keep live (or still fetch) unless skip_http
        need_events = not skip_all_http
        need_prediction = not skip_all_http
        need_sentiment = not (use_short and (
            prefetched.get("sentiment_score") is not None or prefetched.get("market_score") is not None
        ))
        need_training = not skip_all_http

        if skip_all_http:
            need_technical = need_fundamental = need_news = False
            need_events = need_prediction = need_sentiment = need_training = False

        tasks = {}
        # Propagate force so sniper / force=True bypasses downstream analysis caches
        force_query = f"?force={str(force).lower()}"

        if need_technical:
            tasks["technical"] = asyncio.create_task(
                _fetch_optional(client, f"{TECHNICAL_URL}/analyze/{symbol}{force_query}", "Technical")
            )
        if need_fundamental:
            tasks["fundamental"] = asyncio.create_task(
                _fetch_optional(client, f"{FUNDAMENTAL_URL}/analyze/{symbol}{force_query}", "Fundamental")
            )
        if need_news:
            tasks["news"] = asyncio.create_task(
                _fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}{force_query}", "News")
            )
        if need_events:
            tasks["events"] = asyncio.create_task(
                _fetch_optional(client, f"{EVENT_URL}/events/{symbol}{force_query}", "Events")
            )
        if need_prediction:
            # Prediction path currently has no force cache layer; keep URL clean
            tasks["prediction"] = asyncio.create_task(
                _fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction")
            )
        if need_sentiment:
            tasks["sentiment"] = asyncio.create_task(get_market_sentiment())
        if need_training:
            tasks["training"] = asyncio.create_task(get_training_score(symbol))

        if tasks:
            keys = list(tasks.keys())
            results = await asyncio.gather(*(tasks[k] for k in keys))
            fetched = dict(zip(keys, results))
        else:
            fetched = {}

        # Fill pillars from payload first, then from HTTP results
        if not need_technical:
            technical = _derive_technical_from_payload(prefetched or {})
            logger.info("%s technical SHORT-CIRCUIT (from payload)", symbol)
        else:
            technical = fetched.get("technical")

        if not need_fundamental:
            fundamental = _derive_fundamental_from_payload(prefetched or {})
            logger.info("%s fundamental SHORT-CIRCUIT (from payload)", symbol)
        else:
            fundamental = fetched.get("fundamental")

        if not need_news:
            ns = prefetched.get("news_score") if prefetched else None
            news = {"news_score": _safe_int(ns, 50), "from_payload": True, "reasons": ["Short-circuit: news from payload"]} if ns is not None else None
            logger.info("%s news SHORT-CIRCUIT (from payload)", symbol)
        else:
            news = fetched.get("news")

        events = fetched.get("events") if need_events else (prefetched.get("events") or prefetched.get("event_data") if prefetched else None) or {}
        prediction = fetched.get("prediction") if need_prediction else None

        if not need_sentiment:
            ms = prefetched.get("market_score") or prefetched.get("sentiment_score") if prefetched else None
            sentiment = {"market_score": _safe_int(ms, 50), "classification": "NEUTRAL", "from_payload": True}
            logger.info("%s sentiment SHORT-CIRCUIT (from payload)", symbol)
        else:
            sentiment = fetched.get("sentiment")

        training = fetched.get("training") if need_training else {"training_score": 50, "from_payload": True}

        data_insufficient = False

        if not technical or not isinstance(technical, dict):
            technical = await _fallback_technical_from_market_data(symbol)
            if not technical.get("close"):
                technical = {
                    "technical_score": 50,
                    "trend_strength": "unknown",
                    "volume_surge": False,
                    "close": None,
                    "support": None,
                    "resistance": None,
                    "reasons": ["Technical service temporarily unavailable — market-data fallback also empty"],
                    "data_insufficient": True,
                }
        elif technical.get("close") is None:
            fb = await _fallback_technical_from_market_data(symbol)
            if fb.get("close") is not None:
                technical = {**technical, **{k: fb[k] for k in ("close", "support", "resistance") if fb.get(k) is not None}}
                if technical.get("reasons") == ["Technical service temporarily unavailable"] or not technical.get("reasons"):
                    technical["reasons"] = fb.get("reasons") or technical.get("reasons")
                if fb.get("technical_score") and technical.get("technical_score", 50) == 50:
                    technical["technical_score"] = fb["technical_score"]
                    technical["trend_strength"] = fb.get("trend_strength", technical.get("trend_strength"))
        if technical.get("close") is None:
            data_insufficient = True

        if not fundamental or not isinstance(fundamental, dict):
            fundamental = {
                "fundamental_score": 50,
                "valuation": "fair",
                "sector": None,
                "reasons": ["Live data temporarily unavailable — score is based on last known or default values"],
                "metrics": {},
                "fallback_used": True
            }

        technical_score = _safe_int(technical.get("technical_score"), 50)
        fundamental_score = _safe_int(fundamental.get("fundamental_score"), 50)

        news_score = None
        if news and isinstance(news, dict) and news.get("news_score") is not None:
            news_score = _safe_int(news.get("news_score"), None)

        prediction_score = None
        if prediction and isinstance(prediction, dict) and prediction.get("model_loaded"):
            if prediction.get("prediction_score") is not None:
                prediction_score = _safe_int(prediction.get("prediction_score"), None)

        if technical.get("data_insufficient"):
            data_insufficient = True

        sentiment = sentiment if isinstance(sentiment, dict) else {"market_score": 50}
        training = training if isinstance(training, dict) else {"training_score": 50}
        market_score = _safe_int(sentiment.get("market_score"), 50)
        training_score = _safe_int(training.get("training_score"), 50)

        logger.info(f"Market sentiment for {symbol}: {market_score}")

        market_adjustment, market_adjustment_reason = _market_sentiment_adjustment(market_score)

        # ── Multi-horizon scoring (Short / Mid / Long) — short is primary ──
        # event_signals moved up so its nature-based event_score can feed
        # multi_horizon_decide directly (see extras["event_score"] below) —
        # previously computed after mh, so horizons.py never saw it and
        # events only ever nudged the legacy combined_score post-hoc.
        event_signals = _extract_event_signals(events)
        event_delta = event_signals["event_score_delta"]
        event_risk = event_signals["event_risk"]
        event_reasons = event_signals["event_reasons"]

        extras = {
            "rs_vs_nifty": technical.get("rs_score") or technical.get("rs_vs_nifty"),
            "delivery_pct": technical.get("delivery_pct"),
            "quality_score": fundamental.get("quality_score") or fundamental.get("fundamental_score"),
            "peer_relative_score": fundamental.get("peer_relative_score"),
            "event_score": event_signals.get("event_score"),
        }
        flags = {
            "already_owned": already_owned,
            "event_risk": bool((events or {}).get("event_risk") or (events or {}).get("next_earnings_date")),
            "extended": bool(technical.get("extended") or technical.get("overextended")),
            "thin_history": bool(technical.get("data_insufficient") or technical.get("thin_history")),
            "low_liquidity": bool(technical.get("low_liquidity")),
            "live_win_rate": (training or {}).get("live_win_rate") or (training or {}).get("win_rate"),
            "live_win_rate_n": int((training or {}).get("live_win_rate_n") or 0),
            "news_as_of": (news or {}).get("as_of") or (news or {}).get("fetched_at") or (news or {}).get("updated_at"),
            "sentiment_as_of": ((sentiment or {}).get("as_of") or (sentiment or {}).get("fetched_at")) if isinstance(sentiment, dict) else None,
            "as_of": datetime.utcnow().isoformat() + "Z",
        }
        mh = multi_horizon_decide(
            technical=technical if isinstance(technical, dict) else {},
            fundamental=fundamental if isinstance(fundamental, dict) else {},
            news=news if isinstance(news, dict) else None,
            events=events if isinstance(events, dict) else None,
            prediction=prediction if isinstance(prediction, dict) else None,
            market={"market_score": market_score, "classification": sentiment.get("classification"), "trend": sentiment.get("trend")},
            extras=extras,
            flags=flags,
        )

        close = technical.get("close")
        support = technical.get("support")
        resistance = technical.get("resistance")
        trend_strength = technical.get("trend_strength", "unknown")
        volume_surge = bool(technical.get("volume_surge", False))
        dist_to_resistance_pct = None
        if close and resistance and resistance > 0:
            dist_to_resistance_pct = round(((resistance - close) / close) * 100, 2)

        combined = _combined_score(
            technical_score,
            fundamental_score,
            news_score,
            prediction_score,
            training_score,
            market_score,
            event_delta,
            market_adjustment,
        )

        live_wr = (training or {}).get("live_win_rate") or (training or {}).get("win_rate")
        live_n = int((training or {}).get("live_win_rate_n") or 0)
        # Prefer similarity-based success rate (0–100) when available
        if live_wr is None and (training or {}).get("t1_success_probability") is not None:
            live_wr = (training or {}).get("t1_success_probability")
        decision = _decide(
            technical_score,
            fundamental_score,
            news_score,
            prediction_score,
            trend_strength,
            volume_surge,
            dist_to_resistance_pct,
            event_risk,
            already_owned,
            combined,
            data_insufficient,
            live_win_rate=live_wr,
            live_win_rate_n=live_n,
        )

        data_quality = _assess_data_quality(
            technical if isinstance(technical, dict) else {},
            fundamental if isinstance(fundamental, dict) else {},
            news,
            events,
            prediction,
            training,
            data_insufficient,
        )
        gated = _apply_data_quality_gate(decision, data_quality, already_owned, technical=technical if isinstance(technical, dict) else None)
        if gated != decision:
            bits = [
                f"quality={data_quality.get('quality')}",
                f"pillars={data_quality.get('live_count')}/{data_quality.get('total_pillars')}",
                f"n={data_quality.get('live_win_rate_n')}",
            ]
            if data_quality.get("provisional"):
                bits.append("PROVISIONAL→block BUY NOW")
            if data_quality.get("sparse_closed_loop"):
                bits.append("closed-loop n<8")
            if data_quality.get("fallback_fundamental"):
                bits.append("fund fallback")
            reasons_gate = "Data quality gate: " + ", ".join(bits)
            decision = gated
        else:
            reasons_gate = None

        # Catalyst floor: bulk buy / strong results should not stay buried as DO NOT BUY.
        # Never promotes to BUY NOW; still respects provisional (PREPARE max).
        if (
            decision in (Decision.DO_NOT_BUY, Decision.HOLD, Decision.WAIT)
            and event_delta >= 8
            and combined >= 48
        ):
            decision = Decision.PREPARE_TO_BUY
            reasons_gate = (reasons_gate or "") + " | Catalyst floor: event_delta elevated → PREPARE TO BUY (not BUY NOW)"

        entry_low = entry_high = target = stop_loss = None
        if close:
            support_val = support if support else close * 0.95
            entry_low = round(support_val * 1.01, 2)
            entry_high = round(close * 1.005, 2)
            target_pct = 0.08
            if event_signals["earnings_days_out"] is not None:
                d = event_signals["earnings_days_out"]
                if 0 < d <= EARNINGS_BOOST_DAYS:
                    target_pct = 0.12
            if prediction_score is not None:
                target_pct = target_pct * 0.7 + (prediction_score / 100) * 0.05
            target = round(close * (1 + target_pct), 2)
            stop_loss = round(support_val * 0.98, 2)

        confidence = "High" if combined >= 75 else "Medium" if combined >= 55 else "Low"

        reasons: dict = {
            "technical": technical.get("reasons", []),
            "fundamental": fundamental.get("reasons", []),
        }
        if news and isinstance(news, dict):
            reasons["news"] = news.get("reasons", [])
        if prediction and isinstance(prediction, dict) and prediction.get("model_loaded"):
            reasons["prediction"] = [prediction.get("note", "AI prediction available")]
        if event_reasons:
            reasons["event"] = event_reasons
        reasons["market"] = [market_adjustment_reason]
        reasons["training"] = [f"Training intelligence score: {training_score}/100"]

        long_term_hold = _is_long_term_hold_candidate(
            fundamental_score, technical_score, event_risk, data_insufficient
        )

        # Was a hardcoded "2-6 weeks" regardless of the actual setup —
        # scales the estimate to how far the target actually is from
        # entry instead, same reasoning api-gateway's
        # holding_period_estimate already uses for the calendar-date
        # version of this.
        if decision in (Decision.BUY_NOW, Decision.PREPARE_TO_BUY) and target and close:
            move_pct = abs(target - close) / close * 100
            min_weeks = max(2, round(move_pct / 5))
            max_weeks = max(min_weeks + 1, round(move_pct / 2.5))
            holding_period = f"{min(min_weeks, 8)}-{min(max_weeks, 12)} weeks"
        else:
            holding_period = "N/A"

        response = {
            "symbol": symbol.upper(),
            "decision": decision.value,
            "confidence": confidence,
            "combined_score": combined,
            "technical_score": technical_score,
            "fundamental_score": fundamental_score,
            "news_score": news_score,
            "prediction_score": prediction_score,
            "market_score": market_score,   # <-- live market sentiment
            "market_sentiment_adjustment": market_adjustment,
            "training_score": training_score,
            "event_score_delta": event_delta,
            "event_score": event_signals.get("event_score"),
            "event_score_breakdown": (events or {}).get("event_score_breakdown"),
            "event_risk": event_risk,
            "entry_range": {"low": entry_low, "high": entry_high} if entry_low else None,
            "target": target,
            "stop_loss": stop_loss,
            "holding_period": holding_period,
            "long_term_hold": long_term_hold,
            "long_term_hold_estimate": _long_term_hold_estimate(fundamental_score) if long_term_hold else None,
            "close": close,
            "support": support,
            "resistance": resistance,
            "reasons": reasons,
            "valuation": fundamental.get("valuation", "fair"),
            "sector": fundamental.get("sector"),
            "data_insufficient": data_insufficient,
            "fundamental_fallback": fundamental.get("fallback_used", False),
            "data_quality": data_quality,
            "provisional": bool(data_quality.get("provisional")),
            "block_buy_now": bool(data_quality.get("block_buy_now")),
        }


        if reasons_gate:
            response.setdefault("reasons", {})
            dq = list(response["reasons"].get("data_quality") or [])
            dq.append(reasons_gate)
            response["reasons"]["data_quality"] = dq

        if news and isinstance(news, dict):
            response["news_data"] = {
                "headline_count": news.get("headline_count", 0),
                "headlines": news.get("headlines", []),
                "reasons": news.get("reasons", []),
                "summary": news.get("summary") or news.get("news_summary"),
            }
        if events and isinstance(events, dict):
            response["event_data"] = events
        if fundamental.get("metrics"):
            response["fundamental_metrics"] = fundamental["metrics"]

        if decision in (Decision.BUY_NOW, Decision.PREPARE_TO_BUY) and close:
            # ema alignment as a descriptive label — technical-analysis-service
            # exposes ema20/ema50/ema200 as three separate numbers, not a
            # single "ema" value, so this derives the closest honest
            # equivalent rather than picking one of the three arbitrarily.
            ema20, ema50, ema200 = technical.get("ema20"), technical.get("ema50"), technical.get("ema200")
            ema_label = None
            if ema20 is not None and ema50 is not None and ema200 is not None:
                if ema20 > ema50 > ema200:
                    ema_label = "bullish alignment"
                elif ema20 < ema50 < ema200:
                    ema_label = "bearish alignment"
                else:
                    ema_label = "mixed"

            background_tasks.add_task(
                record_prediction_for_training,
                symbol=symbol.upper(),
                decision=decision.value,
                confidence=combined,
                price=close,
                entry_range={"low": entry_low, "high": entry_high},
                target=target,
                stop_loss=stop_loss,
                market_sentiment=sentiment,
                features={
                    "technical": technical_score,
                    "fundamental": fundamental_score,
                    "news": news_score,
                    "prediction": prediction_score,
                    "market": market_score,
                    "market_adjustment": market_adjustment,
                    "training": training_score,
                    "event_delta": event_delta,
                    "event_risk": event_risk,
                    "support": support,
                    "resistance": resistance,
                    "volume_surge": volume_surge,
                    "trend_strength": trend_strength,
                    # Previously never included — rsi/volume_ratio reached
                    # training-service as always-None, which silently
                    # disabled two of the classifier's ten features and
                    # left scanner.py's KNN similarity search comparing
                    # every historical setup on a permanently-missing axis.
                    "rsi": technical.get("rsi"),
                    "volume_ratio": technical.get("volume_ratio"),
                    # macd: technical-analysis-service doesn't compute or
                    # expose a raw MACD value at all (only uses it
                    # internally for scoring) — left None rather than
                    # fabricating a label with nothing real behind it.
                    "macd": None,
                    "ema": ema_label,
                },
                event_data=events,
                fundamental_metrics=fundamental.get("metrics")
            )

        # Attach multi-horizon blocks when available
        try:
            if "mh" in dir() or "mh" in locals():
                response["horizons"] = mh.get("horizons")
                response["final_verdict"] = mh.get("final_verdict")
                # Prefer short-term as primary decision (project focus)
                if mh.get("decision"):
                    response["decision"] = mh["decision"]
                    response["combined_score"] = mh.get("combined_score", response.get("combined_score"))
                    response["confidence"] = mh.get("confidence", response.get("confidence"))
                    response["holding_period"] = mh.get("holding_period", response.get("holding_period"))
                # Re-apply data-quality gate after multi-horizon may promote BUY
                try:
                    dq = response.get("data_quality") or data_quality
                    d_enum = Decision(response.get("decision", Decision.DO_NOT_BUY.value))
                    tech_for_gate = technical if isinstance(technical, dict) else None
                    if not tech_for_gate and isinstance(response.get("technical"), dict):
                        tech_for_gate = response.get("technical")
                    gated2 = _apply_data_quality_gate(d_enum, dq, already_owned, technical=tech_for_gate)
                    if gated2.value != response.get("decision"):
                        response["decision"] = gated2.value
                        response.setdefault("reasons", {})
                        response["reasons"]["data_quality"] = [
                            f"Data quality gate after horizons: {dq.get('quality')} "
                            f"({dq.get('live_count')}/{dq.get('total_pillars')} live)"
                        ]
                        if gated2 in (Decision.WAIT, Decision.DO_NOT_BUY):
                            response["confidence"] = "Low"
                except Exception as _gq:
                    logger.debug("post-horizon quality gate: %s", _gq)
        except Exception as _mh_err:
            logger.warning("multi-horizon attach failed: %s", _mh_err)

        # Cache successful decide payload for free-tier scan speed
        try:
            _cache_set_decide(symbol, response)
        except Exception as ce:
            logger.warning("decide cache set failed: %s", ce)
        if isinstance(response, dict):
            response["from_cache"] = False
        return response

    except Exception as e:
        logger.error(f"Decision failed for {symbol}: {e}", exc_info=True)
        err = str(e)[:200]
        close = support = resistance = None
        try:
            fb = await _fallback_technical_from_market_data(symbol)
            close = fb.get("close")
            support = fb.get("support")
            resistance = fb.get("resistance")
        except Exception:
            pass
        return {
            "symbol": symbol.upper(),
            "decision": Decision.DO_NOT_BUY.value,
            "confidence": "Low",
            "combined_score": 45,
            "technical_score": 50,
            "fundamental_score": 50,
            "news_score": None,
            "prediction_score": None,
            "market_score": 50,
            "market_sentiment_adjustment": 0,
            "training_score": 50,
            "event_score_delta": 0,
            "event_risk": False,
            "entry_range": None,
            "target": None,
            "stop_loss": None,
            "holding_period": "N/A",
            "close": close,
            "support": support,
            "resistance": resistance,
            "reasons": {
                "technical": [f"Decision engine error: {err}"],
                "fundamental": ["Partial response — retry Analyse"],
                "market": ["Market sentiment may be incomplete"],
            },
            "valuation": "fair",
            "sector": None,
            "data_insufficient": close is None,
            "fundamental_fallback": True,
            "error_detail": err,
        }

# ── Bulk decide (optional speed path for scan / GitHub Actions) ─────────────
@app.post("/decide/batch")
async def decide_batch(request: Request):
    """
    Analyse up to BATCH_MAX_SYMBOLS symbols with bounded concurrency.
    Uses the same /decide logic (and its cache). Does not change scoring.
    """
    body = await request.json()
    symbols = body.get("symbols") or []
    force = bool(body.get("force", False))
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols list required")
    if len(symbols) > BATCH_MAX_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Maximum {BATCH_MAX_SYMBOLS} symbols per batch")

    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def one(sym: str):
        async with sem:
            return await decide(sym, force=force)

    results = await asyncio.gather(*(one(s) for s in symbols), return_exceptions=True)
    out = []
    for sym, res in zip(symbols, results):
        if isinstance(res, Exception):
            out.append({"symbol": str(sym).upper(), "decision": "DO NOT BUY", "error": str(res), "combined_score": 0})
        else:
            out.append(res)
    return {"results": out, "count": len(out)}