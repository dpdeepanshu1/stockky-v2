"""
Stockky Decision Prediction Service
Merges: decision-engine + prediction-service + training-service
"""
import os
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "decision"))
sys.path.insert(0, os.path.join(BASE, "prediction"))
sys.path.insert(0, os.path.join(BASE, "training"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-prediction-service")

app = FastAPI(
    title="Stockky Decision Prediction Service",
    version="1.0.2",
    description="Merged decision + prediction + training"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- 1. Mount Decision ----------
try:
    from decision.main import app as dec_app
    app.mount("/decision", dec_app)
    logger.info("✅ Mounted /decision")
except Exception as e:
    logger.error(f"❌ Decision mount failed: {e}")

# ---------- 2. Mount Training ----------
try:
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("✅ Mounted /training")
except Exception as e:
    logger.error(f"❌ Training mount failed: {e}")

# ---------- 3. Manually include Prediction routes (most reliable) ----------
try:
    # Import the prediction app and its key functions
    import prediction.main as pred_module

    # Mount the whole prediction app under /prediction
    app.mount("/prediction", pred_module.app)
    logger.info("✅ Mounted /prediction successfully")

except Exception as e:
    logger.error(f"❌ Prediction mount failed: {e}")

    # Fallback: register the critical endpoint manually
    try:
        from prediction.main import predict as prediction_predict_func
        from prediction.main import health as prediction_health_func

        @app.get("/prediction/predict/{symbol}")
        def prediction_predict(symbol: str):
            return prediction_predict_func(symbol)

        @app.get("/prediction/health")
        def prediction_health():
            return {"status": "ok", "service": "prediction-service (fallback)"}

        @app.get("/prediction/")
        def prediction_root():
            return {"service": "prediction-service", "status": "running (fallback)"}

        logger.info("✅ Prediction endpoints registered via fallback")
    except Exception as e2:
        logger.error(f"❌ Prediction fallback also failed: {e2}")


@app.get("/")
def root():
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.2",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}