# services/training-service/app.py
"""
Training-service FastAPI application.
Exposes REST API endpoints for training intelligence, prediction recording, and evaluation.
"""
import os
import logging
import json
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request  # <-- added Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any   # <-- fixed: added Dict, Any
import joblib
import numpy as np
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import sessionmaker

from models import Base, ensure_schema, PredictionSnapshot, PredictionOutcome, TrainingRun, PaperTrade
from pit_validation import validate_prediction_snapshot, validate_outcome_vs_prediction
import trades as trades_module
from evaluate import (
    evaluate_t1 as _evaluate_t1_prediction,
    compute_training_metrics,
    compute_training_metrics,
    evaluate_t5 as _evaluate_t5_prediction,
    evaluate_pending_predictions,
    compute_training_metrics,
)
from train import request_abort   # <-- abort function

# Optional imports
try:
    from models import ModelRegistry
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False

try:
    from insights import InsightGenerator
    HAS_INSIGHTS = True
except ImportError:
    HAS_INSIGHTS = False

HAS_DB = True

# ---------- IST timezone helper ----------
IST = ZoneInfo("Asia/Kolkata")

def ist_now() -> datetime:
    """Return current time as a naive datetime in IST (UTC+5:30)."""
    return datetime.now(IST).replace(tzinfo=None)

app = FastAPI(title="Training Intelligence", version="1.0")
try:
    from universe_routes import router as universe_router
    app.include_router(universe_router)
    logging.getLogger("training-service").info("Universe training routes mounted at /api/universe/*")
except Exception as _ue:
    logging.getLogger("training-service").warning("Universe routes not mounted: %s", _ue)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MARKET_DATA_URL = os.environ.get("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
SERVICE_URL = os.environ.get(
    "SERVICE_URL",
    os.environ.get("TRAINING_URL", f"{os.environ.get('DECISION_PREDICTION_URL', 'https://decision-prediction-service.onrender.com').rstrip('/')}/training"),
)
DATABASE_URL = os.environ.get('TRAINING_DATABASE_URL') or os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
_db_backend = 'postgres' if DATABASE_URL.startswith(('postgres://', 'postgresql://')) else 'sqlite'
_training_db_env_set = bool(os.environ.get('TRAINING_DATABASE_URL') or os.environ.get('DATABASE_URL'))
logging.getLogger('training-service').info('DB backend=%s (set DATABASE_URL for durable win-rate/T+1/T+5)', _db_backend)
MODEL_STORE_PATH = os.environ.get('MODEL_STORE_PATH', './model-store')
# Neon/Supabase-friendly engine (pool_pre_ping, ssl, postgres:// fix)
try:
    from models import get_engine
    engine = get_engine(DATABASE_URL)
except Exception:
    _url = DATABASE_URL
    if _url.startswith("postgres://"):
        _url = "postgresql://" + _url[len("postgres://"):]
    engine = create_engine(_url, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# Prefer durable/writable paths on free-tier Render (cwd may be read-only after crash)
_DATA_DIR = os.environ.get("TRAINING_DATA_DIR") or os.environ.get("MODEL_STORE_PATH") or "."
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:
    _DATA_DIR = "."
LOCK_FILE = os.path.join(_DATA_DIR, "training.lock")
# Training a full universe can exceed 5 minutes — a short timeout made the lock
# look "stale" mid-run so a second Trigger would delete the lock and abort the job.
LOCK_TIMEOUT_SECONDS = int(os.environ.get("TRAINING_LOCK_TIMEOUT_SECONDS", "2700"))  # 45 min default


# ---------- Pydantic models for prediction recording ----------
class PredictionSnapshotCreate(BaseModel):
    symbol: str
    decision: str  # "BUY NOW", "PREPARE TO BUY", "HOLD", etc.
    confidence: str  # "High", "Medium", "Low"
    price: float
    target: Optional[float] = None
    stop_loss: Optional[float] = None
    entry_range_low: Optional[float] = None
    entry_range_high: Optional[float] = None
    combined_score: float
    technical_score: float
    fundamental_score: float
    news_score: Optional[float] = None
    prediction_score: Optional[float] = None
    market_score: float
    training_score: float
    event_risk: bool = False
    rsi: Optional[float] = None
    macd: Optional[str] = None
    ema: Optional[str] = None
    volume_ratio: Optional[float] = None
    debt_to_equity: Optional[float] = None
    roe: Optional[float] = None
    roce: Optional[float] = None
    market_mood: Optional[str] = None
    nifty_change_pct: Optional[float] = None
    sensex_change_pct: Optional[float] = None
    # These were being sent by decision-engine's record_prediction_for_training()
    # and already have columns on PredictionSnapshot, but weren't declared
    # here — Pydantic silently drops undeclared fields on a request body.
    market_sentiment_adjustment: Optional[float] = None
    holding_period: Optional[str] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    sector: Optional[str] = None
    valuation: Optional[str] = None
    # Renamed from `extra`: decision-engine sends this key as
    # "feature_snapshot" (matching the DB column name), not "extra".
    feature_snapshot: Optional[dict] = None

# ---------- Numpy conversion helper ----------
def convert_numpy(obj):
    """Make values JSON-safe (no NaN/Inf — those break JSON.parse)."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf
            return None
        return v
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, np.ndarray):
        return [convert_numpy(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None:
        return None
    # datetime etc.
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    return obj

# ---------- Startup ----------
@app.on_event("startup")
def startup():
    try:
        from models import init_db
        ok = init_db(engine)
        if ok:
            logger.info("Database schema initialized (create_all + ensure_schema).")
        else:
            logger.error("Database schema init reported failure — trades may 500 until tables exist.")
    except Exception as e:
        logger.exception("Database schema initialization failed: %s", e)
        try:
            Base.metadata.create_all(engine)
            ensure_schema(engine)
            logger.info("Database schema initialized (fallback path).")
        except Exception as e2:
            logger.exception("Fallback schema init also failed: %s", e2)
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            logger.info("Removed stale lock file on startup")
        except Exception as e:
            logger.warning(f"Could not remove lock file: {e}")

# ----------------------------------------------------------------------
# Lock helpers
# ----------------------------------------------------------------------
def is_lock_stale():
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        mtime = os.path.getmtime(LOCK_FILE)
        if time.time() - mtime > LOCK_TIMEOUT_SECONDS:
            return True
        return False
    except Exception:
        return True  # unreadable lock → treat as stale

def acquire_lock():
    if is_lock_stale():
        try:
            os.remove(LOCK_FILE)
            logger.info("Removed stale lock file (age > %ss)", LOCK_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning("Could not remove stale lock: %s", e)
    if os.path.exists(LOCK_FILE):
        return False
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(f"{os.getpid()}\n{time.time()}\n")
        return True
    except Exception as e:
        logger.error("acquire_lock failed: %s", e)
        return False

def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            logger.info("Lock released")
        except Exception as e:
            logger.warning("release_lock failed: %s", e)

def is_training_running():
    """True only for a live lock — stale locks must not block the UI forever."""
    if not os.path.exists(LOCK_FILE):
        return False
    if is_lock_stale():
        try:
            os.remove(LOCK_FILE)
            logger.info("Cleared stale lock inside is_training_running()")
        except Exception:
            pass
        return False
    return True


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def _db_connection_info():
    """Probe DB connectivity for UI (no secrets)."""
    info = {
        "db_backend": _db_backend,
        "db_durable": _db_backend == "postgres",
        "db_connected": False,
        "db_provider": None,
        "db_message": "",
        "db_error": None,
    }
    url = DATABASE_URL or ""
    low = url.lower()
    if "supabase" in low:
        info["db_provider"] = "supabase"
    elif "neon.tech" in low or "neon" in low:
        info["db_provider"] = "neon"
    elif _db_backend == "postgres":
        info["db_provider"] = "postgres"
    else:
        info["db_provider"] = "sqlite"
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            info["db_connected"] = True
            if info["db_durable"]:
                prov = (info["db_provider"] or "postgres").title()
                info["db_message"] = f"Connected to {prov} Postgres — trades, training, and backups persist."
            else:
                info["db_message"] = (
                    "Using local SQLite (ephemeral). Set DATABASE_URL to Supabase/Neon Postgres "
                    "on decision-prediction-service so data survives restarts."
                )
        finally:
            db.close()
    except Exception as e:
        info["db_connected"] = False
        err = str(e)
        # User-friendly common errors
        if "password" in err.lower() or "authentication" in err.lower():
            info["db_error"] = "Database authentication failed — check Supabase password in DATABASE_URL."
        elif "timeout" in err.lower() or "could not connect" in err.lower() or "connection refused" in err.lower():
            info["db_error"] = "Cannot reach database host — check Supabase URL, network, or SSL."
        elif "ssl" in err.lower():
            info["db_error"] = "SSL connection issue with Postgres — ensure sslmode=require in DATABASE_URL."
        elif "does not exist" in err.lower():
            info["db_error"] = "Database name not found — verify Supabase project database name."
        else:
            info["db_error"] = f"Database error: {err[:180]}"
        info["db_message"] = info["db_error"]
    return info


def get_training_status():
    status = {
        'service_url': SERVICE_URL,
        'production_model_exists': False,
        'last_training': None,
        'dataset_size': 0,
        'num_symbols': 0,
        'metrics': {},
        'fold_details': [],
        'model_version': None,
        'training_in_progress': is_training_running(),
        'db_backend': _db_backend,
        'live_win_rate': None,
        'win_rate': None,
    }
    status.update(_db_connection_info())

    db = SessionLocal()
    try:
        latest_run = db.query(TrainingRun).order_by(TrainingRun.run_timestamp.desc()).first()
        if latest_run:
            status['last_training'] = latest_run.run_timestamp.isoformat()
            status['dataset_size'] = latest_run.dataset_size or 0
            status['num_symbols'] = latest_run.num_symbols or 0
            status['metrics'] = convert_numpy(
                json.loads(latest_run.walk_forward_metrics) if latest_run.walk_forward_metrics else {}
            )
            status['fold_details'] = convert_numpy(
                json.loads(latest_run.fold_details) if latest_run.fold_details else []
            )
            status['model_version'] = latest_run.model_version
        # Live win-rate from evaluated prediction snapshots (closed-loop feedback)
        try:
            snaps = db.query(PredictionSnapshot).order_by(PredictionSnapshot.timestamp.desc()).limit(500).all()
            evaluated = 0
            wins = 0
            for s in snaps:
                # Prefer T+5, fall back to T+1
                val = getattr(s, "t5_success", 0) or 0
                if val not in (1, 2):
                    val = getattr(s, "t1_success", 0) or 0
                if val in (1, 2):
                    evaluated += 1
                    if val == 1:
                        wins += 1
            # Expose n always so UI can show progress toward closed-loop
            status['live_win_rate_n'] = evaluated
            # Activate rate from 8 samples (was effectively inert until large n)
            if evaluated >= 8:
                wr = round(wins / evaluated, 4)
                status['live_win_rate'] = wr
                status['win_rate'] = wr
            elif evaluated > 0:
                # Provisional rate for display only — decision still scales by n
                wr = round(wins / evaluated, 4)
                status['live_win_rate'] = wr
                status['win_rate'] = wr
                status['live_win_rate_provisional'] = True
        except Exception as e:
            logger.debug("live win-rate compute skipped: %s", e)
    except Exception as e:
        logger.error(f"Error reading latest training run from DB: {e}")
    finally:
        db.close()

    if HAS_MODEL_REGISTRY:
        try:
            registry = ModelRegistry(SessionLocal)
            prod = registry.get_production_model()
            if prod:
                status['production_model_exists'] = True
                status['model_version'] = prod[2]['version']
        except Exception as e:
            logger.warning(f"Could not read model registry: {e}")

    return convert_numpy(status)

def get_models_list():
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry(SessionLocal)
    return convert_numpy(registry.list_models())

def promote_model(version: str):
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry(SessionLocal)
    ok = registry.promote_model(version)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Model version {version} not found")
    logger.info(f"Promoted model {version} to production")
    return {"status": "success", "version": version}

def get_learning_insights():
    if not HAS_INSIGHTS:
        raise HTTPException(status_code=501, detail="Insights module not available")
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No training report found")
    return {
        "insights": [
            {"insight": "Bullish market regimes show higher T+5 success rates", "sample_size": 124, "confidence": "high", "active": True},
            {"insight": "RSI between 50-65 performs best for BUY signals", "sample_size": 87, "confidence": "medium", "active": True},
            {"insight": "Volume > 1.5x average improves win rate by 12%", "sample_size": 65, "confidence": "high", "active": True}
        ],
        "last_updated": ist_now().isoformat()
    }

def get_summary_metrics():
    db = SessionLocal()
    try:
        latest_run = db.query(TrainingRun).order_by(TrainingRun.run_timestamp.desc()).first()
        if not latest_run:
            return {"error": "No training runs found"}
        metrics = json.loads(latest_run.walk_forward_metrics) if latest_run.walk_forward_metrics else {}
        return {
            "latest_run": {
                "timestamp": latest_run.run_timestamp.isoformat(),
                "dataset_size": latest_run.dataset_size,
                "num_symbols": latest_run.num_symbols,
                "metrics": convert_numpy(metrics)
            }
        }
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# Routes (existing)
# ----------------------------------------------------------------------
@app.get("/")
async def root():
    return JSONResponse(content={
        "message": "Training Service is running",
        "service_url": SERVICE_URL,
        "status": "healthy"
    })

@app.get("/health")
async def health(warm: bool = False):
    """Light health by default — does not touch Postgres (saves Supabase egress).
    Pass ?db=1 only when you need connectivity diagnostics.
    """
    import os as _os
    skip = _os.environ.get("HEALTH_SKIP_DB", "1").lower() in ("1", "true", "yes")
    # warm is for waking the dyno only
    if skip:
        return JSONResponse(content={
            "status": "ok",
            "service": "training",
            "db_checked": False,
            "warm": bool(warm),
        })
    info = _db_connection_info()
    ok = info.get("db_connected", False)
    return JSONResponse(content={
        "status": "ok" if ok else "degraded",
        **info,
    })

@app.post("/api/admin/init-schema")
async def admin_init_schema():
    """Create missing tables on Neon/Postgres (paper_trades, portfolio_account, etc.)."""
    try:
        from models import init_db
        ok = init_db(engine)
        return JSONResponse(content={"ok": ok, "message": "Schema create_all + ensure_schema completed" if ok else "init_db returned false"})
    except Exception as e:
        logger.exception("init-schema failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/lock-status")
async def lock_status():
    """Return whether training lock exists (training in progress)."""
    return JSONResponse(content={"training_in_progress": is_training_running()})

@app.delete("/lock")
async def clear_lock():
    logger.info("DELETE /lock called")
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.info("Lock file removed, calling request_abort()")
        request_abort()
        return JSONResponse(content={"status": "Lock cleared and abort requested"})
    logger.info("No lock file found")
    return JSONResponse(content={"status": "No lock found"})

# ---------- Debug endpoint for forceful abort ----------
@app.post("/debug/abort")
async def debug_abort():
    """Forcefully set the abort event – for testing the stop mechanism."""
    logger.info("POST /debug/abort called – forcing abort")
    request_abort()
    return JSONResponse(content={"status": "Abort event set"})

# ----------------------------------------------------------------------
# API routes
# ----------------------------------------------------------------------
@app.get("/api/status")
async def api_status():
    return JSONResponse(content=get_training_status())

@app.post("/api/train")
@app.post("/train/run")
async def api_trigger_training(background_tasks: BackgroundTasks, label_source: str = "t1_outcome"):
    if label_source not in ("t1_outcome", "trade_pnl"):
        raise HTTPException(status_code=400, detail="label_source must be 't1_outcome' or 'trade_pnl'")
    if not acquire_lock():
        raise HTTPException(
            status_code=409,
            detail=(
                "Training is currently in progress. Wait for completion, or clear a "
                "stale lock via DELETE /training/lock or POST /training/api/lock/clear."
            ),
        )

    def run_training():
        try:
            try:
                from train import write_progress
                write_progress(0, 0, 0, stage="loading_data", detail={"label_source": label_source})
            except Exception:
                pass
            from train import train_model
            train_model(
                SessionLocal(),
                os.environ.get("MODEL_STORE_PATH", "./model-store"),
                label_source=label_source,
            )
        except Exception as e:
            logger.error("Training failed: %s", e)
            try:
                from train import write_progress
                write_progress(0, 0, 0, stage="aborted", detail={"error": str(e)[:300]})
            except Exception:
                pass
        finally:
            release_lock()
            logger.info("Training completed and lock released.")

    background_tasks.add_task(run_training)
    return JSONResponse(
        content={
            "status": "Training started successfully",
            "status_code": "ACCEPTED",
            "label_source": label_source,
            "service_url": SERVICE_URL,
            "progress_url": "/training/api/train/progress",
            "message": "Training job started in background.",
        },
        status_code=202,
    )


@app.get("/api/report")
async def api_report():
    report_path = os.path.join(MODEL_STORE_PATH, 'training_report.joblib')
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No report found")
    try:
        report = joblib.load(report_path)
        return JSONResponse(content=convert_numpy(report))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/models")
async def api_models():
    try:
        models = get_models_list()
        return JSONResponse(content={"models": models})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/models/promote/{version}")
async def api_promote(version: str):
    try:
        result = promote_model(version)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error promoting model: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/insights")
async def api_insights():
    try:
        insights = get_learning_insights()
        return JSONResponse(content=insights)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating insights: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics/summary")
async def api_summary():
    try:
        summary_data = get_summary_metrics()
        return JSONResponse(content=summary_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------------------------------------------------
# NEW: Prediction recording and evaluation endpoints
# ----------------------------------------------------------------------
@app.post("/api/predictions")
async def store_prediction(pred: PredictionSnapshotCreate, background_tasks: BackgroundTasks):
    """Store a prediction snapshot from the decision engine."""
    db = SessionLocal()
    try:
        # Same symbol + same decision + same calendar day = the same real
        # pick, regardless of which caller recorded it first. Without this,
        # decision-engine's automatic background recording and a manual
        # "add to training" click for the same scan result create two rows
        # for one actual trading decision, double-weighting it everywhere
        # downstream, including the classifier's training data.
        today_start = ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
        existing = db.query(PredictionSnapshot).filter(
            PredictionSnapshot.symbol == pred.symbol,
            PredictionSnapshot.decision == pred.decision,
            PredictionSnapshot.timestamp >= today_start,
        ).first()
        if existing:
            return JSONResponse(content={"status": "already_recorded", "prediction_id": existing.prediction_id})

        # Generate unique ID
        pred_id = f"STK-{datetime.now(IST).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        # Clean up old predictions (keep last 90 days to save space)
        cutoff = ist_now() - timedelta(days=90)
        db.query(PredictionSnapshot).filter(PredictionSnapshot.timestamp < cutoff).delete()
        db.commit()

        # Point-in-time validation (non-fatal warnings; hard issues logged)
        try:
            _pit = validate_prediction_snapshot({
                "timestamp": pred.timestamp if hasattr(pred, "timestamp") else None,
                "as_of": getattr(pred, "timestamp", None),
                "combined_score": getattr(pred, "combined_score", None),
                "technical_score": getattr(pred, "technical_score", None),
                "fundamental_score": getattr(pred, "fundamental_score", None),
                "prediction_score": getattr(pred, "prediction_score", None),
                "decision": getattr(pred, "decision", None),
                "feature_snapshot": getattr(pred, "feature_snapshot", None),
                "provisional": getattr(pred, "provisional", None) if hasattr(pred, "provisional") else None,
            })
            if not _pit.get("ok"):
                logger.warning("PIT validation issues for %s: %s", getattr(pred, "symbol", "?"), _pit.get("issues"))
        except Exception as _pit_e:
            logger.debug("PIT validate skip: %s", _pit_e)
        snapshot = PredictionSnapshot(
            prediction_id=pred_id,
            symbol=pred.symbol,
            timestamp=ist_now(),
            price=pred.price,
            decision=pred.decision,
            confidence=pred.confidence,
            combined_score=pred.combined_score,
            technical_score=pred.technical_score,
            fundamental_score=pred.fundamental_score,
            news_score=pred.news_score,
            prediction_score=pred.prediction_score,
            market_score=pred.market_score,
            market_sentiment_adjustment=pred.market_sentiment_adjustment or 0.0,
            training_score=pred.training_score,
            event_risk=pred.event_risk,
            entry_range_low=pred.entry_range_low,
            entry_range_high=pred.entry_range_high,
            target=pred.target,
            stop_loss=pred.stop_loss,
            holding_period=pred.holding_period,
            support=pred.support,
            resistance=pred.resistance,
            sector=pred.sector,
            valuation=pred.valuation,
            market_mood=pred.market_mood,
            nifty_change_pct=pred.nifty_change_pct,
            sensex_change_pct=pred.sensex_change_pct,
            rsi=pred.rsi,
            macd=pred.macd,
            ema=pred.ema,
            volume_ratio=pred.volume_ratio,
            debt_to_equity=pred.debt_to_equity,
            roe=pred.roe,
            roce=pred.roce,
            feature_snapshot=pred.feature_snapshot,
            model_version=None,
            created_at=ist_now(),
            t1_success=0,
            t5_success=0,
            overall_success=0
        )
        db.add(snapshot)
        db.commit()
        logger.info(f"Stored prediction {pred_id} for {pred.symbol}")

        background_tasks.add_task(_evaluate_t1_prediction, pred_id)
        background_tasks.add_task(_evaluate_t5_prediction, pred_id)

        return JSONResponse(content={"status": "stored", "prediction_id": pred_id})
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to store prediction: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/evaluate/t1")
async def api_evaluate_t1(background_tasks: BackgroundTasks, max_batch: int = 50, sync: bool = True):
    """
    T+1 evaluation of due predictions.
    sync=true (default): run in-request so GitHub Actions / curl get real counts
    (background tasks die on free-tier sleep).
    """
    max_batch = max(1, min(int(max_batch or 50), 150))
    if sync:
        try:
            result = evaluate_pending_predictions("T+1", max_batch=max_batch)
            metrics = {}
            try:
                metrics = compute_training_metrics() or {}
            except Exception:
                pass
            # Flatten result fields so UI/gateway logs see pipeline without digging
            payload = {
                "status": "T+1 evaluation complete",
                "sync": True,
                "result": result,
                "metrics_t1": (metrics.get("T+1") if isinstance(metrics, dict) else None),
            }
            if isinstance(result, dict):
                for k in (
                    "ok", "pending", "due", "waiting", "attempted", "succeeded",
                    "backfilled", "period", "reasons", "pipeline", "message",
                    "labeled_sample", "skipped_sample",
                ):
                    if k in result:
                        payload[k] = result[k]
            return JSONResponse(content=payload)
        except Exception as e:
            logger.error("T+1 evaluation failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
    def run_eval():
        try:
            evaluate_pending_predictions("T+1", max_batch=max_batch)
        except Exception as e:
            logger.error("T+1 evaluation failed: %s", e)
    background_tasks.add_task(run_eval)
    return JSONResponse(content={"status": "T+1 evaluation triggered", "sync": False})

@app.post("/api/evaluate/t5")
async def api_evaluate_t5(background_tasks: BackgroundTasks, max_batch: int = 50, sync: bool = True):
    """T+5 evaluation — sync by default for reliable cron delivery."""
    max_batch = max(1, min(int(max_batch or 50), 150))
    if sync:
        try:
            result = evaluate_pending_predictions("T+5", max_batch=max_batch)
            payload = {"status": "T+5 evaluation complete", "sync": True, "result": result}
            if isinstance(result, dict):
                for k in (
                    "ok", "pending", "due", "waiting", "attempted", "succeeded",
                    "backfilled", "period", "reasons", "pipeline", "message",
                    "labeled_sample", "skipped_sample",
                ):
                    if k in result:
                        payload[k] = result[k]
            return JSONResponse(content=payload)
        except Exception as e:
            logger.error("T+5 evaluation failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
    def run_eval():
        try:
            evaluate_pending_predictions("T+5", max_batch=max_batch)
        except Exception as e:
            logger.error("T+5 evaluation failed: %s", e)
    background_tasks.add_task(run_eval)
    return JSONResponse(content={"status": "T+5 evaluation triggered", "sync": False})


@app.get("/api/evaluate/status")
async def api_evaluate_status():
    """Progress snapshot for T+1 and T+5 evaluation queues.

    Returns pending/evaluated counts, how many are due (calendar time elapsed),
    success rates, and a rough remaining-time estimate for a full sweep.
    """
    db = SessionLocal()
    try:
        now = ist_now()
        snaps = db.query(PredictionSnapshot).order_by(PredictionSnapshot.timestamp.desc()).limit(2000).all()
        total = len(snaps)

        def _bucket(period: str, success_attr: str, min_days: int):
            pending = 0
            evaluated = 0
            success = 0
            due = 0  # pending AND enough calendar time has passed
            earliest_due = None
            for s in snaps:
                val = getattr(s, success_attr, 0) or 0
                # 0 = not evaluated, 1 = success, 2 = fail (scanner convention)
                if val in (1, 2):
                    evaluated += 1
                    if val == 1:
                        success += 1
                else:
                    pending += 1
                    if s.timestamp:
                        age_days = (now - s.timestamp).total_seconds() / 86400.0
                        if age_days >= min_days:
                            due += 1
                        else:
                            remain = min_days - age_days
                            if earliest_due is None or remain < earliest_due:
                                earliest_due = remain
            # ~2.5s per symbol for yfinance fetch in a sweep (empirical free-tier)
            eta_sweep_sec = int(due * 2.5) if due else 0
            next_unlock_hours = round(earliest_due * 24, 1) if earliest_due is not None and due == 0 and pending > 0 else None
            return {
                "period": period,
                "total": total,
                "pending": pending,
                "evaluated": evaluated,
                "due_now": due,
                "success": success,
                "success_rate_pct": round(success / evaluated * 100, 1) if evaluated else None,
                "progress_pct": round(evaluated / total * 100, 1) if total else 0.0,
                "eta_sweep_seconds": eta_sweep_sec,
                "eta_sweep_label": (
                    f"~{eta_sweep_sec // 60}m {eta_sweep_sec % 60}s" if eta_sweep_sec >= 60
                    else f"~{eta_sweep_sec}s" if eta_sweep_sec > 0
                    else "—"
                ),
                "next_unlock_hours": next_unlock_hours,
                "status": (
                    "complete" if pending == 0 and total > 0
                    else "ready" if due > 0
                    else "waiting" if pending > 0
                    else "empty"
                ),
            }

        t1 = _bucket("T+1", "t1_success", 1)
        t5 = _bucket("T+5", "t5_success", 5)
        return JSONResponse(content={
            "t1": t1,
            "t5": t5,
            "generated_at": now.isoformat(),
        })
    except Exception as e:
        logger.exception("evaluate status failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/predictions/history")
async def prediction_history(limit: int = 50, offset: int = 0):
    """Return recent predictions with outcomes."""
    db = SessionLocal()
    try:
        results = (
            db.query(PredictionSnapshot)
            .order_by(PredictionSnapshot.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        out = []
        for r in results:
            outcomes = db.query(PredictionOutcome).filter(
                PredictionOutcome.prediction_id == r.prediction_id
            ).all()
            out.append({
                "prediction_id": r.prediction_id,
                "symbol": r.symbol,
                "timestamp": r.timestamp.isoformat(),
                "decision": r.decision,
                "price": r.price,
                "t1_success": r.t1_success,
                "t5_success": r.t5_success,
                "outcomes": [{"period": o.evaluation_period, "return_pct": o.return_pct, "success": o.success} for o in outcomes]
            })
        return JSONResponse(content={"predictions": out, "total": len(out)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# NEW: Daily / weekly pick-tracking rollups
# ----------------------------------------------------------------------
def _build_period_rollup(period: str, lookback: int) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        cutoff = ist_now() - (timedelta(days=lookback) if period == "daily" else timedelta(weeks=lookback))
        snapshots = db.query(PredictionSnapshot).filter(PredictionSnapshot.timestamp >= cutoff).all()
        if not snapshots:
            return []

        pred_ids = [s.prediction_id for s in snapshots]
        outcomes = db.query(PredictionOutcome).filter(PredictionOutcome.prediction_id.in_(pred_ids)).all()
        outcomes_by_pred: Dict[str, Dict[str, PredictionOutcome]] = {}
        for o in outcomes:
            outcomes_by_pred.setdefault(o.prediction_id, {})[o.evaluation_period] = o

        def _bucket_key(ts: datetime) -> str:
            if period == "daily":
                return ts.date().isoformat()
            iso_year, iso_week, _ = ts.isocalendar()
            return f"{iso_year}-W{iso_week:02d}"

        buckets: Dict[str, Dict[str, Any]] = {}
        for s in snapshots:
            key = _bucket_key(s.timestamp)
            b = buckets.setdefault(key, {
                "period": key, "predictions_recorded": 0, "buy_now": 0, "prepare_to_buy": 0,
                "symbols": set(), "t1_evaluated": 0, "t1_success": 0, "t1_returns": [],
                "t5_evaluated": 0, "t5_success": 0, "t5_returns": [],
            })
            b["predictions_recorded"] += 1
            b["symbols"].add(s.symbol)
            if s.decision == "BUY NOW":
                b["buy_now"] += 1
            elif s.decision == "PREPARE TO BUY":
                b["prepare_to_buy"] += 1
            po = outcomes_by_pred.get(s.prediction_id, {})
            t1, t5 = po.get("T+1"), po.get("T+5")
            if t1 is not None:
                b["t1_evaluated"] += 1
                b["t1_success"] += int(bool(t1.success))
                if t1.return_pct is not None:
                    b["t1_returns"].append(t1.return_pct)
            if t5 is not None:
                b["t5_evaluated"] += 1
                b["t5_success"] += int(bool(t5.success))
                if t5.return_pct is not None:
                    b["t5_returns"].append(t5.return_pct)

        results = []
        for key in sorted(buckets.keys(), reverse=True):
            b = buckets[key]
            results.append({
                "period": b["period"], "predictions_recorded": b["predictions_recorded"],
                "unique_symbols": len(b["symbols"]), "buy_now": b["buy_now"], "prepare_to_buy": b["prepare_to_buy"],
                "t1_evaluated": b["t1_evaluated"],
                "t1_success_rate": round(b["t1_success"] / b["t1_evaluated"] * 100, 1) if b["t1_evaluated"] else None,
                "t1_avg_return_pct": round(float(np.mean(b["t1_returns"])), 2) if b["t1_returns"] else None,
                "t5_evaluated": b["t5_evaluated"],
                "t5_success_rate": round(b["t5_success"] / b["t5_evaluated"] * 100, 1) if b["t5_evaluated"] else None,
                "t5_avg_return_pct": round(float(np.mean(b["t5_returns"])), 2) if b["t5_returns"] else None,
                "t1_pending": b["predictions_recorded"] - b["t1_evaluated"],
                "t5_pending": b["predictions_recorded"] - b["t5_evaluated"],
            })
        return results
    finally:
        db.close()

@app.get("/api/metrics/daily")
async def api_metrics_daily(days: int = 30):
    try:
        return JSONResponse(content=convert_numpy(_build_period_rollup("daily", days)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics/weekly")
async def api_metrics_weekly(weeks: int = 12):
    try:
        return JSONResponse(content=convert_numpy(_build_period_rollup("weekly", weeks)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------------------------------------------------
# NEW: Manual "add all actionable picks to training + open trades" action
# ----------------------------------------------------------------------
class ActionableCommitRequest(BaseModel):
    picks: List[PredictionSnapshotCreate]
    capital_per_trade: float = 10000.0
    # Default False so "Add to Training" does not open trades.
    # Frontend passes open_trades=true explicitly for "to Trade" actions.
    open_trades: bool = False
    # When True, refresh today's snapshot scores instead of skipping as already_recorded
    force_refresh: bool = False

@app.post("/api/actionable/commit")
async def commit_actionable_picks(req: ActionableCommitRequest, background_tasks: BackgroundTasks):
    """Backs the 'Add all actionable stocks to training' button: for each
    BUY NOW / PREPARE TO BUY pick from a finished scan, records it
    (idempotent via the same dedup guard as store_prediction) and, if
    open_trades, opens a paper trade against it.

    Same symbol+decision on the same IST calendar day → already_recorded
    (or updated when force_refresh / scores moved). Training tracking stays
    one row per symbol/decision/day so T+1/T+5 is not duplicated.
    """
    db = SessionLocal()
    results = []
    try:
        for pick in req.picks:
            today_start = ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
            existing = db.query(PredictionSnapshot).filter(
                PredictionSnapshot.symbol == pick.symbol,
                PredictionSnapshot.decision == pick.decision,
                PredictionSnapshot.timestamp >= today_start,
            ).first()

            if existing:
                pred_id = existing.prediction_id
                # Refresh live fields so re-click isn't a no-op for the user
                score_moved = False
                try:
                    if pick.price and existing.price and abs(float(pick.price) - float(existing.price)) / max(float(existing.price), 1e-6) > 0.005:
                        score_moved = True
                    if pick.combined_score is not None and existing.combined_score is not None:
                        if abs(float(pick.combined_score) - float(existing.combined_score)) >= 2:
                            score_moved = True
                except Exception:
                    score_moved = bool(req.force_refresh)

                if req.force_refresh or score_moved:
                    for field in (
                        "price", "confidence", "combined_score", "technical_score", "fundamental_score",
                        "news_score", "prediction_score", "market_score", "training_score",
                        "entry_range_low", "entry_range_high", "target", "stop_loss",
                        "rsi", "macd", "ema", "volume_ratio",
                    ):
                        val = getattr(pick, field, None)
                        if val is not None:
                            setattr(existing, field, val)
                    if pick.market_sentiment_adjustment is not None:
                        existing.market_sentiment_adjustment = pick.market_sentiment_adjustment
                    db.commit()
                    record_status = "updated"
                else:
                    record_status = "already_recorded"
            else:
                pred_id = f"STK-{datetime.now(IST).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
                snapshot = PredictionSnapshot(
                    prediction_id=pred_id, symbol=pick.symbol, timestamp=ist_now(), price=pick.price,
                    decision=pick.decision, confidence=pick.confidence, combined_score=pick.combined_score,
                    technical_score=pick.technical_score, fundamental_score=pick.fundamental_score,
                    news_score=pick.news_score, prediction_score=pick.prediction_score,
                    market_score=pick.market_score, market_sentiment_adjustment=pick.market_sentiment_adjustment or 0.0,
                    training_score=pick.training_score, event_risk=pick.event_risk,
                    entry_range_low=pick.entry_range_low, entry_range_high=pick.entry_range_high,
                    target=pick.target, stop_loss=pick.stop_loss, holding_period=pick.holding_period,
                    support=pick.support, resistance=pick.resistance, sector=pick.sector, valuation=pick.valuation,
                    market_mood=pick.market_mood, nifty_change_pct=pick.nifty_change_pct,
                    sensex_change_pct=pick.sensex_change_pct, rsi=pick.rsi, macd=pick.macd, ema=pick.ema,
                    volume_ratio=pick.volume_ratio, debt_to_equity=pick.debt_to_equity, roe=pick.roe, roce=pick.roce,
                    feature_snapshot=pick.feature_snapshot, model_version=None, created_at=ist_now(),
                    t1_success=0, t5_success=0, overall_success=0,
                )
                db.add(snapshot)
                db.commit()
                background_tasks.add_task(_evaluate_t1_prediction, pred_id)
                background_tasks.add_task(_evaluate_t5_prediction, pred_id)
                record_status = "stored"

            trade_id, trade_status = None, "not_requested"
            if req.open_trades:
                trade, was_new, trade_error = trades_module.open_trade(pred_id, capital=req.capital_per_trade)
                if trade is not None:
                    trade_id = trade.trade_id
                    trade_status = "opened" if was_new else "already_open_or_closed"
                else:
                    trade_status = f"failed: {trade_error}"

            results.append({
                "symbol": pick.symbol, "prediction_id": pred_id, "record_status": record_status,
                "trade_id": trade_id, "trade_status": trade_status,
            })
        dbinfo = _db_connection_info()
        return JSONResponse(content={
            "results": results,
            "db_backend": dbinfo.get("db_backend"),
            "db_durable": dbinfo.get("db_durable"),
            "db_connected": dbinfo.get("db_connected"),
            "db_message": dbinfo.get("db_message") or dbinfo.get("db_error"),
        })
    except Exception as e:
        db.rollback()
        logger.error(f"Error committing actionable picks: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# NEW: Stock price history — for charts (1D/5D/1M/1Y/5Y), wraps yfinance
# directly since this service already depends on it. The more natural
# long-term home for this is market-data-service, which already owns
# price data — this is a stopgap since that file isn't available to add
# it there instead.
# ----------------------------------------------------------------------
_CHART_PERIOD_MAP = {
    "1d": ("1d", "5m"),
    "5d": ("5d", "15m"),
    "1mo": ("1mo", "1d"),
    "1y": ("1y", "1wk"),
    "5y": ("5y", "1mo"),
}

@app.get("/api/stock/history/{symbol}")
async def stock_history(symbol: str, period: str = "1mo"):
    """Chart data via market-data-service (avoids Yahoo rate-limit on this dyno)."""
    if period not in _CHART_PERIOD_MAP:
        raise HTTPException(status_code=400, detail=f"period must be one of {list(_CHART_PERIOD_MAP.keys())}")
    # Map UI period → market-data period (daily candles are enough for chart)
    md_period = {"1d": "1mo", "5d": "1mo", "1mo": "1mo", "1y": "1y", "5y": "5y"}.get(period, "1mo")
    import httpx
    last_err = None
    data = None
    for attempt in range(3):
        try:
            if attempt == 0:
                try:
                    httpx.get(f"{MARKET_DATA_URL}/health", params={"warm": "true"}, timeout=8)
                except Exception:
                    pass
            resp = httpx.get(
                f"{MARKET_DATA_URL}/history/{symbol.upper()}",
                params={"period": md_period, "interval": "1d"},
                timeout=45,
            )
            if resp.status_code in (502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                import time as _t
                _t.sleep(1.2 * (attempt + 1))
                continue
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"No price history for {symbol}")
            resp.raise_for_status()
            data = resp.json()
            break
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            import time as _t
            _t.sleep(1.2 * (attempt + 1))
    if data is None:
        # Fallback: direct yfinance once (best-effort)
        try:
            import yfinance as yf
            yf_period, yf_interval = _CHART_PERIOD_MAP[period]
            hist = yf.Ticker(symbol.upper() + ".NS").history(period=yf_period, interval=yf_interval)
            if hist is None or hist.empty:
                raise HTTPException(status_code=503, detail=f"Chart temporarily unavailable (rate limit). Try again shortly. ({last_err})")
            points = []
            for idx, row in hist.iterrows():
                try:
                    points.append({
                        "date": idx.isoformat(),
                        "open": round(float(row["Open"]), 2),
                        "high": round(float(row["High"]), 2),
                        "low": round(float(row["Low"]), 2),
                        "close": round(float(row["Close"]), 2),
                        "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                    })
                except Exception:
                    continue
            if not points:
                raise HTTPException(status_code=503, detail="Chart temporarily unavailable")
            first_close = points[0]["close"]
            last_close = points[-1]["close"]
            return JSONResponse(content={
                "symbol": symbol.upper(),
                "period": period,
                "points": points,
                "change_pct": round((last_close - first_close) / first_close * 100, 2) if first_close else None,
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error fetching history for %s: %s", symbol, e)
            raise HTTPException(status_code=503, detail=f"Chart temporarily unavailable: {e}")

    candles = data.get("candles") or []
    points = []
    for c in candles:
        try:
            points.append({
                "date": c.get("date"),
                "open": c.get("open"),
                "high": c.get("high"),
                "low": c.get("low"),
                "close": c.get("close"),
                "volume": c.get("volume") or 0,
            })
        except Exception:
            continue
    if not points:
        raise HTTPException(status_code=404, detail=f"No price history for {symbol}")
    first_close = points[0].get("close") or 0
    last_close = points[-1].get("close") or 0
    change = None
    if first_close:
        change = round((last_close - first_close) / first_close * 100, 2)
    return JSONResponse(content={
        "symbol": symbol.upper(),
        "period": period,
        "points": points,
        "change_pct": change,
    })


# ----------------------------------------------------------------------
# NEW: Shared portfolio account
# ----------------------------------------------------------------------
class DepositRequest(BaseModel):
    amount: float
    note: Optional[str] = None

@app.get("/api/portfolio/summary")
async def portfolio_summary():
    return JSONResponse(content=convert_numpy(trades_module.get_portfolio_summary()))

@app.post("/api/portfolio/deposit")
async def portfolio_deposit(req: DepositRequest):
    account, error = trades_module.deposit_funds(req.amount, note=req.note)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return JSONResponse(content={
        "status": "deposited", "cash_balance": round(account.cash_balance, 2),
        "total_deposited": round(account.total_deposited, 2),
    })

@app.get("/api/trades/report/daily")
async def trades_report_daily(days: int = 30):
    return JSONResponse(content=convert_numpy(trades_module.get_daily_trade_report(days)))

@app.get("/api/trades/report/weekly")
async def trades_report_weekly(weeks: int = 12):
    return JSONResponse(content=convert_numpy(trades_module.get_weekly_trade_report(weeks)))

# ----------------------------------------------------------------------
# NEW: Paper trading endpoints
# ----------------------------------------------------------------------
@app.get("/api/trades")
async def list_trades(status: str = "all"):
    db = SessionLocal()
    try:
        q = db.query(PaperTrade)
        if status == "open":
            q = q.filter(PaperTrade.status == "OPEN")
        elif status == "closed":
            q = q.filter(PaperTrade.status == "CLOSED")
        try:
            from models import init_db
            init_db(engine)
        except Exception:
            pass
        rows = q.order_by(PaperTrade.entry_date.desc()).all()
        return JSONResponse(content=convert_numpy([{
            "trade_id": t.trade_id, "prediction_id": t.prediction_id, "symbol": t.symbol,
            "capital_allocated": t.capital_allocated, "entry_price": t.entry_price, "quantity": t.quantity,
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "target": t.target, "stop_loss": t.stop_loss, "status": t.status,
            "current_price": t.current_price, "exit_price": t.exit_price,
            "exit_date": t.exit_date.isoformat() if t.exit_date else None, "exit_reason": t.exit_reason,
            "pnl_amount": t.pnl_amount, "pnl_pct": t.pnl_pct,
            "last_marked_at": t.last_marked_at.isoformat() if t.last_marked_at else None,
        } for t in rows]))
    finally:
        db.close()

@app.get("/api/trades/summary")
async def trades_summary():
    return JSONResponse(content=convert_numpy(trades_module.get_trade_summary()))

@app.post("/api/trades/mark-to-market")
async def trigger_mark_to_market(background_tasks: BackgroundTasks):
    def run():
        try:
            trades_module.mark_all_open_trades()
        except Exception as e:
            logger.error(f"Mark-to-market sweep failed: {e}")
    background_tasks.add_task(run)
    return JSONResponse(content={"status": "mark-to-market sweep triggered"})

@app.post("/api/trades/{trade_id}/close")
async def close_trade(trade_id: str):
    trade = trades_module.close_trade_manually(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found or already closed")
    return JSONResponse(content={"status": "closed", "trade_id": trade.trade_id, "exit_price": trade.exit_price, "pnl_pct": trade.pnl_pct})

# ----------------------------------------------------------------------
# NEW: /api/lock/clear — same effect as DELETE /lock (calls request_abort()
# too), but under /api/ in case the gateway only proxies that prefix and
# not bare /training/lock. POST since that's the one method already
# proven to work through the gateway (matches /api/train).
# ----------------------------------------------------------------------
@app.post("/api/lock/clear")
async def api_clear_lock():
    logger.info("POST /api/lock/clear called")
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.info("Lock file removed via /api/lock/clear, calling request_abort()")
        request_abort()
        return JSONResponse(content={"status": "Lock cleared and abort requested"})
    return JSONResponse(content={"status": "No lock found"})

@app.get("/api/lock-status")
async def api_lock_status():
    return JSONResponse(content={"training_in_progress": is_training_running()})

@app.get("/api/train/progress")
@app.get("/train/status")
async def api_train_progress():
    """Polled by the animated Training tab view — current stage
    (loading_data/data_loaded/splitting/fitting_model/evaluating/
    saving_model/done/aborted/idle) plus stage-specific detail, e.g. the
    sample of symbols in the training set once data's loaded."""
    from train import get_training_progress
    data = convert_numpy(get_training_progress())
    if not isinstance(data, dict):
        data = {"stage": "idle", "detail": {}}
    data["is_running"] = is_training_running()
    # Derive percent for UIs that expect a simple 0–100 bar
    stage = str(data.get("stage") or "idle")
    pct_map = {
        "idle": 0,
        "loading_data": 15,
        "data_loaded": 30,
        "building_features": 40,
        "splitting": 45,
        "walk_forward": 55,
        "fitting_model": 70,
        "calibrating": 80,
        "evaluating": 85,
        "saving_model": 92,
        "done": 100,
        "aborted": 0,
        "error": 0,
        "Failed": 0,
        "Completed": 100,
    }
    if "percent" not in data or data.get("percent") is None:
        data["percent"] = pct_map.get(stage, 10 if data.get("is_running") else 0)
    if data.get("is_running") and stage in ("idle", None, ""):
        data["stage"] = "loading_data"
        data["percent"] = max(int(data.get("percent") or 0), 5)
    return JSONResponse(content=data)


@app.post("/train/clear-lock")
async def train_clear_lock_alias():
    """Alias for clients that call /train/clear-lock."""
    return await api_clear_lock()


# ----------------------------------------------------------------------
# Aliases for frontend (no /api prefix)
# ----------------------------------------------------------------------
@app.get("/model-status")
async def model_status():
    return JSONResponse(content=get_training_status())

@app.get("/training-score/{symbol}")
async def training_score(symbol: str):
    """Per-symbol training intelligence + global live win-rate for closed-loop thresholds."""
    from scanner import TrainingScanner
    scanner = TrainingScanner(SessionLocal, MODEL_STORE_PATH)
    score = scanner.score_symbol(symbol)
    live_wr = None
    try:
        st = get_training_status()
        live_wr = st.get("live_win_rate") or st.get("win_rate") or st.get("overall_win_rate")
        live_n = st.get("live_win_rate_n") or 0
    except Exception:
        pass
    if not score:
        return {
            "symbol": (symbol or "").upper(),
            "training_score": None,
            "t1_success_probability": None,
            "t5_success_probability": None,
            "model_success_probability": None,
            "available": False,
            "live_win_rate": live_wr,
            "live_win_rate_n": live_n,
            "message": "No training score for this symbol yet — add to Training from a scan first",
        }
    if isinstance(score, dict):
        score = dict(score)
        score.setdefault("available", True)
        # Normalize alternate keys from scanner
        if score.get("training_score") is None and score.get("score") is not None:
            score["training_score"] = score.get("score")
        if score.get("t1_success_probability") is None:
            score["t1_success_probability"] = score.get("t1_prob") or score.get("t1_success_rate")
        if score.get("t5_success_probability") is None:
            score["t5_success_probability"] = score.get("t5_prob") or score.get("t5_success_rate")
        if live_wr is not None and score.get("live_win_rate") is None:
            score["live_win_rate"] = live_wr
        score["live_win_rate_n"] = live_n
        score.setdefault("symbol", (symbol or "").upper())
    return score

@app.post("/train")
async def trigger_train(background_tasks: BackgroundTasks):
    return await api_trigger_training(background_tasks)

# ----------------------------------------------------------------------
# Run with uvicorn
# ----------------------------------------------------------------------

@app.get("/api/rl/explore-info")
async def api_rl_explore_info():
    """Research notes + bandit snapshot (not live trading)."""
    try:
        from rl_explore import explore_note, bandit
        return JSONResponse(content={**explore_note(), "bandit": bandit.snapshot()})
    except Exception as e:
        return JSONResponse(content={"error": str(e)[:200]}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get('PORT', 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)

# ----------------------------------------------------------------------
# NEW: Additional trade management endpoints (clear-backup, backups, add)
# ----------------------------------------------------------------------
@app.post("/api/trades/clear-backup")
def api_clear_trades_backup():
    try:
        from trades import clear_all_with_backup
        result = clear_all_with_backup()
        status = 200 if result.get("ok") else 500
        return JSONResponse(content=result, status_code=status)
    except Exception as e:
        logger.exception("clear-backup failed")
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/trades/backups")
def api_list_trade_backups():
    try:
        from trades import list_trade_backups
        return list_trade_backups()
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/trades/backups/{filename}")
def api_get_trade_backup(filename: str):
    """Return parsed backup JSON for the modal viewer."""
    try:
        from trades import get_trade_backup
        data, err = get_trade_backup(filename)
        if err:
            return JSONResponse(content={"ok": False, "error": err}, status_code=404 if "not found" in err.lower() else 400)
        return JSONResponse(content={"ok": True, "filename": filename, "backup": data})
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/trades/manual")
async def api_open_manual_trade(request: Request):
    """Manual paper trade from Trade page (symbol + qty/capital). Always logs AI warning client-side."""
    try:
        body = await request.json()
        symbol = (body.get("symbol") or "").strip()
        quantity = body.get("quantity")
        price = body.get("price")
        capital = body.get("capital")
        note = body.get("note")
        trade, was_new, err = trades_module.open_manual_trade(
            symbol=symbol,
            quantity=float(quantity or 0),
            price=float(price) if price is not None else None,
            capital=float(capital) if capital is not None else None,
            note=note,
        )
        if trade is None:
            detail = err or "failed"
            status = 400
            if "not enough cash" in detail.lower() or "not enough cash balance" in detail.lower():
                status = 402
            raise HTTPException(status_code=status, detail=detail)
        return JSONResponse(content={
            "ok": True,
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "quantity": trade.quantity,
            "entry_price": trade.entry_price,
            "capital_allocated": trade.capital_allocated,
            "was_new": was_new,
            "ai_warning": (
                "Manual trades bypass the AI decision engine. "
                "Position size and timing are not validated by Stockky scores."
            ),
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("manual trade failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trades/{trade_id}/add")
async def api_add_to_trade(trade_id: str, request: Request):
    """Groww-style add more quantity to an open paper position."""
    try:
        body = await request.json()
        qty = float(body.get("quantity") or 0)
        price = body.get("price")
        if qty <= 0:
            raise HTTPException(status_code=400, detail="quantity must be > 0")
        from trades import add_quantity_to_trade
        # Prefer real DB session if available
        db = None
        try:
            db = SessionLocal()
        except Exception:
            db = None
        result = add_quantity_to_trade(db, trade_id, qty, price)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "add failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)