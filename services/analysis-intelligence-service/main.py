"""
Stockky Analysis Intelligence Service
Merges: technical + fundamental + news + event + sentiment

Mount strategy: each sub-app is loaded via importlib with a unique module name
so same-named helpers (e.g. utils.py) across folders cannot collide on sys.modules.

Mount failures are recorded in MOUNT_STATUS and exposed on GET /health so a
broken submodule is visible immediately instead of silent 404s in production.
"""
import os
import sys
import logging
import importlib.util
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analysis-intelligence-service")

app = FastAPI(
    title="Stockky Analysis Intelligence Service",
    version="1.0.2",
    description="Merged technical, fundamental, news, event, sentiment analysis",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# path_prefix -> {ok, error?, label}
MOUNT_STATUS: dict = {}


_SUBAPP_DIRS = frozenset(
    os.path.normcase(os.path.join(BASE, d))
    for d in ("technical", "fundamental", "news", "event", "sentiment")
)


def _hidden_while_loading(entry) -> bool:
    """True for sys.path entries a sub-app must NOT see while it is being imported.

    * '' (the implicit cwd): the loader chdir()s into the sub-app folder, so '' would
      silently resolve to a folder the loader did not choose to expose.
    * any sibling sub-app folder (technical/fundamental/news/event/sentiment), compared
      after abspath/normcase so relative or trailing-slash spellings are caught too.
    Non-string entries are left alone (never raise from path hygiene).
    """
    if not isinstance(entry, str):
        return False
    if entry == "":
        return True
    try:
        return os.path.normcase(os.path.abspath(entry)) in _SUBAPP_DIRS
    except Exception:  # pragma: no cover - abspath only fails on exotic embedded NULs
        return entry in _SUBAPP_DIRS


def _load_subapp(folder: str, module_alias: str):
    """Load folder/main.py as an isolated module name; temporarily prefer that folder on path."""
    folder_path = os.path.join(BASE, folder)
    main_py = os.path.join(folder_path, "main.py")
    if not os.path.isfile(main_py):
        raise FileNotFoundError(main_py)
    prev = sys.path[:]
    try:
        # Computed BEFORE os.chdir below, so relative entries resolve against the cwd
        # the process was started with.
        sys.path = [folder_path] + [p for p in prev if not _hidden_while_loading(p)]
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

for folder, alias, prefix, label in _MOUNTS:
    try:
        mod = _load_subapp(folder, alias)
        sub = getattr(mod, "app", None)
        if sub is None:
            raise RuntimeError(f"{folder}/main.py has no 'app' attribute")
        app.mount(prefix, sub)
        MOUNT_STATUS[prefix] = {"ok": True, "label": label, "folder": folder}
        logger.info("✅ Mounted %s (%s)", prefix, label)
    except Exception as e:
        MOUNT_STATUS[prefix] = {
            "ok": False,
            "label": label,
            "folder": folder,
            "error": str(e)[:300],
        }
        logger.error("❌ Failed to mount %s (%s): %s", prefix, label, e)


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
        "service": "Stockky Analysis Intelligence Service",
        "version": "1.0.2",
        "status": "running" if summary["all_ok"] else "degraded",
        "modules": ["technical", "fundamental", "news", "event", "sentiment"],
        **summary,
    }


@app.get("/health")
def health():
    """
    Health includes mount map so ops/UI can see which sub-apps failed to import.
    status: ok | degraded (some mounts failed) | error (no mounts)
    """
    summary = _mount_summary()
    if not summary["mounted"]:
        status = "error"
    elif summary["failed"]:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "service": "analysis-intelligence-service",
        **summary,
    }
