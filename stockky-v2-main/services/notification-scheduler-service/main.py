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
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
