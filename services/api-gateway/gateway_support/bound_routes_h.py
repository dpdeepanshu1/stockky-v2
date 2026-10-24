"""Route handlers moved out of main.py in E83 (MainRef batch H, after E55 and E76-E80). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_h(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names (`ref.Depends(ref._require_admin)`, `ref.Request`, ...), which must be evaluated when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_h(ref):
    root_router = APIRouter()
    ops_rate_limits_event_router = APIRouter()
    ops_check_alert_router = APIRouter()
    ops_refresh_static_params_router = APIRouter()
    market_history_router = APIRouter()
    ops_db_status_router = APIRouter()
    set_watchlist_router = APIRouter()
    add_to_watchlist_router = APIRouter()
    run_scan_post_router = APIRouter()
    training_other_proxy_router = APIRouter()
    repair_all_missing_router = APIRouter()

    @root_router.get("/")
    def root():
        return {
            "service": "Stockky API Gateway",
            "version": "2.5.16",
            "status": "running",
            "parallel_workers": ref.MAX_PARALLEL_WORKERS,
            "endpoints": {
                "/health": "GET – health check",
                "/ready": "GET – lightweight readiness check",
                "/system/health": "GET – health of all downstream services",
                "/wake/all": "POST – wake all services",
                "/watchlist": "GET/POST – manage watchlist",
                "/watchlist/add": "POST – add symbols",
                "/watchlist/{symbol}": "DELETE – remove symbol",
                "/stock/{symbol}": "GET – get decision for a symbol",
                "/scan": "GET – synchronous scan (legacy)",
                "/scan/start": "POST – start async parallel scan, returns task_id",
                "/scan/status/{task_id}": "GET – get progress/result of async scan",
                "/scan/stream": "GET – NDJSON stream of scan results (incremental UI, avoids 100s timeout)",
                "/scan/watchlist": "GET – scan only your watchlist",
                "/scan/universe": "GET – preview current scan universe",
                "/scan/universe/cache": "DELETE – clear universe cache",

                "/searched": "GET – list searched symbols",
                "/market/top-gainers": "GET – top 10 gainers",
                "/market/top-losers": "GET – top 10 losers",
                "/market/most-active": "GET – top 10 most active by volume",
                "/market/trending": "GET – trending stocks (momentum + news)",
                "/market/indices": "GET – live NIFTY 50 & SENSEX (IST time, Cache-Control)",
                "/notifications/health": "GET – notification service health",
                "/notifications/config": "GET/POST – get/update notification config",
                "/notifications/config/{channel}": "DELETE – clear a channel",
                "/notifications/test": "POST – test notifications",
                "/notifications/send-picks": "POST – manually send picks to Telegram (splits long messages)",
                "/training/status": "GET – get training model status",
                "/training/train": "POST – trigger a new training run",
                "/training/score/{symbol}": "GET – get training intelligence score for a symbol",
                "/api/surprise/scan": "GET – lightweight surprise momentum scan",
                "/api/surprise/scan/stream": "GET – NDJSON stream of surprise hits",
                "/api/surprise/static": "GET – surprise_static_feed baselines",
                "/surprise/premarket": "POST/GET – premarket baselines (injects scan universe)",

                "/quote/{symbol}": "GET – near-realtime quote proxy",
                "/circuits": "GET – circuit-breaker snapshots",
                "/metrics": "GET – JSON metrics (?format=prom for Prometheus text)",
                "/ops/check-alert": "POST – alert if a circuit is open or error rate is high (cron)",
                "/docs": "Swagger UI documentation",
            },
        }

    @ops_rate_limits_event_router.post("/ops/rate-limits/event")
    async def ops_rate_limits_event(payload: dict):
        """Services may POST {source, status, path?, detail?, symbol?} when they hit 429/503."""
        try:
            _src = str(payload.get("source") or "unknown")
            _status = int(payload.get("status") or 0)
            ref.rate_limit_monitor.record(
                source=_src,
                status=_status,
                path=str(payload.get("path") or ""),
                detail=str(payload.get("detail") or ""),
                symbol=str(payload.get("symbol") or ""),
            )
            try:
                # Both labels come from the request body, and every distinct value is a new series kept
                # for the life of the process: bound them (see metrics.LabelLimiter). The monitor above
                # keeps the raw values; only the counter's labels are collapsed.
                _status_label = str(_status) if (_status == 0 or 100 <= _status <= 599) else "other"
                ref.metrics.inc(
                    "rate_limit_events",
                    source=ref.label_limiter.clean("rate_limit_events.source", _src),
                    status=ref.label_limiter.clean("rate_limit_events.status", _status_label, max_distinct=24),
                )
            except Exception as me:
                # the event is already recorded — a counter failure must not report 400
                ref.logger.warning("rate_limit_events metric failed: %s", me)
            return {"ok": True}
        except Exception as e:
            return ref.JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=400)

    @ops_check_alert_router.post("/ops/check-alert")
    async def ops_check_alert():
        """Evaluate simple thresholds and notify if unhealthy (for cron).

    Alerts when any circuit is open or recent dependency error rate is high.
    Repeats of the same problem set are suppressed for OPS_ALERT_COOLDOWN_SEC.
    """
        snaps = ref.all_snapshots()
        open_circuits = [k for k, v in snaps.items() if v.get("state") == "open"]
        snap = ref.metrics.snapshot()
        counters = snap.get("counters") or {}
        errors = sum(v for k, v in counters.items() if "dependency_errors" in k)
        oks = sum(v for k, v in counters.items() if "dependency_ok" in k)
        total = errors + oks
        err_rate = (errors / total) if total else 0.0

        problems = []
        if open_circuits:
            problems.append(f"Open circuits: {', '.join(open_circuits)}")
        if total >= 20 and err_rate >= 0.4:
            problems.append(f"Dependency error rate {err_rate:.0%} ({int(errors)}/{int(total)})")

        if not problems:
            ref._OPS_ALERT_STATE.clear()
            return {"alerted": False, "ok": True, "open_circuits": [], "error_rate": err_rate}

        # Signature is stable across calls (the error-rate text changes every minute, so it is
        # keyed on the condition, not the message).
        signature = (tuple(sorted(open_circuits)), bool(total >= 20 and err_rate >= 0.4))
        cooldown = ref._ops_alert_cooldown_sec()
        last = ref._OPS_ALERT_STATE.get("last")
        if cooldown > 0 and last and last["signature"] == signature:
            elapsed = ref._ops_alert_clock() - last["at"]
            if elapsed < cooldown:
                return {
                    "alerted": False,
                    "suppressed": True,
                    "cooldown_remaining_sec": int(cooldown - elapsed),
                    "problems": problems,
                    "open_circuits": open_circuits,
                    "error_rate": err_rate,
                }

        title = "⚠️ Stockky ops alert"
        message = " | ".join(problems)
        delivered = False
        try:
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                r = await client.post(
                    f"{ref.NOTIFICATION_URL.rstrip('/')}/notify",
                    json={
                        "title": title,
                        "message": message[:1500],
                        "channel": "all",
                        "urgency": "high",
                    },
                )
                delivered = r.status_code == 200 and bool((r.json() or {}).get("delivered"))
        except Exception as e:
            ref.logger.warning("ops alert notify failed: %s", e)
        ref.metrics.inc("stockky_ops_alerts_total")
        if delivered:
            ref._OPS_ALERT_STATE["last"] = {"signature": signature, "at": ref._ops_alert_clock()}
        return {
            "alerted": True,
            "delivered": delivered,
            "problems": problems,
            "open_circuits": open_circuits,
            "error_rate": err_rate,
        }

    @ops_refresh_static_params_router.post("/ops/refresh-static-params")
    async def ops_refresh_static_params(limit: int = 60):
        """Nightly job: re-fetch fundamentals / events / news for scan universe into Redis (24h TTL).

    Live quotes & decisions still refresh during market hours; this only warms
    slow-changing layers so daytime traffic hits cache and stays under rate limits.
    """
        universe = (await ref.asyncio.to_thread(ref._build_scan_universe))[: max(10, min(limit, 80))]
        refreshed = {"fundamental": 0, "event": 0, "news": 0, "errors": 0}
        client = ref._get_http_client()  # shared keepalive pool
        if True:
            for sym in universe:
                base = (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
                if not base:
                    continue
                try:
                    # Force refresh: clear cache keys then re-fetch into 24h TTL
                    try:
                        if ref._redis:
                            ref._redis.delete(f"{ref.FUNDAMENTAL_CACHE_PREFIX}{base}")
                            ref._redis.delete(f"{ref.EVENT_CACHE_PREFIX}{base}")
                            ref._redis.delete(f"{ref.NEWS_CACHE_PREFIX}{base}")
                    except Exception:
                        pass
                    metrics_data, _fb = await ref._fetch_fundamental_cached(base, client)
                    if metrics_data is not None:
                        refreshed["fundamental"] += 1
                    ev = await ref._fetch_events_cached(base, client)
                    if ev is not None:
                        refreshed["event"] += 1
                    news = await ref._fetch_news_cached(base, client)
                    if news is not None:
                        refreshed["news"] += 1
                except Exception as e:
                    refreshed["errors"] += 1
                    ref.logger.warning("refresh-static-params %s: %s", base, e)
                await ref.asyncio.sleep(0.25)

        # Also refresh hot-stocks cache so next UI load is cheap
        try:
            if ref._redis:
                ref._redis.delete(ref.HOT_STOCKS_CACHE_KEY)
        except Exception:
            pass

        return {
            "status": "ok",
            "universe_size": len(universe),
            "refreshed": refreshed,
            "ttl_seconds": ref.STATIC_PARAM_TTL,
            "at": ref.datetime.now(ref.IST).isoformat(),
        }

    @market_history_router.get("/market/history/{symbol}")
    async def market_history(symbol: str, period: str = "1mo"):
        """Chart candles — market-data first, training history fallback."""
        sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
        md_period = {"1d": "1mo", "5d": "1mo", "1mo": "1mo", "1y": "1y", "5y": "5y", "3mo": "3mo", "6mo": "6mo"}.get(period, "1mo")
        last_err = None
        client = ref._get_http_client()  # shared keepalive pool
        _hist_timeout = ref.httpx.Timeout(10.0, connect=5.0)  # fast-fail; both sources 404 on current deploy
        if True:
            try:
                if ref._wake_pings_enabled():
                    try:
                        await client.get(f"{ref.MARKET_DATA_URL}/health", params={"warm": "true"}, timeout=_hist_timeout)
                    except Exception:
                        pass
                r = await client.get(f"{ref.MARKET_DATA_URL}/history/{sym}", params={"period": md_period, "interval": "1d"}, timeout=_hist_timeout)
                if r.status_code == 200:
                    data = r.json()
                    candles = data.get("candles") or []
                    points = []
                    for c in candles:
                        if c.get("close") is None:
                            continue
                        points.append({
                            "date": c.get("date"),
                            "open": c.get("open"),
                            "high": c.get("high"),
                            "low": c.get("low"),
                            "close": c.get("close"),
                            "volume": c.get("volume") or 0,
                        })
                    if points:
                        # Daily candles only: trim the 1-month window to the span asked for so change_pct
                        # covers the day / week requested (1d = last two sessions, 5d = last five).
                        _keep = {"1d": 2, "5d": 5}.get(period)
                        if _keep:
                            points = points[-_keep:]
                        first = points[0]["close"] or 0
                        last = points[-1]["close"] or 0
                        chg = round((last - first) / first * 100, 2) if first else None
                        return {"symbol": sym, "period": period, "points": points, "change_pct": chg, "source": "market-data"}
                last_err = f"market-data HTTP {r.status_code}"
            except Exception as e:
                # AUDIT FIX: httpx's own timeout exceptions (ConnectTimeout,
                # ReadTimeout, PoolTimeout, etc.) stringify to "" when no
                # message was attached, so str(e) alone silently swallowed the
                # real failure — every timeout-caused 503 here rendered as a
                # blank, undiagnosable "Chart unavailable for X: ; training "
                # with no indication anything had even timed out. Always
                # include the exception's class name so the cause survives
                # into the response even when its message is empty.
                last_err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            # training service history
            try:
                r2 = await client.get(f"{ref.TRAINING_URL}/api/stock/history/{sym}", params={"period": period if period in ("1d","5d","1mo","1y","5y") else "1mo"}, timeout=_hist_timeout)
                if r2.status_code == 200:
                    data = r2.json()
                    data["source"] = data.get("source") or "training"
                    return data
                last_err = f"{last_err}; training HTTP {r2.status_code}"
            except Exception as e:
                last_err = f"{last_err}; training {type(e).__name__}: {e}" if str(e) else f"{last_err}; training {type(e).__name__}"
        raise ref.HTTPException(status_code=503, detail=f"Chart unavailable for {sym}: {last_err}")

    @ops_db_status_router.get("/ops/db-status")
    async def ops_db_status():
        """Frontend banner: is Supabase/Postgres connected on decision-prediction/training?"""
        try:
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                r = await client.get(f"{ref.TRAINING_URL.rstrip('/')}/health")
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        return {
                            "ok": False,
                            "db_connected": False,
                            "db_message": "Training service answered 200 but not with JSON",
                            "db_error": "invalid JSON from training /health",
                        }
                    return {
                        "ok": True,
                        "source": "training",
                        **{k: data.get(k) for k in (
                            "db_backend", "db_durable", "db_connected", "db_provider",
                            "db_message", "db_error", "status",
                        ) if k in data or True},
                    }
                return {
                    "ok": False,
                    "db_connected": False,
                    "db_message": f"Training service health HTTP {r.status_code}",
                    "db_error": f"Training service health HTTP {r.status_code}",
                }
        except Exception as e:
            return {
                "ok": False,
                "db_connected": False,
                "db_backend": "unknown",
                "db_durable": False,
                "db_message": f"Cannot reach training service: {e}",
                "db_error": str(e)[:200],
            }

    @set_watchlist_router.post("/watchlist")
    def set_watchlist(update: ref.WatchlistUpdate):
        symbols = [c for c in (s.strip().upper().replace(".NS", "").replace(".BO", "") for s in update.symbols) if c]
        ref._save_watchlist(symbols)
        ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY)
        return {"symbols": symbols}

    @add_to_watchlist_router.post("/watchlist/add")
    def add_to_watchlist(update: ref.WatchlistUpdate):
        current = set(ref._load_watchlist())
        added, already = [], []
        for s in update.symbols:
            su = s.strip().upper().replace(".NS", "").replace(".BO", "")
            if not su:
                continue
            if su in current:
                already.append(su)
            else:
                current.add(su)
                added.append(su)
        symbols = sorted(current)
        ref._save_watchlist(symbols)
        ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY)
        msg = None
        if already and not added:
            msg = "Already in watchlist"
        elif already:
            msg = f"Already in watchlist: {', '.join(already)}"
        return {"symbols": symbols, "added": added, "already": already, "message": msg}

    @run_scan_post_router.post("/scan")
    async def run_scan_post(
        force_refresh: bool = True,
        lite: ref.Optional[bool] = None,
        background_tasks: ref.BackgroundTasks = None,
    ):
        """
    Compatibility alias: overnight cron and external tools used POST /scan and got 405
    because only GET /scan existed. Delegate to the async parallel scan starter.
    """
        # FastAPI injects BackgroundTasks when annotated. When called directly without one, pass None
        # through: start_scan() then runs the scan on a daemon thread instead of queueing it on a
        # throwaway BackgroundTasks that nothing would ever execute.
        return (await ref.asyncio.to_thread(ref.start_scan, force_refresh=force_refresh, lite=lite, background_tasks=background_tasks))

    @training_other_proxy_router.api_route("/training/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def training_other_proxy(path: str, request: ref.Request):
        """Proxy /training/* to the training sub-app.

    Never forward Accept-Encoding. Always return decoded JSON via JSONResponse
    so the browser never sees gzip/br binary (which causes
    "Unexpected token ... is not valid JSON").
    """
        heavy = any(
            x in path
            for x in (
                "actionable/commit",
                "train",
                "evaluate",
                "mark-to-market",
                "walk",
                "history",
                "clear-backup",
                "universe/ingest",
                "universe/train",
                "api/universe",
                "portfolio/deposit",
            )
        )
        # Shared client default was too short → 504 on ingest; always pass explicit timeout
        timeout = 180.0 if heavy else 90.0
        try:
            body = await request.body()
            fwd_headers = {"Accept": "application/json"}
            ct = request.headers.get("content-type")
            if ct:
                fwd_headers["Content-Type"] = ct

            target_url = f"{ref.TRAINING_URL.rstrip('/')}/{path.lstrip('/')}"
            client = ref._get_http_client()  # shared keepalive pool
            if True:
                response = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    content=body if body else None,
                    params=request.query_params,
                    timeout=timeout,
                )

            # Always try JSON first — portfolio/deposit/trades all return JSON
            try:
                data = response.json()
                return ref.JSONResponse(content=data, status_code=response.status_code)
            except Exception:
                pass

            # Non-JSON upstream (HTML error page, empty, etc.)
            text_body = response.text or ""
            # Strip control chars that break JSON.parse on the client
            safe = "".join(ch for ch in text_body[:800] if ch == "\n" or ch == "\t" or ord(ch) >= 32)
            return ref.JSONResponse(
                content={"detail": safe or f"Upstream HTTP {response.status_code}"},
                status_code=response.status_code if response.status_code >= 400 else 502,
            )
        except ref.httpx.TimeoutException:
            raise ref.HTTPException(status_code=504, detail=f"Training service timeout for /{path}")
        except ref.httpx.RequestError as e:
            raise ref.HTTPException(status_code=502, detail=f"Training service unreachable: {e}")
        except Exception as e:
            ref.logger.exception("training proxy error for path=%s", path)
            raise ref.HTTPException(status_code=502, detail=f"Training proxy error: {e}")

    @repair_all_missing_router.post("/api/feed/repair-all")
    @repair_all_missing_router.post("/data-feed/repair-all")
    async def repair_all_missing(background_tasks: ref.BackgroundTasks, limit: int = 5000):
        """Kick off a background job that repairs EVERY incomplete feed record,
    not just the next 15. Poll /api/feed/repair-all/status for progress."""
        if ref._REFILL_ALL_JOB.get("status") == "running":
            return {"ok": True, "already_running": True, **ref._REFILL_ALL_JOB}
        limit = max(1, min(int(limit or 5000), 5000))
        background_tasks.add_task(ref._run_refill_all_job, limit)
        return {"ok": True, "started": True, "message": "Refill All started in the background."}

    return SimpleNamespace(
        root=root,
        root_router=root_router,
        ops_rate_limits_event=ops_rate_limits_event,
        ops_rate_limits_event_router=ops_rate_limits_event_router,
        ops_check_alert=ops_check_alert,
        ops_check_alert_router=ops_check_alert_router,
        ops_refresh_static_params=ops_refresh_static_params,
        ops_refresh_static_params_router=ops_refresh_static_params_router,
        market_history=market_history,
        market_history_router=market_history_router,
        ops_db_status=ops_db_status,
        ops_db_status_router=ops_db_status_router,
        set_watchlist=set_watchlist,
        set_watchlist_router=set_watchlist_router,
        add_to_watchlist=add_to_watchlist,
        add_to_watchlist_router=add_to_watchlist_router,
        run_scan_post=run_scan_post,
        run_scan_post_router=run_scan_post_router,
        training_other_proxy=training_other_proxy,
        training_other_proxy_router=training_other_proxy_router,
        repair_all_missing=repair_all_missing,
        repair_all_missing_router=repair_all_missing_router,
    )
