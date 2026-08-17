"""
Prediction Service — XGBoost probability model + LLM explanation.

Upgraded to support technical + fundamental + news features.
Walk-forward and calibration remain on the training side.
"""

import os
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pred_features import (
    FEATURE_COLUMNS,
    TECHNICAL_COLUMNS,
    build_full_feature_vector,
    latest_feature_vector,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
ANALYSIS_URL = os.getenv("ANALYSIS_INTELLIGENCE_URL", os.getenv("ANALYSIS_URL", "")).rstrip("/")
MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip() or None
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip() or None

app = FastAPI(title="Stockky Prediction Service", version="0.7.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_model = None
if os.path.exists(MODEL_PATH):
    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("Loaded trained model from %s", MODEL_PATH)
    except Exception as e:
        logger.error("Failed to load model: %s", e)
else:
    logger.warning("No trained model found — using fallback")


@app.get("/")
async def root():
    return {
        "service": "Stockky Prediction Service",
        "version": "0.7.0",
        "status": "running",
        "features": "technical + fundamental + news",
        "model_loaded": _model is not None,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "prediction-service", "model_loaded": _model is not None}


@app.get("/model/info")
def model_info():
    """Describe the loaded prediction model (features, path, type)."""
    from pred_features import FEATURE_COLUMNS, TECHNICAL_COLUMNS, FUNDAMENTAL_COLUMNS, NEWS_COLUMNS
    info = {
        "model_loaded": _model is not None,
        "model_path": MODEL_PATH,
        "feature_count": len(FEATURE_COLUMNS),
        "features": FEATURE_COLUMNS,
        "technical_features": TECHNICAL_COLUMNS,
        "fundamental_features": FUNDAMENTAL_COLUMNS,
        "news_features": NEWS_COLUMNS,
        "note": "Live /predict uses latest fund+news. Retrain with pred_train.py after compute_feature_frame fix.",
    }
    if _model is not None:
        info["model_type"] = type(_model).__name__
        for attr in ("n_features_in_", "classes_", "feature_importances_"):
            if hasattr(_model, attr):
                val = getattr(_model, attr)
                try:
                    import numpy as np
                    if hasattr(val, "tolist"):
                        val = val.tolist()
                    elif isinstance(val, (list, tuple)) and len(val) > 40:
                        val = list(val)[:40] + ["…"]
                except Exception:
                    val = str(val)[:200]
                info[attr] = val
    return info


def _fetch_history(symbol: str) -> pd.DataFrame:
    """Fetch OHLCV from market-data with retries for free-tier cold starts."""
    import time as _time

    url = f"{MARKET_DATA_URL}/history/{symbol}"
    last_err = None
    data = None
    for attempt in range(4):
        try:
            if attempt == 0:
                try:
                    httpx.get(f"{MARKET_DATA_URL}/health", params={"warm": "true"}, timeout=8)
                except Exception:
                    pass
            resp = httpx.get(url, params={"period": "1y"}, timeout=60)
            if resp.status_code in (502, 503, 504):
                last_err = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                wait = 1.5 * (2 ** attempt)
                logger.warning(
                    "market-data %s for %s (attempt %s/4), retry in %.1fs",
                    resp.status_code, symbol, attempt + 1, wait,
                )
                _time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.HTTPError as e:
            last_err = e
            wait = 1.5 * (2 ** attempt)
            logger.warning(
                "market-data error for %s (attempt %s/4): %s — retry in %.1fs",
                symbol, attempt + 1, str(e)[:120], wait,
            )
            _time.sleep(wait)
    if data is None:
        raise HTTPException(status_code=502, detail=f"Market data unreachable after retries: {last_err}")

    candles = data.get("candles", [])
    if len(candles) < 210:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only {len(candles)} trading days of history available; need at least 210 "
                "for a stable prediction (EMA200 needs that much runway)."
            ),
        )
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.rename(columns=str.title, inplace=True)
    return df


def _fetch_fundamentals(symbol: str) -> Dict[str, Any]:
    """Fetch latest available fundamentals from market-data (point-in-time for live = latest)."""
    try:
        url = f"{MARKET_DATA_URL}/fundamentals/{symbol}"
        resp = httpx.get(url, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            # Normalize common key names
            return {
                "pe_ratio": data.get("pe_ratio") or data.get("trailingPE") or data.get("pe"),
                "roe": data.get("roe") or data.get("returnOnEquity"),
                "debt_to_equity": data.get("debt_to_equity") or data.get("debtToEquity"),
                "revenue_growth_yoy": data.get("revenue_growth_yoy") or data.get("revenueGrowth"),
                "profit_growth_yoy": data.get("profit_growth_yoy") or data.get("earningsGrowth"),
                "promoter_holding": data.get("promoter_holding") or data.get("promoterHolding"),
            }
    except Exception as e:
        logger.warning("Fundamentals fetch failed for %s: %s", symbol, e)
    return {}


def _fetch_news_scores(symbol: str) -> Dict[str, Any]:
    """
    Fetch recent news/sentiment scores.
    Uses analysis-intelligence service if available, otherwise returns neutral defaults.
    For live inference we use the latest available scores (correct by definition).
    """
    scores: Dict[str, Any] = {
        "news_sentiment_7d": 0.0,
        "news_sentiment_14d": 0.0,
        "earnings_surprise_flag": 0.0,
        "days_to_next_earnings": 30.0,
        "recent_event_score": 0.0,
    }
    if not ANALYSIS_URL:
        return scores

    try:
        # News
        news_resp = httpx.get(f"{ANALYSIS_URL}/news/analyze/{symbol}", timeout=15)
        if news_resp.status_code == 200:
            news = news_resp.json()
            # Expect a score 0-100 or -1 to +1; normalize to roughly -1..+1 range
            raw = news.get("news_score") or news.get("sentiment_score") or 50
            try:
                raw_f = float(raw)
                if raw_f > 1.5:  # probably 0-100 scale
                    sent = (raw_f - 50.0) / 50.0
                else:
                    sent = raw_f
                scores["news_sentiment_7d"] = max(-1.0, min(1.0, sent))
                scores["news_sentiment_14d"] = scores["news_sentiment_7d"]
            except Exception:
                pass

        # Events
        event_resp = httpx.get(f"{ANALYSIS_URL}/event/events/{symbol}", timeout=15)
        if event_resp.status_code == 200:
            ev = event_resp.json()
            next_earn = ev.get("next_earnings_date")
            if next_earn:
                try:
                    dt = datetime.fromisoformat(str(next_earn).replace("Z", "+00:00"))
                    days = (dt.replace(tzinfo=None) - datetime.utcnow()).days
                    scores["days_to_next_earnings"] = float(max(0, min(90, days)))
                except Exception:
                    pass
            if ev.get("earnings_surprise") or ev.get("recent_positive_event"):
                scores["earnings_surprise_flag"] = 1.0
            # Simple event score
            score = 0.0
            if ev.get("bulk_deals"):
                score += 0.3
            if ev.get("insider_buy"):
                score += 0.4
            if scores["earnings_surprise_flag"]:
                score += 0.3
            scores["recent_event_score"] = min(1.0, score)
    except Exception as e:
        logger.warning("News/Event fetch failed for %s: %s", symbol, e)

    return scores


def _describe_feature(features: dict, key: str, fmt: str = "{:.1f}", default="n/a") -> str:
    val = features.get(key)
    return fmt.format(val) if val is not None else default


def _call_groq(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            timeout=20,
        )
        if resp.status_code == 200:
            note = resp.json()["choices"][0]["message"]["content"].strip()
            return note or None
        logger.warning("Groq returned %s: %s", resp.status_code, resp.text[:300])
    except Exception as e:
        logger.warning("Groq call failed: %s", repr(e))
    return None


def _call_gemini(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None
    try:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 100},
            },
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            note = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return note or None
        logger.warning("Gemini returned %s: %s", resp.status_code, resp.text[:300])
    except Exception as e:
        logger.warning("Gemini call failed: %s", repr(e))
    return None


def _generate_llm_note(features: dict, probability: float, symbol: str) -> str:
    fallback = f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."

    rsi = _describe_feature(features, "rsi_14")
    adx = _describe_feature(features, "adx_14")
    pe = _describe_feature(features, "pe_ratio")
    roe = _describe_feature(features, "roe")
    sent = _describe_feature(features, "news_sentiment_7d", "{:.2f}")
    vol_ratio = _describe_feature(features, "volume_ratio_20", "{:.2f}")

    system_prompt = (
        "You are a concise stock analyst. Given technical, fundamental and sentiment readings, "
        "explain in exactly one clear sentence why the model estimates the probability it does. "
        "No disclaimers."
    )
    user_prompt = (
        f"{symbol}: RSI={rsi}, ADX={adx}, PE={pe}, ROE={roe}, "
        f"news sentiment 7d={sent}, volume={vol_ratio}x. "
        f"Model says {round(probability * 100)}% chance of +5% in 10 days. Explain in one sentence."
    )

    for call in (_call_gemini, _call_groq):
        note = call(system_prompt, user_prompt)
        if note:
            return note
    return fallback


def _align_features(features: dict, model) -> dict:
    """Return only the features the model was trained on, fill missing with 0."""
    if hasattr(model, "feature_names_in_"):
        expected = list(model.feature_names_in_)
        aligned = {col: features.get(col, 0.0) for col in expected}
        return aligned
    # Fallback: use all known columns
    return {col: features.get(col, 0.0) for col in FEATURE_COLUMNS}


@app.get("/predict/{symbol}")
def predict(symbol: str):
    """
    Live prediction using technical + fundamental + news features.
    For live calls, latest available fundamental and news data are used
    (correct by definition — no future information).
    """
    try:
        if _model is None:
            return {
                "symbol": symbol.upper(),
                "model_loaded": False,
                "probability": None,
                "prediction_score": None,
                "note": "No trained model yet.",
                "features_used": "none",
            }

        df = _fetch_history(symbol)
        fundamental = _fetch_fundamentals(symbol)
        news = _fetch_news_scores(symbol)

        # Full feature vector (technical + fundamental + news)
        features = build_full_feature_vector(
            history_df=df,
            fundamental_snapshot=fundamental,
            news_scores=news,
            as_of_date=datetime.utcnow(),
        )

        aligned = _align_features(features, _model)
        X = pd.DataFrame([aligned])

        if hasattr(_model, "feature_names_in_"):
            X = X[list(_model.feature_names_in_)]

        probability = float(_model.predict_proba(X)[0, 1])
        # Convert probability to 0-100 score commonly used by decision engine
        prediction_score = round(probability * 100, 2)

        note = _generate_llm_note(features, probability, symbol.upper())

        return {
            "symbol": symbol.upper(),
            "model_loaded": True,
            "probability": round(probability, 4),
            "prediction_score": prediction_score,
            "note": note,
            "features_used": "technical+fundamental+news",
            "feature_snapshot": {k: round(v, 4) if isinstance(v, (int, float)) else v for k, v in aligned.items()},
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Prediction failed for %s", symbol)
        return {
            "symbol": symbol.upper(),
            "model_loaded": _model is not None,
            "probability": None,
            "prediction_score": None,
            "note": f"Prediction error: {str(e)[:120]}",
            "features_used": "error",
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8007)), reload=True)
