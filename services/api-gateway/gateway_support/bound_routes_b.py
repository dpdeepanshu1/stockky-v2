"""Route handlers moved out of main.py in E76 (the second MainRef batch after E55). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_b(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_b(ref):
    ops_neon_keepalive_router = APIRouter()
    market_session_router = APIRouter()
    notifications_health_router = APIRouter()
    get_notification_config_router = APIRouter()
    training_status_router = APIRouter()
    get_training_score_router = APIRouter()
    ops_overnight_status_router = APIRouter()
    ops_overnight_get_config_router = APIRouter()
    ops_activity_status_router = APIRouter()
    repair_all_status_router = APIRouter()
    repair_all_stop_router = APIRouter()

    @ops_neon_keepalive_router.get("/ops/neon-keepalive")
    @ops_neon_keepalive_router.post("/ops/neon-keepalive")
    async def ops_neon_keepalive():
        """
        Cron-friendly Neon keep-alive (every ~4 minutes recommended).

        Prevents Neon free-tier compute auto-suspend so the next user scan
        does not pay 0.5–2.5s cold-start latency.

        Example (GitHub Actions / external cron):
          curl -X POST https://<api-gateway>/ops/neon-keepalive
        """
        result = ref._neon_keepalive_ping()
        return {
            **result,
            "at": ref.datetime.now(ref.IST).isoformat(),
            "hint": "Schedule every 4 minutes while you need warm Neon",
        }

    @market_session_router.get("/market/session")
    def market_session():
        """Current NSE session phase for UI / keep-warm / quote policy."""
        now = ref.datetime.now(ref.IST)
        phase = ref._market_session_phase_ist()
        return {
            "phase": phase,
            "is_open": phase == "open",
            # The phase helper is authoritative for "holiday": the two sources used to be
            # unreconciled, so a holiday phase on a calendar-clean weekday reported both
            # is_holiday and is_market_day as True.
            "is_market_day": phase != "holiday" and (
                phase not in ("closed",) or (now.weekday() < 5 and not ref.is_nse_holiday(now.date()))
            ),
            "is_holiday": phase == "holiday",
            "now_ist": now.isoformat(),
            "session_window": "09:15–15:30 IST Mon–Fri (ex holidays)",
            "quote_polling": phase in ("preopen", "open", "post"),
        }

    @notifications_health_router.get("/notifications/health")
    def notifications_health():
        try:
            resp = ref.httpx.get(f"{ref.NOTIFICATION_URL}/health", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")
        except ValueError as e:
            # 200 with a non-JSON body: same upstream-failure class as the rest.
            raise ref.HTTPException(status_code=502, detail=f"Notification service returned a non-JSON health body: {e}")

    @get_notification_config_router.get("/notifications/config")
    def get_notification_config():
        try:
            resp = ref.httpx.get(f"{ref.NOTIFICATION_URL}/config", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

    @training_status_router.get("/training/status")
    async def training_status():
        try:
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                resp = await client.get(f"{ref.TRAINING_URL.rstrip('/')}/model-status")
                # Also try /api/status if model-status 404s
                if resp.status_code == 404:
                    resp = await client.get(f"{ref.TRAINING_URL.rstrip('/')}/api/status")
                resp.raise_for_status()
                return resp.json()
        except ref.HTTPException:
            raise
        except Exception as e:
            raise ref.HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

    @get_training_score_router.get("/training/score/{symbol}")
    async def get_training_score(symbol: str):
        try:
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                resp = await client.get(f"{ref.TRAINING_URL.rstrip('/')}/training-score/{symbol}")
                if resp.status_code == 404:
                    return {
                        "symbol": symbol.upper(),
                        "score": None,
                        "available": False,
                        "message": "No training score for this symbol yet",
                    }
                resp.raise_for_status()
                return resp.json()
        except ref.HTTPException:
            raise
        except Exception as e:
            raise ref.HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

    @ops_overnight_status_router.get("/ops/overnight/status")
    async def ops_overnight_status():
        try:
            async with ref.httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{ref.SCHEDULER_URL}/overnight/status")
                return ref._overnight_proxy_body(r)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @ops_overnight_get_config_router.get("/ops/overnight/config")
    async def ops_overnight_get_config():
        try:
            async with ref.httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{ref.SCHEDULER_URL}/overnight/config")
                return ref._overnight_proxy_body(r)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @ops_activity_status_router.get("/ops/activity")
    def ops_activity_status():
        return {
            "ok": True,
            "activity_paused": ref.activity_paused(),
            "quote_loop_enabled": bool(ref._QUOTE_LOOP_ENABLED),
            "scan_cancel_flags": len(ref._SCAN_CANCEL_FLAGS),
        }

    @repair_all_status_router.get("/api/feed/repair-all/status")
    @repair_all_status_router.get("/data-feed/repair-all/status")
    async def repair_all_status():
        return dict(ref._REFILL_ALL_JOB)

    @repair_all_stop_router.post("/api/feed/repair-all/stop")
    @repair_all_stop_router.post("/data-feed/repair-all/stop")
    async def repair_all_stop():
        if ref._REFILL_ALL_JOB.get("status") != "running":
            # Nothing to stop: don't leave a stale cancel flag behind for the next run to trip over.
            return {"ok": True, "stopped": False, "message": "No Refill All job is running."}
        ref._REFILL_ALL_JOB["cancel_requested"] = True
        return {"ok": True, "message": "Stop requested — will halt after the current batch."}

    return SimpleNamespace(
        ops_neon_keepalive_router=ops_neon_keepalive_router, ops_neon_keepalive=ops_neon_keepalive,
        market_session_router=market_session_router, market_session=market_session,
        notifications_health_router=notifications_health_router, notifications_health=notifications_health,
        get_notification_config_router=get_notification_config_router, get_notification_config=get_notification_config,
        training_status_router=training_status_router, training_status=training_status,
        get_training_score_router=get_training_score_router, get_training_score=get_training_score,
        ops_overnight_status_router=ops_overnight_status_router, ops_overnight_status=ops_overnight_status,
        ops_overnight_get_config_router=ops_overnight_get_config_router, ops_overnight_get_config=ops_overnight_get_config,
        ops_activity_status_router=ops_activity_status_router, ops_activity_status=ops_activity_status,
        repair_all_status_router=repair_all_status_router, repair_all_status=repair_all_status,
        repair_all_stop_router=repair_all_stop_router, repair_all_stop=repair_all_stop,
    )
