"""Data-feed cache and background-job routes: bulk quote cache view, structure-refresh stop, refill-additional status. Moved out of main.py in E49, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

from fastapi import APIRouter

bulk_cache_router = APIRouter()


@bulk_cache_router.get("/api/quotes/bulk-cache")
@bulk_cache_router.get("/data-feed/bulk-cache")
async def api_bulk_quote_cache():
    """Shared bulk quote cache status + map (market-aware TTL)."""
    from data_feed import get_bulk_quote_cache, bulk_cache_age_sec, should_refresh_bulk_cache
    cache = get_bulk_quote_cache()
    return {
        "ok": True,
        "age_sec": bulk_cache_age_sec(),
        "should_refresh": should_refresh_bulk_cache(),
        "meta": (cache or {}).get("_meta"),
        "count": len((cache or {}).get("quotes") or {}),
        "quotes": (cache or {}).get("quotes") or {},
    }


feed_job_router = APIRouter()


@feed_job_router.post("/data-feed/structure-refresh/stop")
@feed_job_router.post("/api/data-feed/structure-refresh/stop")
def data_feed_structure_refresh_stop():
    """Ask a running structure refresh to stop after the symbols in flight."""
    try:
        import structure_refresh as _sr
        _sr.request_stop()
        return {"ok": True, "stop_requested": True, **_sr.get_job()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@feed_job_router.get("/data-feed/refill-additional/status")
@feed_job_router.get("/api/data-feed/refill-additional/status")
def data_feed_refill_status():
    """Status of the Refill Additional Data job."""
    try:
        from refill_additional import get_refill_job
        return {"ok": True, **get_refill_job()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "status": "idle"}
