"""Route handlers moved out of main.py in E79 (MainRef batch E, after E55, E76, E77 and E78). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_e(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_e(ref):
    wake_all_services_router = APIRouter()
    momentum_movers_route_router = APIRouter()
    get_watchlist_router = APIRouter()
    remove_from_watchlist_router = APIRouter()
    get_symbol_events_router = APIRouter()
    get_searched_symbols_router = APIRouter()
    get_last_scan_router = APIRouter()
    get_scan_status_router = APIRouter()
    market_top_gainers_router = APIRouter()
    market_top_losers_router = APIRouter()
    market_most_active_router = APIRouter()
    data_feed_meta_router = APIRouter()
    stockky_hot_status_router = APIRouter()
    stockky_hot_result_router = APIRouter()
    stockky_hot_premarket_status_router = APIRouter()
    stockky_hot_stop_router = APIRouter()

    @wake_all_services_router.get("/wake-all")
    @wake_all_services_router.post("/wake-all")
    async def wake_all_services():
        """Hit health?warm=1 on all upstream services (free-tier anti-sleep)."""
        results = await ref._wake_required_services()
        ok = sum(1 for v in results.values() if v.get("ok"))
        return {
            "status": "ok" if ok else "degraded",
            "warmed_ok": ok,
            "total": len(results),
            "services": results,
        }

    @momentum_movers_route_router.get("/market/momentum-movers")
    def momentum_movers_route():
        """
    2026-09-03 — exposes the existing full-market NSE gainers/losers/
    volume-gainers scan (_get_momentum_movers, already used internally by
    the hot-picks/scan endpoints) as its own lightweight endpoint, so
    real-trade-service's dynamic_universe.py can widen its auto-tracked
    watchlist using this broader, catalyst-agnostic momentum source in
    addition to the volume-shock scanner it already uses — not a new
    detection mechanism, just a new way to reach an existing one.
    Read-only, cheap: returns symbols only, no scoring/pillar work.
    """
        try:
            symbols = ref._get_momentum_movers()
        except Exception as e:
            ref.logger.warning("momentum_movers_route failed: %s", e)
            symbols = []
        return {"symbols": symbols, "count": len(symbols)}

    @get_watchlist_router.get("/watchlist")
    def get_watchlist():
        return {"symbols": ref._load_watchlist()}

    @remove_from_watchlist_router.delete("/watchlist/{symbol}")
    def remove_from_watchlist(symbol: str):
        current = ref._load_watchlist()
        target = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
        updated = [s for s in current if s != target]
        ref._save_watchlist(updated)
        ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY)   # a removed symbol must stop being scanned now, not at cache expiry
        return {"symbols": updated}

    @get_symbol_events_router.get("/events/{symbol}")
    def get_symbol_events(symbol: str):
        """Frontend-facing proxy for the Analysis page's event section —
    previously the only event data reaching the frontend was whatever
    decision-engine happened to pass through on the Decision object
    (next_earnings_date only); this exposes event-tracker-service's
    full categorized (upcoming/recent/recent_changes) view directly."""
        try:
            _sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
            resp = ref.httpx.get(f"{ref.EVENT_URL}/events/{_sym}/categorized", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            ref.logger.warning(f"Failed to fetch categorized events for {symbol}: {e}")
            raise ref.HTTPException(status_code=502, detail=f"event-tracker-service unreachable: {e}")

    @get_searched_symbols_router.get("/searched")
    def get_searched_symbols():
        return {"symbols": ref._load_searched()}

    @get_last_scan_router.get("/scan/last")
    def get_last_scan():
        """Last full or partial market scan (Redis). Used on dashboard refresh."""
        cached = ref._redis_get(ref.LAST_FULL_SCAN_KEY)
        if not cached or not isinstance(cached, dict):
            return {"ok": False, "detail": "No scan cached yet", "result": None}
        res = cached.get("result") if isinstance(cached.get("result"), dict) else {}
        total = cached.get("total") or cached.get("universe_size")
        # Same partial rule as GET /scan and POST /scan/start: the flags on the entry
        # AND on its result block, plus the 90% completion rule.
        try:
            _p = int(cached.get("processed") or res.get("scanned") or 0)
            _t = int(total or res.get("universe_size") or _p or 0)
            below_90 = _t > 0 and _p < int(_t * 0.9)
        except (TypeError, ValueError):
            below_90 = False
        return {
            "ok": True,
            "task_id": cached.get("task_id"),
            "scanned_at": cached.get("scanned_at"),
            "partial": bool(
                cached.get("partial")
                or cached.get("cancelled")
                or res.get("partial")
                or res.get("stopped_early")
                or res.get("cancelled")
                or below_90
            ),
            "processed": cached.get("processed"),
            "total": total,
            "result": cached.get("result"),
        }

    @get_scan_status_router.get("/scan/status/{task_id}")
    def get_scan_status(task_id: str):
        """Return scan progress. Missing task → soft response (not hard 404) so UI can stop cleanly."""

        data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id)
        if not data:
            return {
                "task_id": task_id,
                "status": "unknown",
                "processed": 0,
                "total": 0,
                "cancelled": True,
                "partial": True,
                "message": "Scan task not found (expired or power-off). Safe to start a new scan.",
                "ok": False,
            }
            # was: raise HTTPException
        if data.get("status") == "running":
            processed = data.get("processed", 0)
            total = data.get("total", 0)
            elapsed = data.get("elapsed", 0)
            if processed > 0 and elapsed > 0:
                avg_time_per_stock = elapsed / processed
                remaining_stocks = max(0, total - processed)
                estimated_remaining = round(remaining_stocks * avg_time_per_stock, 1)
                data["estimated_remaining"] = estimated_remaining
            else:
                data["estimated_remaining"] = None
        return data

    @market_top_gainers_router.get("/market/top-gainers")
    def market_top_gainers():
        return ref._movers_response(ref._rank_market_rows(ref._get_nifty50_data(), "change_pct", True))

    @market_top_losers_router.get("/market/top-losers")
    def market_top_losers():
        return ref._movers_response(ref._rank_market_rows(ref._get_nifty50_data(), "change_pct", False))

    @market_most_active_router.get("/market/most-active")
    def market_most_active():
        return ref._movers_response(ref._rank_market_rows(ref._get_nifty50_data(), "volume", True))

    @data_feed_meta_router.get("/data-feed/meta")
    @data_feed_meta_router.get("/api/data-feed/meta")
    def data_feed_meta():
        """Last successful feed timestamp, stock count, job status."""
        store = ref._feed_store()
        meta = store.meta()
        job = store.job()
        return {"ok": True, "meta": meta, "job": job}

    @stockky_hot_status_router.get("/stockky-hot/status")
    def stockky_hot_status():
        job = ref.hot_job_get(ref._redis_get)
        cached = ref._redis_get(ref.HOT_RESULT_KEY) or ref._redis_get(ref.HOT_STOCKS_CACHE_KEY)
        return {
            **job,
            "ok": True,   # after the job spread: the route itself succeeded, job state lives in `status`
            "has_result": bool(cached),
            "result_generated_at": (cached or {}).get("generated_at") if isinstance(cached, dict) else None,
        }

    @stockky_hot_result_router.get("/stockky-hot/result")
    def stockky_hot_result():
        """Last persisted Hot Picks result (DB/Redis) — does not trigger a new run."""
        cached = ref._redis_get(ref.HOT_RESULT_KEY) or ref._redis_get(ref.HOT_STOCKS_CACHE_KEY)
        if not cached or not isinstance(cached, dict):
            # a truthy non-dict value in the cache is as unusable as no result at all
            return {"ok": False, "detail": "No Hot Picks result yet — run Search Hot Picks Stocks"}
        return {**cached, "ok": True, "cached": True}

    @stockky_hot_premarket_status_router.get("/stockky-hot/premarket/status")
    def stockky_hot_premarket_status():
        """Progress of the Hot Picks 'Premarket' bulk price pre-feed job."""
        job = ref.hot_premarket_job_get(ref._redis_get)
        return {**job, "ok": True}

    @stockky_hot_stop_router.post("/stockky-hot/stop")
    def stockky_hot_stop():
        """Halt the running Hot Picks scan after the current symbol.

    Symbols already scored are ranked, cached and written to
    hotpicks_static_feed, so a stop is a "keep what you have", not a discard —
    same semantics as /surprise/ipo/stop and the data-feed repair stop.
    """
        try:
            from hotpicks_store import request_hotpicks_stop

            request_hotpicks_stop()
        except Exception as e:
            return {"ok": False, "detail": f"stop unavailable: {str(e)[:160]}"}
        job = ref.hot_job_get(ref._redis_get)
        if job.get("status") != "running":
            # `message` last: the finished job's own message must not replace it.
            return {**job, "ok": True, "stopping": False, "message": "No Hot Picks scan is running"}
        ref.hot_job_set(
            ref._redis_set,
            ref._redis_get,
            message="Stop requested — finishing current symbol…",
            stop_requested=True,
        )
        return {"ok": True, "stopping": True, "message": "Stopping after the current symbol"}

    return SimpleNamespace(
        wake_all_services=wake_all_services,
        wake_all_services_router=wake_all_services_router,
        momentum_movers_route=momentum_movers_route,
        momentum_movers_route_router=momentum_movers_route_router,
        get_watchlist=get_watchlist,
        get_watchlist_router=get_watchlist_router,
        remove_from_watchlist=remove_from_watchlist,
        remove_from_watchlist_router=remove_from_watchlist_router,
        get_symbol_events=get_symbol_events,
        get_symbol_events_router=get_symbol_events_router,
        get_searched_symbols=get_searched_symbols,
        get_searched_symbols_router=get_searched_symbols_router,
        get_last_scan=get_last_scan,
        get_last_scan_router=get_last_scan_router,
        get_scan_status=get_scan_status,
        get_scan_status_router=get_scan_status_router,
        market_top_gainers=market_top_gainers,
        market_top_gainers_router=market_top_gainers_router,
        market_top_losers=market_top_losers,
        market_top_losers_router=market_top_losers_router,
        market_most_active=market_most_active,
        market_most_active_router=market_most_active_router,
        data_feed_meta=data_feed_meta,
        data_feed_meta_router=data_feed_meta_router,
        stockky_hot_status=stockky_hot_status,
        stockky_hot_status_router=stockky_hot_status_router,
        stockky_hot_result=stockky_hot_result,
        stockky_hot_result_router=stockky_hot_result_router,
        stockky_hot_premarket_status=stockky_hot_premarket_status,
        stockky_hot_premarket_status_router=stockky_hot_premarket_status_router,
        stockky_hot_stop=stockky_hot_stop,
        stockky_hot_stop_router=stockky_hot_stop_router,
    )
