"""
Stockky Analysis Intelligence Service
Merges: technical + fundamental + news + event + sentiment

Mount strategy: each sub-app is loaded via importlib with a unique module name
so same-named helpers (e.g. utils.py) across folders cannot collide on sys.modules.
"""
import os
import sys
import logging
import importlib.util
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analysis-intelligence-service")

app = FastAPI(
    title="Stockky Analysis Intelligence Service",
    version="1.0.1",
    description="Merged technical, fundamental, news, event, sentiment analysis",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _load_subapp(folder: str, module_alias: str):
    """Load folder/main.py as an isolated module name; temporarily prefer that folder on path."""
    folder_path = os.path.join(BASE, folder)
    main_py = os.path.join(folder_path, "main.py")
    if not os.path.isfile(main_py):
        raise FileNotFoundError(main_py)
    # Prefer this subdir for its local imports (peers.py, indianapi_fallback.py, …)
    # without permanently stacking every sibling (reduces cross-folder name collisions).
    prev = sys.path[:]
    try:
        sys.path = [folder_path] + [p for p in prev if p not in (
            os.path.join(BASE, d) for d in ("technical", "fundamental", "news", "event", "sentiment")
        )]
        spec = importlib.util.spec_from_file_location(module_alias, main_py)
        mod = importlib.util.module_from_spec(spec)
        # Register under unique name so importlib caches do not clash
        sys.modules[module_alias] = mod
        assert spec.loader is not None
        old_cwd = os.getcwd()
        try:
            os.chdir(folder_path)
            spec.loader.exec_module(mod)
        finally:
            os.chdir(old_cwd)
        return mod
    finally:
        sys.path = prev


_MOUNTS = (
    ("technical", "ai_technical_main", "/technical", "technical analysis"),
    ("fundamental", "ai_fundamental_main", "/fundamental", "fundamental analysis"),
    ("news", "ai_news_main", "/news", "news intelligence"),
    ("event", "ai_event_main", "/event", "event tracker"),
    ("sentiment", "ai_sentiment_main", "/sentiment", "market sentiment"),
)

for folder, alias, mount_path, label in _MOUNTS:
    try:
        mod = _load_subapp(folder, alias)
        sub = getattr(mod, "app", None)
        if sub is None:
            raise RuntimeError(f"{folder}.main has no FastAPI app")
        app.mount(mount_path, sub)
        logger.info("Mounted %s (%s)", mount_path, label)
    except Exception as e:
        logger.warning("Could not mount %s: %s", mount_path, e)


@app.get("/")
def root():
    return {
        "service": "Stockky Analysis Intelligence Service",
        "version": "1.0.1",
        "status": "running",
        "modules": ["technical", "fundamental", "news", "event", "sentiment"],
        "endpoints": {
            "/technical/analyze/{symbol}": "Technical analysis",
            "/fundamental/analyze/{symbol}": "Fundamental analysis",
            "/news/analyze/{symbol}": "News sentiment",
            "/event/events/{symbol}": "Event tracker",
            "/sentiment/...": "Market sentiment",
            "/health": "Health check",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "analysis-intelligence-service", "mount_isolation": "importlib"}
