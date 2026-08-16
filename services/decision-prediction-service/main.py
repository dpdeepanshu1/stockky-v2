"""
Stockky Decision Prediction Service
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
    version="1.0.8",
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

# ---------- Prediction (fully isolated) ----------
try:
    prediction_dir = os.path.join(BASE, "prediction")

    # Keep only prediction folder at the front of sys.path
    original_path = sys.path[:]
    sys.path = [prediction_dir] + [p for p in original_path if "training" not in p and "decision" not in p]

    pred_file = os.path.join(prediction_dir, "main.py")
    spec = importlib.util.spec_from_file_location("pred_service", pred_file)
    pred_mod = importlib.util.module_from_spec(spec)

    old_cwd = os.getcwd()
    os.chdir(prediction_dir)
    spec.loader.exec_module(pred_mod)
    os.chdir(old_cwd)

    # Restore original path
    sys.path = original_path

    @app.get("/prediction/")
    def prediction_root():
        model_loaded = getattr(pred_mod, "_model", None) is not None
        return {
            "service": "prediction-service",
            "status": "running",
            "model_loaded": model_loaded
        }

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
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.8",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}