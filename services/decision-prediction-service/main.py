"""
Stockky Decision Prediction Service - Final
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

app = FastAPI(title="Stockky Decision Prediction Service", version="1.0.6")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- Decision ----------
try:
    sys.path.insert(0, os.path.join(BASE, "decision"))
    from decision.main import app as dec_app
    app.mount("/decision", dec_app)
    logger.info("✅ Mounted /decision")
except Exception as e:
    logger.error(f"❌ Decision: {e}")

# ---------- Training ----------
try:
    sys.path.insert(0, os.path.join(BASE, "training"))
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("✅ Mounted /training")
except Exception as e:
    logger.error(f"❌ Training: {e}")

# ---------- Prediction (fully isolated) ----------
try:
    # Completely clean path for prediction
    clean_path = [p for p in sys.path if "training" not in p and "decision" not in p]
    prediction_dir = os.path.join(BASE, "prediction")
    clean_path.insert(0, prediction_dir)
    sys.path = clean_path

    # Load prediction/main.py by absolute path
    pred_file = os.path.join(prediction_dir, "main.py")
    spec = importlib.util.spec_from_file_location("pred_mod", pred_file)
    pred_mod = importlib.util.module_from_spec(spec)

    # Execute with prediction folder as current directory
    old_cwd = os.getcwd()
    os.chdir(prediction_dir)
    spec.loader.exec_module(pred_mod)
    os.chdir(old_cwd)

    @app.get("/prediction/")
    def prediction_root():
        return {"service": "prediction-service", "status": "running"}

    @app.get("/prediction/health")
    def prediction_health():
        return {"status": "ok", "service": "prediction-service"}

    @app.get("/prediction/predict/{symbol}")
    def prediction_predict(symbol: str):
        return pred_mod.predict(symbol)

    logger.info("✅ Prediction endpoints ready")
except Exception as e:
    logger.error(f"❌ Prediction failed: {e}")
    import traceback
    logger.error(traceback.format_exc())


@app.get("/")
def root():
    return {"service": "Stockky Decision Prediction Service", "version": "1.0.6", "status": "running"}

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}