"""Route handlers moved out of main.py in E86 (MainRef batch K, after E55 and E76-E84). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py); names that are locals of a handler or of its nested functions stay as they were. `build_bound_routes_k(ref)` returns a namespace: one router per handler
(include each where the handler used to be) and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names, which must be evaluated
when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_k(ref):
    market_trending_router = APIRouter()
    start_bulk_feed_router = APIRouter()
    data_feed_refill_additional_router = APIRouter()
    data_feed_run_router = APIRouter()
    api_surprise_premarket_proxy_router = APIRouter()

    @market_trending_router.get("/market/trending")
    async def market_trending():
        """Trending stocks — runs blocking work in a thread with a hard 20 s budget."""

        def _blocking():
            movers = ref._get_momentum_movers()
            news = ref._get_news_mentioned_symbols()
            trending = list(set(movers + news))
            trending_data = []
            # group233: price the (at most 10) names with ONE POST /quotes/bulk instead of one GET /quote each.
            _bulk_rows: dict = {}
            try:
                _tsyms = [str(x).upper().replace(".NS", "").replace(".BO", "").strip() for x in trending[:10] if x]
                if _tsyms:
                    _br = ref.httpx.post(f"{ref.MARKET_DATA_URL.rstrip('/')}/quotes/bulk", json={"symbols": _tsyms}, timeout=8)
                    if _br.status_code == 200:
                        for _q in ((_br.json() or {}).get("quotes") or []):
                            if isinstance(_q, dict):
                                _k = str(_q.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
                                if _k:
                                    _bulk_rows[_k] = _q
            except Exception as e:
                ref.logger.debug("trending bulk quotes: %s", e)
            for sym in trending[:10]:
                try:
                    price = change = change_pct = None
                    try:
                        data = _bulk_rows.get(str(sym).upper().replace(".NS", "").replace(".BO", "").strip())
                        if data:
                            p = data.get("price") or data.get("cmp")
                            prev = data.get("previous_close")
                            if p is not None and float(p) > 0:
                                price = round(float(p), 2)
                                if prev and float(prev) > 0:
                                    change = round(float(p) - float(prev), 2)
                                    change_pct = round((change / float(prev)) * 100, 2)
                                elif data.get("day_change_pct") is not None:
                                    change_pct = data.get("day_change_pct")
                                    change = round(price * change_pct / 100, 2) if change_pct is not None else None
                    except Exception as e:
                        ref.logger.debug("trending market-data quote %s: %s", sym, e)

                    if price is None:
                        # E86: the direct-yfinance last resort lives in _trending_yf_row (module level), so this handler can move out of main.py.
                        _yf_row = ref._trending_yf_row(sym)
                        if _yf_row is None:
                            continue
                        price, change, change_pct = _yf_row

                    trending_data.append({
                        "symbol": sym,
                        "price": price,
                        "change": change,
                        "change_pct": change_pct,
                    })
                except Exception as e:
                    ref.logger.debug("trending-stocks: skipping %s after fetch failure: %s", sym, e)
            return {"data": trending_data, "count": len(trending_data)}

        try:
            return await ref.asyncio.wait_for(
                ref.asyncio.get_event_loop().run_in_executor(None, _blocking),
                timeout=20.0,
            )
        except ref.asyncio.TimeoutError:
            ref.logger.warning("market/trending hit 20 s budget — returning empty")
            return {"data": [], "count": 0}
        except Exception as e:
            # Anything other than the 20s budget (e.g. a bad response from
            # _get_momentum_movers()/_get_news_mentioned_symbols()) used to
            # propagate out of run_in_executor as an unhandled 500 — degrade
            # to empty data instead, same shape as the timeout branch above.
            ref.logger.warning("market/trending failed: %s — returning empty", e)
            return {"data": [], "count": 0}

    @start_bulk_feed_router.post("/data-feed/start-bulk-feed")
    @start_bulk_feed_router.post("/api/data-feed/start-bulk-feed")
    @start_bulk_feed_router.post("/api/feed/start-bulk-feed")
    async def start_bulk_feed(
        force: bool = True,
        use_universe: bool = True,
        background_tasks: ref.BackgroundTasks = None,
    ):
        """
    Yahoo bulk price feeder — NON-BLOCKING.

    Returns 200 in <100 ms. Heavy yfinance download runs in BackgroundTasks
    so Render/Vercel gateway never hits the 98-second timeout.

    1) Build symbol list from scan universe (or existing feed index)
    2) Mark job=running for UI polling
    3) Schedule background worker that does the chunked bulk download (run_bulk) + Neon upsert
    """
        from data_feed import run_bulk_yahoo_price_feed, clear_data_feed_stop

        try:
            clear_data_feed_stop()
        except Exception:
            pass

        symbols: list = []
        if use_universe:
            try:
                symbols = (await ref.asyncio.to_thread(ref._build_scan_universe)) or []
            except Exception as e:
                ref.logger.warning("bulk-feed universe: %s", e)
                symbols = []
            # group426: the scan universe (~500 symbols, sometimes a stale copy) is smaller than the feed index (587), so ~48 indexed
            # symbols (MARUTI, ONGC, HAL ...) were never priced by this job. Append the indexed ones (duplicates are dropped below).
            # BULK_FEED_INCLUDE_INDEX=0 restores the old list.
            if symbols and ref._bulk_feed_include_index():
                try:
                    _indexed = [x for x in (ref._feed_store().list_symbols() or []) if x]
                except Exception:
                    _indexed = []
                if _indexed:
                    ref.logger.info("bulk-feed: scan universe %d + feed index %d (duplicates are dropped next)", len(symbols), len(_indexed))
                    symbols = list(symbols) + _indexed
        if not symbols:
            try:
                symbols = ref._feed_store().list_symbols() or []
            except Exception:
                symbols = []
        if not symbols:
            try:
                symbols = list(ref._get_nifty_indices() or [])[:150]
            except Exception:
                symbols = []

        symbols = [
            str(s).upper().replace(".NS", "").replace(".BO", "").strip()
            for s in symbols
            if s
        ]
        # de-dupe preserve order
        seen = set()
        clean = []
        for s in symbols:
            if s and s not in seen:
                seen.add(s)
                clean.append(s)
        symbols = clean

        if not symbols:
            raise ref.HTTPException(status_code=400, detail="No symbols available for bulk feed")

        # Mark job running for UI (instant)
        try:
            store = ref._feed_store()
            store.set_job(
                status="running",
                message=f"Bulk Yahoo feed started for {len(symbols)} symbols (background)…",
                processed=0,
                total=len(symbols),
                ok_count=0,
                error_count=0,
                stop_requested=False,
                started_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                ).isoformat(),
            )
        except Exception:
            pass

        def _bulk_worker(syms: list):
            """Runs entirely outside the HTTP request — no gateway timeout risk."""
            try:
                result = run_bulk_yahoo_price_feed(syms, merge_existing=True)
                n = int((result or {}).get("tracked_stocks") or 0)
                store = ref._feed_store()
                store.set_job(
                    status="done",
                    message=(result or {}).get("message") or f"Bulk feed done: {n}",
                    processed=n,
                    total=len(syms),
                    ok_count=n,
                    error_count=0,
                    finished_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                    ).isoformat(),
                )
                store.set_meta(
                    last_success_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                    ).isoformat(),
                    last_count=n,
                    last_message=(result or {}).get("message"),
                    source="yfinance_bulk_bg",
                )
                ref.logger.info("background bulk feed finished: %s", result)
            except Exception as e:
                ref.logger.exception("background bulk feed failed: %s", e)
                try:
                    ref._feed_store().set_job(
                        status="error",
                        message=str(e)[:200],
                        finished_at=__import__("datetime").datetime.now(
                            __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))
                        ).isoformat(),
                    )
                except Exception:
                    pass

        if background_tasks is not None:
            background_tasks.add_task(_bulk_worker, symbols)
        else:
            # Fallback if FastAPI somehow omits BackgroundTasks (should not happen)
            import threading
            threading.Thread(target=_bulk_worker, args=(symbols,), daemon=True).start()

        return {
            "ok": True,
            "status": "started",
            "started": True,
            "total": len(symbols),
            "message": (
                f"Data feed background worker initiated for {len(symbols)} symbols. "
                "Ingesting into Cache DB. Poll /data-feed/status."
            ),
        }

    @data_feed_refill_additional_router.post("/data-feed/refill-additional")
    @data_feed_refill_additional_router.post("/api/data-feed/refill-additional")
    async def data_feed_refill_additional(
        background_tasks: ref.BackgroundTasks,
        force: bool = True,
    ):
        """
    Manual / API trigger: Refill Additional Data (fundamentals + technical + events)
    into the data-feed store with force=true upstream calls.

    Returns immediately; work runs in BackgroundTasks. Poll
    GET /data-feed/status or /data-feed/refill-additional/status.
    """
        try:
            from refill_additional import get_refill_job, run_refill_additional, _set_job
            job = get_refill_job()
            if job.get("status") == "running" and not force:
                return {"ok": True, "already_running": True, **job}

            # Resolve universe before returning so UI shows total quickly
            store = ref._feed_store()
            symbols = []
            try:
                symbols = list(store.list_symbols() or [])
            except Exception:
                symbols = []
            if not symbols:
                try:
                    symbols = (await ref.asyncio.to_thread(ref._build_scan_universe))
                except Exception:
                    symbols = []
            symbols = [str(s).upper().replace(".NS", "").replace(".BO", "") for s in (symbols or []) if s]
            _set_job(
                status="running",
                message=f"Starting Refill Additional Data for {len(symbols)} symbols…",
                processed=0,
                total=len(symbols),
                ok_count=0,
                error_count=0,
            )

            def _worker(syms=symbols):
                try:
                    run_refill_additional(syms)
                except Exception as e:
                    ref.logger.exception("refill_additional worker: %s", e)
                    try:
                        _set_job(status="error", message=str(e)[:240])
                    except Exception:
                        pass

            background_tasks.add_task(_worker)
            return {
                "ok": True,
                "status": "running",
                "message": f"Refill Additional Data started for {len(symbols)} symbols",
                "total": len(symbols),
            }
        except Exception as e:
            ref.logger.exception("data_feed_refill_additional: %s", e)
            raise ref.HTTPException(status_code=500, detail=str(e)[:240])

    @data_feed_run_router.post("/data-feed/run")
    @data_feed_run_router.post("/api/data-feed/run")
    async def data_feed_run(
        background_tasks: ref.BackgroundTasks,
        force: bool = False,
        resume: bool = False,
        only_new: bool = False,
    ):
        """Feed slow fields for scan universe.

    - force=true  → full refresh from index 0
    - resume=true → continue from checkpoint cursor (Resume)
    - only_new=true → only symbols not already in feed store (new universe members)
    - default     → start fresh only if not running
    Never auto-starts from status polling — UI/scheduler must POST explicitly.
    """
        store = ref._feed_store()
        job = store.job()
        if job.get("status") == "running" and not force and not resume:
            return {"ok": True, "already_running": True, **job}

        universe = (await ref.asyncio.to_thread(ref._build_scan_universe))
        if not universe:
            universe = list(ref._get_nifty_indices() or [])[:150]
        _df_max = int(ref.os.getenv("DATA_FEED_MAX_SYMBOLS", "0") or 0)
        if _df_max > 0:
            universe = universe[:_df_max]
        universe = [u.upper().replace(".NS", "").replace(".BO", "") for u in universe]
        # Full universe by default. Only DATA_FEED_MAX_SYMBOLS>0 truncates (explicit opt-in).

        if not universe:
            # Same guard as /data-feed/start-bulk-feed. Without it an empty universe reported `started: true`,
            # wrote a "running" job with total 0 and queued a worker that had nothing to do.
            raise ref.HTTPException(status_code=400, detail="No symbols available for data feed")

        if only_new:
            store = ref._feed_store()
            fresh = []
            for s in universe:
                try:
                    entry = await ref.asyncio.to_thread(store.get_symbol, s)  # group303
                    if not entry:
                        fresh.append(s)
                except Exception:
                    fresh.append(s)
            if not fresh:
                return {
                    "ok": True,
                    "started": False,
                    "mode": "only_new",
                    "total": 0,
                    "message": "No new symbols — all universe members already have feed data. Use full feed only if you need a refresh.",
                }
            universe = fresh
            ref.logger.info("data-feed only_new: %s symbols without feed entry", len(universe))


        # Checkpoint: list of already-fed symbols + cursor
        checkpoint = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
        done_set = set(checkpoint.get("done") or [])
        start_idx = 0
        ok_n = int(job.get("ok_count") or 0)
        err_n = int(job.get("error_count") or 0)

        if resume and not force:
            # Prefer explicit cursor; else skip symbols already in done
            start_idx = int(checkpoint.get("cursor") or job.get("processed") or 0)
            start_idx = max(0, min(start_idx, len(universe)))
            # If job was stopped mid-way, resume from there
            if job.get("status") in ("stopped", "error", "idle", "done") and start_idx >= len(universe):
                start_idx = 0
                done_set = set()
                ok_n = 0
                err_n = 0
            store.set_job(
                status="running",
                processed=start_idx,
                total=len(universe),
                started_at=job.get("started_at") or ref.datetime.now(ref.IST).isoformat(),
                resumed_at=ref.datetime.now(ref.IST).isoformat(),
                message=f"Resuming from {start_idx}/{len(universe)}…",
                errors=err_n,
                ok_count=ok_n,
                error_count=err_n,
                stop_requested=False,
                checkpoint={"cursor": start_idx, "done": list(done_set), "universe": universe},
            )
            mode = "resume"
        else:
            # Full refresh / new run
            done_set = set()
            start_idx = 0
            ok_n = 0
            err_n = 0
            store.set_job(
                status="running",
                processed=0,
                total=len(universe),
                started_at=ref.datetime.now(ref.IST).isoformat(),
                elapsed_sec=0,
                estimated_remaining_sec=None,
                message=f"Feeding {len(universe)} symbols…",
                errors=0,
                ok_count=0,
                error_count=0,
                stop_requested=False,
                checkpoint={"cursor": 0, "done": [], "universe": universe},
            )
            mode = "refresh" if force else "start"

        async def _run(start_at: int, ok0: int, err0: int, done0: set):
            ref.clear_data_feed_stop()
            ok_n = ok0
            err_n = err0
            done_set = set(done0)
            client = ref._get_http_client()  # shared keepalive pool
            bulk_ran = False  # True only when THIS run did the bulk seed (a resume that skips it did not)

            # ── PHASE 0: Chunked Yahoo bulk quotes (50/chunk, threads=True) ──
            # Replaces sequential /quote calls that took ~15 min with ~20s total.
            if start_at == 0 and not done_set:
                try:
                    from data_feed import run_bulk_yahoo_price_feed_cached as run_bulk_yahoo_price_feed
                    store.set_job(
                        status="running",
                        message=f"Bulk Yahoo quotes for {len(universe)} symbols (chunks of 50)…",
                        processed=0,
                        total=len(universe),
                        updated_at=ref.datetime.now(ref.IST).isoformat(),
                    )
                    # run_bulk is sync (yfinance) — offload to thread so event loop stays responsive
                    bulk_result = await ref.asyncio.to_thread(
                        run_bulk_yahoo_price_feed, universe, True
                    )
                    bulk_ran = True
                    saved = int((bulk_result or {}).get("tracked_stocks") or 0)
                    for sym in (bulk_result or {}).get("symbols") or []:
                        done_set.add(str(sym).upper().replace(".NS", "").replace(".BO", ""))
                    ok_n = max(ok_n, saved)
                    store.set_job(
                        status="running",
                        message=(
                            f"Bulk 5-field seed done: {saved}/{len(universe)} "
                            f"(price+RSI local; PE/ROCE/sentiment baseline)"
                        ),
                        processed=saved,
                        total=len(universe),
                        ok_count=ok_n,
                        updated_at=ref.datetime.now(ref.IST).isoformat(),
                        checkpoint={"cursor": 0, "done": list(done_set), "universe": universe},
                    )
                    ref.logger.info("data-feed bulk phase: %s", bulk_result)
                    # Skip sequential /analyze peer storms when bulk covered ≥80% of universe
                    skip_fund = ((ref.os.getenv("DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK") or "").strip() or "1").lower() in (
                        "1", "true", "yes", "on",
                    )
                    if skip_fund and saved >= max(1, int(0.35 * len(universe))):
                        ts = ref.datetime.now(ref.IST).isoformat()
                        msg = (
                            f"Data feed bulk-complete for {saved} stocks at {ts} "
                            f"(local RSI + baseline PE/ROCE/sentiment; use Repair for real fundamentals)"
                        )
                        store.set_meta(
                            last_success_at=ts,
                            last_count=saved,
                            last_errors=0,
                            last_message=msg,
                            source="bulk_5field",
                            universe_size=len(universe),
                            partial=saved < len(universe),
                        )
                        store.set_job(
                            status="done",
                            processed=len(universe),
                            total=len(universe),
                            message=msg,
                            ok_count=saved,
                            error_count=0,
                            finished_at=ts,
                            checkpoint={"cursor": len(universe), "done": list(done_set), "universe": universe},
                        )
                        ref.logger.info(msg)
                        return
                except Exception as e:
                    ref.logger.warning("data-feed bulk phase failed (continuing sequential fund fill): %s", e)

            # PHASE 1: optional concurrent fund fill (rate-safe). Skip when bulk already
            # covered enough OR DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK=1 and any seed exists.
            # Default ON: after bulk seed, do not sequential /fundamental/analyze (rate-limit storm).
            # Set DATA_FEED_SKIP_SEQUENTIAL_FUND=0 to force slow fund fill.
            _skip_seq = ((ref.os.getenv("DATA_FEED_SKIP_SEQUENTIAL_FUND") or "").strip() or "1").lower() in (
                "1", "true", "yes", "on",
            )
            # Only when this run actually did the bulk seed. A resumed run carries `ok_count` from the earlier
            # run but skipped the bulk phase, and used to hit this branch immediately and declare the job done
            # with the remaining symbols never fed; it now continues into the sequential fill below.
            if _skip_seq and ok_n > 0 and bulk_ran:
                ts = ref.datetime.now(ref.IST).isoformat()
                msg = f"Data feed stopped after bulk seed ({ok_n} rows) — sequential fund skipped"
                store.set_job(status="done", processed=len(universe), total=len(universe),
                              message=msg, ok_count=ok_n, finished_at=ts)
                store.set_meta(last_success_at=ts, last_count=ok_n, last_message=msg,
                               universe_size=len(universe), partial=True)
                ref.logger.info(msg)
                return

            if True:
                # Concurrent fund-only fill (no news storm) — max 3 in-flight
                fund_sem = ref.asyncio.Semaphore(int(((ref.os.getenv("DATA_FEED_FUND_CONCURRENCY") or "").strip() or "3")))

                async def _feed_one(base: str):
                    async with fund_sem:
                        fund = None
                        events = None
                        try:
                            r = await client.get(f"{ref.FUNDAMENTAL_URL}/analyze/{base}", timeout=25)
                            if r.status_code == 200:
                                fund = r.json()
                        except Exception:
                            pass
                        try:
                            r = await client.get(f"{ref.EVENT_URL}/events/{base}", timeout=12)
                            if r.status_code == 200:
                                events = r.json()
                        except Exception:
                            pass
                        return base, fund, events

                for i in range(start_at, len(universe)):
                    # Cooperative stop (process flag OR job flag) — ASAP
                    jnow = store.job()
                    if ref.data_feed_stop_requested() or jnow.get("stop_requested") or jnow.get("status") in ("stopped", "stopping"):
                        ts = ref.datetime.now(ref.IST).isoformat()
                        msg = f"Stopped at {i}/{len(universe)} — committed {ok_n} fed stocks at {ts}"
                        store.set_meta(
                            last_success_at=ts,
                            last_count=ok_n,
                            last_errors=err_n,
                            last_message=msg,
                            source="stop",
                            universe_size=len(universe),
                            partial=True,
                        )
                        store.set_job(
                            status="stopped",
                            processed=i,
                            total=len(universe),
                            message=msg,
                            errors=err_n,
                            ok_count=ok_n,
                            error_count=err_n,
                            finished_at=ts,
                            stop_requested=False,
                            checkpoint={"cursor": i, "done": list(done_set), "universe": universe},
                        )
                        ref.logger.info(msg)
                        return

                    base = universe[i]
                    if base in done_set:
                        store.set_job(
                            status="running",
                            processed=i + 1,
                            total=len(universe),
                            message=f"Skip cached {base} ({i+1}/{len(universe)})",
                            errors=err_n,
                            ok_count=ok_n,
                            error_count=err_n,
                            updated_at=ref.datetime.now(ref.IST).isoformat(),
                            checkpoint={"cursor": i + 1, "done": list(done_set), "universe": universe},
                        )
                        continue
                    try:
                        base, fund, events = await _feed_one(base)
                        if fund or events:
                            payload = ref.extract_feed_payload(base, fund, events)
                            await ref.asyncio.to_thread(store.put_symbol, base, payload, ref.DATA_FEED_TTL)  # group303
                            done_set.add(base)
                            ok_n += 1
                        else:
                            err_n += 1
                    except Exception as e:
                        err_n += 1
                        ref.logger.debug("data-feed %s: %s", base, e)

                    store.set_job(
                        status="running",
                        processed=i + 1,
                        total=len(universe),
                        message=f"Fed {ok_n}/{len(universe)} ({base})",
                        errors=err_n,
                        ok_count=ok_n,
                        error_count=err_n,
                        updated_at=ref.datetime.now(ref.IST).isoformat(),
                        checkpoint={"cursor": i + 1, "done": list(done_set), "universe": universe},
                    )
                    feed_batch = max(5, int(((ref.os.getenv("DATA_FEED_BATCH_SIZE") or "").strip() or "20")))
                    if (i + 1) % feed_batch == 0:
                        # Complete batch → warm all upstreams, then continue
                        store.set_job(
                            message=f"Batch done {i + 1}/{len(universe)} — warming services…",
                            updated_at=ref.datetime.now(ref.IST).isoformat(),
                        )
                        try:
                            await ref._warm_upstream_services(client)
                        except Exception as e:
                            ref.logger.debug("data-feed batch warm: %s", e)
                        await ref.asyncio.sleep(0.5)
                    elif (i + 1) % 5 == 0:
                        await ref.asyncio.sleep(0.25)

            ts = ref.datetime.now(ref.IST).isoformat()
            msg = f"Data feed successfully for {ok_n} stocks at {ts}"
            store.set_meta(
                last_success_at=ts,
                last_count=ok_n,
                last_errors=err_n,
                last_message=msg,
                source="manual_or_api_or_scheduler",
                universe_size=len(universe),
                partial=False,
            )
            store.set_job(
                status="done",
                processed=len(universe),
                total=len(universe),
                message=msg,
                errors=err_n,
                ok_count=ok_n,
                error_count=err_n,
                finished_at=ts,
                stop_requested=False,
                checkpoint={"cursor": len(universe), "done": list(done_set), "universe": universe},
            )
            ref.logger.info(msg)

        background_tasks.add_task(_run, start_idx, ok_n, err_n, done_set)
        return {
            "ok": True,
            "started": True,
            "mode": mode,
            "resume_from": start_idx,
            "total": len(universe),
            "message": (
                f"Data feed resumed from {start_idx}/{len(universe)}"
                if mode == "resume"
                else f"Data feed started for {len(universe)} scan-universe stocks"
            ),
        }

    @api_surprise_premarket_proxy_router.post("/surprise/premarket")
    @api_surprise_premarket_proxy_router.get("/surprise/premarket")
    async def api_surprise_premarket_proxy(request: ref.Request):
        """
    Premarket baselines → Neon surprise_static_feed.

    Prefer RUNNING ON THE GATEWAY (has DATABASE_URL / Neon).
    market-data often lacks DATABASE_URL → schema_failed; gateway always ensures schema
    and computes baselines with yfinance, then writes Neon.

    Body optional: {"symbols": [...]} — else injects scan universe.
    Query background=true (default) returns immediately; poll /surprise/premarket/status.
    """
        import threading
        import json as _json

        # Cache "schema already confirmed ready" for this process so a second
        # premarket click (or the frontend's own retry after a timeout) doesn't
        # re-run ensure_surprise_schema() synchronously on the request path.
        # ensure_surprise_schema() opens a fresh DB connection (cold Oracle
        # wallet handshake can take several seconds) BEFORE the background
        # thread is even spawned — on a slow/cold connection that alone can
        # exceed Render's/nginx's default proxy timeout and return 504, even
        # though background=true was requested and would have returned
        # instantly otherwise. Once schema is confirmed ready once, skip the
        # live check on every subsequent call (schema doesn't disappear).
        try:
            ref._SURPRISE_SCHEMA_CONFIRMED
        except AttributeError:      # E86: MainRef raises AttributeError (not NameError) for a main.py global that is not defined yet
            ref._SURPRISE_SCHEMA_CONFIRMED = False

        if ref._SURPRISE_SCHEMA_CONFIRMED:
            schema = {"ok": True, "table": "surprise_static_feed", "cached": True}
        else:
            try:
                from surprise_schema import ensure_surprise_schema
                schema = await ref.asyncio.to_thread(ensure_surprise_schema)  # group356: DDL round trips, off the event loop
                if schema.get("ok"):
                    ref._SURPRISE_SCHEMA_CONFIRMED = True
            except Exception as e:
                schema = {"ok": False, "error": str(e)[:200]}
                ref.logger.warning("gateway ensure_surprise_schema: %s", e)

        if not schema.get("ok"):
            _sb = schema.get("backend") or "postgres"
            return ref.JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "schema_failed",
                    "message": (
                        "Set ORACLE_DSN + wallet env on api-gateway (Oracle deployment)"
                        if _sb == "oracle"
                        else "Set DATABASE_URL or CACHE_DATABASE_URL on api-gateway to Neon pooler URL"
                    ),
                    "schema": schema,
                },
            )

        # Parse symbols
        raw = await request.body()
        payload = {}
        if raw:
            try:
                payload = _json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}

        symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else None
        q = request.query_params.get("symbols")
        if not symbols and q:
            symbols = [x.strip() for x in q.split(",") if x.strip()]

        # 2026-08-25 fix: when no explicit symbol list is given, this used to
        # call _build_scan_universe() RIGHT HERE — synchronously, on the
        # request path, before the background thread below even exists. That
        # function hits the live NSE securities-list API (the same one this
        # session's NSE-403 fix already had to retry/refresh sessions for),
        # so on a slow or currently-blocked NSE response this alone can run
        # past Render's/nginx's proxy timeout and return 504 — even though
        # background=true was requested and the docstring promises an
        # immediate return. Universe resolution now happens INSIDE the
        # background thread (see _job() below) when background=true; it only
        # stays synchronous here for the non-background call, where blocking
        # is already an accepted part of that code path's contract.
        background = str(request.query_params.get("background", "true")).lower() in (
            "1", "true", "yes", ""
        )
        force = str(request.query_params.get("force", "false")).lower() in ("1", "true", "yes")

        try:
            from surprise_premarket import (
                precalculate_surprise_baselines,
                get_premarket_progress,
                default_universe_from_env,
            )
        except Exception as e:
            ref.logger.warning("surprise_premarket import failed: %s", e)
            return {
                "ok": False,
                "accepted": False,
                "error": f"surprise_premarket unavailable: {str(e)[:160]}",
                "schema": schema,
            }

        def _normalise_universe(uni) -> list:
            # Normalise FIRST, then drop empties: filtering the raw entries first
            # let a bare ".NS" through, turning into "" and defeating the
            # default-universe fallback.
            out = []
            for _s in (uni or []):
                _b = ref._hot_base(_s)
                if _b:
                    out.append(_b)
            return out

        # Already-running check BEFORE any (slow, NSE-backed) universe build.
        prog = get_premarket_progress()
        if prog.get("is_running"):
            return {
                "ok": True,
                "accepted": False,
                "already_running": True,
                "message": "Premarket already running — poll /surprise/premarket/status",
                "progress": prog,
                "schema": schema,
            }

        explicit_symbols = bool(symbols)
        if not symbols and not background:
            # Synchronous path only: resolve the universe now, same as before.
            try:
                uni = (await ref.asyncio.to_thread(ref._build_scan_universe))
                symbols = _normalise_universe(uni)
            except Exception as e:
                ref.logger.warning("universe inject failed: %s", e)
                symbols = []
            if not symbols:
                symbols = default_universe_from_env()

        if background:
            def _job(explicit_syms=(list(symbols) if explicit_symbols else None), force=force):
                try:
                    syms = explicit_syms
                    if not syms:
                        # Universe resolution deferred to here (see fix note
                        # above) — this thread can take as long as it needs;
                        # it no longer holds the HTTP response open.
                        try:
                            uni = ref._build_scan_universe()
                            syms = _normalise_universe(uni)
                        except Exception as e:
                            ref.logger.warning("gateway premarket job: universe inject failed: %s", e)
                            syms = []
                        if not syms:
                            syms = default_universe_from_env()
                    precalculate_surprise_baselines(syms, force=force)
                except Exception as e:
                    ref.logger.exception("gateway premarket job: %s", e)

            threading.Thread(target=_job, daemon=True, name="gw-surprise-premarket").start()
            return {
                "ok": True,
                "accepted": True,
                "background": True,
                "symbols": len(symbols) if explicit_symbols else None,
                "universe_injected": "resolving in background" if not explicit_symbols else len(symbols),
                "runner": "api-gateway",
                "backend": schema.get("backend"),
                "message": (
                    "Premarket started on gateway (%s) — poll /surprise/premarket/status"
                    % ("Oracle ADB" if schema.get("backend") == "oracle" else "Neon")
                ),
                "progress": get_premarket_progress(),
                "schema": schema,
            }

        try:
            result = await ref.asyncio.to_thread(precalculate_surprise_baselines, symbols, force=force)  # group361: off the event loop
        except Exception as e:
            # The background job logs and swallows a baseline error; the
            # synchronous path now reports it as a structured error instead of an
            # unhandled 500.
            ref.logger.exception("gateway premarket (sync): %s", e)
            return {"ok": False, "error": str(e)[:200], "runner": "api-gateway", "schema": schema}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        result["runner"] = "api-gateway"
        result["schema"] = schema
        return result

    return SimpleNamespace(
        market_trending=market_trending,
        market_trending_router=market_trending_router,
        start_bulk_feed=start_bulk_feed,
        start_bulk_feed_router=start_bulk_feed_router,
        data_feed_refill_additional=data_feed_refill_additional,
        data_feed_refill_additional_router=data_feed_refill_additional_router,
        data_feed_run=data_feed_run,
        data_feed_run_router=data_feed_run_router,
        api_surprise_premarket_proxy=api_surprise_premarket_proxy,
        api_surprise_premarket_proxy_router=api_surprise_premarket_proxy_router,
    )
