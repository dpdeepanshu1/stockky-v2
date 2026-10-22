"""Price-alert routes (moved out of main.py in E48, code unchanged). Storage lives in data_feed (imported inside each handler on purpose: tests patch
`data_feed.list_price_alerts` / `add_price_alert` / `delete_price_alert`). `POST /api/price-alerts/evaluate` stays in main.py."""
from __future__ import annotations

import asyncio
import math

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/api/price-alerts")
@router.get("/price-alerts")
async def api_list_price_alerts():
    from data_feed import list_price_alerts
    alerts = list_price_alerts()
    return {"ok": True, "alerts": alerts, "count": len(alerts)}


@router.post("/api/price-alerts")
@router.post("/price-alerts")
async def api_add_price_alert(request: Request):
    """Body: {symbol, target_price, direction: above|below, note?}"""
    from data_feed import add_price_alert
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    if not isinstance(body, dict):
        # A truthy non-object (e.g. [1]) used to crash on `.get` with a bare 500.
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    sym = str(body.get("symbol") or "").strip()
    try:
        target = float(body.get("target_price") or body.get("target") or 0)
    except (TypeError, ValueError):
        target = 0
    if not math.isfinite(target):
        # json.loads accepts NaN / Infinity, and `nan <= 0` is False, so they used to pass validation.
        target = 0
    direction = str(body.get("direction") or "above").lower()
    note = str(body.get("note") or "")
    if not sym or target <= 0:
        raise HTTPException(status_code=400, detail="symbol and target_price required")
    entry = await asyncio.to_thread(add_price_alert, sym, target, direction=direction, note=note)  # group361: off the event loop
    return {"ok": True, "alert": entry}


@router.delete("/api/price-alerts/{alert_id}")
@router.delete("/price-alerts/{alert_id}")
async def api_delete_price_alert(alert_id: str):
    from data_feed import delete_price_alert
    ok = await asyncio.to_thread(delete_price_alert, alert_id)  # group361: off the event loop
    if not ok:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"ok": True, "deleted": alert_id}
