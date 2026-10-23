"""Stockky Hot Picks read routes (durable table + feed audit; hotpicks_store is imported inside the handlers). Moved out of main.py in E49, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

from fastapi import APIRouter

hot_table_router = APIRouter()


@hot_table_router.get("/stockky-hot/table")
def stockky_hot_table(hours: int = 24):
    """Hot Picks from the durable table (default: last 24h).

    Serves hotpicks_static_feed directly — Neon on Render, Oracle ADB on the
    Oracle VM, identical JSON either way. This is what the tab paints instantly
    on open, before (or instead of) triggering a scan.
    """
    try:
        from hotpicks_store import hotpicks_db_payload, HOTPICKS_TABLE_HOURS
    except Exception as e:
        return {"ok": False, "rows": [], "count": 0, "hours": hours, "fresh": False,
                "detail": f"hotpicks store unavailable: {str(e)[:160]}"}
    try:
        window = int(hours or HOTPICKS_TABLE_HOURS)
    except Exception:
        window = 24
    window = max(1, min(window, 24 * 30))
    payload = hotpicks_db_payload(window)
    if not payload:
        return {
            "ok": True,
            "rows": [],
            "count": 0,
            "hours": window,
            "fresh": False,
            "detail": "No stored Hot Picks in this window yet — run a scan.",
        }
    rows = []
    for section in ("news_driven", "results_driven", "bulk_insider_driven"):
        rows.extend(payload.get(section) or [])
    return {**payload, "ok": True, "rows": rows}


hot_audit_router = APIRouter()


@hot_audit_router.get("/stockky-hot/audit")
def stockky_hot_audit():
    """Hot Picks feed health: backend in use, row counts, staleness, gaps."""
    try:
        from hotpicks_store import hotpicks_audit
    except Exception as e:
        return {"ok": False, "issues": [f"hotpicks store unavailable: {str(e)[:160]}"]}
    return hotpicks_audit()
