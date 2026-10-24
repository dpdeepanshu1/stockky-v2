"""Route handlers moved out of main.py in E81 (MainRef batch G, after E55 and E76-E80). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_g(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names (`ref.Depends(ref._require_admin)`, `ref.Request`, ...), which must be evaluated when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_g(ref):
    set_notification_config_router = APIRouter()
    delete_notification_channel_router = APIRouter()
    notifications_call_me_router = APIRouter()
    test_notification_channels_router = APIRouter()
    trigger_training_router = APIRouter()
    ops_circuit_reset_router = APIRouter()
    ops_overnight_set_config_router = APIRouter()
    ops_overnight_run_router = APIRouter()
    ops_power_off_router = APIRouter()
    ops_resume_activity_router = APIRouter()
    hard_reset_database_router = APIRouter()

    @set_notification_config_router.post("/notifications/config", dependencies=[ref.Depends(ref._require_admin)])
    def set_notification_config(update: ref.NotificationChannelUpdate):
        try:
            payload = update.model_dump(exclude_none=True)
            # Wake notification service (free-tier cold start)
            try:
                ref.httpx.get(f"{ref.NOTIFICATION_URL}/health", timeout=8)
            except Exception:
                pass
            resp = ref.httpx.post(
                f"{ref.NOTIFICATION_URL}/config",
                json=payload,
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

    @delete_notification_channel_router.delete("/notifications/config/{channel}", dependencies=[ref.Depends(ref._require_admin)])
    def delete_notification_channel(channel: str):
        try:
            resp = ref.httpx.delete(f"{ref.NOTIFICATION_URL}/config/{channel}", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

    @notifications_call_me_router.api_route("/notifications/call/me", dependencies=[ref.Depends(ref._require_admin)], methods=["GET", "POST"])
    def notifications_call_me(request: ref.Request, message: str = "Stockky test call alert"):
        """Proxy CallMeBot test / manual call. CallMeBot can be slow — use long timeout."""
        try:
            msg = message or "Stockky test call alert"
            try:
                ref.httpx.get(f"{ref.NOTIFICATION_URL}/health", timeout=10)
            except Exception:
                pass
            resp = ref.httpx.post(
                f"{ref.NOTIFICATION_URL}/call/me",
                params={"message": msg},
                timeout=70,
            )
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.TimeoutException:
            raise ref.HTTPException(
                status_code=504,
                detail="CallMeBot timed out (slow network or free-tier cold start). Config may still be OK — try Test again in 20s.",
            )
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"CallMeBot unreachable: {e}")

    @test_notification_channels_router.post("/notifications/test", dependencies=[ref.Depends(ref._require_admin)])
    def test_notification_channels():
        """Proxy a real test send to every enabled channel.

    Fix: this used a flat 15s timeout, but the downstream /test dispatch can
    fan out to CallMeBot, which tries a voice call then a text fallback per
    recipient at up to 35s each (see notifications_call_me above, which
    already uses timeout=70 for the same reason) — so a config with
    CallMeBot enabled routinely took well past 15s and came back as a
    misleading "Notification service unreachable" 502 even though nothing
    was actually unreachable. Match the longer timeout and the wake-first +
    distinct-timeout-message pattern already used by call/me and config.
    """
        try:
            ref.httpx.get(f"{ref.NOTIFICATION_URL}/health", timeout=10)
        except Exception:
            pass
        try:
            resp = ref.httpx.post(f"{ref.NOTIFICATION_URL}/test", timeout=75)
            resp.raise_for_status()
            return resp.json()
        except ref.httpx.TimeoutException:
            raise ref.HTTPException(
                status_code=504,
                detail=(
                    "Notification test timed out (CallMeBot voice+text fallback and/or "
                    "free-tier cold start can take a while). Channels may still have "
                    "fired — check each app, or try Test again in 20s."
                ),
            )
        except ref.httpx.HTTPError as e:
            raise ref.HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

    @trigger_training_router.post("/training/train", dependencies=[ref.Depends(ref._require_admin)])
    async def trigger_training():
        try:
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                base = ref.TRAINING_URL.rstrip("/")
                resp = await client.post(f"{base}/api/train")
                if resp.status_code == 404:
                    resp = await client.post(f"{base}/train")
                if resp.status_code >= 400:
                    detail = resp.text[:300]
                    try:
                        detail = resp.json()
                    except Exception:
                        pass
                    raise ref.HTTPException(status_code=resp.status_code, detail=detail)
                return resp.json()
        except ref.HTTPException:
            raise
        except Exception as e:
            raise ref.HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

    @ops_circuit_reset_router.post("/ops/circuit-reset", dependencies=[ref.Depends(ref._require_admin)])
    @ops_circuit_reset_router.get("/ops/circuit-reset")
    async def ops_circuit_reset():
        """Force-close all circuit breakers after intentional warm-up / cold-start recovery.

    Use before a full market scan when services were sleeping and breakers opened
    on ReadTimeout. Safe to call any time.
    """
        try:
            from circuit_breaker import reset_all_breakers, all_snapshots
            names = reset_all_breakers()
            snaps = all_snapshots()
            return {
                "ok": True,
                "reset": names,
                "count": len(names),
                "snapshots": snaps,
                "message": f"Reset {len(names)} circuit breaker(s). Ready for scan.",
            }
        except Exception as e:
            ref.logger.warning("circuit-reset: %s", e)
            return {"ok": False, "error": str(e)[:200]}

    @ops_overnight_set_config_router.post("/ops/overnight/config", dependencies=[ref.Depends(ref._require_admin)])
    async def ops_overnight_set_config(
        enabled: ref.Optional[bool] = None,
        datafeed_time: ref.Optional[str] = None,
        premarket_time: ref.Optional[str] = None,
        rest_between_steps_sec: ref.Optional[int] = None,
    ):
        try:
            params = {}
            if enabled is not None:
                params["enabled"] = enabled
            if datafeed_time is not None:
                params["datafeed_time"] = datafeed_time
            if premarket_time is not None:
                params["premarket_time"] = premarket_time
            if rest_between_steps_sec is not None:
                params["rest_between_steps_sec"] = rest_between_steps_sec
            async with ref.httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{ref.SCHEDULER_URL}/overnight/config", params=params)
                return ref._overnight_proxy_body(r)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @ops_overnight_run_router.post("/ops/overnight/run", dependencies=[ref.Depends(ref._require_admin)])
    async def ops_overnight_run(phase: str = "datafeed"):
        try:
            async with ref.httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{ref.SCHEDULER_URL}/overnight/run", params={"phase": phase})
                return ref._overnight_proxy_body(r)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @ops_power_off_router.post("/ops/power-off", dependencies=[ref.Depends(ref._require_admin)])
    async def ops_power_off(background_tasks: ref.BackgroundTasks):
        """Force-stop ALL user-initiated activity (same path as process shutdown).

    1) Commit / checkpoint in-progress scan + data-feed + hot-picks
    2) Signal cancel everywhere (process-local + durable)
    3) Pause activity gate so quote loop / workers idle
    4) Frontend should reload and only hit health/keepalive afterwards
    """
        phases = ref._graceful_shutdown_commit(reason="power_off")

        # Best-effort stop training lock on decision-prediction service
        try:
            client = ref._get_http_client()
            urls = [
                f"{ref.TRAINING_URL.rstrip('/')}/api/train/stop",
                f"{ref.TRAINING_URL.rstrip('/')}/training/stop",
                f"{ref.DECISION_URL.rstrip('/')}/training/clear-lock",
            ]
            stopped_tr = False
            for u in urls:
                try:
                    r = await client.post(u, timeout=8)
                    if r.status_code < 500:
                        stopped_tr = True
                        break
                except Exception:
                    continue
            phases.append({"phase": "training", "ok": True, "detail": "stop signalled" if stopped_tr else "best-effort"})
        except Exception as e:
            phases.append({"phase": "training", "ok": False, "detail": str(e)[:120]})

        phases.append({"phase": "ready", "ok": True, "detail": "All stoppable jobs force-stopped. Only health/keepalive allowed until resume."})
        return {
            "ok": True,
            "message": "Power Off complete — all activity stopped; checkpoints committed",
            "phases": phases,
            "activity_paused": True,
            "hint": "UI should reload. Background quote fan-out and jobs are paused. GitHub workflows remain independent.",
        }

    @ops_resume_activity_router.post("/ops/resume-activity", dependencies=[ref.Depends(ref._require_admin)])
    def ops_resume_activity():
        """Clear Power-Off pause so scans / feeds / quotes can run again."""
        ref.set_activity_paused(False)
        try:
            ref._SCAN_CANCEL_FLAGS.discard("__ALL__")
        except Exception:
            pass
        return {"ok": True, "activity_paused": False, "message": "Activity resumed"}

    @hard_reset_database_router.post("/data-feed/hard-reset", dependencies=[ref.Depends(ref._require_admin)])
    @hard_reset_database_router.post("/api/data-feed/hard-reset", dependencies=[ref.Depends(ref._require_admin)])
    @hard_reset_database_router.post("/api/feed/hard-reset", dependencies=[ref.Depends(ref._require_admin)])
    async def hard_reset_database(preserve_days: int = 7):
        """
    Wipes VOLATILE stockky_kv fields (price/volume) and re-asserts unique
    constraint + index on k. Called by the frontend "Feed Fresh Data" button
    *before* /data-feed/run so a corrupted / over-₹5000 universe is nuked
    on autopilot.

    Durable/slow per-symbol fields (PE ratio, ROCE, sector, technical &
    fundamental scores, model prediction outputs, etc.) updated within the
    last `preserve_days` days are snapshotted and restored automatically —
    only stale (older than preserve_days) or price/volume fields are lost.
    Pass preserve_days=0 to force the old full-wipe behavior.
    """
        try:
            from kv_cache import hard_reset_stockky_kv
            from data_feed import clear_local_data_feed_caches, request_data_feed_stop, clear_data_feed_stop

            # Stop any in-flight feed first
            try:
                request_data_feed_stop()
            except Exception:
                pass

            # asyncio.to_thread (session55 sweep, part 2): hard_reset_stockky_kv
            # is a blocking Neon write touching every kv row outside
            # preserve_days — this is a manual admin action, but the write can
            # run long on a large table, so it still shouldn't stall every other
            # request for its duration.
            result = await ref.asyncio.to_thread(hard_reset_stockky_kv, preserve_days=preserve_days)
            try:
                clear_local_data_feed_caches()
            except Exception:
                pass
            try:
                clear_data_feed_stop()
            except Exception:
                pass

            # Destroy Split-Brain ghosts: scan-universe cache, last-scan, known-symbols
            ghost_keys = [
                ref.SCAN_UNIVERSE_KEY,
                "stockky:last_full_scan",
                "stockky:known_symbols",
                "stockky:data_feed:index",
                "stockky:data_feed:meta",
                "stockky:data_feed:job",
                "stockky:hot_result",
                "stockky:hot_result_db",
                "stockky:hot_job",
            ]
            for gk in ghost_keys:
                # BUG FIX (2026-09-03, pyflakes audit): this used to be
                # `_redis_delete(gk) if "_redis_delete" in dir() else None` —
                # the exact same `dir()`-with-no-arguments anti-pattern already
                # fixed once in this file for `_get_nifty50_data` (see the
                # 2026-09-01 note on _get_momentum_movers above): dir() with no
                # arguments returns names in the CURRENT LOCAL SCOPE, not module
                # globals, and `_redis_delete` was never a local variable here —
                # so the condition was always False and this line silently did
                # nothing on every call, for a function that (like
                # _fetch_nse_fundamentals above) was never actually defined
                # anywhere in the codebase either. Removed rather than
                # reimplemented: the two blocks below (_kv_cache.kv_delete and
                # _redis.delete) already cover both real ghost-key cleanup paths
                # this service uses, so this was dead weight, not a gap.
                # asyncio.to_thread: kv_delete is a blocking Neon call, per key
                # (9 keys) — small/fixed count so this was flagged low-priority,
                # but it's still a manual admin action worth keeping off the
                # event loop rather than an exception to the rule swept above.
                try:
                    if ref._kv_cache is not None:
                        await ref.asyncio.to_thread(ref._kv_cache.kv_delete, gk)
                except Exception:
                    pass
                try:
                    if ref._redis:
                        await ref.asyncio.to_thread(ref._redis.delete, gk)
                except Exception:
                    pass

            # Reset job/meta so UI shows 0 stocks immediately
            try:
                store = ref._feed_store()
                store.set_job(
                    status="idle",
                    message="Hard-reset complete — ready for fresh feed",
                    stop_requested=False,
                    processed=0,
                    total=0,
                    ok_count=0,
                )
                store.set_meta(
                    last_success_at=None,
                    last_count=0,
                    last_message="Hard-reset — memory + DB wiped",
                    stock_count=0,
                )
            except Exception as e:
                ref.logger.debug("hard-reset job/meta clear: %s", e)

            if not isinstance(result, dict):
                # No status contract from the wipe (e.g. None): the wipe ran, so report success
                # with the default message instead of crashing on `.get` after the work is done.
                result = {}
            if result.get("status") == "error":
                raise ref.HTTPException(status_code=500, detail=result.get("message", "hard-reset failed"))
            result["message"] = result.get("message") or "Database wiped, locked, and memory cleared. Ready for feed."
            result["ghosts_cleared"] = ghost_keys
            return result
        except ref.HTTPException:
            raise
        except Exception as e:
            ref.logger.exception("hard_reset_database: %s", e)
            raise ref.HTTPException(status_code=500, detail=str(e)[:240])

    return SimpleNamespace(
        set_notification_config=set_notification_config,
        set_notification_config_router=set_notification_config_router,
        delete_notification_channel=delete_notification_channel,
        delete_notification_channel_router=delete_notification_channel_router,
        notifications_call_me=notifications_call_me,
        notifications_call_me_router=notifications_call_me_router,
        test_notification_channels=test_notification_channels,
        test_notification_channels_router=test_notification_channels_router,
        trigger_training=trigger_training,
        trigger_training_router=trigger_training_router,
        ops_circuit_reset=ops_circuit_reset,
        ops_circuit_reset_router=ops_circuit_reset_router,
        ops_overnight_set_config=ops_overnight_set_config,
        ops_overnight_set_config_router=ops_overnight_set_config_router,
        ops_overnight_run=ops_overnight_run,
        ops_overnight_run_router=ops_overnight_run_router,
        ops_power_off=ops_power_off,
        ops_power_off_router=ops_power_off_router,
        ops_resume_activity=ops_resume_activity,
        ops_resume_activity_router=ops_resume_activity_router,
        hard_reset_database=hard_reset_database,
        hard_reset_database_router=hard_reset_database_router,
    )
