"""
Stockky Notification Scheduler Service
Merges: notification-service + scheduler-service
"""
import os
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = os.path.dirname(os.path.abspath(__file__))
# Sibling mounts: prefer isolated folder order (notification then scheduler)
sys.path.insert(0, os.path.join(BASE, "scheduler"))
sys.path.insert(0, os.path.join(BASE, "notification"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-scheduler-service")

# 2026-10-04 (log noise): httpx logs every outbound request at INFO and uvicorn logs every /health probe
# (container healthchecks + the browser's api.ping()) -- together most of the log volume. httpx -> WARNING
# (HTTPX_LOG_LEVEL=INFO restores it); successful /health access lines are dropped (ACCESS_LOG_HEALTH=1 restores
# them; a non-200 /health still logs).
def _quiet_noisy_loggers() -> None:
    import re as _re
    import logging as _lg
    import os as _os
    _lvl = getattr(_lg, (_os.getenv("HTTPX_LOG_LEVEL") or "WARNING").strip().upper(), _lg.WARNING)
    if not isinstance(_lvl, int):
        _lvl = _lg.WARNING
    for _n in ("httpx", "httpcore"):
        _lg.getLogger(_n).setLevel(_lvl)
    if (_os.getenv("ACCESS_LOG_HEALTH") or "").strip().lower() in ("1", "true", "yes"):
        return
    _pat = _re.compile(r'"GET \S*/health(\?\S*)? HTTP/[\d.]+" 200')

    class _NoHealthAccess(_lg.Filter):
        def filter(self, record):  # noqa: A003
            try:
                return not _pat.search(record.getMessage())
            except Exception:  # noqa: BLE001
                return True

    _acc = _lg.getLogger("uvicorn.access")
    if not any(getattr(f, "_stockky_health_filter", False) for f in _acc.filters):
        _flt = _NoHealthAccess()
        _flt._stockky_health_filter = True
        _acc.addFilter(_flt)


_quiet_noisy_loggers()

app = FastAPI(
    title="Stockky Notification Scheduler Service",
    version="1.0.0",
    description="Merged notification and scheduler"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

try:
    from notification.main import app as notif_app
    app.mount("/notification", notif_app)
    logger.info("Mounted notification")
except Exception as e:
    logger.warning(f"Could not mount notification: {e}")

try:
    from scheduler.main import app as sched_app
    app.mount("/scheduler", sched_app)
    logger.info("Mounted scheduler")
except Exception as e:
    logger.warning(f"Could not mount scheduler: {e}")

@app.get("/")
def root():
    return {
        "service": "Stockky Notification Scheduler Service",
        "version": "1.0.0",
        "status": "running",
        "modules": ["notification", "scheduler"]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "notification-scheduler-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(((os.getenv("PORT") or "").strip() or 8000)))
