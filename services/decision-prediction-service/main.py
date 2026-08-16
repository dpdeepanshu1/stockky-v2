"""
Stockky Decision Prediction Service
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
    version="1.0.3",
    description="Merged decision + prediction + training"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- 1. Decision ----------
try:
    sys.path.insert(0, os.path.join(BASE, "decision"))
    from decision.main import app as dec_app
    app.mount("/decision", dec_app)
    logger.info("✅ Mounted /decision")
except Exception as e:
    logger.error(f"❌ Decision failed: {e}")

# ---------- 2. Training ----------
try:
    # Clear previous path pollution
    sys.path = [p for p in sys.path if "prediction" not in p and "decision" not in p]
    sys.path.insert(0, os.path.join(BASE, "training"))
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("✅ Mounted /training")
except Exception as e:
    logger.error(f"❌ Training failed: {e}")

# ---------- 3. Prediction (isolated) ----------
try:
    # Completely isolate prediction path
    sys.path = [p for p in sys.path if "training" not in p and "decision" not in p]
    prediction_path = os.path.join(BASE, "prediction")
    sys.path.insert(0, prediction_path)

    # Change directory so relative imports work correctly
    old_cwd = os.getcwd()
    os.chdir(prediction_path)

    import main as pred_main
    app.mount("/prediction", pred_main.app)

    os.chdir(old_cwd)
    logger.info("✅ Mounted /prediction")
except Exception as e:
    logger.error(f"❌ Prediction mount failed: {e}")


@app.get("/")
def root():
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.3",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}