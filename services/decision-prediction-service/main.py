"""
Stockky Decision Prediction Service - Reliable version
"""
import os
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-prediction-service")

app = FastAPI(
    title="Stockky Decision Prediction Service",
    version="1.0.4",
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
    sys.path = [p for p in sys.path if "decision" not in p]
    sys.path.insert(0, os.path.join(BASE, "training"))
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("✅ Mounted /training")
except Exception as e:
    logger.error(f"❌ Training failed: {e}")

# ---------- Prediction (direct registration - most reliable) ----------
try:
    prediction_dir = os.path.join(BASE, "prediction")
    sys.path.insert(0, prediction_dir)
    os.chdir(prediction_dir)

    # Import the actual predict function
    from main import predict as pred_func
    from main import health as pred_health
    from main import root as pred_root

    os.chdir(BASE)

    @app.get("/prediction/")
    def prediction_root():
        return {"service": "prediction-service", "status": "running", "version": "direct"}

    @app.get("/prediction/health")
    def prediction_health():
        return {"status": "ok", "service": "prediction-service"}

    @app.get("/prediction/predict/{symbol}")
    def prediction_predict(symbol: str):
        return pred_func(symbol)

    logger.info("✅ Prediction endpoints registered directly")
except Exception as e:
    logger.error(f"❌ Prediction registration failed: {e}")
    import traceback
    logger.error(traceback.format_exc())


@app.get("/")
def root():
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.4",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}