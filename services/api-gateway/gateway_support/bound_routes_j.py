"""Route handlers moved out of main.py in E85 (MainRef batch J, after E55 and E76-E84). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py); names that are locals of a handler or of its nested functions stay as they were. `build_bound_routes_j(ref)` returns a namespace: one router per handler
(include each where the handler used to be) and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names, which must be evaluated
when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_j(ref):
    ops_wake_db_all_router = APIRouter()
    system_health_router = APIRouter()
    start_scan_router = APIRouter()
    stream_market_scan_router = APIRouter()
    scan_watchlist_router = APIRouter()
    send_picks_to_telegram_router = APIRouter()
    api_surprise_scan_stream_router = APIRouter()
    api_surprise_notify_top_picks_router = APIRouter()
    catalyst_alert_scan_router = APIRouter()
    stockky_hot_premarket_router = APIRouter()
    stockky_hot_run_router = APIRouter()
    api_hotpicks_notify_top_picks_router = APIRouter()

    @ops_wake_db_all_router.get("/ops/wake-db-all")
    @ops_wake_db_all_router.post("/ops/wake-db-all")
    async def ops_wake_db_all():
        """
    One button/one call to wake every Neon-backed database this app talks to:
      - gateway's own Neon connection (kv_cache / feed store)
      - training service's DB (TRAINING_DATABASE_URL / DATABASE_URL — via its
        own /health, which pings the DB on the way through)
      - training service's separate cache DB, if CACHE_DATABASE_URL is set
        (kv_cache.py on the training side falls back through
        TRAINING_DATABASE_URL -> DATABASE_URL -> CACHE_DATABASE_URL -> KV_DATABASE_URL,
        so hitting /training/health also warms whichever one is actually wired up)

    Called automatically on frontend page load, and available as a manual
    "Wake DB" button in the Dashboard header and Settings tab so a cold Neon
    compute doesn't make the first click of the day wait 1-3s per query.
    """
        out: dict = {"ok": True, "at": ref.datetime.now(ref.IST).isoformat(), "targets": {}}

        # group302: the old body ran the gateway's blocking DB ping (a pool checkout + SELECT) directly on the event
        # loop, so a busy pool froze every other request, and any slow target turned the whole pill red even though the
        # Oracle database was fine. Now: the ping runs in a worker thread under a budget, a failed target is retried once,
        # and `ok` means "the durable database answered" (any target), with per-target detail kept for the UI.
        # DB_WAKE_TIMEOUT_S (default 20) is the per-target budget.
        try:
            _budget = max(3.0, float(((ref.os.getenv("DB_WAKE_TIMEOUT_S") or "").strip() or "20")))
        except ValueError:
            _budget = 20.0

        async def _gateway_target() -> dict:
            try:
                return await ref.asyncio.wait_for(ref.asyncio.to_thread(ref._neon_keepalive_ping), timeout=_budget)
            except ref.asyncio.TimeoutError:
                return {"ok": False, "error": f"no answer within {_budget:.0f}s (gateway busy, not necessarily down)"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200]}

        async def _training_target() -> dict:
            try:
                client = ref._get_http_client()
                url = (ref.TRAINING_URL or "").rstrip("/")
                if not url:
                    return {"ok": False, "error": "TRAINING_URL not configured"}
                r = await client.get(f"{url}/health", params={"warm": "true"}, timeout=_budget)
                training_ok = r.status_code == 200
                data = {}
                try:
                    data = r.json() if training_ok else {}
                except Exception:
                    data = {}
                return {
                    "ok": training_ok and data.get("db_connected") is not False,
                    "db_connected": data.get("db_connected"),
                    "db_backend": data.get("db_backend"),
                }
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200] or type(e).__name__}

        async def _with_one_retry(fn) -> dict:
            res = await fn()
            if not res.get("ok"):
                await ref.asyncio.sleep(1.5)
                res2 = await fn()
                if res2.get("ok"):
                    res2["retried"] = True
                    return res2
            return res

        gw, tr = await ref.asyncio.gather(_with_one_retry(_gateway_target), _with_one_retry(_training_target))
        out["targets"]["gateway_neon"] = gw
        out["targets"]["training_db"] = tr
        flags = [bool(t.get("ok")) for t in out["targets"].values()]
        out["all_ok"] = all(flags)
        out["ok"] = any(flags)
        out["partial"] = out["ok"] and not out["all_ok"]
        return out

    @system_health_router.get("/system/health")
    async def system_health():
        async def check(name: str, url: str, required: bool):
            if not url:
                return name, {"ok": False, "required": required, "status": "not_configured", "url": None}
            timeout = 15 if name == "market-data" else 10
            try:
                client = ref._get_http_client()  # shared keepalive pool
                if True:
                    resp = await client.get(f"{url.rstrip('/')}/health", timeout=timeout)
                if resp.status_code == 200:
                    body = {}
                    try:
                        body = resp.json() if resp.content else {}
                    except Exception:
                        body = {}
                    failed = body.get("failed") or []
                    mounts = body.get("mounts") or {}
                    # Sub-services that expose MOUNT_STATUS: degraded if any mount failed
                    mount_ok = not failed
                    ok = mount_ok
                    status = "up" if ok else "degraded"
                    entry = {
                        "ok": ok,
                        "required": required,
                        "status": status,
                        "url": url,
                    }
                    if failed:
                        entry["failed_mounts"] = failed
                    if mounts:
                        entry["mounts"] = mounts
                    if body.get("status"):
                        entry["upstream_status"] = body.get("status")
                    return name, entry
                return name, {"ok": False, "required": required, "status": f"http_{resp.status_code}", "url": url}
            except Exception as e:
                return name, {"ok": False, "required": required, "status": "unreachable", "error": str(e)[:100], "url": url}

        results = await ref.asyncio.gather(
            *(check(name, cfg["url"], cfg["required"]) for name, cfg in ref.SYSTEM_SERVICES.items())
        )
        services = {"api-gateway": {"ok": True, "required": True, "status": "up", "url": None}}
        services.update(dict(results))
        required_ok = all(v["ok"] for v in services.values() if v["required"])
        all_ok = all(v["ok"] for v in services.values())
        return {"required_ok": required_ok, "all_ok": all_ok, "services": services}

    @start_scan_router.post("/scan/start")
    def start_scan(
        force_refresh: bool = False,
        lite: bool = None,
        background_tasks: ref.BackgroundTasks = None,
    ):
        """
    Start async parallel market scan.
    - force_refresh: rebuild universe + ignore last-scan cache
    - lite: skip Gemini + redundant enrichment when Decision already has data (faster on free tier)
    If a recent full scan exists (LAST_FULL_SCAN_TTL) and force_refresh is false, returns cached task.
    """
        try:
            if lite is None:
                use_lite = ref.SCAN_LITE_DEFAULT or ref._should_force_lite_scan()
            else:
                use_lite = bool(lite)
            # When circuits open, always prefer lite unless caller forced full (lite=False explicitly is respected)

            if force_refresh:
                ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY, ref.LAST_FULL_SCAN_KEY)

            # Serve recent COMPLETE scan from cache unless force_refresh.
            # Partial scans (e.g. 60/300 after Stop) must CONTINUE: cache-hit the done
            # symbols via batch_result/Neon, then process the remaining universe upstream.
            if not force_refresh:
                cached = ref._redis_get(ref.LAST_FULL_SCAN_KEY)
                if cached and isinstance(cached, dict) and cached.get("result"):
                    res = cached.get("result") or {}
                    processed = int(
                        cached.get("processed")
                        or res.get("scanned")
                        or 0
                    )
                    total = int(
                        cached.get("total")
                        or res.get("universe_size")
                        or processed
                        or 0
                    )
                    is_partial = bool(
                        cached.get("partial")
                        or cached.get("cancelled")
                        or res.get("partial")
                        or res.get("stopped_early")
                        or res.get("cancelled")
                        or (total > 0 and processed < int(total * 0.9))
                    )
                    if not is_partial and total > 0 and processed >= int(total * 0.9):
                        task_id = cached.get("task_id") or str(ref.uuid.uuid4())
                        ref._redis_set(ref.SCAN_TASK_PREFIX + task_id, {
                            "status": "done",
                            "total": total,
                            "processed": processed,
                            "elapsed": res.get("elapsed_seconds", 0),
                            "result": res,
                            "error": None,
                            "from_cache": True,
                            "scanned_at": cached.get("scanned_at"),
                        }, ttl=3600)
                        return {
                            "task_id": task_id,
                            "from_cache": True,
                            "scanned_at": cached.get("scanned_at"),
                            "universe_size": total,
                            "message": "Returning recent complete scan (within cache TTL). Use force_refresh=true for a new run.",
                        }
                    else:
                        ref.logger.info(
                            "Last scan was partial (%s/%s) — continuing full universe "
                            "(batch_result/DB cache hits for already scored symbols)",
                            processed, total,
                        )

            if force_refresh:
                ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY)

            universe = ref._build_scan_universe_forced() if force_refresh else ref._build_scan_universe()
            task_id = str(ref.uuid.uuid4())
            if background_tasks is not None:
                background_tasks.add_task(ref.run_scan_parallel, task_id, universe, use_lite)
            else:
                # Called directly (FastAPI always injects one over HTTP): run the scan on a daemon thread
                # with its own event loop, same fallback idiom as /data-feed/start-bulk-feed. Previously
                # this was an AttributeError reported as a 500.
                import threading

                def _scan_thread():
                    try:
                        ref.asyncio.run(ref.run_scan_parallel(task_id, universe, use_lite))
                    except Exception as _e:  # run_scan_parallel records its own failures; this is a last resort
                        ref.logger.error("Scan thread %s crashed: %s", task_id, _e)

                threading.Thread(target=_scan_thread, name=f"scan-{task_id[:8]}", daemon=True).start()
            return {
                "task_id": task_id,
                "from_cache": False,
                "lite": use_lite,
                "universe_size": len(universe),
                "message": f"Scanning {len(universe)} symbols (dynamic universe; DB/batch cache for hits, upstream for rest)",
            }
        except Exception as e:
            ref.logger.error(f"Scan start failed: {e}")
            raise ref.HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")

    @stream_market_scan_router.get("/api/scan/stream")
    @stream_market_scan_router.get("/scan/stream")
    async def stream_market_scan(
        lite: bool = None,
        force_refresh: bool = False,
    ):
        """
    Stream market-scan results as application/x-ndjson.
    Each line is one completed symbol JSON object so the UI can update
    incrementally instead of waiting for the full 300-stock response
    (avoids Render 100s gateway timeout and perceived freezes).

    Flow:
      1. Bulk Neon get_many for all static feeds (1 query)
      2. Bounded concurrent workers (Semaphore)
      3. Yield each result as soon as its batch chunk finishes
    """
        if lite is None:
            use_lite = ref.SCAN_LITE_DEFAULT or ref._should_force_lite_scan()
        else:
            use_lite = bool(lite)

        universe = (await ref.asyncio.to_thread(ref._build_scan_universe))
        if force_refresh:
            try:
                ref._redis_set(ref.SCAN_UNIVERSE_KEY, None, ttl=1)
            except Exception:
                pass
            universe = (await ref.asyncio.to_thread(ref._build_scan_universe_forced))
        universe = await ref.asyncio.to_thread(ref._prioritize_universe, universe)  # group361: off the event loop
        total = len(universe)

        async def event_generator():
            client = ref._get_http_client()
            sem = ref.asyncio.Semaphore(ref.MAX_PARALLEL_WORKERS)
            start = ref.time.time()
            # Never inherit a stale Power-Off / cancel from a previous session
            try:
                ref._SCAN_CANCEL_FLAGS.discard("__ALL__")
            except Exception:
                pass
            # Critical: clear activity_paused so a prior Power-Off does not abort
            # the stream after the first chunk (was causing 10–25 symbol "full" scans).
            try:
                ref.set_activity_paused(False)
            except Exception:
                pass

            if total <= 0:
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "error",
                    "error": "empty_universe",
                    "total": 0,
                }) + "\n"
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "done",
                    "processed": 0,
                    "total": 0,
                    "elapsed": 0,
                }) + "\n"
                return

            # 1) Single bulk Neon load (canonical stockky:data_feed:sym: + alias feed:)
            prefetched = {}
            try:
                from data_feed import get_all_stock_feeds
                bases = [
                    s.upper().replace(".NS", "").replace(".BO", "").strip()
                    for s in universe
                ]
                prefetched = await ref.asyncio.to_thread(get_all_stock_feeds, bases) or {}  # group361: off the event loop
                # Also warm process-local store for any later get_symbol
                try:
                    store = ref._feed_store()
                    for b, row in prefetched.items():
                        if isinstance(row, dict):
                            store.put_symbol  # attribute check
                    # lightweight local warm without re-write to Neon
                    from data_feed import DATA_FEED_PREFIX, FEED_ALIAS_PREFIX, _LOCAL_SYMBOLS, _LOCAL_INDEX
                    for b, row in prefetched.items():
                        if not isinstance(row, dict):
                            continue
                        _LOCAL_SYMBOLS[DATA_FEED_PREFIX + b] = dict(row)
                        _LOCAL_SYMBOLS[FEED_ALIAS_PREFIX + b] = dict(row)
                        _LOCAL_INDEX.add(b)
                except Exception:
                    pass
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "feed_bulk_loaded",
                    "total": total,
                    "feed_hits": len(prefetched),
                    "lite": use_lite,
                    "workers": ref.MAX_PARALLEL_WORKERS,
                }) + "\n"
            except Exception as e:
                ref.logger.warning("scan stream feed bulk: %s", e)
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "feed_bulk_error",
                    "error": str(e)[:200],
                    "total": total,
                }) + "\n"

            # 2) Process in chunks so results stream early
            chunk_size = max(10, min(25 if use_lite else ref.SCAN_BATCH_SIZE, 25 if use_lite else 15))
            processed = 0
            for i in range(0, total, chunk_size):
                if "__ALL__" in ref._SCAN_CANCEL_FLAGS:
                    yield ref.json.dumps({
                        "_meta": True,
                        "event": "cancelled",
                        "processed": processed,
                        "total": total,
                    }) + "\n"
                    break
                # Explicit cancel only — residual activity_paused must not stop the stream.
                chunk = universe[i : i + chunk_size]
                # Keepalive meta so proxies/clients don't idle-timeout mid-universe
                try:
                    yield ref.json.dumps({
                        "_meta": True,
                        "event": "heartbeat",
                        "processed": processed,
                        "total": total,
                        "chunk": i // max(chunk_size, 1) + 1,
                        "elapsed": round(ref.time.time() - start, 1),
                    }) + "\n"
                except Exception:
                    pass
                # Live prices for this chunk in parallel
                try:
                    chunk_prices = await ref._fetch_prices_bulk_async(chunk, client)
                except Exception:
                    chunk_prices = {}
                for sym in chunk:
                    base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                    px = chunk_prices.get(base)
                    if px is None:
                        continue
                    if not isinstance(prefetched, dict):
                        prefetched = {}
                    row = dict(prefetched.get(base) or {})
                    row.setdefault("close", px)
                    row.setdefault("price", px)
                    prefetched[base] = row

                # Sticky Fix Step 3: lite = pure gateway eval (no decision HTTP)
                if use_lite:
                    batch = []
                    for sym in chunk:
                        base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                        fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                        px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                        try:
                            batch.append(ref._lite_evaluate_from_feed(sym, fed or {}, px))
                        except Exception as e:
                            batch.append({"symbol": base, "decision": "ERROR", "error": str(e)[:200]})
                else:
                    # Full mode: bounded wait per symbol; cold microservices must not stall the stream
                    STREAM_SYM_TIMEOUT = float(((ref.os.getenv("SCAN_STREAM_SYMBOL_TIMEOUT") or "").strip() or "12"))

                    async def _ultra_or_instant(sym: str):
                        base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                        fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                        px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                        try:
                            return await ref.asyncio.wait_for(
                                ref._analyze_one_symbol_ultra(
                                    sym,
                                    client,
                                    sem,
                                    lite=False,
                                    # Gemini is reserved for single-stock Analyse
                                    # (/stock/{symbol}) — a 300-500 symbol Run Market
                                    # Scan calling Gemini once per stock would blow
                                    # through its RPM limit and slow the whole scan
                                    # down for a summary nobody reads mid-scan. Every
                                    # other pillar still runs at full depth (lite stays
                                    # False) — only the Gemini call is skipped.
                                    skip_gemini=True,
                                    feed_row=fed,
                                    prefetched_feeds=prefetched,
                                ),
                                timeout=STREAM_SYM_TIMEOUT,
                            )
                        except Exception as e:
                            ref.logger.debug("stream ultra fallback %s: %s", base, e)
                            try:
                                from instant_scanner import compute_instant_scores
                                out = compute_instant_scores(
                                    base, fed or {}, {"price": px} if px > 0 else {}
                                )
                                out["fallback_instant"] = True
                                out["fallback_reason"] = str(e)[:120]
                                return out
                            except Exception as e2:
                                return {
                                    "symbol": base,
                                    "decision": "ERROR",
                                    "error": str(e2)[:200],
                                }

                    try:
                        batch = await ref.asyncio.gather(
                            *[_ultra_or_instant(sym) for sym in chunk],
                            return_exceptions=True,
                        )
                    except Exception as e:
                        ref.logger.exception("scan stream chunk failed: %s", e)
                        for sym in chunk:
                            processed += 1
                            yield ref.json.dumps({
                                "symbol": sym,
                                "decision": "ERROR",
                                "error": str(e)[:200],
                                "_progress": {
                                    "processed": processed,
                                    "total": total,
                                    "elapsed": round(ref.time.time() - start, 1),
                                },
                            }, default=str) + "\n"
                        continue
                for sym, res in zip(chunk, batch):
                    processed += 1
                    if isinstance(res, Exception):
                        try:
                            base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                            fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else {}
                            px = float(chunk_prices.get(base) or 0) if chunk_prices else 0.0
                            from instant_scanner import compute_instant_scores
                            out = compute_instant_scores(base, fed or {}, {"price": px} if px > 0 else {})
                            out["fallback_instant"] = True
                            out["fallback_reason"] = str(res)[:120]
                        except Exception:
                            out = {
                                "symbol": sym,
                                "decision": "ERROR",
                                "error": str(res)[:200],
                            }
                    elif isinstance(res, dict):
                        out = res
                    else:
                        out = {"symbol": sym, "decision": "ERROR", "error": "invalid"}
                    # Sticky Fix Step 1: unified price resolution + all frontend aliases
                    if isinstance(out, dict):
                        try:
                            from price_resolver import ensure_row_price, extract_safe_price, apply_price_aliases
                            base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                            fed = (prefetched or {}).get(base) if isinstance(prefetched, dict) else None
                            tick = {"price": None}
                            # Prefer bulk-injected feed close already in prefetched
                            out = ensure_row_price(out, feed=fed if isinstance(fed, dict) else None)
                            if extract_safe_price(decision=out) <= 0:
                                try:
                                    px = ref._fetch_price_from_quote(sym)
                                    if px is not None:
                                        out = apply_price_aliases(out, float(px))
                                except Exception:
                                    pass
                        except Exception as _pe:
                            ref.logger.debug("price_resolver stream: %s", _pe)
                    out["_progress"] = {
                        "processed": processed,
                        "total": total,
                        "elapsed": round(ref.time.time() - start, 1),
                    }
                    yield ref.json.dumps(out, default=str) + "\n"

            yield ref.json.dumps({
                "_meta": True,
                "event": "done",
                "processed": processed,
                "total": total,
                "elapsed": round(ref.time.time() - start, 1),
            }) + "\n"

        return ref.StreamingResponse(
            event_generator(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @scan_watchlist_router.get("/scan/watchlist")
    def scan_watchlist():
        watchlist = ref._load_watchlist()
        if not watchlist:
            return {
                "scanned": 0,
                "universe_size": 0,
                "watchlist_size": 0,
                "recommendations": [],
                "watchlist_candidates": [],
                "verdict": "Watchlist is empty. Add some symbols first.",
                "market_mood": "Neutral",
                "market_stats": {
                    "buy_signals": 0,
                    "sell_signals": 0,
                    "hold_signals": 0,
                    "cautious": 0,
                },
                "all_results": [],
                "errors": [],
            }

        results = []
        errors = []

        # Fix: this used to loop over the watchlist sequentially, and each
        # symbol already does its own chain of upstream calls (decide, quote
        # fallback, fundamentals merge, news, events, prediction) — several
        # seconds each in the warm case, tens of seconds in the cold case (see
        # the decision/decide/RELIANCE timeout in the same test run). Serialized
        # across even a handful of watchlist symbols, that easily blows past any
        # normal client/proxy timeout. Bounded concurrency turns
        # O(N × per-symbol time) into O(N / concurrency × per-symbol time)
        # instead, without changing per-symbol behavior at all.
        #
        # Fix (30 Aug 2026, round 2 — REVERTED, see round 3): raising
        # WATCHLIST_SCAN_CONCURRENCY 4 -> 8 here made things *worse*:
        # test_20260830_134442.log shows /scan/watchlist still timing out, and
        # now market/top-gainers and market/top-losers (completely unrelated
        # endpoints) started timing out too. Cause: this function's per-symbol
        # concurrency is a *second* layer on top of each symbol's own 5-way
        # inner job concurrency (price/fundamentals/news/events/prediction), so
        # outer=8 meant up to 8 x 5 = 40 simultaneous outbound calls hitting
        # analysis-intelligence-service and market-data-service at once. This
        # codebase already has an established, deliberately conservative cap for
        # exactly this reason — see MAX_PARALLEL_WORKERS above and its comment:
        # "Higher values (12-20) spawn 4-5 internal HTTP calls per stock ->
        # 50-100+ concurrent requests ... causing PoolTimeout / ReadTimeout and
        # circuit-breaker opens". 40 concurrent calls from watchlist scan alone
        # blew straight through that ceiling and starved every other endpoint
        # sharing the same free-tier backends, which is exactly what the second
        # test run showed.
        #
        # Fix (30 Aug 2026, round 3): one shared semaphore
        # (_SCAN_HTTP_CONCURRENCY, aligned with MAX_PARALLEL_WORKERS, default 8)
        # now caps TOTAL concurrent outbound calls across every symbol and every
        # inner job in this request, regardless of outer fan-out — matching the
        # pattern already used by /scan's own asyncio.Semaphore(8). Outer
        # concurrency is back down to a modest default; it no longer needs to
        # carry the load-limiting responsibility, the semaphore does.
        _SCAN_CONCURRENCY = int(((ref.os.getenv("WATCHLIST_SCAN_CONCURRENCY") or "").strip() or "4"))
        _SCAN_HTTP_CONCURRENCY = int(((ref.os.getenv("WATCHLIST_SCAN_HTTP_CONCURRENCY") or "").strip() or str(ref.MAX_PARALLEL_WORKERS)))
        _SCAN_DEADLINE_SECONDS = float(((ref.os.getenv("WATCHLIST_SCAN_TIMEOUT_SECONDS") or "").strip() or "18"))
        _http_sem = ref.threading.BoundedSemaphore(max(1, _SCAN_HTTP_CONCURRENCY))

        def _scan_one_symbol(client: ref.httpx.Client, symbol: str):
            with _http_sem:
                resp = client.get(f"{ref.DECISION_URL}/decide/{symbol}")
            resp.raise_for_status()
            raw = resp.json()
            normalized = ref._normalize_decision_response(raw, symbol)

            # Fix (30 Aug 2026): these four enrichment lookups (price fallback,
            # fundamentals merge, news, events, prediction) are independent of
            # one another — each only reads/writes its own keys on `normalized`
            # — but used to run one after another. On a cache miss each can
            # cost several seconds (fundamentals in particular; see
            # peer_multi_quarter.py's module docstring in
            # analysis-intelligence-service for why that one was especially
            # slow), so sequentially they could push a single symbol's scan
            # time past any reasonable client timeout — this is what made
            # /scan/watchlist time out in testing even after the per-symbol
            # decide/predict calls themselves were fast. Running them
            # concurrently bounds one symbol's wall time to the slowest single
            # lookup instead of the sum of all of them, on top of the
            # outer per-symbol concurrency already in place above.
            # Each job below acquires the *shared* _http_sem (not a per-symbol
            # one) before its actual network call — that's what keeps total
            # concurrent outbound calls across the whole watchlist scan capped
            # at _SCAN_HTTP_CONCURRENCY regardless of how many symbols/jobs are
            # queued. Acquiring only around the call itself (not the whole job
            # function) keeps cheap no-op jobs (e.g. price/news/events when
            # already cached on `normalized`) from taking a slot they don't
            # need.
            def _job_price():
                if normalized.get("close") is None:
                    with _http_sem:
                        return ref._fetch_price_from_quote(symbol)
                return None

            def _job_fundamentals():
                with _http_sem:
                    ref._merge_fundamentals(normalized, symbol)
                return None

            def _job_news():
                if normalized.get("news_score") is None:
                    with _http_sem:
                        return ref._fetch_news(symbol)
                return None

            def _job_events():
                if normalized.get("event_risk") is False and not normalized.get("reasons", {}).get("event"):
                    with _http_sem:
                        return ref._fetch_events(symbol)
                return None

            def _job_prediction():
                if normalized.get("prediction_score") is None:
                    try:
                        with _http_sem:
                            pred_resp = client.get(f"{ref.PREDICTION_URL}/predict/{symbol}", timeout=30)
                        if pred_resp.status_code == 200:
                            return pred_resp.json()
                    except Exception as e:
                        ref.logger.warning(f"Prediction service lookup failed during watchlist scan for {symbol}: {e}")
                return None

            jobs = {
                "price": _job_price,
                "fundamentals": _job_fundamentals,
                "news": _job_news,
                "events": _job_events,
                "prediction": _job_prediction,
            }
            job_results = {}
            with ref.ThreadPoolExecutor(max_workers=len(jobs)) as inner_pool:
                futures = {inner_pool.submit(fn): name for name, fn in jobs.items()}
                for fut in ref.as_completed(futures):
                    name = futures[fut]
                    try:
                        job_results[name] = fut.result()
                    except Exception as e:
                        ref.logger.warning(f"Watchlist scan enrichment '{name}' failed for {symbol}: {str(e) or type(e).__name__}")
                        job_results[name] = None

            price = job_results.get("price")
            if price is not None:
                normalized["close"] = price
                if normalized.get("support") is None:
                    normalized["support"] = round(price * 0.95, 2)
                if normalized.get("resistance") is None:
                    normalized["resistance"] = round(price * 1.05, 2)

            news = job_results.get("news")
            if news:
                normalized["news_score"] = news.get("news_score")
                reasons = normalized.get("reasons", {})
                if news.get("reasons"):
                    reasons["news"] = news["reasons"]
                    normalized["reasons"] = reasons

            events = job_results.get("events")
            if events:
                # See the async analyzer's equivalent block for why
                # the full dict is passed through, not just
                # next_earnings_date.
                normalized["event_data"] = events
                if events.get("next_earnings_date"):
                    normalized["event_risk"] = True
                    reasons = normalized.get("reasons", {})
                    reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                    normalized["reasons"] = reasons

            pred_data = job_results.get("prediction")
            if pred_data and pred_data.get("model_loaded"):
                normalized["prediction_score"] = pred_data.get("prediction_score")
                normalized["prediction_note"] = pred_data.get("note")

            # Adds a concrete calendar-date holding period estimate
            # alongside whatever decision-engine's own holding_period
            # string is (often a static "2-6 weeks" or "N/A") — kept as
            # a separate field so nothing that already reads
            # holding_period breaks.
            entry = normalized.get("entry_range") or {}
            entry_price = entry.get("low") or normalized.get("close")
            normalized["holding_period_estimate"] = ref._estimate_holding_period(
                entry_price, normalized.get("target"), normalized.get("decision")
            )
            normalized["natural_language_summary"] = ref._generate_summary(normalized)
            return normalized

        # timeout=30 (was 180): 180s per HTTP call was never the intent, it was
        # just "big enough to not be the thing that times out" from before the
        # deadline below existed. Now that the endpoint has its own explicit
        # wall-clock budget, a single upstream call hanging for 180s is exactly
        # the failure mode we need to bound, not paper over.
        limits = ref.httpx.Limits(max_connections=_SCAN_HTTP_CONCURRENCY * 2, max_keepalive_connections=_SCAN_HTTP_CONCURRENCY)
        client = ref.httpx.Client(timeout=30, limits=limits)
        pool = ref.ThreadPoolExecutor(max_workers=max(1, _SCAN_CONCURRENCY))
        not_done = set()
        try:
            futures = {pool.submit(_scan_one_symbol, client, symbol): symbol for symbol in watchlist}
            done, not_done = ref.wait(futures.keys(), timeout=_SCAN_DEADLINE_SECONDS)
            for fut in done:
                symbol = futures[fut]
                try:
                    results.append(fut.result())
                except ref.httpx.HTTPError as e:
                    ref.logger.warning("Watchlist scan skipped %s: %s", symbol, e)
                    errors.append({"symbol": symbol, "error": str(e)})
                except Exception as e:
                    ref.logger.warning("Watchlist scan failed for %s: %s", symbol, e)
                    errors.append({"symbol": symbol, "error": str(e)})
            for fut in not_done:
                symbol = futures[fut]
                ref.logger.warning(
                    "Watchlist scan deadline (%gs) hit before %s completed — "
                    "returning partial results instead of blocking the response.",
                    _SCAN_DEADLINE_SECONDS, symbol,
                )
                errors.append({
                    "symbol": symbol,
                    "error": f"scan deadline ({_SCAN_DEADLINE_SECONDS:g}s) exceeded, skipped this run",
                })
        finally:
            # Fix (30 Aug 2026, round 3): round 2's `pool.shutdown(wait=False,
            # cancel_futures=True)` here never closed `client` at all — every
            # /scan/watchlist call that hit the deadline leaked an
            # httpx.Client (and its open connection pool) for good, which on
            # repeated calls compounds into exactly the kind of connection/
            # resource exhaustion that starves unrelated endpoints. cancel_
            # futures only drops jobs that hadn't started yet — anything mid-
            # flight keeps running and still needs the client until it's done.
            # Fix: hand the still-running stragglers, the pool, and the client
            # to a background daemon thread that waits for them (bounded by its
            # own grace period) and only then shuts down / closes — so
            # resources always get released, just not on the response's clock.
            if not_done:
                ref.threading.Thread(
                    target=ref._cleanup_scan_resources,
                    args=(pool, client, list(not_done), _SCAN_DEADLINE_SECONDS),
                    daemon=True,
                    name="watchlist-scan-cleanup",
                ).start()
            else:
                pool.shutdown(wait=False)
                client.close()

        results.sort(key=ref._score_sort_key, reverse=True)
        actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
        top_picks = ref._select_top_picks(actionable, limit=5)
        # horizon picks for watchlist scan
        def _horizon_picks_wl(results_list, horizon_key, limit=5):
            scored = []
            for r in results_list:
                if r.get("decision") == "ERROR":
                    continue
                hz = (r.get("horizons") or {}).get(horizon_key) or {}
                sc = hz.get("score")
                if sc is None:
                    sc = r.get("combined_score", 0) or 0
                    if horizon_key == "mid":
                        sc = sc * 0.95
                    elif horizon_key == "long":
                        sc = (r.get("fundamental_score") or sc) * 0.9 + (r.get("combined_score") or 0) * 0.1
                decision = hz.get("decision") or r.get("decision")
                min_sc = {"short": 54, "mid": 56, "long": 58}.get(horizon_key, 54)
                # A DO NOT BUY row is never promoted to PREPARE TO BUY on score alone
                # (it used to be, which put "Top N Picks" under a "DO NOT BUY ANY
                # STOCK TODAY" verdict). An empty board still falls back to the
                # overall ranking below, with each row's true decision.
                if decision in ("DO NOT BUY", "SELL"):
                    continue
                if decision in ("BUY NOW", "PREPARE TO BUY") or (sc or 0) >= min_sc:
                    scored.append({**r, "_hz_score": sc, "horizon_focus": horizon_key})
            scored.sort(key=ref._board_rank_key, reverse=True)
            return scored[:limit]
        top_picks_short = _horizon_picks_wl(results, "short")
        top_picks_mid = _horizon_picks_wl(results, "mid")
        top_picks_long = _horizon_picks_wl(results, "long")
        # Guarantee Top-5 boards never empty (watchlist scan path)
        sorted_all = sorted(
            [r for r in results if r.get("decision") != "ERROR"],
            key=lambda x: x.get("combined_score", 0) or 0,
            reverse=True,
        )
        if not top_picks_short:
            top_picks_short = top_picks or sorted_all[:5]
        if not top_picks_mid:
            top_picks_mid = sorted_all[5:10] if len(sorted_all) >= 10 else sorted_all[:min(5, len(sorted_all))]
        if not top_picks_long:
            top_picks_long = sorted_all[10:15] if len(sorted_all) >= 15 else sorted_all[:min(5, len(sorted_all))]
        final_verdict_scan = {
            "preferred_horizon": "short",
            "short_count": len(top_picks_short),
            "mid_count": len(top_picks_mid),
            "long_count": len(top_picks_long),
            "headline": f"Short: {len(top_picks_short)} pick(s). Mid: {len(top_picks_mid)}, Long: {len(top_picks_long)}.",
            "best_short": top_picks_short[0].get("symbol") if top_picks_short else None,
        }
        ref._record_symbol_outcomes(results)  # feeds universe self-pruning — see _build_scan_universe

        buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
        sell_count = len([r for r in results if r.get("decision") == "SELL"])
        hold_count = len([r for r in results if r.get("decision") == "HOLD"])

        if buy_count >= 5:
            market_mood = "Bullish"
        elif sell_count > buy_count:
            market_mood = "Bearish"
        elif buy_count > 0:
            market_mood = "Selective"
        else:
            market_mood = "Cautious"

        verdict = f"{len(top_picks)} opportunity(ies) found" if top_picks else "No strong signals in your watchlist"

        result = {
            "scanned": len(results),
            "universe_size": len(watchlist),
            "watchlist_size": len(watchlist),
            "recommendations": top_picks_short,
            "recommendations_short": top_picks_short,
            "recommendations_mid": top_picks_mid,
            "recommendations_long": top_picks_long,
            "final_verdict": final_verdict_scan,
            "watchlist_candidates": [],
            "verdict": verdict,
            "market_mood": market_mood,
            "market_stats": {
                "buy_signals": buy_count,
                "sell_signals": sell_count,
                "hold_signals": hold_count,
                "cautious": len(results) - buy_count - sell_count - hold_count,
            },
            "all_results": results,
            "errors": errors,
            # True whenever the deadline above cut the run short (fewer results
            # than watchlist entries and at least one "deadline exceeded" error)
            # — lets callers/UI show "partial results, retry for the rest"
            # instead of silently looking like a smaller watchlist.
            "partial": len(results) < len(watchlist) and any("deadline" in e.get("error", "") for e in errors),
        }

        # Fix (30 Aug 2026, round 3): this was a synchronous call on the
        # response path — _send_scan_notification does a real outbound POST
        # (timeout=15) plus a possible second CallMeBot POST (timeout=20), so
        # on top of the scan deadline above it could add another 15-20s+ before
        # the response went out, which is what pushed this endpoint from ~18s
        # to over the client's 25s cutoff. Notifications are best-effort side
        # effects, not something the caller is waiting on — send it from a
        # background thread so it can take as long as it needs without holding
        # up the /scan/watchlist response.
        ref.threading.Thread(
            target=ref._send_scan_notification,
            args=(result.get("recommendations", []), result["verdict"], result["scanned"], result["universe_size"]),
            daemon=True,
            name="watchlist-scan-notify",
        ).start()
        return result

    @send_picks_to_telegram_router.post("/notifications/send-picks", dependencies=[ref.Depends(ref._require_admin)])
    def send_picks_to_telegram(payload: dict):
        recs = payload.get("recommendations", [])
        if not recs:
            raise ref.HTTPException(status_code=400, detail="No recommendations provided")
        if not isinstance(recs, list):
            # A string used to be sliced per character into "(invalid pick)" lines.
            raise ref.HTTPException(status_code=400, detail="recommendations must be a list")

        ref._wake_notification_service()

        msg_type = payload.get("type", "top5")
        if msg_type == "top5":
            title = "📊 *Top 5 Picks from Market Scan*"
            picks = recs[:5]
        else:
            title = "📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)*"
            picks = recs

        def _num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _fmt_holding(h):
            # scan_watchlist attaches holding_period_estimate as a dict; print it
            # as a readable range rather than a Python repr.
            if isinstance(h, dict):
                lo, hi = h.get("min_days"), h.get("max_days")
                if lo is not None and hi is not None:
                    return f"{lo}–{hi} days" if lo != hi else f"{lo} days"
                if hi is not None:
                    return f"up to {hi} days"
                if lo is not None:
                    return f"{lo}+ days"
                return "N/A"
            return h

        def format_pick(r, index):
            if not isinstance(r, dict):
                return f"{index}. *(invalid pick)*"
            symbol = r.get("symbol") or "?"
            decision = r.get("decision") or "UNKNOWN"
            score = r.get("combined_score")
            if score is None:
                score = r.get("score") or 0
            close = _num(r.get("close"))
            target = _num(r.get("target"))
            stop = _num(r.get("stop_loss"))
            entry = r.get("entry_range")
            if not isinstance(entry, dict):
                entry = {}
            entry_low = _num(entry.get("low"))
            entry_high = _num(entry.get("high"))
            holding = _fmt_holding(r.get("holding_period") or r.get("holding_period_estimate") or "N/A")
            lines = [f"{index}. *{symbol}* – {decision} (Score: {score})"]
            if close is not None:
                lines.append(f"   Current: ₹{close:.2f}")
            if entry_low is not None and entry_high is not None:
                lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
            if target is not None:
                upside = ((target - close) / close * 100) if close else 0
                # ``:+`` keeps the sign right for a target below the close.
                lines.append(f"   Target: ₹{target:.2f} ({upside:+.1f}%)")
            if stop is not None:
                lines.append(f"   Stop: ₹{stop:.2f}")
            if holding and holding != "N/A":
                lines.append(f"   Hold: {holding}")
            return "\n".join(lines)

        def _delivered(resp, what: str) -> bool:
            """Parse the notify reply; any malformed reply is a 502, not a 500."""
            try:
                data = resp.json()
            except ValueError as e:
                raise ref.HTTPException(status_code=502, detail=f"{what}: notification service returned a non-JSON reply: {e}")
            if not isinstance(data, dict):
                raise ref.HTTPException(status_code=502, detail=f"{what}: notification service returned an unexpected reply")
            if data.get("delivered"):
                return True
            error_note = data.get("note", "Delivery failed")
            raise ref.HTTPException(status_code=502, detail=f"{what}: {error_note}")

        if msg_type == "top5":
            lines = [title, ""]
            for i, r in enumerate(picks, 1):
                lines.append(format_pick(r, i))
                lines.append("")
            message = "\n".join(lines)
            try:
                resp = ref.httpx.post(
                    f"{ref.NOTIFICATION_URL}/notify",
                    json={"title": "Market Scan Picks", "message": message, "channel": "telegram"},
                    timeout=15,
                )
                resp.raise_for_status()
                _delivered(resp, "Notification service failed to deliver")
                return {"success": True, "sent": len(picks), "message": "Notification sent"}
            except ref.httpx.HTTPError as e:
                raise ref.HTTPException(status_code=502, detail=f"Notification service failed: {e}")

        pick_strings = []
        for i, r in enumerate(picks, 1):
            pick_strings.append(format_pick(r, i))

        base_header = title + "\n\n"
        MAX_CHARS = 4000
        # Room for the longest header ("(Part NN/NN)") plus the trailing newline;
        # a single oversized pick is truncated to fit instead of being sent
        # after an empty header-only part.
        _max_pick = max(100, MAX_CHARS - len(base_header) - 40)
        chunks = []
        current_chunk = []
        current_len = len(base_header)
        for pick_str in pick_strings:
            if len(pick_str) > _max_pick:
                pick_str = pick_str[: _max_pick - 1] + "…"
            pick_len = len(pick_str) + 1
            if current_chunk and current_len + pick_len + 10 > MAX_CHARS:
                chunks.append(current_chunk)
                current_chunk = []
                current_len = len(base_header)
            current_chunk.append(pick_str)
            current_len += pick_len + 1
        if current_chunk:
            chunks.append(current_chunk)

        total_chunks = len(chunks)
        sent_count = 0

        def _partial_note() -> str:
            # Telegram has no rollback: tell the caller what was already delivered.
            if sent_count:
                return f" ({sent_count} of {total_chunks} part(s) were already delivered and cannot be recalled)"
            return ""

        for idx, chunk in enumerate(chunks, 1):
            if total_chunks > 1:
                header = f"{title} (Part {idx}/{total_chunks})\n\n"
            else:
                header = f"{title}\n\n"
            message = header + "\n".join(chunk) + "\n"
            try:
                resp = ref.httpx.post(
                    f"{ref.NOTIFICATION_URL}/notify",
                    json={"title": "Market Scan Picks", "message": message, "channel": "telegram"},
                    timeout=15,
                )
                resp.raise_for_status()
                _delivered(resp, f"Part {idx} failed")
                sent_count += 1
            except ref.HTTPException as he:
                if sent_count:
                    raise ref.HTTPException(status_code=502, detail=f"{he.detail}{_partial_note()}")
                raise
            except ref.httpx.HTTPError as e:
                raise ref.HTTPException(status_code=502, detail=f"Notification service failed for part {idx}: {e}{_partial_note()}")

        return {"success": True, "sent": len(picks), "parts": total_chunks, "message": f"Notification sent in {total_chunks} parts"}

    @api_surprise_scan_stream_router.get("/api/surprise/scan/stream")
    @api_surprise_scan_stream_router.get("/surprise/scan/stream")
    async def api_surprise_scan_stream(
        force_reload: bool = False,
        symbols: str = None,
    ):
        """
    NDJSON stream of surprise hits with progress (like /scan/stream).
    Lines: meta static_loaded → progress batches → scored stocks → done.
    """
        try:
            from surprise_scanner import surprise_engine
        except Exception as e:
            raise ref.HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

        sym_list = None
        if symbols:
            sym_list = [x.strip() for x in symbols.replace(";", ",").split(",") if x.strip()]

        async def event_generator():
            t0 = ref.time.time()
            client = ref._get_http_client()
            n_static = await ref.asyncio.to_thread(surprise_engine.load_static_cache, bool(force_reload))  # group302: blocking SQL, off the loop
            yield ref.json.dumps({
                "_meta": True,
                "event": "static_loaded",
                "static_loaded": n_static,
                "total": n_static,
            }) + "\n"
            if n_static == 0:
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "error",
                    "error": "surprise_static_feed empty — run premarket first",
                }) + "\n"
                yield ref.json.dumps({"_meta": True, "event": "done", "hits": 0, "universe": 0, "elapsed": 0}) + "\n"
                return

            if sym_list:
                keys = [
                    s.upper().replace(".NS", "").replace(".BO", "").strip()
                    for s in sym_list if s
                ]
                keys = [k for k in keys if k in surprise_engine.static_cache]
            else:
                keys = [
                    k for k, v in surprise_engine.static_cache.items()
                    if v.get("is_liquid", True)
                ] or list(surprise_engine.static_cache.keys())

            total = len(keys)
            hits = 0
            quotes_ok = 0
            quotes_failed = 0
            chunk = 20
            yield ref.json.dumps({
                "_meta": True,
                "event": "scan_start",
                "total": total,
                "static_loaded": n_static,
            }) + "\n"

            # group242: price the whole key list with chunked POST /quotes/bulk first; the per-symbol _fetch_quote calls
            # below then find their tick already in hand and only the symbols bulk missed still go out one by one.
            _prime = getattr(surprise_engine, "prime_bulk_ticks", None)
            if _prime is not None:
                try:
                    await _prime(client, ref.MARKET_DATA_URL, keys)
                except Exception as e:  # never let the prefetch break the stream: fall back to per-symbol quotes
                    ref.logger.warning("surprise stream: bulk prefetch failed (%s) - quoting symbol by symbol", e)

            for i in range(0, total, chunk):
                batch = keys[i : i + chunk]
                try:
                    ticks = await ref.asyncio.gather(
                        *[surprise_engine._fetch_quote(client, ref.MARKET_DATA_URL, s) for s in batch],
                        return_exceptions=True,
                    )
                except Exception as e:
                    ref.logger.warning("surprise stream chunk: %s", e)
                    ticks = [None] * len(batch)

                for sym, tick in zip(batch, ticks):
                    if isinstance(tick, Exception) or not tick:
                        # No quote: nothing real to score. Scoring an empty tick
                        # could still return a "hit" built from the baseline alone.
                        quotes_failed += 1
                        continue
                    quotes_ok += 1
                    scored = surprise_engine.score_stock(sym, tick)
                    if scored:
                        hits += 1
                        scored["_progress"] = {
                            "processed": min(i + chunk, total),
                            "total": total,
                            "hits": hits,
                            "quotes_ok": quotes_ok,
                            "elapsed": round(ref.time.time() - t0, 1),
                        }
                        yield ref.json.dumps(scored, default=str) + "\n"

                processed = min(i + chunk, total)
                elapsed = round(ref.time.time() - t0, 1)
                eta = None
                if processed > 0 and processed < total and elapsed > 0:
                    eta = round((total - processed) * (elapsed / processed), 1)
                yield ref.json.dumps({
                    "_meta": True,
                    "event": "progress",
                    "processed": processed,
                    "total": total,
                    "hits": hits,
                    "quotes_ok": quotes_ok,
                    "elapsed": elapsed,
                    "eta_sec": eta,
                    "percent": int(100 * processed / max(total, 1)),
                }) + "\n"

            yield ref.json.dumps({
                "_meta": True,
                "event": "done",
                "hits": hits,
                "universe": total,
                "quotes_ok": quotes_ok,
                "quotes_failed": quotes_failed,
                "elapsed": round(ref.time.time() - t0, 1),
            }) + "\n"

        return ref.StreamingResponse(
            event_generator(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api_surprise_notify_top_picks_router.post("/surprise/notify-top-picks", dependencies=[ref.Depends(ref._require_admin)])
    async def api_surprise_notify_top_picks(top_n: int = ref.Query(5, ge=1, le=20)):
        """
    Manual "send to Telegram" button for the Surprise Momentum tab.
    Re-uses the exact same surprise_engine.scan() the tab's live scan uses
    (already sorted by score, high to low), takes the top N, and forwards a
    formatted message to notification-scheduler-service's /notify with
    channel="telegram" (falls back to the outbox if Telegram isn't
    configured — see /notify's own behaviour for that).
    """
        try:
            from surprise_scanner import surprise_engine
        except Exception as e:
            raise ref.HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

        client = ref._get_http_client()
        try:
            result = await surprise_engine.scan(
                client=client,
                market_data_url=ref.MARKET_DATA_URL,
                symbols=None,
                force_reload_static=False,
            )
        except Exception as e:
            # Mapped like the sibling surprise routes instead of a bare 500.
            ref.logger.exception("surprise notify-top-picks scan failed: %s", e)
            raise ref.HTTPException(status_code=500, detail=f"surprise scan failed: {str(e)[:240]}")
        if not isinstance(result, dict):
            result = {}
        stocks = result.get("stocks") or []
        if not stocks:
            return {
                "ok": False,
                "sent": False,
                "count": 0,
                "message": result.get("error") or "No Surprise Momentum picks available right now.",
            }

        top = stocks[: max(1, min(int(top_n), 20))]
        lines = [f"🎯 *Surprise Momentum — Top {len(top)} Picks*"]
        for i, s in enumerate(top, 1):
            sym = s.get("symbol")
            score = s.get("score")
            tier = str(s.get("tier") or "").upper()
            price = s.get("price")
            chg = s.get("change_pct")
            target = s.get("target_1")
            stop = s.get("trailing_stop")
            trig = s.get("trigger_type") or ""
            try:
                chg_str = f"{float(chg):+.2f}%"
            except (TypeError, ValueError):
                chg_str = "—"

            def _val(v, money=False):
                # A missing field prints a dash, never "None" / "₹None".
                if v is None or v == "":
                    return "—"
                return f"₹{v}" if money else str(v)

            lines.append(
                f"{i}. {_val(sym)} — {tier or '—'} (score {_val(score)}/100)\n"
                f"   {_val(price, True)} ({chg_str}) · Target {_val(target, True)} · Stop {_val(stop, True)}\n"
                f"   {trig}"
            )
        message = "\n".join(lines)

        try:
            # Moved off the event loop via asyncio.to_thread (session55 sweep,
            # same pattern as evaluate_price_alerts()/_quote_broadcast_loop
            # below) — httpx.post here is the blocking (sync-client) call.
            resp = await ref.asyncio.to_thread(
                ref.httpx.post,
                f"{ref.NOTIFICATION_URL}/notify",
                json={
                    "title": "Surprise Momentum — Top Picks",
                    "message": message,
                    "channel": "telegram",
                },
                timeout=15,
            )
            try:
                detail = resp.json()
            except Exception:
                detail = {"status_code": resp.status_code}
            _status = getattr(resp, "status_code", 200)
            http_ok = not (isinstance(_status, int) and _status >= 400)
            delivered = bool(http_ok and isinstance(detail, dict) and detail.get("delivered"))
            out = {
                # A 4xx/5xx reply from the notification service is a failure the
                # caller should see in `ok`, not only inside notification_result.
                "ok": http_ok,
                "sent": delivered,
                "count": len(top),
                "symbols": [s.get("symbol") for s in top],
                "notification_result": detail,
            }
            if not http_ok:
                out["error"] = f"notification service returned HTTP {_status}"
            return out
        except Exception as e:
            return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}

    @catalyst_alert_scan_router.get("/catalysts/alert")
    @catalyst_alert_scan_router.post("/catalysts/alert")
    async def catalyst_alert_scan(
        background_tasks: ref.BackgroundTasks,
        force: bool = True,
        notify: bool = True,
        sync: bool = False,
        batch_size: int = 0,
        restart: bool = False,
    ):
        """Pre-market / intraday catalyst sweep — FULL universe in batches.

    Does not reduce stock count. Processes HOT_BATCH_SIZE (default 25) symbols
    per batch, warms upstream services between batches, then continues until
    every candidate is scored. Poll GET /catalysts/alert/status.
    """
        job_key = "stockky:catalyst_job"
        # A non-numeric CATALYST_BATCH_SIZE falls back to 25 instead of a 500.
        batch_size = int(batch_size or ref._env_int("CATALYST_BATCH_SIZE", 25))
        batch_size = max(8, min(batch_size, 40))

        def _set_job(**kw):
            cur = ref._redis_get(job_key) or {}
            if not isinstance(cur, dict):
                cur = {}
            cur.update(kw)
            cur["updated_at"] = ref.datetime.now(ref.IST).isoformat()
            ref._redis_set(job_key, cur, ttl=86400)
            return cur

        existing = ref._redis_get(job_key) or {}
        # ``force`` only bypasses the hot-stocks cache. It used to also bypass this
        # already-running guard, and it defaults to True, so a plain call always
        # started a second sweep. Only an explicit ``restart=true`` overrides it.
        if isinstance(existing, dict) and existing.get("status") == "running" and not restart:
            try:
                upd = existing.get("updated_at") or existing.get("started_at")
                ts = ref.datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=ref.IST)
                if (ref.datetime.now(ref.IST) - ts).total_seconds() < 180:
                    return {"ok": True, "already_running": True, **existing}
            except Exception:
                pass

        _set_job(
            status="running",
            started_at=ref.datetime.now(ref.IST).isoformat(),
            message="Catalyst sweep started (full universe, batched)",
            force=force,
            notify=notify,
            actionable_count=0,
            processed=0,
            total=0,
            batch_size=batch_size,
            batch_index=0,
            error=None,
        )

        async def _work():
            ka_task = None
            stop_evt = ref.asyncio.Event()
            try:

                async def _keepalive_loop():
                    while not stop_evt.is_set():
                        try:
                            await ref._warm_upstream_services()
                        except Exception:
                            pass
                        try:
                            await ref.asyncio.wait_for(stop_evt.wait(), timeout=75.0)
                        except ref.asyncio.TimeoutError:
                            pass

                ka_task = ref.asyncio.create_task(_keepalive_loop())

                def _progress(i, total, sym, batch=0):
                    _set_job(
                        status="running",
                        message=f"Batch {batch + 1}: scoring {i + 1}/{total} ({sym})",
                        processed=i + 1,
                        total=total,
                        batch_index=batch,
                        batch_size=batch_size,
                    )

                _set_job(message="Building full catalyst universe…")
                # max_symbols=None → ALL candidates; internal loop warms every batch_size
                # Temporarily set env for this process batch size
                ref.os.environ["HOT_BATCH_SIZE"] = str(batch_size)

                hot = await ref.stockky_hot_stocks(
                    force=force,
                    max_symbols=None,  # full universe — no reduction
                    progress_cb=_progress,
                )

                picks = []
                for section in ("bulk_insider_driven", "results_driven", "news_driven"):
                    for item in (hot or {}).get(section) or []:
                        dec = (item.get("decision") or "").upper()
                        # A "high" signal must not list a DO NOT BUY / SELL row.
                        if dec in ("BUY NOW", "PREPARE TO BUY") or (
                            item.get("signal_strength") == "high" and dec not in ("DO NOT BUY", "SELL")
                        ):
                            picks.append({**item, "section": section})

                rank_sec = {"bulk_insider_driven": 0, "results_driven": 1, "news_driven": 2}
                picks.sort(key=lambda x: (rank_sec.get(x.get("section"), 9), -(x.get("score") or 0)))
                seen = set()
                unique = []
                for p in picks:
                    s = (p.get("symbol") or "").upper()
                    if not s or s in seen:
                        continue
                    seen.add(s)
                    unique.append(p)

                _universe_n = (hot or {}).get("universe_size")
                _universe_n = _universe_n if _universe_n is not None else 0
                lines = [
                    "🔥 *Stockky Catalyst Alert*",
                    f"IST {ref.datetime.now(ref.IST).strftime('%Y-%m-%d %H:%M')}",
                    f"Universe screened: {_universe_n} · Actionable: {len(unique)}",
                    "",
                ]
                if not unique:
                    lines.append("No strong catalyst names right now.")
                else:
                    for i, p in enumerate(unique[:15], 1):
                        sym = p.get("symbol")
                        dec = p.get("decision")
                        sc = p.get("score")
                        sec = (p.get("section") or "").replace("_driven", "")
                        why = ""
                        rs = p.get("reasons") or []
                        if isinstance(rs, list) and rs:
                            why = str(rs[0])[:80]
                        elif p.get("summary"):
                            why = str(p.get("summary"))[:80]
                        lines.append(f"{i}. *{sym}* — {dec} (score {sc}) [{sec}]")
                        if why:
                            lines.append(f"   _{why}_")

                message = "\n".join(lines)
                notified = False
                if notify and unique:
                    try:
                        client = ref._get_http_client()  # shared keepalive pool
                        if True:
                            _nresp = await client.post(
                                f"{ref.NOTIFICATION_URL.rstrip('/')}/notify",
                                json={"title": "Catalyst Alert", "message": message, "channel": "telegram"},
                            )
                        # A non-2xx reply is a failed delivery: take the fallback
                        # path below instead of reporting notified=True.
                        _nstatus = getattr(_nresp, "status_code", 200)
                        if isinstance(_nstatus, int) and not (200 <= _nstatus < 300):
                            raise RuntimeError(f"notify returned HTTP {_nstatus}")
                        notified = True
                    except Exception as e:
                        ref.logger.warning("catalyst telegram failed: %s", e)
                        # BUG FIX (2026-09-01): `"send_picks_to_telegram" in dir()`
                        # checks the CURRENT LOCAL SCOPE, not module globals —
                        # send_picks_to_telegram is a module-level function (never
                        # a local variable here), so this guard was always False
                        # and this fallback notify path never actually ran when
                        # the primary /notify POST above failed. Call it directly;
                        # the surrounding try/except already covers a real failure.
                        try:
                            ref.send_picks_to_telegram({"recommendations": unique[:10]})
                            notified = True
                        except Exception as e2:
                            ref.logger.warning("catalyst telegram fallback: %s", e2)

                total_u = int((hot or {}).get("universe_size") or 0)
                result = {
                    "ok": True,
                    "status": "done",
                    "actionable_count": len(unique),
                    "picks": unique[:20],
                    "hot_universe_size": total_u,
                    "notified": notified,
                    "message_preview": message[:500],
                    "generated_at": ref.datetime.now(ref.IST).isoformat(),
                    "message": f"Done — screened {total_u} · {len(unique)} actionable (batched {batch_size})",
                    "processed": total_u,
                    "total": total_u,
                    "batch_size": batch_size,
                }
                _set_job(**result)
                return result
            except Exception as e:
                ref.logger.exception("catalyst alert failed")
                _set_job(status="error", error=str(e)[:300], message=f"Error: {e}")
                return {"ok": False, "status": "error", "error": str(e)[:300]}
            finally:
                # Stop the keep-alive loop on EVERY exit path (it used to leak
                # when the scan failed — only the success path stopped it).
                stop_evt.set()
                if ka_task is not None:
                    # wait_for itself cancels ka_task if this coroutine is cancelled.
                    try:
                        await ref.asyncio.wait_for(ka_task, timeout=2)
                    except Exception:
                        ka_task.cancel()

        if sync:
            return await _work()

        background_tasks.add_task(_work)
        return {
            "ok": True,
            "status": "running",
            "accepted": True,
            "batch_size": batch_size,
            "message": "Catalyst alert started — full universe in batches; poll /catalysts/alert/status",
            "poll": "/catalysts/alert/status",
        }

    @stockky_hot_premarket_router.post("/stockky-hot/premarket")
    async def stockky_hot_premarket(background_tasks: ref.BackgroundTasks):
        """Pre-feed prices for EVERY eligible stock in ONE bulk call, before
    running Search Hot Picks Stocks.

    Today, Hot Picks' own price-enrichment step (see stockky_hot_stocks's
    "Price enrichment" block) only ever READS from the shared data-feed
    store — it never fetches over the network — so any symbol whose feed
    row is stale/missing shows "₹—" on its card. This endpoint is the fix:
    it builds the full eligible universe the same way the main market scan
    does (_build_scan_universe — every eligible stock, not just Hot Picks'
    ~285-symbol catalyst-seeded shortlist) and pre-feeds it via the SAME
    bulk pipeline the Data Feed tab uses (run_bulk_yahoo_price_feed —
    bhavcopy first, then ONE POST /quotes/bulk call for whatever's left,
    never one symbol at a time), writing into the same store (Neon on
    Render, Oracle ADB on the Oracle deployment — store dialect-detects
    automatically). Once this finishes, click 'Search Hot Picks Stocks' —
    its price lookups will already be warm.
    """
        job = ref.hot_premarket_job_get(ref._redis_get)
        if job.get("status") == "running":
            return {"ok": True, "already_running": True, **job}

        try:
            universe = (await ref.asyncio.to_thread(ref._build_scan_universe))
        except Exception as e:
            return {"ok": False, "error": f"could not build universe: {str(e)[:160]}"}

        if not universe:
            return {"ok": False, "error": "scan universe is empty — nothing to pre-feed"}

        ref.hot_premarket_job_set(
            ref._redis_set,
            ref._redis_get,
            status="running",
            processed=0,
            total=len(universe),
            started_at=ref.datetime.now(ref.IST).isoformat(),
            message=f"Pre-feeding {len(universe)} eligible stocks (bulk)…",
            finished_at=None,
        )

        def _run_premarket(syms: list):
            """Runs entirely outside the HTTP request — a full-universe bulk
        feed can take a while; background=true semantics match every
        other premarket job in this app (IPO, Surprise).
        
        Price source priority (2026-09-06):
          Market OPEN  → AngelOne LTP sweep first (POST /quotes/bulk on
                          market-data-service which waterfalls Angel→yf),
                          then bhavcopy for any remaining misses.
          Market CLOSED → bhavcopy (yesterday's close) only — same as before.
        """
            try:
                from data_feed import run_bulk_yahoo_price_feed
                phase = ref._market_session_phase_ist()
                market_open = phase in ("preopen", "open", "post")

                # Bug G fix (2026-09-06): original order was AngelOne THEN bhavcopy.
                # bhavcopy's put_symbols_bulk() calls merge_feed_payload() which
                # unconditionally overwrites volatile price fields (price/ltp/close)
                # from the incoming payload — bhavcopy's T-1 close silently won over
                # AngelOne's fresh LTP every time, making the sweep a no-op.
                # Fix: run bhavcopy FIRST (T-1 close baseline for all symbols),
                # then AngelOne sweep SECOND (live LTP overwrites for open market),
                # so live prices always win. merge_feed_payload's "incoming wins for
                # volatile fields" rule now works in our favour.
                angelone_hits = 0

                # Step 1: bhavcopy baseline (always — market open or closed)
                result = run_bulk_yahoo_price_feed(syms, merge_existing=True) or {}

                # Step 2: AngelOne live LTP sweep — only during market session.
                # Runs AFTER bhavcopy so live prices overwrite the T-1 baseline.
                if market_open:
                    try:
                        import time as _time
                        import httpx as _httpx
                        chunk_sz = 50  # AngelOne's documented per-request cap
                        total_syms = len(syms)
                        ref.hot_premarket_job_set(
                            ref._redis_set, ref._redis_get,
                            message=f"AngelOne LTP sweep: overlaying live prices on {total_syms} bhavcopy-seeded symbols…",
                        )
                        _pm_store = ref._feed_store()
                        for _ci, _chunk_start in enumerate(range(0, total_syms, chunk_sz)):
                            _chunk = syms[_chunk_start: _chunk_start + chunk_sz]
                            try:
                                _r = _httpx.post(
                                    f"{ref.MARKET_DATA_URL.rstrip('/')}/quotes/bulk",
                                    json={"symbols": _chunk},
                                    timeout=12.0,
                                )
                                if _r.status_code == 200:
                                    _body = _r.json()
                                    _quotes = _body.get("quotes") or []
                                    for _q in _quotes:
                                        _sym_q = str(_q.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
                                        if not _sym_q:
                                            continue
                                        _px = None
                                        for _k in ("price", "ltp", "close", "cmp", "last_price"):
                                            try:
                                                _v = float(_q.get(_k) or 0)
                                                if _v > 0:
                                                    _px = _v
                                                    break
                                            except (TypeError, ValueError):
                                                pass
                                        if _px:
                                            try:
                                                _existing = _pm_store.get_symbol(_sym_q) or {}
                                                _existing.update({
                                                    "symbol": _sym_q,
                                                    "price": _px,
                                                    "ltp": _px,
                                                    "close": _px,
                                                    "source": _q.get("source", "angelone_rest"),
                                                    "fetched_at": _q.get("fetched_at", ""),
                                                    "price_refreshed_at": _q.get("fetched_at", ""),
                                                })
                                                _pm_store.put_symbol(_sym_q, _existing, ttl=ref.DATA_FEED_TTL)
                                                angelone_hits += 1   # count only prices actually written
                                            except Exception as _we:
                                                ref.logger.debug("hot premarket store-write failed for %s: %s", _sym_q, _we)
                            except Exception as _ce:
                                ref.logger.debug("hot premarket AngelOne chunk %s failed (non-fatal): %s", _ci, _ce)
                            _time.sleep(0.3)  # stay well under AngelOne rate limit
                        ref.logger.info(
                            "hot premarket AngelOne sweep: %s/%s symbols had live LTP (written after bhavcopy)",
                            angelone_hits, total_syms,
                        )
                        ref.hot_premarket_job_set(
                            ref._redis_set, ref._redis_get,
                            message=f"AngelOne overlay done ({angelone_hits}/{total_syms} live prices written over bhavcopy baseline).",
                        )
                    except Exception as _ae:
                        ref.logger.warning("hot premarket AngelOne sweep failed (non-fatal): %s", _ae)
                n = int(result.get("tracked_stocks") or 0)
                ts = ref.datetime.now(ref.IST).isoformat()
                base_msg = result.get("message") or f"Pre-fed {n}/{len(syms)} stocks"
                # angelone_hits is updated during the sweep (Step 2) which runs after
                # run_bulk_yahoo_price_feed (Step 1), so the count here is final.
                if market_open and angelone_hits > 0:
                    final_msg = f"{base_msg} · {angelone_hits} live LTPs overlaid from AngelOne"
                else:
                    final_msg = base_msg
                ref.hot_premarket_job_set(
                    ref._redis_set,
                    ref._redis_get,
                    status="done",
                    processed=n,
                    total=len(syms),
                    message=final_msg,
                    finished_at=ts,
                )
            except Exception as e:
                ref.logger.exception("hot premarket bulk-feed failed")
                ref.hot_premarket_job_set(
                    ref._redis_set,
                    ref._redis_get,
                    status="error",
                    message=str(e)[:200],
                    finished_at=ref.datetime.now(ref.IST).isoformat(),
                )

        background_tasks.add_task(_run_premarket, universe)
        return {
            "ok": True,
            "started": True,
            "total": len(universe),
            "message": f"Premarket bulk pre-feed started for {len(universe)} eligible stocks",
        }

    @stockky_hot_run_router.post("/stockky-hot/run")
    async def stockky_hot_run(background_tasks: ref.BackgroundTasks, force: bool = True):
        """Start Hot Picks search with REAL progress (pipeline UI polls /stockky-hot/status).

    Progress used to be faked — total was hard-coded to 100 and processed jumped
    0 → 10 → 30 → (long silent await) → 90 → 100. Since the ETA is derived from
    elapsed/processed, a fabricated processed=10 two seconds in projected "a few
    seconds remaining" for a scan that takes minutes, and then nothing moved at
    all while stockky_hot_stocks() ran. Now total is the real universe size and
    processed is the real symbol index, reported from stockky_hot_stocks's
    existing progress_cb hook, so the countdown means something.
    """
        job = ref.hot_job_get(ref._redis_get)
        if job.get("status") == "running":
            return {"ok": True, "already_running": True, **job}

        # Fresh run: drop any stop request left over from a previous scan, and make
        # sure the durable table exists before we have rows to write into it.
        try:
            from hotpicks_store import clear_hotpicks_stop, ensure_hotpicks_schema

            await ref.asyncio.to_thread(clear_hotpicks_stop)      # group356: durable-key write + schema DDL, off the event loop
            await ref.asyncio.to_thread(ensure_hotpicks_schema)
        except Exception as e:
            ref.logger.debug("hotpicks pre-run setup skipped: %s", e)

        ref.hot_job_set(
            ref._redis_set,
            ref._redis_get,
            status="running",
            processed=0,
            total=0,
            started_at=ref.datetime.now(ref.IST).isoformat(),
            message="Building catalyst universe…",
            estimated_remaining_sec=None,
            finished_at=None,
            current_symbol=None,
            stopped=False,
        )

        async def _run_hot():
            # Throttle: the scan can call back hundreds of times and each write is a
            # durable kv write. One write per symbol is fine for a ~50-symbol
            # universe, but cap it so a large universe cannot hammer the DB.
            state = {"last_write": 0.0, "last_processed": -1}
            try:
                min_interval = float(((ref.os.getenv("HOT_PROGRESS_MIN_INTERVAL_SEC") or "").strip() or "1.0"))
            except (TypeError, ValueError):
                # A bad env value must not kill the worker before the try block below
                # (job stuck "running", stop flag never cleared) — use the default.
                min_interval = 1.0

            def _on_symbol(processed: int, total: int, symbol: str, batch: int = 0):
                """Called by stockky_hot_stocks before each symbol."""
                try:
                    now = ref.time.time()
                    is_first = state["last_processed"] < 0
                    is_last = total and processed >= total - 1
                    if not is_first and not is_last and (now - state["last_write"]) < min_interval:
                        return
                    state["last_write"] = now
                    state["last_processed"] = processed
                    ref.hot_job_set(
                        ref._redis_set,
                        ref._redis_get,
                        processed=int(processed),
                        total=int(total or 0),
                        current_symbol=str(symbol),
                        message=f"Scanning {symbol} ({processed + 1}/{total})",
                    )
                except Exception:
                    pass

            try:
                # Clear short cache so a forced run really re-scans
                if force:
                    try:
                        if ref._redis:
                            ref._redis.delete(ref.HOT_STOCKS_CACHE_KEY)
                    except Exception:
                        pass
                result = await ref.stockky_hot_stocks(force=force, progress_cb=_on_symbol)
                if result is not None and not isinstance(result, dict):
                    raise TypeError(f"unexpected scan result type: {type(result).__name__}")
                ts = ref.datetime.now(ref.IST).isoformat()
                was_stopped = bool(isinstance(result, dict) and result.get("stopped_early"))
                total = int((result or {}).get("universe_size") or 0)
                done = int((result or {}).get("processed_symbols") or total)
                if isinstance(result, dict):
                    result = {**result, "generated_at": result.get("generated_at") or ts, "persisted_at": ts}
                    ref._redis_set(ref.HOT_RESULT_KEY, result, ttl=20 * 3600)
                picks = sum(
                    len((result or {}).get(k) or [])
                    for k in ("news_driven", "results_driven", "bulk_insider_driven")
                )
                ref.hot_job_set(
                    ref._redis_set,
                    ref._redis_get,
                    status="stopped" if was_stopped else "done",
                    processed=done,
                    total=total or done,
                    current_symbol=None,
                    stopped=was_stopped,
                    message=(
                        f"Stopped after {done}/{total or done} symbols — {picks} pick(s) kept"
                        if was_stopped
                        else f"Hot Picks ready at {ts} — {picks} pick(s)"
                    ),
                    finished_at=ts,
                    estimated_remaining_sec=0,
                )
            except Exception as e:
                ref.logger.exception("hot run failed")
                ref.hot_job_set(
                    ref._redis_set,
                    ref._redis_get,
                    status="error",
                    current_symbol=None,
                    estimated_remaining_sec=None,
                    message=str(e)[:200],
                )
            finally:
                # Never leave the flag set — the next run must not stop instantly.
                try:
                    from hotpicks_store import clear_hotpicks_stop

                    clear_hotpicks_stop()
                except Exception:
                    pass

        background_tasks.add_task(_run_hot)
        return {"ok": True, "started": True, "message": "Hot Picks search started"}

    @api_hotpicks_notify_top_picks_router.post("/stockky-hot/notify-top-picks", dependencies=[ref.Depends(ref._require_admin)])
    async def api_hotpicks_notify_top_picks(top_n: int = ref.Query(5, ge=1, le=20)):
        """
    Manual "Send Top 5 to Telegram" button for the Hot Picks tab — same
    pattern as /surprise/notify-top-picks (Surprise Momentum tab already
    had this; Hot Picks did not). Reuses whatever is currently cached
    (live scan result or the durable hotpicks_static_feed fallback) rather
    than triggering a fresh scan, so this responds instantly.
    """
        def _has_picks(p) -> bool:
            if not isinstance(p, dict):
                return False
            return bool(
                (p.get("news_driven") or [])
                or (p.get("results_driven") or [])
                or (p.get("bulk_insider_driven") or [])
            )

        cached = ref._redis_get(ref.HOT_STOCKS_CACHE_KEY)
        if not _has_picks(cached):
            # Falls through on an empty/stale cache dict too, not only a missing one,
            # so picks sitting in the durable table are not hidden.
            try:
                from hotpicks_store import hotpicks_db_payload
                cached = await ref.asyncio.to_thread(hotpicks_db_payload)  # group361: off the event loop
            except Exception:
                cached = None

        if not _has_picks(cached):
            return {
                "ok": False,
                "sent": False,
                "count": 0,
                "message": "No Hot Picks available right now — run a scan first.",
            }

        merged = (
            list(cached.get("bulk_insider_driven") or [])
            + list(cached.get("results_driven") or [])
            + list(cached.get("news_driven") or [])
        )
        order = {"BUY NOW": 0, "PREPARE TO BUY": 1, "DO NOT BUY": 2, "SELL": 3}
        merged.sort(key=lambda x: (order.get((x.get("decision") or "").upper(), 9), -(x.get("score") or 0)))
        # `merged` is never empty here: _has_picks() above already returned early
        # when all three lists were empty, so `top` always has at least one item.
        top = merged[: max(1, min(int(top_n), 20))]

        lines = [f"🔥 *Stockky Hot Picks — Top {len(top)}*"]
        for i, s in enumerate(top, 1):
            sym = s.get("symbol")
            decision = s.get("decision") or "—"
            score = s.get("score")
            price = s.get("price") or s.get("close")
            section = (s.get("section") or "").replace("_", " ")
            price_str = f"₹{price}" if price else "price n/a"
            lines.append(f"{i}. {sym} — {decision} (score {score if score is not None else '—'}/100)\n   {price_str} · {section}")
        message = "\n".join(lines)

        try:
            # asyncio.to_thread — see the note on the equivalent httpx.post in
            # api_surprise_notify_top_picks above.
            resp = await ref.asyncio.to_thread(
                ref.httpx.post,
                f"{ref.NOTIFICATION_URL}/notify",
                json={"title": "Stockky Hot Picks — Top Picks", "message": message, "channel": "telegram"},
                timeout=15,
            )
            try:
                detail = resp.json()
            except Exception:
                detail = {"status_code": resp.status_code}
            _status = getattr(resp, "status_code", 200)
            http_ok = not (isinstance(_status, int) and _status >= 400)
            delivered = bool(http_ok and isinstance(detail, dict) and detail.get("delivered"))
            out = {
                # Same contract as /surprise/notify-top-picks: a 4xx/5xx from the
                # notification service is a failure the caller sees in `ok`.
                "ok": http_ok,
                "sent": delivered,
                "count": len(top),
                "symbols": [s.get("symbol") for s in top],
                "notification_result": detail,
            }
            if not http_ok:
                out["error"] = f"notification service returned HTTP {_status}"
            return out
        except Exception as e:
            return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}

    return SimpleNamespace(
        ops_wake_db_all=ops_wake_db_all,
        ops_wake_db_all_router=ops_wake_db_all_router,
        system_health=system_health,
        system_health_router=system_health_router,
        start_scan=start_scan,
        start_scan_router=start_scan_router,
        stream_market_scan=stream_market_scan,
        stream_market_scan_router=stream_market_scan_router,
        scan_watchlist=scan_watchlist,
        scan_watchlist_router=scan_watchlist_router,
        send_picks_to_telegram=send_picks_to_telegram,
        send_picks_to_telegram_router=send_picks_to_telegram_router,
        api_surprise_scan_stream=api_surprise_scan_stream,
        api_surprise_scan_stream_router=api_surprise_scan_stream_router,
        api_surprise_notify_top_picks=api_surprise_notify_top_picks,
        api_surprise_notify_top_picks_router=api_surprise_notify_top_picks_router,
        catalyst_alert_scan=catalyst_alert_scan,
        catalyst_alert_scan_router=catalyst_alert_scan_router,
        stockky_hot_premarket=stockky_hot_premarket,
        stockky_hot_premarket_router=stockky_hot_premarket_router,
        stockky_hot_run=stockky_hot_run,
        stockky_hot_run_router=stockky_hot_run_router,
        api_hotpicks_notify_top_picks=api_hotpicks_notify_top_picks,
        api_hotpicks_notify_top_picks_router=api_hotpicks_notify_top_picks_router,
    )
