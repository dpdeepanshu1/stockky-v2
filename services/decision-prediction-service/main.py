"""
Stockky Decision Prediction Service
Merges: decision + training + prediction

Mount failures are recorded in MOUNT_STATUS and exposed on GET /health.
sys.path is restored after each sub-app load to avoid utils/models shadowing.
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
    version="1.1.0",
    description="Merged decision, prediction, training",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MOUNT_STATUS: dict = {}


def _load_folder_app(folder: str, module_alias: str, attr: str = "app"):
    """Load folder/main.py or folder/app.py in isolation; restore sys.path after."""
    folder_path = os.path.join(BASE, folder)
    candidates = [
        os.path.join(folder_path, "main.py"),
        os.path.join(folder_path, "app.py"),
    ]
    main_py = next((p for p in candidates if os.path.isfile(p)), None)
    if not main_py:
        raise FileNotFoundError(f"No main.py/app.py in {folder_path}")
    prev = sys.path[:]
    try:
        # Prefer this folder only for the duration of load
        sys.path = [folder_path] + [p for p in prev if p not in (
            os.path.join(BASE, d) for d in ("decision", "training", "prediction")
        )]
        spec = importlib.util.spec_from_file_location(module_alias, main_py)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_alias] = mod
        assert spec.loader is not None
        old_cwd = os.getcwd()
        try:
            os.chdir(folder_path)
            spec.loader.exec_module(mod)
        finally:
            os.chdir(old_cwd)
        sub = getattr(mod, attr, None)
        if sub is None and attr != "app":
            sub = getattr(mod, "app", None)
        if sub is None:
            raise RuntimeError(f"{folder} has no '{attr}' FastAPI app")
        return mod, sub
    finally:
        sys.path = prev


# ---------- Decision ----------
try:
    _mod, dec_app = _load_folder_app("decision", "dp_decision_main", "app")
    app.mount("/decision", dec_app)
    MOUNT_STATUS["/decision"] = {"ok": True, "label": "decision engine", "folder": "decision"}
    logger.info("✅ Mounted /decision")
except Exception as e:
    MOUNT_STATUS["/decision"] = {
        "ok": False,
        "label": "decision engine",
        "folder": "decision",
        "error": str(e)[:300],
    }
    logger.error("❌ Decision failed: %s", e)

# ---------- Training ----------
try:
    _mod, train_app = _load_folder_app("training", "dp_training_app", "app")
    app.mount("/training", train_app)
    MOUNT_STATUS["/training"] = {"ok": True, "label": "training", "folder": "training"}
    logger.info("✅ Mounted /training")
except Exception as e:
    MOUNT_STATUS["/training"] = {
        "ok": False,
        "label": "training",
        "folder": "training",
        "error": str(e)[:300],
    }
    logger.error("❌ Training failed: %s", e)

# ---------- Prediction (isolated mount like analysis service) ----------
try:
    pred_mod, pred_app = _load_folder_app("prediction", "dp_prediction_main", "app")
    app.mount("/prediction", pred_app)
    MOUNT_STATUS["/prediction"] = {"ok": True, "label": "prediction", "folder": "prediction"}
    logger.info("✅ Mounted /prediction")
except Exception as e:
    # Fallback: register routes manually if sub-app has predict() but no app
    try:
        pred_dir = os.path.join(BASE, "prediction")
        prev = sys.path[:]
        sys.path = [pred_dir] + prev
        pred_file = os.path.join(pred_dir, "main.py")
        spec = importlib.util.spec_from_file_location("dp_pred_fallback", pred_file)
        pred_mod = importlib.util.module_from_spec(spec)
        sys.modules["dp_pred_fallback"] = pred_mod
        old_cwd = os.getcwd()
        os.chdir(pred_dir)
        assert spec.loader is not None
        spec.loader.exec_module(pred_mod)
        os.chdir(old_cwd)
        sys.path = prev

        @app.get("/prediction/")
        def prediction_root():
            model_loaded = getattr(pred_mod, "_model", None) is not None
            return {"service": "prediction-service", "status": "running", "model_loaded": model_loaded}

        @app.get("/prediction/health")
        def prediction_health():
            return {"status": "ok", "service": "prediction-service"}

        @app.get("/prediction/predict/{symbol}")
        def prediction_predict(symbol: str):
            try:
                return pred_mod.predict(symbol)
            except Exception as pe:
                return {"symbol": symbol, "error": str(pe)[:200], "prediction_score": None}

        MOUNT_STATUS["/prediction"] = {
            "ok": True,
            "label": "prediction",
            "folder": "prediction",
            "mode": "route_fallback",
        }
        logger.info("✅ Prediction endpoints ready (route fallback)")
    except Exception as e2:
        MOUNT_STATUS["/prediction"] = {
            "ok": False,
            "label": "prediction",
            "folder": "prediction",
            "error": str(e2)[:300],
        }
        logger.error("❌ Prediction failed: %s", e2)


def _mount_summary() -> dict:
    mounted = [p for p, s in MOUNT_STATUS.items() if s.get("ok")]
    failed = [p for p, s in MOUNT_STATUS.items() if not s.get("ok")]
    return {
        "mounts": MOUNT_STATUS,
        "mounted": mounted,
        "failed": failed,
        "all_ok": len(failed) == 0 and len(mounted) > 0,
    }


@app.get("/")
def root():
    summary = _mount_summary()
    return {
        "service": "Stockky Decision Prediction Service",
        "version": "1.1.0",
        "status": "running" if summary["all_ok"] else "degraded",
        "modules": ["decision", "prediction", "training"],
        **summary,
    }


@app.get("/health")
def health():
    summary = _mount_summary()
    if not summary["mounted"]:
        status = "error"
    elif summary["failed"]:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "service": "decision-prediction-service",
        **summary,
    }
