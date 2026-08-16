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
sys.path.insert(0, os.path.join(BASE, "decision"))
sys.path.insert(0, os.path.join(BASE, "prediction"))
sys.path.insert(0, os.path.join(BASE, "training"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-prediction-service")

app = FastAPI(
    title="Stockky Decision Prediction Service",
    version="1.0.0",
    description="Merged decision engine, prediction, and training"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

try:
    from decision.main import app as dec_app
    app.mount("/decision", dec_app)
    logger.info("Mounted decision engine")
except Exception as e:
    logger.warning(f"Could not mount decision: {e}")

try:
    from prediction.main import app as pred_app
    app.mount("/prediction", pred_app)
    logger.info("Mounted prediction")
except Exception as e:
    logger.warning(f"Could not mount prediction: {e}")

try:
    from training.app import app as train_app
    app.mount("/training", train_app)
    logger.info("Mounted training")
except Exception as e:
    logger.warning(f"Could not mount training: {e}")

@app.get("/")
def root():
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.0.0",
        "status": "running",
        "modules": ["decision", "prediction", "training"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-prediction-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8004)))
