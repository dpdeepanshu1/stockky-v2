"""IPO-scan and Surprise-scan status / stop routes (progress files and stop flags live in ipo_scanner / surprise_premarket, imported inside the handlers). Moved out of main.py in E49, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

from fastapi import APIRouter

ipo_status_router = APIRouter()


@ipo_status_router.get("/ipo/status")
@ipo_status_router.get("/surprise/ipo/status")
async def api_ipo_scan_status():
    try:
        from ipo_scanner import get_ipo_scan_progress
        return get_ipo_scan_progress()
    except Exception as e:
        return {"status": "error", "message": str(e)[:160]}


ipo_stop_router = APIRouter()


@ipo_stop_router.post("/ipo/stop")
@ipo_stop_router.post("/surprise/ipo/stop")
async def api_ipo_stop():
    """Stop an in-progress IPO scan after the current symbol — IPO Checker
    tab's Stop button. Mirrors /api/data-feed/stop's behaviour: partial
    results already analyzed are kept, not discarded."""
    try:
        from ipo_scanner import request_ipo_stop
        request_ipo_stop()
        return {"ok": True, "message": "Stop requested — will halt after the current symbol."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


premarket_status_router = APIRouter()


@premarket_status_router.get("/surprise/premarket/status")
@premarket_status_router.get("/api/surprise/premarket/status")
async def api_surprise_premarket_status():
    """Premarket progress from gateway-local progress file."""
    try:
        from surprise_premarket import get_premarket_progress
        return get_premarket_progress()
    except Exception as e:
        return {
            "is_running": False,
            "stage": "error",
            "percent": 0,
            "error": str(e)[:160],
            "message": str(e)[:160],
        }


surprise_stop_router = APIRouter()


@surprise_stop_router.post("/surprise/stop")
@surprise_stop_router.post("/api/surprise/stop")
async def api_surprise_stop():
    """Stop button for the Surprise tab — halts the premarket baseline job
    (between symbols) AND the live surprise scan's waterfall-fill loop, since
    both share the same stop flag (surprise_premarket.premarket_stop_requested).
    Whatever was already computed/fetched before Stop is kept, not discarded."""
    try:
        from surprise_premarket import request_premarket_stop
        request_premarket_stop()
        return {"ok": True, "message": "Stop requested — will halt after the current symbol/chunk."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
