"""
Decision Engine Service v0.7.4
Changes:
- Fetches market sentiment from API Gateway's /market/indices endpoint (fast and reliable)
- Always includes the live market_score in the response
- Added retry and logging
- Speed: in-process + Redis decide cache, bulk /decide/batch endpoint (free-tier friendly)
- Multi-horizon scoring (short/mid/long) via horizons.py
"""
import os
import json
import asyncio
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

# ---- Service URLs (env-driven; aligned with config/service_urls.py) ----
_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
_DP = os.getenv("DECISION_PREDICTION_URL", "https://decision-prediction-service.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI.rstrip('/')}/technical")
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
DECIDE_CACHE_TTL_OPEN = int(os.getenv("DECIDE_CACHE_TTL_OPEN", "300"))
DECIDE_CACHE_TTL_CLOSED = int(os.getenv("DECIDE_CACHE_TTL_CLOSED", "21600"))
BATCH_MAX_SYMBOLS = int(os.getenv("DECIDE_BATCH_MAX", "25"))
BATCH_CONCURRENCY = int(os.getenv("DECIDE_BATCH_CONCURRENCY", "8"))

_decide_mem_cache: dict = {}  # symbol -> (expires_ts, payload)
_redis = None
try:
    from upstash_redis import Redis
    _url = os.getenv("UPSTASH_REDIS_REST_URL")
    _tok = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if _url and _tok:
        _redis = Redis(url=_url, token=_tok)
        _redis.ping()
        logger.info("Decision-engine connected to Upstash Redis for decide cache")
except Exception as e:
    logger.warning("Decision-engine Redis cache unavailable: %s", e)

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
    return None

def _cache_set_decide(symbol: str, payload: dict):
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    ttl = _cache_ttl()
    _decide_mem_cache[sym] = (time_module_time() + ttl, payload)
    if _redis:
        try:
            _redis.setex(f"decide:{sym}", ttl, json.dumps(payload, default=str))
        except Exception:
            pass

def time_module_time():
    import time as _t
    return _t.time()

app = FastAPI(title="Stockky Decision Engine", version="0.7.4")
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
    return {"service": "Stockky Decision Engine", "version": "0.7.4", "status": "running",
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
    breaker = get_breaker(f"decision:{label.lower()}", failure_threshold=4, recovery_timeout=90)
    if not breaker.allow():
        logger.warning("%s circuit OPEN — skip (retry in %.0fs)", label, breaker.retry_after())
        return None
    try:
        resp = await client.get(url, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        breaker.record_success()
        return data
    except Exception as e:
        breaker.record_failure(str(e))
        logger.warning("%s unavailable: %s", label, e)
        return None


# ── Market Sentiment fetch from API Gateway ──────────────────────
async def get_market_sentiment() -> dict:
    """Fetch live market sentiment from the API Gateway's /market/indices endpoint."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Use the API Gateway's endpoint – it always returns a score
                resp = await client.get(f"{API_GATEWAY_URL}/market/indices?force_refresh=false")
                if resp.status_code == 200:
                    data = resp.json()
                    score = data.get("market_score", 50)
                    logger.info(f"Market sentiment fetched from API Gateway: {score}")
                    return {"market_score": score, **data}
                else:
                    logger.warning(f"API Gateway returned {resp.status_code} (attempt {attempt+1})")
        except Exception as e:
            logger.warning(f"Market sentiment fetch attempt {attempt+1} failed: {e}")
            if attempt == 0:
                await asyncio.sleep(0.5)
    # Fallback
    logger.warning("All market sentiment fetches failed, using neutral 50")
    return {"market_score": 50, "classification": "NEUTRAL", "trend": "Neutral"}


# ── Training Intelligence fetch ──────────────────────────────────────
async def get_training_score(symbol: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{TRAINING_SERVICE_URL}/training-score/{symbol}")
            if resp.status_code == 200:
                data = resp.json()
                return data
            else:
                logger.warning(f"Training score for {symbol} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not fetch training score for {symbol}: {e}")
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
        return {"event_score_delta": 0, "event_risk": False, "event_reasons": [], "earnings_days_out": None}

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
                delta += 6
                reasons.append(f"📈 Earnings surprise: +{surprise_pct:.1f}% beat")
            elif surprise_pct < -5:
                delta -= 6
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

    # Bulk/Block deals
    bulk_deals = events.get("bulk_deals") or []
    if bulk_deals:
        delta += 4
        reasons.append(f"📦 Bulk/Block deal detected")

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
        "event_score_delta": max(-15, min(15, delta)),
        "event_risk": event_risk,
        "event_reasons": reasons,
        "earnings_days_out": earnings_days_out,
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

    weights = {
        "t": 0.30,
        "f": 0.20,
        "n": 0.15,
        "p": 0.15,
        "m": 0.10,
        "train": 0.10,
    }

    total = (
        technical_score * weights["t"] +
        fundamental_score * weights["f"] +
        news * weights["n"] +
        pred * weights["p"] +
        market_score * weights["m"] +
        training_score * weights["train"]
    )

    total += event_delta + market_adjustment
    return round(max(0, min(100, total)), 1)


# ── Decision logic ──────────────────────────────────────────
def _live_win_rate_threshold_shift(live_win_rate) -> float:
    """Closed-loop: positive shift = harder BUY bars when live edge is weak."""
    if live_win_rate is None:
        return 0.0
    try:
        wr = float(live_win_rate)
    except (TypeError, ValueError):
        return 0.0
    if wr > 1.5:
        wr = wr / 100.0
    wr = max(0.0, min(1.0, wr))
    # Baseline expected edge ~55%. Below → raise bars; above → ease slightly.
    delta = (0.55 - wr) * 20.0
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
    bar_shift = _live_win_rate_threshold_shift(live_win_rate)
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
        async with httpx.AsyncClient(timeout=10.0) as client:
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
    pillars = {
        "price": bool(technical and technical.get("close") is not None),
        "technical": bool(
            technical
            and not technical.get("data_insufficient")
            and technical.get("technical_score") is not None
        ),
        "fundamental": bool(fundamental and not fundamental.get("fallback_used")),
        "news": bool(news and isinstance(news, dict) and news.get("news_score") is not None),
        "events": bool(events and isinstance(events, dict)),
        "prediction": bool(
            prediction and isinstance(prediction, dict) and prediction.get("model_loaded")
        ),
        "training": bool(
            training and isinstance(training, dict) and training.get("training_score") is not None
        ),
    }
    live = sum(1 for v in pillars.values() if v)
    total = len(pillars)
    core_ok = pillars["price"] and pillars["technical"]
    actionable_ok = core_ok and live >= 3 and not data_insufficient
    quality = "high" if live >= 5 and core_ok else "medium" if live >= 3 and core_ok else "low"
    return {
        "pillars": pillars,
        "live_count": live,
        "total_pillars": total,
        "quality": quality,
        "actionable_ok": actionable_ok,
        "core_ok": core_ok,
    }


def _apply_data_quality_gate(decision: Decision, quality: dict, already_owned: bool) -> Decision:
    """Force WAIT / DO NOT BUY when free-tier data is too thin for a confident buy."""
    if already_owned:
        return decision
    if decision not in (Decision.BUY_NOW, Decision.PREPARE_TO_BUY):
        return decision
    if quality.get("actionable_ok"):
        return decision
    if decision == Decision.BUY_NOW and quality.get("core_ok") and quality.get("live_count", 0) >= 2:
        return Decision.WAIT
    return Decision.DO_NOT_BUY


# ── Main route ────────────────────────────────────────────────────
@app.get("/decide/{symbol}")
async def decide(symbol: str, already_owned: bool = False, background_tasks: BackgroundTasks = None, force: bool = False):
    # Speed: serve from decide cache unless force=true
    if not force:
        cached = _cache_get_decide(symbol)
        if cached and isinstance(cached, dict) and cached.get("decision"):
            cached = dict(cached)
            cached["from_cache"] = True
            return cached
    try:
        async with httpx.AsyncClient(timeout=70) as client:
            technical_task = asyncio.create_task(_fetch_optional(client, f"{TECHNICAL_URL}/analyze/{symbol}", "Technical"))
            fundamental_task = asyncio.create_task(_fetch_optional(client, f"{FUNDAMENTAL_URL}/analyze/{symbol}", "Fundamental"))
            news_task = asyncio.create_task(_fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}", "News"))
            events_task = asyncio.create_task(_fetch_optional(client, f"{EVENT_URL}/events/{symbol}", "Events"))
            prediction_task = asyncio.create_task(_fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction"))
            sentiment_task = asyncio.create_task(get_market_sentiment())
            training_task = asyncio.create_task(get_training_score(symbol))

            technical, fundamental, news, events, prediction, sentiment, training = await asyncio.gather(
                technical_task, fundamental_task, news_task, events_task,
                prediction_task, sentiment_task, training_task
            )

        data_insufficient = False

        if not technical or not isinstance(technical, dict):
            technical = {
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": None,
                "support": None,
                "resistance": None,
                "reasons": ["Technical service temporarily unavailable"],
            }
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

        technical_score = int(technical.get("technical_score", 50))
        fundamental_score = int(fundamental.get("fundamental_score", 50))

        news_score = None
        if news and "news_score" in news:
            val = news["news_score"]
            if val is not None:
                try:
                    news_score = int(val)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid news_score for {symbol}: {val}")

        prediction_score = None
        if prediction and prediction.get("model_loaded"):
            val = prediction.get("prediction_score")
            if val is not None:
                try:
                    prediction_score = int(val)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid prediction_score for {symbol}: {val}")

        if technical.get("data_insufficient"):
            data_insufficient = True

        market_score = sentiment.get("market_score", 50)
        training_score = training.get("training_score", 50)

        logger.info(f"Market sentiment for {symbol}: {market_score}")

        market_adjustment, market_adjustment_reason = _market_sentiment_adjustment(market_score)

        # ── Multi-horizon scoring (Short / Mid / Long) — short is primary ──
        extras = {
            "rs_vs_nifty": technical.get("rs_score") or technical.get("rs_vs_nifty"),
            "delivery_pct": technical.get("delivery_pct"),
            "quality_score": fundamental.get("quality_score") or fundamental.get("fundamental_score"),
            "peer_relative_score": fundamental.get("peer_relative_score"),
        }
        flags = {
            "already_owned": already_owned,
            "event_risk": bool((events or {}).get("event_risk") or (events or {}).get("next_earnings_date")),
            "extended": bool(technical.get("extended") or technical.get("overextended")),
            "thin_history": bool(technical.get("data_insufficient") or technical.get("thin_history")),
            "low_liquidity": bool(technical.get("low_liquidity")),
            "live_win_rate": (training or {}).get("live_win_rate") or (training or {}).get("win_rate"),
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

        event_signals = _extract_event_signals(events)
        event_delta = event_signals["event_score_delta"]
        event_risk = event_signals["event_risk"]
        event_reasons = event_signals["event_reasons"]

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
        gated = _apply_data_quality_gate(decision, data_quality, already_owned)
        if gated != decision:
            reasons_gate = f"Data quality gate: {data_quality.get('quality')} ({data_quality.get('live_count')}/{data_quality.get('total_pillars')} pillars live)"
            decision = gated
        else:
            reasons_gate = None

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
                    gated2 = _apply_data_quality_gate(d_enum, dq, already_owned)
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
        return {
            "symbol": symbol.upper(),
            "decision": Decision.DO_NOT_BUY.value,
            "confidence": "Low",
            "combined_score": 0,
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
            "close": None,
            "support": None,
            "resistance": None,
            "reasons": {
                "technical": ["Error processing request"],
                "fundamental": ["Error processing request"],
                "market": ["Market sentiment unavailable"]
            },
            "valuation": "fair",
            "sector": None,
            "data_insufficient": True,
            "fundamental_fallback": True,
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