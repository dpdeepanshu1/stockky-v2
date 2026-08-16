"""
Stockky Decision Prediction Service - Final reliable version
"""
import os
import sys
import logging
import importlib.util
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-prediction-service")

app = FastAPI(
    title="Stockky Decision Prediction Service",
    version="1.0.5",
    description="Merged decision + prediction + training"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- Decision ----------
try:
    sys.path.insert(0, os.path.join(BASE, "decision"))
    from decision.main import app as dec_app
    app.mount("/decision", dec_app)
    logger.info("✅ Mounted /decision")
except Exception as e:
    logger.error(f"❌ Decision failed: {e}")

# ---------- Training ----------
try:
    sys.path.insert(0, os.path.join(BASE, "training"))
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("✅ Mounted /training")
except Exception as e:
    logger.error(f"❌ Training failed: {e}")

# ---------- Prediction (using importlib - no circular import) ----------
try:
    prediction_path = os.path.join(BASE, "prediction", "main.py")
    spec = importlib.util.spec_from_file_location("prediction_service", prediction_path)
    pred_module = importlib.util.module_from_spec(spec)

    # Temporarily add prediction folder to path for its internal imports
    sys.path.insert(0, os.path.join(BASE, "prediction"))
    spec.loader.exec_module(pred_module)

    # Register the routes
    @app.get("/prediction/")
    def prediction_root():
        return {"service": "prediction-service", "status": "running", "method": "importlib"}

    @app.get("/prediction/health")
    def prediction_health():
        return {"status": "ok", "service": "prediction-service"}

    @app.get("/prediction/predict/{symbol}")
    def prediction_predict(symbol: str):
        return pred_module.predict(symbol)

    logger.info("✅ Prediction endpoints registered via importlib")
except Exception as e:
    logger.error(f"❌ Prediction failed: {e}")
    import traceback
    logger.error(traceback.format_exc())


@app.get("/")
def root():
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.5",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}