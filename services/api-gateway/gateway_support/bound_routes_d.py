"""Route handlers moved out of main.py in E78 (MainRef batch D, after E55, E76 and E77). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_d(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name. No `from __future__ import annotations` here: the signatures use main.py names (`ref.Request`, `ref.Query(...)`), which must be evaluated when the
factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_d(ref):
    ops_qstash_tick_router = APIRouter()
    scan_batch_router = APIRouter()
    api_run_premarket_feed_router = APIRouter()
    api_surprise_repair_batch_router = APIRouter()
    api_ipo_notify_top_picks_router = APIRouter()
    stockky_hot_endpoint_router = APIRouter()
    data_feed_update_single_router = APIRouter()
    data_feed_update_batch_router = APIRouter()
    data_feed_update_batch_refresh_router = APIRouter()
    data_feed_resume_router = APIRouter()
    stockky_hot_repair_batch_router = APIRouter()

    @ops_qstash_tick_router.post("/ops/qstash/tick")
    @ops_qstash_tick_router.get("/ops/qstash/tick")
    async def ops_qstash_tick(request: ref.Request):
        """QStash callback: light keepalive + optional evaluate. No Redis required."""
        try:
            sig = request.headers.get("Upstash-Signature") or request.headers.get("upstash-signature") or ""
            body = await request.body()
            if ref.qstash_client is not None:
                _gw_base = (ref.os.environ.get("API_GATEWAY_URL") or "").strip().rstrip("/")
                _expected_url = f"{_gw_base}{request.url.path}" if _gw_base else None
                if not ref.qstash_client.verify_signature(sig, body, expected_url=_expected_url):
                    raise ref.HTTPException(status_code=401, detail="Invalid QStash signature")
        except ref.HTTPException:
            raise
        except Exception:
            # Fail CLOSED: if the signature check itself crashes we cannot tell a real
            # QStash callback from a forged one, so refuse rather than run the tick
            # unauthenticated. (Missing keys / PyJWT are handled inside
            # qstash_client.verify_signature, which still accepts by design.)
            ref.logger.exception("QStash signature verification crashed — rejecting tick")
            raise ref.HTTPException(status_code=503, detail="QStash signature verification unavailable")
        # Light warm only
        # The callback itself is what wakes this instance; the response no longer claims
        # to have "warmed" /health and /ops/keepalive (it only ever listed their names).
        results = {"ok": True, "source": "qstash"}
        try:
            # Fan-out wake is expensive — only if body asks
            try:
                import json as _json
                payload = _json.loads(body.decode() or "{}") if body else {}
            except Exception:
                payload = {}
            if payload.get("action") == "structure-refresh":   # group360: schedule in QStash daily after the close (the daily bar is usable ~20 min after 15:30 IST)
                try:
                    results["structure_refresh"] = await ref.asyncio.to_thread(ref._start_structure_refresh, bool(payload.get("force")), False, None)
                except Exception as e:  # noqa: BLE001
                    results["structure_refresh_error"] = str(e)[:120]
            if payload.get("action") == "sniper-observe-eval":   # group361: schedule this in QStash (daily, after the close) to fill sniper outcomes
                try:
                    import sniper_observation as _so
                    results["sniper_observe"] = await ref.asyncio.to_thread(_so.evaluate_pending)
                except Exception as e:  # noqa: BLE001
                    results["sniper_observe_error"] = str(e)[:120]
            if payload.get("action") in ("wake", "wake-all", "scan"):
                try:
                    await ref.ops_keepalive(deep=False)
                    results["keepalive"] = True
                except Exception as e:
                    results["keepalive_error"] = str(e)[:120]
        except Exception as e:
            results["error"] = str(e)[:200]
        return results

    @scan_batch_router.post("/scan/batch")
    async def scan_batch(request: ref.Request):
        """
        Analyse a batch of symbols (max 15) and return results quickly.
        Already uses Neon bulk prefetch + asyncio.Semaphore parallel workers
        (same family as /scan/start and /api/scan/stream). Kept for GHA runners.
        """
        try:
            data = await request.json()
        except ValueError:
            raise ref.HTTPException(status_code=400, detail="Request body must be JSON")
        if not isinstance(data, dict):
            raise ref.HTTPException(status_code=400, detail="Request body must be a JSON object")
        raw_symbols = data.get("symbols", [])
        if isinstance(raw_symbols, str):
            # {"symbols": "TCS"} used to be iterated character by character (T, C, S).
            raw_symbols = [p for p in raw_symbols.replace(";", ",").split(",")]
        if not isinstance(raw_symbols, list):
            raise ref.HTTPException(status_code=400, detail="symbols must be a list of symbol strings")
        # Blank entries do not count toward the cap (they are dropped below).
        raw_symbols = [s for s in raw_symbols if str(s).strip()]
        if len(raw_symbols) > 15:
            raise ref.HTTPException(status_code=400, detail="Maximum 15 symbols per batch")

        # Normalize once so feed keys and analyze() symbol always match (RELIANCE.NS → RELIANCE)
        symbols = [
            str(s).upper().replace(".NS", "").replace(".BO", "").strip()
            for s in raw_symbols
            if str(s).strip()
        ]

        # Use a semaphore to limit concurrent downstream calls inside this batch
        # Aligned with MAX_PARALLEL_WORKERS=8 so analysis service is never overloaded
        sem = ref.asyncio.Semaphore(8)
        client = ref._get_http_client()  # shared keepalive pool
        if True:
            # One Neon bulk for this batch
            try:
                batch_feeds = await ref.asyncio.to_thread(ref._feed_store().get_symbols_bulk, symbols) or {}  # group361: off the event loop
            except Exception:
                batch_feeds = {}
            tasks = [
                ref._analyze_one_symbol_ultra(
                    sym,
                    client,
                    sem,
                    feed_row=batch_feeds.get(sym),
                    prefetched_feeds=batch_feeds,
                    # Multi-symbol batch — Gemini reserved for single-stock Analyse.
                    skip_gemini=True,
                )
                for sym in symbols
            ]
            results = await ref.asyncio.gather(*tasks, return_exceptions=True)

            final_results = []
            for sym, result in zip(symbols, results):
                if isinstance(result, Exception):
                    final_results.append({"symbol": sym, "decision": "ERROR", "error": str(result)})
                else:
                    if isinstance(result, dict):
                        result["symbol"] = sym  # enforce normalized symbol
                    final_results.append(result)
            return {"results": final_results}

    @api_run_premarket_feed_router.post("/api/surprise/run-premarket-feed")
    @api_run_premarket_feed_router.post("/surprise/run-premarket-feed")
    @api_run_premarket_feed_router.get("/api/surprise/run-premarket-feed")
    async def api_run_premarket_feed(force: bool = False, request: ref.Request = None):
        """
        Market-aware surprise quote feed:
          - OPEN: cache ≤ 2h
          - CLOSED: durable cache (no Yahoo storm)
          - force=true: always refresh sequentially with 0.5s gaps
        """
        symbols = None
        try:
            if request is not None:
                body = await request.body()
                if body:
                    import json as _json
                    payload = _json.loads(body.decode("utf-8") or "{}")
                    if isinstance(payload, dict) and payload.get("symbols"):
                        symbols = ref._clean_symbol_list(payload["symbols"])
        except Exception:
            symbols = None
        if not symbols:
            try:
                symbols = (await ref.asyncio.to_thread(ref._build_scan_universe))[:200]
            except Exception:
                symbols = None
        try:
            from surprise_scanner import run_market_aware_surprise_feed
            return await run_market_aware_surprise_feed(
                symbols=symbols,
                market_data_url=ref.MARKET_DATA_URL,
                force=force,
            )
        except Exception as e:
            ref.logger.exception("run-premarket-feed: %s", e)
            raise ref.HTTPException(status_code=500, detail=str(e)[:240])

    @api_surprise_repair_batch_router.post("/api/surprise/repair-batch")
    @api_surprise_repair_batch_router.post("/surprise/repair-batch")
    async def api_surprise_repair_batch(limit: int = ref.Query(15, ge=1, le=100), symbol: ref.Optional[str] = ref.Query(None)):
        """Fill missing surprise quotes via market-data waterfall (0.5s pacing).
        Optional symbol= targets a single ticker (UI Repair button).
        """
        try:
            from surprise_scanner import repair_surprise_batch
            # AUDIT FIX: repair_surprise_batch() is a sync function that loops
            # up to `limit` (100 max) symbols with a time.sleep() pace-out
            # between each — called directly from this async route it ran
            # straight on the event loop, stalling every other api-gateway
            # request for the full repair duration. Offload to a worker
            # thread, same fix pattern as circuit_breaker.py/rate_limit_monitor.py.
            return await ref.asyncio.to_thread(
                repair_surprise_batch,
                limit=limit,
                market_data_url=ref.MARKET_DATA_URL,
                symbol=symbol,
            )
        except Exception as e:
            ref.logger.exception("surprise repair-batch: %s", e)
            raise ref.HTTPException(status_code=500, detail=str(e)[:240])

    @api_ipo_notify_top_picks_router.post("/ipo/notify-top-picks", dependencies=[ref.Depends(ref._require_admin)])
    @api_ipo_notify_top_picks_router.post("/surprise/ipo/notify-top-picks", dependencies=[ref.Depends(ref._require_admin)])
    async def api_ipo_notify_top_picks(top_n: int = ref.Query(5, ge=1, le=20)):
        """Manual 'send to Telegram' button for the IPO Tracker tab — mirrors
        /surprise/notify-top-picks and /stockky-hot/notify-top-picks exactly
        (same notification-scheduler-service /notify call, channel='telegram'),
        just sourced from the IPO list (best score first, pre-listing/listing-
        day/upcoming stages first — same ordering run_ipo_scan already sorts
        the stored list into) instead of Surprise Momentum's or Hot Picks' own
        scan results."""
        try:
            from ipo_scanner import get_ipo_list
            listing = get_ipo_list(display_days=None)
        except Exception as e:
            raise ref.HTTPException(status_code=500, detail=f"ipo_scanner import failed: {e}")

        results = listing.get("results") or []
        if not results:
            return {"ok": False, "sent": False, "count": 0, "message": "No IPO Tracker results available right now — run Scan IPOs first."}

        top = results[: max(1, min(int(top_n), 20))]
        lines = [f"🆕 *IPO Tracker — Top {len(top)}*"]
        for i, r in enumerate(top, 1):
            sym = r.get("symbol")
            decision = r.get("decision") or "—"
            # A real IPO score of 0 is a score, not "missing": only fall back to
            # the advisory score when ipo_score is actually absent.
            score = r.get("ipo_score")
            if score is None:
                score = r.get("pre_listing_advisory_score")
            stage = str(r.get("stage") or "").replace("_", " ")
            issue_px = r.get("issue_price")
            cur_px = r.get("current_price")
            chg = r.get("current_vs_issue_pct")
            try:
                chg_str = f"{float(chg):+.2f}%" if chg is not None else "—"
            except (TypeError, ValueError):
                chg_str = "—"
            lines.append(
                f"{i}. {sym} — {decision} (score {score if score is not None else '—'}/100, {stage})\n"
                f"   Issue ₹{issue_px if issue_px is not None else '—'} → "
                f"Current ₹{cur_px if cur_px is not None else '—'} ({chg_str})"
            )
        message = "\n".join(lines)

        try:
            # asyncio.to_thread — see the note on the equivalent httpx.post in
            # api_surprise_notify_top_picks above.
            resp = await ref.asyncio.to_thread(
                ref.httpx.post,
                f"{ref.NOTIFICATION_URL}/notify",
                json={"title": "IPO Tracker — Top Picks", "message": message, "channel": "telegram"},
                timeout=15,
            )
            try:
                detail = resp.json()
            except Exception:
                detail = {"status_code": resp.status_code}
            delivered = bool(isinstance(detail, dict) and detail.get("delivered"))
            return {
                "ok": True,
                "sent": delivered,
                "count": len(top),
                "symbols": [r.get("symbol") for r in top],
                "notification_result": detail,
            }
        except Exception as e:
            return {"ok": False, "sent": False, "count": len(top), "error": str(e)[:300]}

    @stockky_hot_endpoint_router.get("/stockky-hot")
    async def stockky_hot_endpoint(force: bool = False, background_tasks: ref.BackgroundTasks = None):
        """HTTP entry for Stockky 🔥 Stocks tab — full universe, batched internally.

        Fix: on a cache miss this used to run the full-universe scan inline on
        the request (stockky_hot_stocks), which for a few hundred symbols can
        easily run well past any client/proxy timeout — curl gave up at 25s
        with http_code=000 in testing even though the scan was still running
        server-side (same class of bug as the old /scheduler/hydrate/weekend
        blocking call). /stockky-hot/run + /stockky-hot/status already solve
        this correctly with a background job + poll pattern, so a cold cache
        now reuses that instead of duplicating a second, blocking code path.
        force=true still runs synchronously inline (unchanged) for callers
        with a suitably long client-side timeout (manual/GHA use).
        """
        if force:
            return await ref.stockky_hot_stocks(force=True, max_symbols=None, progress_cb=None)

        # 2026-10-21: _redis_get can fall through to the Oracle-backed kv_cache; called straight from this
        # async endpoint it froze the event loop (3.2 s = two bounded 1.5 s reads). Off the loop AND bounded.
        cached = await ref._redis_get_bounded(ref.HOT_STOCKS_CACHE_KEY)
        if cached:
            return {**cached, "cached": True}

        # Cache is cold: kick off the same background job /stockky-hot/run uses
        # (no-op if one is already running) and return immediately instead of
        # blocking the request on a full universe scan.
        if background_tasks is not None:
            try:
                await ref.stockky_hot_run(background_tasks, force=True)
            except Exception as e:
                ref.logger.warning("stockky-hot background warm-up failed to start: %s", e)

        stale = await ref._redis_get_bounded(ref.HOT_RESULT_KEY)
        if stale:
            return {**stale, "cached": True, "stale": True, "warming": True}

        return {
            "ok": True,
            "warming": True,
            "message": "Hot Picks is warming up (first run or cache expired) — poll /stockky-hot/status, or retry in a few seconds.",
            "news_driven": [],
            "results_driven": [],
            "bulk_insider_driven": [],
        }

    @data_feed_update_single_router.post("/api/feed/update")
    @data_feed_update_single_router.post("/data-feed/update")
    async def data_feed_update_single(request: ref.Request):
        """Persist one symbol feed payload into Neon (canonical + alias keys)."""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "JSON object required"}
        symbol = payload.get("symbol")
        if not symbol:
            return {"ok": False, "error": "symbol required"}
        try:
            from data_feed import save_stock_feed, extract_feed_payload
            base = str(symbol).upper().replace(".NS", "").replace(".BO", "").strip()
            # Accept either full payload or nested fields
            body = dict(payload)
            body.pop("symbol", None)
            if body.get("fundamental") or body.get("events"):
                row = extract_feed_payload(
                    base,
                    fundamental=body.get("fundamental") if isinstance(body.get("fundamental"), dict) else body,
                    events=body.get("events") if isinstance(body.get("events"), dict) else None,
                    extra={k: v for k, v in body.items() if k not in ("fundamental", "events")},
                )
            else:
                row = dict(body)
                row["symbol"] = base
            # Universal ≤ ₹5000 gate — reject at the write boundary, not just at
            # the bulk-Yahoo seed. Previously this endpoint would happily persist
            # any price, which is how over-cap symbols entered the feed store.
            if ref._row_price_over_cap(row):
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "symbol": base,
                    "error": f"price above ₹{ref.MAX_UNIVERSE_PRICE:.0f} cap — not saved",
                }
            save_stock_feed(base, row)
            return {"ok": True, "status": "SUCCESS", "symbol": base}
        except Exception as e:
            ref.logger.exception("data_feed update failed")
            return {"ok": False, "error": str(e)[:300]}

    @data_feed_update_batch_router.post("/api/feed/batch")
    @data_feed_update_batch_router.post("/data-feed/batch")
    async def data_feed_update_batch(request: ref.Request):
        """Bulk upsert: { "feeds": { "RELIANCE": {...}, ... } } or list of payloads."""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        feeds = {}
        if isinstance(payload, dict):
            raw = payload.get("feeds") or payload.get("items") or payload.get("data")
            if isinstance(raw, dict):
                feeds = raw
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("symbol"):
                        feeds[str(item["symbol"])] = item
            else:
                # treat entire dict as symbol->payload if values are dicts
                if payload and all(isinstance(v, dict) for v in payload.values()):
                    feeds = payload
        if not feeds:
            return {"ok": False, "error": "No feeds provided", "count": 0}
        try:
            from data_feed import save_stock_feed
            n = 0
            rejected = 0
            # BUG FIX (session 55, event-loop-blocking-I/O audit): this loop used
            # to call save_stock_feed() — a blocking Neon/durable-KV write —
            # inline, once per symbol, directly on the event loop with no cap on
            # `feeds` size. Same bug class already found/fixed in
            # circuit_breaker.py, rate_limit_monitor.py, and batch_worker.py: a
            # large POST body (this route, unlike /api/feed/update-batch, has no
            # DATA_FEED_UPDATE_BATCH_MAX cap) could serialize dozens+ blocking DB
            # round-trips ahead of every other request this process is handling.
            # Fix: filter/validate inline (cheap, in-memory), then run all the
            # actual writes concurrently off the event loop via asyncio.to_thread.
            to_write: list = []
            for sym, row in feeds.items():
                if not isinstance(row, dict):
                    continue
                base = str(sym).upper().replace(".NS", "").replace(".BO", "").strip()
                body = dict(row)
                body["symbol"] = base
                # Universal ≤ ₹5000 gate (see _row_price_over_cap) — bulk upsert
                # previously bypassed the cap entirely.
                if ref._row_price_over_cap(body):
                    rejected += 1
                    continue
                to_write.append((base, body))

            if to_write:
                results = await ref.asyncio.gather(
                    *[ref.asyncio.to_thread(save_stock_feed, base, body) for base, body in to_write],
                    return_exceptions=True,
                )
                for (base, _body), res in zip(to_write, results):
                    if isinstance(res, Exception):
                        ref.logger.warning("data_feed batch write failed for %s: %s", base, res)
                    else:
                        n += 1
            return {"ok": True, "status": "SUCCESS", "count": n, "rejected_over_cap": rejected}
        except Exception as e:
            ref.logger.exception("data_feed batch failed")
            return {"ok": False, "error": str(e)[:300], "count": 0}

    @data_feed_update_batch_refresh_router.post("/api/feed/update-batch")
    @data_feed_update_batch_refresh_router.post("/data-feed/update-batch")
    async def data_feed_update_batch_refresh(request: ref.Request):
        """
        Process a small symbol list in ONE short HTTP request (GitHub Action driver).

        Body: { "symbols": ["RELIANCE", "TCS", ...] }  — max 15 recommended.
        Fetches fundamental + events for each, writes Neon feed, returns counts.
        Designed so each call finishes in <1–2 min (well under Render's ~100m hard cap).
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list) or not symbols:
            return {"ok": False, "error": "symbols list required", "ok_count": 0, "error_count": 0}
        # Hard cap protects free-tier CPU / upstream rate limits
        max_n = int(((ref.os.getenv("DATA_FEED_UPDATE_BATCH_MAX") or "").strip() or "15"))
        symbols = [
            str(s).upper().replace(".NS", "").replace(".BO", "").strip()
            for s in symbols
            if s
        ][:max_n]
        if not symbols:
            return {"ok": False, "error": "no valid symbols", "ok_count": 0, "error_count": 0}

        from data_feed import extract_feed_payload
        store = ref._feed_store()
        ok_n = 0
        err_n = 0
        results = []
        client = ref._get_http_client()
        for base in symbols:
            try:
                fund = None
                events = None
                try:
                    r = await client.get(f"{ref.FUNDAMENTAL_URL}/analyze/{base}", timeout=35)
                    if r.status_code == 200:
                        fund = r.json()
                    elif r.status_code in (429, 503):
                        try:
                            ref.rate_limit_monitor.record(
                                source="analysis",
                                status=r.status_code,
                                path=f"/fundamental/{base}",
                                detail="update-batch",
                                symbol=base,
                            )
                        except Exception:
                            pass
                except Exception as e:
                    ref.logger.debug("update-batch fund %s: %s", base, e)
                try:
                    r = await client.get(f"{ref.EVENT_URL}/events/{base}", timeout=20)
                    if r.status_code == 200:
                        events = r.json()
                except Exception as e:
                    ref.logger.debug("update-batch events %s: %s", base, e)
                if fund or events:
                    row = extract_feed_payload(base, fund, events)
                    # Universal ≤ ₹5000 gate (see _row_price_over_cap) — the
                    # GitHub Action driver previously wrote every price it fetched.
                    if ref._row_price_over_cap(row):
                        err_n += 1
                        results.append({"symbol": base, "ok": False, "error": "price above cap"})
                        # Same pacing as every other outcome: this branch used to `continue` straight past the
                        # sleep, so a run of over-cap symbols hammered the upstream at full speed.
                        await ref.asyncio.sleep(0.15)
                        continue
                    await ref.asyncio.to_thread(store.put_symbol, base, row, ref.DATA_FEED_TTL)  # group303
                    ok_n += 1
                    results.append({"symbol": base, "ok": True})
                else:
                    err_n += 1
                    results.append({"symbol": base, "ok": False, "error": "no fund/events"})
            except Exception as e:
                err_n += 1
                results.append({"symbol": base, "ok": False, "error": str(e)[:120]})
            await ref.asyncio.sleep(0.15)

        return {
            "ok": True,
            "ok_count": ok_n,
            "error_count": err_n,
            "processed": len(symbols),
            "results": results,
        }

    @data_feed_resume_router.post("/data-feed/resume")
    @data_feed_resume_router.post("/api/data-feed/resume")
    async def data_feed_resume(background_tasks: ref.BackgroundTasks):
        """Resume from last checkpoint.

        If a previous run is stuck as 'running' (worker died), force-stop/commit first
        so resume can start a new background task from the cursor.
        """
        store = ref._feed_store()
        job = store.job()
        if job.get("status") == "running":
            # Force-commit current checkpoint so a new worker can continue
            await ref.data_feed_stop(force=True)
        return await ref.data_feed_run(background_tasks, force=False, resume=True)

    @stockky_hot_repair_batch_router.post("/stockky-hot/repair-batch")
    def stockky_hot_repair_batch(limit: int = ref.Query(15, ge=1, le=100), symbol: ref.Optional[str] = ref.Query(None)):
        """Repair stored Hot Picks rows: first fill any missing PRICE via the
        market-data waterfall, then run a real SCORE repair (decision/pillar
        scores/trade levels) via the decision service for rows still missing them.
        Nothing is fabricated — the score pass copies exactly what the decision
        service returns. This is what makes the UI "Repair" button actually clear
        the blank Entry/Target/Stop/Tech/Fund/… dashes, not just price."""
        try:
            from hotpicks_store import hotpicks_repair_batch, hotpicks_repair_scores
        except Exception as e:
            return {"status": "error", "error": f"hotpicks store unavailable: {str(e)[:160]}"}
        market_url = ref._env_url("MARKET_DATA_URL", "", rstrip=False)
        decision_url = ref._env_url("DECISION_URL", ref.DECISION_URL, rstrip=False)
        try:
            price_res = hotpicks_repair_batch(limit=limit, symbol=symbol, market_data_url=market_url)
        except Exception as e:
            # Isolated like the score pass below, so a price failure no longer
            # becomes a bare 500 that also skips the score repair.
            price_res = {"status": "error", "error": str(e)[:160]}
        score_res = {}
        try:
            score_res = hotpicks_repair_scores(limit=limit, symbol=symbol, decision_url=decision_url)
        except Exception as e:
            score_res = {"status": "error", "error": str(e)[:160]}
        price_repaired = list(price_res.get("repaired") or [])
        score_repaired = list(score_res.get("repaired") or [])
        combined = list(dict.fromkeys(price_repaired + score_repaired))
        # Overall status: completed if either pass did (or would do) real work.
        status = "completed"
        if price_res.get("status") == "error" and score_res.get("status") == "error":
            status = "error"
        elif price_res.get("status") in ("not_found",) and not score_repaired and score_res.get("status") in ("not_found", "no_data"):
            status = price_res.get("status")
        return {
            "status": status,
            "repaired": combined,
            "price_repaired": price_repaired,
            "score_repaired": score_repaired,
            "attempted": (price_res.get("attempted") or 0) + (score_res.get("attempted") or 0),
            "price_detail": price_res,
            "score_detail": score_res,
        }

    return SimpleNamespace(
        ops_qstash_tick=ops_qstash_tick,
        ops_qstash_tick_router=ops_qstash_tick_router,
        scan_batch=scan_batch,
        scan_batch_router=scan_batch_router,
        api_run_premarket_feed=api_run_premarket_feed,
        api_run_premarket_feed_router=api_run_premarket_feed_router,
        api_surprise_repair_batch=api_surprise_repair_batch,
        api_surprise_repair_batch_router=api_surprise_repair_batch_router,
        api_ipo_notify_top_picks=api_ipo_notify_top_picks,
        api_ipo_notify_top_picks_router=api_ipo_notify_top_picks_router,
        stockky_hot_endpoint=stockky_hot_endpoint,
        stockky_hot_endpoint_router=stockky_hot_endpoint_router,
        data_feed_update_single=data_feed_update_single,
        data_feed_update_single_router=data_feed_update_single_router,
        data_feed_update_batch=data_feed_update_batch,
        data_feed_update_batch_router=data_feed_update_batch_router,
        data_feed_update_batch_refresh=data_feed_update_batch_refresh,
        data_feed_update_batch_refresh_router=data_feed_update_batch_refresh_router,
        data_feed_resume=data_feed_resume,
        data_feed_resume_router=data_feed_resume_router,
        stockky_hot_repair_batch=stockky_hot_repair_batch,
        stockky_hot_repair_batch_router=stockky_hot_repair_batch_router,
    )
