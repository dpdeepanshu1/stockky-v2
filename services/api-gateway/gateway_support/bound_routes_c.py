"""Route handlers moved out of main.py in E77 (the third MainRef batch, after E55 and E76). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_c(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_c(ref):
    stock_structure_router = APIRouter()
    cancel_scan_router = APIRouter()
    scan_stop_all_router = APIRouter()
    get_scan_universe_router = APIRouter()
    get_universe_router = APIRouter()
    api_surprise_audit_router = APIRouter()
    api_surprise_scan_router = APIRouter()
    api_evaluate_price_alerts_router = APIRouter()
    repair_single_stock_router = APIRouter()
    repair_batch_missing_router = APIRouter()
    data_feed_stop_router = APIRouter()

    @stock_structure_router.get("/stock/{symbol}/structure")
    async def stock_structure(symbol: str, force: bool = False):
        """Group 349: market-structure block + flat summary for one symbol (proxy to the technical service). Always 200 with a safe
        'unavailable' default when the engine/upstream is down, so the UI panel can render its empty state instead of an error."""
        base = (symbol or "").strip().upper().replace(".NS", "").replace(".BO", "")
        empty = {"symbol": base, "market_structure": None, "structure_summary": ref.ms_passthrough.safe_summary(None)}
        if not ref.ms_passthrough.enabled() or not base:
            return empty
        try:
            async with ref.httpx.AsyncClient() as client:
                resp = await ref._cb_get(client, "technical", f"{ref.TECHNICAL_URL}/structure/{base}" + ("?force=true" if force else ""), timeout=20)
            if resp.status_code != 200:
                return empty
            data = resp.json()
            blk = ref.ms_passthrough.safe_block(data)
            if blk is None:
                return empty
            return {"symbol": base, "market_structure": blk, "structure_summary": ref.ms_passthrough.safe_summary(data)}
        except Exception as e:  # noqa: BLE001
            ref.logger.debug("structure proxy %s: %s", base, e)
            return empty

    @cancel_scan_router.post("/scan/cancel/{task_id}")
    def cancel_scan(task_id: str):
        """Request cancel — process-local flag + durable key; commit partial; stops ASAP."""
        ref._SCAN_CANCEL_FLAGS.add(task_id)
        ref._redis_set(ref.SCAN_TASK_PREFIX + task_id + ":cancel", True, ttl=3600)
        data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id)
        if not isinstance(data, dict):
            data = {}
        try:

            data = dict(data)
            data["cancel_requested"] = True
            data["status"] = "cancelled" if data.get("status") in (None, "running") else data.get("status")
            data["partial"] = True
            data["message"] = data.get("message") or "Stop requested — partial results committed"
            ref._redis_set(ref.SCAN_TASK_PREFIX + task_id, data, ttl=3600)
        except Exception:
            pass
        return {
            "ok": True,
            "status": "cancel_requested",
            "processed_so_far": data.get("processed", 0),
            "total": data.get("total", 0),
            "message": "Scan stop signalled — worker will commit partial and exit",
        }

    @scan_stop_all_router.post("/scan/stop-all")
    def scan_stop_all():
        """Force-stop every running scan task (commit partial)."""
        ref._SCAN_CANCEL_FLAGS.add("__ALL__")
        n = 0
        failed = 0
        try:
            for k in list(ref._mem_kv.keys()):
                if str(k).startswith(ref.SCAN_TASK_PREFIX) and not str(k).endswith(":cancel"):
                    data = ref._mem_kv.get(k)
                    if isinstance(data, dict) and data.get("status") == "running":
                        data = dict(data)
                        data["cancel_requested"] = True
                        data["status"] = "cancelled"
                        data["partial"] = True
                        ref._mem_kv[k] = data
                        # One failing durable write must not end the sweep: the
                        # remaining running tasks still need to be stopped.
                        try:
                            ref._redis_set(k, data, ttl=3600)
                            ref._redis_set(str(k) + ":cancel", True, ttl=3600)
                        except Exception as _we:
                            failed += 1
                            ref.logger.warning("scan/stop-all durable write failed for %s: %s", k, _we)
                            continue
                        n += 1
        except Exception:
            pass
        out = {"ok": True, "stopped": n, "message": "All scans stop-signalled"}
        if failed:
            out["durable_write_failures"] = failed
        return out

    @get_scan_universe_router.get("/scan/universe")
    async def get_scan_universe(cached: bool = False):
        # 2026-09-18 fix: added `cached` query param (mirrors /surprise/scan).
        # When cached=true, real-trade-service gets a sub-second response on any
        # warm or stale hit — it only pays the full rebuild cost when both the
        # live Neon key AND the stale-fallback key are completely absent (i.e. the
        # very first ever build on a brand-new deployment, not a routine restart).
        # Callers: candidate_engine/candidates.py already passes ?cached=true for
        # its /surprise/scan call; same pattern applied here so the same client
        # timeout (25s) is safe even right after a restart.
        if cached:
            # Try live cache first (may already be warm from pre-warm task)
            live = ref._redis_get(ref.SCAN_UNIVERSE_KEY)
            if live and isinstance(live, list) and len(live) > 0:
                live = ref._filter_equities(live)
                live = await ref.asyncio.to_thread(ref._filter_symbols_under_max_price, live)  # group361: off the event loop
                if len(live) >= 50:
                    searched = await ref._load_searched_safe()
                    movers, _partial = await ref._movers_with_deadline()
                    return {
                        "total": len(live),
                        "symbols": live,
                        "searched_symbols_included": [s for s in searched if s in live],
                        "momentum_movers": movers,
                        "momentum_movers_partial": _partial,
                        "max_price": ref.MAX_UNIVERSE_PRICE,
                        "cached": True,
                    }
            # Live cache cold — try stale Neon fallback
            if ref._kv_cache is not None:
                try:
                    stale = ref._kv_cache.get_stale(ref.SCAN_UNIVERSE_STALE_KEY)
                    if stale and isinstance(stale, list) and len(stale) >= 50:
                        stale = ref._filter_equities(stale)
                        stale = await ref.asyncio.to_thread(ref._filter_symbols_under_max_price, stale)  # group361: off the event loop
                        if len(stale) >= 50:
                            # Repopulate live key (short TTL) so next caller is fast
                            ref._redis_set(ref.SCAN_UNIVERSE_KEY, stale, ttl=300)
                            # Kick off background rebuild so data freshens soon
                            try:
                                ref.asyncio.create_task(ref.asyncio.to_thread(ref._build_scan_universe_fresh))
                            except Exception:
                                pass
                            searched = await ref._load_searched_safe()
                            movers, _partial = await ref._movers_with_deadline()
                            ref.logger.info(
                                "scan/universe: served %d-symbol stale fallback (background rebuild scheduled)",
                                len(stale),
                            )
                            return {
                                "total": len(stale),
                                "symbols": stale,
                                "searched_symbols_included": [s for s in searched if s in stale],
                                "momentum_movers": movers,
                                "momentum_movers_partial": _partial,
                                "max_price": ref.MAX_UNIVERSE_PRICE,
                                "cached": True,
                                "stale": True,
                            }
                except Exception as _se:
                    ref.logger.debug("scan/universe cached stale-read failed: %s", _se)
            # Both caches cold — fall through to full build (first-ever deployment)
            ref.logger.info("scan/universe: both caches cold, running full synchronous build")

        build_task = ref.asyncio.ensure_future(ref.asyncio.to_thread(ref._build_scan_universe))  # already ≤₹5000 filtered
        try:
            universe = await ref.asyncio.wait_for(ref.asyncio.shield(build_task), timeout=ref.SCAN_UNIVERSE_BUILD_DEADLINE_S)
        except ref.asyncio.TimeoutError:
            # Serve ANY cached universe rather than blow the caller's 25s timeout;
            # the build keeps running in its thread and refreshes the caches itself.
            fallback = None
            try:
                fallback = ref._redis_get(ref.SCAN_UNIVERSE_KEY)
                if not (isinstance(fallback, list) and len(fallback) >= 50) and ref._kv_cache is not None:
                    fallback = ref._kv_cache.get_stale(ref.SCAN_UNIVERSE_STALE_KEY)
            except Exception:
                fallback = None
            # The >=50 check must hold AFTER the equity/price filters, otherwise a
            # fallback that shrinks to a handful of symbols would be served.
            fb = []
            if isinstance(fallback, list) and len(fallback) >= 50:
                try:
                    fb = await ref.asyncio.to_thread(ref._filter_symbols_under_max_price, ref._filter_equities(fallback))  # group361: off the event loop
                except Exception as _fe:
                    ref.logger.warning("scan/universe: fallback filter failed: %s", _fe)
                    fb = []
            if len(fb) >= 50:
                ref.logger.warning("scan/universe: full build exceeded %.0fs — served %d-symbol cached fallback, build continues in background",
                               ref.SCAN_UNIVERSE_BUILD_DEADLINE_S, len(fb))
                movers, _partial = await ref._movers_with_deadline()
                return {"total": len(fb), "symbols": fb, "searched_symbols_included": [], "momentum_movers": movers,
                        "momentum_movers_partial": _partial, "max_price": ref.MAX_UNIVERSE_PRICE,
                        "cached": True, "stale": True, "deadline_exceeded": True}
            ref.logger.warning("scan/universe: full build exceeded %.0fs and no cached universe exists — waiting for the build", ref.SCAN_UNIVERSE_BUILD_DEADLINE_S)
            universe = await build_task
        searched = await ref._load_searched_safe()
        movers, _partial = await ref._movers_with_deadline()
        return {
            "total": len(universe),
            "symbols": universe,
            "searched_symbols_included": [s for s in searched if s in universe],
            "momentum_movers": movers,
            "momentum_movers_partial": _partial,
            "max_price": ref.MAX_UNIVERSE_PRICE,
        }

    @get_universe_router.get("/api/universe")
    @get_universe_router.get("/universe")
    async def get_universe():
        """
        Stateless ≤ ₹5000 universe — prefers Neon data-feed (survives container sleep).
        Order:
          1) Neon feed symbols with price unknown or ≤ 5000 (durable, no RAM dependency)
          2) Decision-service training universe (if available)
          3) Local scan-universe builder
        Always re-applies the price gate.
        """
        symbols: ref.List[str] = []

        # 1) Neon / data-feed (anti-amnesia primary source)
        try:
            from data_feed import list_feed_symbols_from_neon_under_max_price
            symbols = await ref.asyncio.to_thread(list_feed_symbols_from_neon_under_max_price, ref.MAX_UNIVERSE_PRICE) or []  # group361: off the event loop
        except Exception as e:
            ref.logger.debug("api/universe neon feed: %s", e)
            symbols = []

        # 2) Training universe fallback
        if not symbols:
            try:
                decision_base = ref._env_url("DECISION_URL", ref.DECISION_URL, rstrip=False)
                root = decision_base.rstrip("/")
                if root.endswith("/decision"):
                    root = root[: -len("/decision")]
                training_url = f"{root}/training/universe"
                async with ref.httpx.AsyncClient() as client:
                    res = await client.get(training_url, timeout=10.0)
                    if res.status_code == 200:
                        body = res.json()
                        if isinstance(body, list):
                            symbols = [str(s) for s in body]
                        elif isinstance(body, dict):
                            symbols = [str(s) for s in (body.get("symbols") or body.get("universe") or [])]
            except Exception as e:
                ref.logger.debug("api/universe training fetch: %s", e)
                symbols = []

        # 3) Dynamic scan universe last resort
        if not symbols:
            try:
                symbols = (await ref.asyncio.to_thread(ref._build_scan_universe))
            except Exception:
                symbols = []

        filtered = await ref.asyncio.to_thread(ref._filter_symbols_under_max_price, symbols)  # group361: off the event loop
        return filtered

    @api_surprise_audit_router.get("/api/surprise/audit")
    @api_surprise_audit_router.get("/surprise/audit")
    async def api_surprise_audit():
        """Premarket / surprise feed health for the Surprise dashboard.

        Memoised for AUDIT_TTL_SEC — the panel refetches on every tab mount and this
        walks the whole tracked set, which is the bulk of the tab's load time. See
        _audit_cache_get for why a short TTL is safe here."""
        cached = ref._audit_cache_get("surprise_audit")
        if cached is not None:
            return cached
        try:
            from surprise_scanner import audit_surprise_feed
            return ref._audit_cache_put("surprise_audit", audit_surprise_feed())
        except Exception as e:
            ref.logger.exception("surprise audit: %s", e)
            return {
                "ok": False,
                "total_tracked": 0,
                "fully_populated": 0,
                "missing_data": 0,
                "health_score": 0,
                "incomplete_stocks": [],
                "message": str(e)[:200],
            }

    @api_surprise_scan_router.get("/api/surprise/scan")
    @api_surprise_scan_router.get("/surprise/scan")
    async def api_surprise_scan(
        force_reload: bool = False,
        symbols: str = None,
        cached: bool = False,
        limit: int = None,
        refresh: bool = False,
    ):
        """
        Lightweight surprise scan:
          1) bulk-load surprise_static_feed from Neon
          2) concurrent live quotes (bounded)
          3) score filter — see surprise_scanner.MIN_SCORE / MIN_CHANGE_PCT for the
             live thresholds (this docstring previously said ">=60, change >1%",
             which drifted out of sync with the actual values; read from the
             module constants instead of hardcoding a number here again).

        `cached=true` (default False): serve the last computed full-universe
        result if it's still fresh (see SurpriseStockEngine.scan's
        cached_max_age_sec) instead of running a new scan. Callers like
        candidate_engine that just want a cheap read of "what does the surprise
        tab currently show" should pass this — see the 2026-09-01 incident fix
        in surprise_scanner.py for why this used to be silently ignored.
        `limit` (optional): truncate the returned `stocks` list to the top N
        by score — candidate_engine's own use of this endpoint always passed
        limit=20 but nothing here ever honored it.
        """
        try:
            from surprise_scanner import surprise_engine
        except Exception as e:
            raise ref.HTTPException(status_code=500, detail=f"surprise_scanner import failed: {e}")

        sym_list = None
        if symbols:
            sym_list = [x.strip() for x in symbols.replace(";", ",").split(",") if x.strip()]

        if not sym_list and not force_reload and not refresh:
            _closed = await ref._surprise_closed_market_result(surprise_engine, limit)
            if _closed is not None:
                return _closed

        client = ref._get_http_client()
        # Session 72 (#3): never run past the callers' 25s client timeout. The scan
        # runs as a shielded task; if it exceeds SURPRISE_SCAN_DEADLINE_S we return
        # the last computed result (flagged stale) while the scan finishes and
        # refreshes the cache in the background. With no prior result at all we
        # simply keep waiting (first-ever scan) — never worse than before.
        _scan_task = ref._start_surprise_scan(
            surprise_engine, client=client, sym_list=sym_list,
            force_reload=bool(force_reload), cached=bool(cached),
        )
        try:
            result = await ref.asyncio.wait_for(ref.asyncio.shield(_scan_task), timeout=ref.SURPRISE_SCAN_DEADLINE_S)
        except ref.asyncio.TimeoutError:
            _last = getattr(surprise_engine, "_last_result", None)
            if _last is not None and not sym_list:
                ref.logger.warning("surprise/scan: exceeded %.0fs — served last computed result (age %.0fs); scan continues in background",
                               ref.SURPRISE_SCAN_DEADLINE_S, ref.time.time() - getattr(surprise_engine, "_last_scan_ts", ref.time.time()))
                result = dict(_last)
                result["from_cache"] = True
                result["stale"] = True
                result["deadline_exceeded"] = True
                result["cache_age_sec"] = round(ref.time.time() - getattr(surprise_engine, "_last_scan_ts", ref.time.time()), 1)
            else:
                ref.logger.warning("surprise/scan: exceeded %.0fs and no prior result to serve — waiting for the scan", ref.SURPRISE_SCAN_DEADLINE_S)
                try:
                    result = await _scan_task
                except Exception as e:
                    ref.logger.exception("surprise/scan failed: %s", e)
                    raise ref.HTTPException(status_code=500, detail=f"surprise scan failed: {str(e)[:240]}")
        except Exception as e:
            # Same mapping as the sibling surprise routes (a clear 500 detail
            # instead of an unhandled error).
            ref.logger.exception("surprise/scan failed: %s", e)
            raise ref.HTTPException(status_code=500, detail=f"surprise scan failed: {str(e)[:240]}")
        # ``limit`` is applied whenever it is given: 0 (like a negative value)
        # means "no rows", it no longer silently means "unlimited".
        if limit is not None and isinstance(result.get("stocks"), list):
            result = dict(result)
            result["stocks"] = result["stocks"][: max(0, int(limit))]
        return result

    @api_evaluate_price_alerts_router.post("/api/price-alerts/evaluate")
    @api_evaluate_price_alerts_router.post("/price-alerts/evaluate")
    async def api_evaluate_price_alerts():
        """Evaluate alerts vs bulk cache / feed; notify on triggers."""
        from data_feed import evaluate_price_alerts
        # asyncio.to_thread (session55 sweep, part 2): evaluate_price_alerts()
        # is a blocking kv_get/kv_set call, and each iteration below can add a
        # blocking _wake_notification_service() (~httpx.get, up to 5s) plus a
        # blocking httpx.post — this is a manually-triggered endpoint, but on a
        # multi-alert trigger it's the same "loop of blocking calls inline on
        # the event loop" pattern already fixed for /api/feed/batch and
        # _quote_broadcast_loop, so it gets the same treatment rather than being
        # left as a single-call exception.
        try:
            triggered = await ref.asyncio.to_thread(evaluate_price_alerts)
        except Exception as e:
            ref.logger.warning("price alert evaluation failed: %s", e)
            return {"ok": False, "error": str(e)[:200], "triggered": [], "triggered_count": 0, "notified": 0}
        notified = 0
        for t in triggered:
            try:
                await ref.asyncio.to_thread(ref._wake_notification_service)
                msg = (
                    f"Price alert: {t.get('symbol')} is ₹{t.get('current_price')} "
                    f"({t.get('direction')} ₹{t.get('target_price')})"
                )
                if t.get("note"):
                    msg += f" — {t.get('note')}"
                resp = await ref.asyncio.to_thread(
                    ref.httpx.post,
                    f"{ref.NOTIFICATION_URL}/notify",
                    json={"title": f"Price Alert · {t.get('symbol')}", "message": msg, "channel": "all"},
                    timeout=12,
                )
                if resp.status_code == 200:
                    notified += 1
            except Exception as e:
                ref.logger.debug("price alert notify: %s", e)
        return {
            "ok": True,
            "triggered": triggered,
            "triggered_count": len(triggered),
            "notified": notified,
        }

    @repair_single_stock_router.post("/api/feed/repair-single/{symbol}")
    @repair_single_stock_router.post("/data-feed/repair-single/{symbol}")
    @repair_single_stock_router.post("/api/data-feed/repair-single/{symbol}")
    @repair_single_stock_router.post("/feed/repair-single/{symbol}")
    async def repair_single_stock(symbol: str):
        """Repair one symbol — fetch only missing fields, merge into Cache DB.

        Always returns 200 with status + patched_fields so the UI Repair button
        never sees a 422/timeout ghost. High-price names (e.g. MRF) keep their
        live price; missing RSI/PE/ROCE/sentiment are filled from upstreams or
        safe baselines so audit health recovers immediately.
        """
        import urllib.parse
        sym = urllib.parse.unquote(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not sym:
            return {"status": "error", "symbol": symbol, "message": "empty symbol", "complete": False}
        try:
            client = ref._get_http_client()
            result = await ref._patch_single_stock_feed(sym, client)
            return {"status": "success", "ok": True, **result}
        except Exception as e:
            ref.logger.exception("repair_single_stock %s: %s", sym, e)
            # Never 500 the UI — report failure payload so button can show message
            return {
                "status": "error",
                "ok": False,
                "symbol": sym,
                "patched_fields": [],
                "still_missing": ["unknown"],
                "complete": False,
                "message": str(e)[:200],
            }

    @repair_batch_missing_router.post("/api/feed/repair-batch")
    @repair_batch_missing_router.post("/data-feed/repair-batch")
    async def repair_batch_missing(limit: int = 10):
        """
        Rate-safe batch repair (default 10). 500ms spacing between symbols.
        """
        limit = max(1, min(int(limit or 10), 25))
        try:
            audit = await ref.audit_missing_feed_data(limit=limit)
        except Exception as e:
            # Same "never 500 the UI" contract as repair-single: report the failure as an envelope.
            ref.logger.exception("repair_batch_missing audit: %s", e)
            return {
                "status": "error",
                "ok": False,
                "repaired_count": 0,
                "successish_count": 0,
                "repaired": [],
                "repaired_symbols": [],
                "message": str(e)[:200],
            }
        targets = [item["symbol"] for item in (audit.get("incomplete_stocks") or [])[:limit]]
        client = ref._get_http_client()
        repaired = []
        for sym in targets:
            try:
                res = await ref._patch_single_stock_feed(sym, client)
                repaired.append(res)
            except Exception as e:
                repaired.append({"symbol": sym, "error": str(e)[:160], "complete": False})
            await ref.asyncio.sleep(0.5)
        ok_n = sum(1 for r in repaired if r.get("complete") or r.get("patched_fields"))
        return {
            "status": "completed",
            "repaired_count": len(repaired),
            "successish_count": ok_n,
            "repaired": repaired,
            "repaired_symbols": [r.get("symbol") for r in repaired],
        }

    @data_feed_stop_router.post("/data-feed/stop")
    @data_feed_stop_router.post("/api/data-feed/stop")
    async def data_feed_stop(force: bool = True):
        """Stop data feed and commit checkpoint immediately.

        Free-tier workers often die after sleep, so cooperative stop alone leaves
        status=running forever. We always force-commit from the last checkpoint.

        The job is read BEFORE anything is written: the "stopping" marker used to
        be written first, so the "not running" guard always saw "stopping"
        (force=false could never stop anything, and a no-op call flipped an idle
        job to stopping with stop_requested=True and nothing undid it).
        """
        store = ref._feed_store()
        job = store.job()
        status = job.get("status")
        if status not in ("running", "stopping", "stopped") and not force:
            return {"ok": True, "stopped": False, "detail": f"Not running (status={status})", **job}

        # Hard stop first — worker sees this on next symbol (no Neon round-trip)
        try:
            ref.request_data_feed_stop()
        except Exception:
            pass
        # Mark stopping so in-flight loop exits even if Event was cleared
        try:
            store.set_job(stop_requested=True, status="stopping", message="Stop requested — finishing current symbol…")
        except Exception:
            pass
        # Re-read: the stopping write may have merged fields we want to keep.
        try:
            job = store.job() or job
        except Exception:
            pass

        try:
            cp = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
            cursor = int(cp.get("cursor") or job.get("processed") or 0)
            total = int(job.get("total") or 0)
            ok_n = int(job.get("ok_count") or 0)
            err_n = int(job.get("error_count") or job.get("errors") or 0)
            # Prefer counting done list if present
            done = list(cp.get("done") or [])
            if done:
                ok_n = max(ok_n, len(done))
            ts = ref.datetime.now(ref.IST).isoformat()
            msg = f"Stopped at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
            store.set_meta(
                last_success_at=ts,
                last_count=ok_n,
                last_errors=err_n,
                last_message=msg,
                source="stop",
                universe_size=total,
                partial=bool(total and cursor < total),
            )
            job = store.set_job(
                status="stopped",
                processed=cursor,
                total=total,
                message=msg,
                errors=err_n,
                ok_count=ok_n,
                error_count=err_n,
                finished_at=ts,
                stop_requested=False,
                checkpoint={
                    "cursor": cursor,
                    "done": done,
                    "universe": cp.get("universe") or [],
                },
            )
        except Exception as e:
            # The stop signal is already out; only the checkpoint commit failed.
            ref.logger.exception("data-feed/stop commit failed: %s", e)
            return {
                "ok": False,
                "stopped": False,
                "stop_signalled": True,
                "error": f"stop signalled but checkpoint commit failed: {str(e)[:200]}",
            }
        return {"ok": True, "stopped": True, "message": msg, **job}

    return SimpleNamespace(
        stock_structure=stock_structure,
        stock_structure_router=stock_structure_router,
        cancel_scan=cancel_scan,
        cancel_scan_router=cancel_scan_router,
        scan_stop_all=scan_stop_all,
        scan_stop_all_router=scan_stop_all_router,
        get_scan_universe=get_scan_universe,
        get_scan_universe_router=get_scan_universe_router,
        get_universe=get_universe,
        get_universe_router=get_universe_router,
        api_surprise_audit=api_surprise_audit,
        api_surprise_audit_router=api_surprise_audit_router,
        api_surprise_scan=api_surprise_scan,
        api_surprise_scan_router=api_surprise_scan_router,
        api_evaluate_price_alerts=api_evaluate_price_alerts,
        api_evaluate_price_alerts_router=api_evaluate_price_alerts_router,
        repair_single_stock=repair_single_stock,
        repair_single_stock_router=repair_single_stock_router,
        repair_batch_missing=repair_batch_missing,
        repair_batch_missing_router=repair_batch_missing_router,
        data_feed_stop=data_feed_stop,
        data_feed_stop_router=data_feed_stop_router,
    )
