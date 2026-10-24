"""Route handlers moved out of main.py in E80 (MainRef batch F, after E55, E76, E77, E78 and E79). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py). `build_bound_routes_f(ref)` returns a namespace: one router per handler (include each where the handler used to be)
and the handler functions by name."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_f(ref):
    ops_idle_tick_router = APIRouter()
    health_router = APIRouter()
    wake_all_services_probe_router = APIRouter()
    run_scan_router = APIRouter()
    get_market_indices_router = APIRouter()
    clear_universe_cache_router = APIRouter()
    catalyst_alert_status_router = APIRouter()
    ops_keepalive_router = APIRouter()
    refresh_prepare_to_buy_router = APIRouter()
    data_feed_status_router = APIRouter()
    purge_over_cap_feed_symbols_router = APIRouter()

    @ops_idle_tick_router.post("/ops/idle-tick")
    async def ops_idle_tick():
        """Called by frontend after ~5 min idle during market hours only.

    Light background work: refresh indices cache if stale, optionally warm
    hot-stocks if cache missing. Never runs heavy scans. Off-hours: no-op.
    """
        phase = ref._market_session_phase_ist()
        if phase not in ("preopen", "open", "post"):
            return {
                "ran": False,
                "reason": "off_market",
                "phase": phase,
                "note": "Background idle work only during market window; use manual Wake otherwise.",
            }
        did = []
        try:
            nk = ref._neon_keepalive_ping()
            did.append("neon_keepalive_ok" if nk.get("ok") else "neon_keepalive_error")
        except Exception as e:
            ref.logger.debug("idle-tick neon keepalive: %s", e)
            did.append("neon_keepalive_error")
        try:
            # Indices: cheap, 5 min cache already
            (await ref.asyncio.to_thread(ref.get_market_indices, force_refresh=False))
            did.append("indices")
        except Exception as e:
            ref.logger.debug("idle-tick indices: %s", e)
        try:
            cached = ref._redis_get(ref.HOT_STOCKS_CACHE_KEY)
            if not cached:
                # Only rebuild if empty — respects short market TTL
                await ref.stockky_hot_stocks(force=False)
                did.append("hot_stocks_miss")
            else:
                did.append("hot_stocks_cached")
        except Exception as e:
            ref.logger.debug("idle-tick hot: %s", e)
        return {
            "ran": True,
            "phase": phase,
            "actions": did,
            "at": ref.datetime.now(ref.IST).isoformat(),
        }

    @health_router.get("/health")
    def health(warm: bool = False):
        return {
            "status": "ok",
            "service": "api-gateway",
            "redis": bool(ref._redis),
            "durable_cache": ref._durable_cache_configured(),
            "ready": True
        }

    @wake_all_services_probe_router.post("/wake/all")
    async def wake_all_services_probe():   # session72: was a 2nd `wake_all_services` shadowing the /wake-all handler
        results = {}
        client = ref._get_http_client()  # shared keepalive pool
        if True:
            for name, svc in ref.SYSTEM_SERVICES.items():
                url = svc["url"]
                if not url:
                    results[name] = {"ok": False, "error": "no url"}
                    continue
                try:
                    resp = await client.get(f"{url.rstrip('/')}/health")
                    results[name] = {"ok": resp.status_code == 200, "status": resp.status_code}
                except Exception as e:
                    results[name] = {"ok": False, "error": str(e)}
        return {"results": results}

    @run_scan_router.get("/scan")
    async def run_scan(force_refresh: bool = False, lite: bool = False):
        """
    Legacy synchronous scan entrypoint.

    Previously: sequential httpx loop over the full universe hitting decide/{symbol}
    live with no Neon prefetch (slow + rate-limit heavy).

    Now: thin wrapper around the same parallel DB-prefetch pipeline used by
    POST /scan/start and GET /api/scan/stream. Starts run_scan_parallel, waits for
    completion, returns the final result payload (or task status if still running
    past the wait budget).
    """
        if force_refresh:
            ref._drop_cache_keys(ref.SCAN_UNIVERSE_KEY, ref.LAST_FULL_SCAN_KEY)

        # Serve recent complete scan from cache when not forcing
        if not force_refresh:
            cached = ref._redis_get(ref.LAST_FULL_SCAN_KEY)
            if cached and isinstance(cached, dict) and cached.get("result"):
                res = cached.get("result") or {}
                processed = int(cached.get("processed") or res.get("scanned") or 0)
                total = int(cached.get("total") or res.get("universe_size") or processed or 0)
                is_partial = bool(
                    cached.get("partial")
                    or cached.get("cancelled")
                    or res.get("partial")
                    or (total > 0 and processed < int(total * 0.9))
                )
                if not is_partial and total > 0 and processed >= int(total * 0.9):
                    return res

        universe = (await ref.asyncio.to_thread(ref._build_scan_universe_forced if force_refresh else ref._build_scan_universe))
        if not universe:
            return {
                "scanned_at": ref.datetime.now(ref.IST).isoformat(),
                "scanned": 0,
                "universe_size": 0,
                "recommendations": [],
                "all_results": [],
                "errors": ["empty_universe"],
                "verdict": "NO_DATA",
            }

        use_lite = bool(lite) if lite else (ref.SCAN_LITE_DEFAULT or ref._should_force_lite_scan())
        task_id = str(ref.uuid.uuid4())
        # Run the real parallel pipeline in-process (Neon bulk + semaphore workers)
        try:
            await ref.run_scan_parallel(task_id, universe, use_lite)
        except Exception as e:
            ref.logger.exception("legacy /scan parallel wrapper failed: %s", e)
            data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id) or {}
            if data.get("result"):
                return data["result"]
            raise ref.HTTPException(status_code=500, detail=f"Scan failed: {str(e)[:200]}")

        data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id) or {}
        result = data.get("result")
        if isinstance(result, dict) and result:
            return result

        # Fallback shape if parallel task stored status without nested result
        return {
            "scanned_at": ref.datetime.now(ref.IST).isoformat(),
            "scanned": data.get("processed") or 0,
            "universe_size": data.get("total") or len(universe),
            "recommendations": (data.get("result") or {}).get("recommendations", []) if isinstance(data.get("result"), dict) else [],
            "all_results": (data.get("result") or {}).get("all_results", []) if isinstance(data.get("result"), dict) else [],
            "errors": [data.get("error")] if data.get("error") else [],
            "verdict": (data.get("result") or {}).get("verdict", "UNKNOWN") if isinstance(data.get("result"), dict) else "UNKNOWN",
            "task_id": task_id,
            "status": data.get("status"),
            "parallel": True,
            "deprecated_note": "GET /scan now uses parallel DB-prefetch pipeline (same as /scan/start)",
        }

    @get_market_indices_router.get("/market/indices")
    def get_market_indices(force_refresh: bool = False):
        """
    Fetch real-time NIFTY 50 and SENSEX index values with a moderated market score.
    - Uses mapping: -0.3 percentage points -> 0, 0% -> 50, +0.3 percentage points -> 100.
    - Uses IST (Asia/Kolkata) for the fetched_at timestamp, formatted as hh:mm:ss AM/PM.
    - Adds Cache-Control headers to prevent browser caching.
    """
        now_ist = ref.datetime.now(ref.IST)
        fetched_at_str = now_ist.strftime("%I:%M:%S %p")

        if not force_refresh:
            cached = ref._redis_get(ref.INDICES_CACHE_KEY)
            # A cache entry holding inf/NaN (written by an older build) is ignored
            # and rebuilt rather than served: it cannot be JSON-encoded.
            if cached and isinstance(cached, dict) and ref._json_finite(cached):
                cached["fetched_at"] = fetched_at_str
                return ref.JSONResponse(
                    content=cached,
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    }
                )

        try:
            nifty = ref._IndexTicker("^NSEI")
            sensex = ref._IndexTicker("^BSESN")
            nifty_hist = nifty.history(period="1d")
            sensex_hist = sensex.history(period="1d")
            if nifty_hist.empty or sensex_hist.empty:
                # Handled by the degrade path below (last-known copy / zero fallback, HTTP 200).
                raise ValueError("Index data temporarily unavailable")

            nifty_close = nifty_hist['Close'].iloc[-1]
            nifty_open = nifty_hist['Open'].iloc[0]
            nifty_prev_close = nifty_hist['Close'].iloc[0] if len(nifty_hist) > 1 else nifty_open
            nifty_change = nifty_close - nifty_prev_close
            nifty_change_pct = ref._safe_pct(nifty_change, nifty_prev_close)

            sensex_close = sensex_hist['Close'].iloc[-1]
            sensex_open = sensex_hist['Open'].iloc[0]
            sensex_prev_close = sensex_hist['Close'].iloc[0] if len(sensex_hist) > 1 else sensex_open
            sensex_change = sensex_close - sensex_prev_close
            sensex_change_pct = ref._safe_pct(sensex_change, sensex_prev_close)

            avg_change = (nifty_change_pct + sensex_change_pct) / 2
            # Group 221: real previous-session change, reported beside (not instead of) the open-based one.
            nifty_vs_prev = ref._prev_session_change(nifty, nifty_close)
            sensex_vs_prev = ref._prev_session_change(sensex, sensex_close)
            # 2026-09-03 fix: sensitivity raised 0.3 → 1.5.
            # Old formula mapped ±0.3% Nifty move to full 0-100 range, so a
            # routine -0.26% flat/slightly-down day scored 7 — "disaster" level.
            # Real crashes are -2% to -3%. At sensitivity=1.5:
            #   -1.5% → score 0 (genuine bad day), 0% → 50, +1.5% → 100
            #   today's -0.26% → score ~41 (correctly: slightly weak, not catastrophic)
            # This stops the regime gate from blocking individual volume-shock
            # movers on routine flat-Nifty days.
            sensitivity = 1.5
            raw_score = 50 + (avg_change / sensitivity) * 50
            market_score = max(0, min(100, raw_score))

            if market_score >= 60:
                mood = "BULLISH"
            elif market_score <= 40:
                mood = "BEARISH"
            else:
                mood = "NEUTRAL"

            result = {
                "nifty": {
                    "price": round(nifty_close, 2),
                    "change": round(nifty_change, 2),
                    "change_pct": round(nifty_change_pct, 2)
                },
                "sensex": {
                    "price": round(sensex_close, 2),
                    "change": round(sensex_change, 2),
                    "change_pct": round(sensex_change_pct, 2)
                },
                "market_mood": mood,
                "market_score": round(market_score),
                "fetched_at": fetched_at_str,
            }
            if nifty_vs_prev:
                result["nifty_vs_prev_close"] = nifty_vs_prev
            if sensex_vs_prev:
                result["sensex_vs_prev_close"] = sensex_vs_prev
            if not ref._json_finite(result):
                raise ValueError("non-finite index values")
            ref._redis_set(ref.INDICES_CACHE_KEY, result, ttl=300)
            ref._redis_set(ref.INDICES_LAST_KNOWN, result, ttl=86400)
            return ref.JSONResponse(
                content=result,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )

        except Exception as e:
            ref.logger.error(f"Error fetching indices: {e}")
            last_known = ref._redis_get(ref.INDICES_LAST_KNOWN)
            if last_known and isinstance(last_known, dict) and ref._json_finite(last_known):
                last_known = dict(last_known)   # never mutate the stored copy in place
                last_known["fetched_at"] = fetched_at_str
                last_known["stale"] = True
                ref._redis_set(ref.INDICES_CACHE_KEY, last_known, ttl=60)
                return ref.JSONResponse(
                    content=last_known,
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    }
                )
            else:
                fallback = {
                    "nifty": {"price": 0, "change": 0, "change_pct": 0},
                    "sensex": {"price": 0, "change": 0, "change_pct": 0},
                    "market_mood": "NEUTRAL",
                    "market_score": 50,
                    "fetched_at": fetched_at_str,
                    "stale": True,
                    "fallback": True
                }
                # Short-lived only: writing the zero fallback to the 24 h last-known
                # key meant the NEXT outage served zeros as if they were a real copy.
                ref._redis_set(ref.INDICES_CACHE_KEY, fallback, ttl=60)
                return ref.JSONResponse(
                    content=fallback,
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    }
                )

    @clear_universe_cache_router.delete("/scan/universe/cache")
    def clear_universe_cache():
        """Clear every cache layer holding the scan universe.

    Used to do nothing (yet report success) unless the legacy Redis client
    was configured; the real stores are the Neon/kv layer and the in-process
    fallback, so those are cleared too and the response says what happened.
    """
        cleared = []
        errors = []
        if ref._kv_cache is not None:
            try:
                ref._kv_cache.delete(ref.SCAN_UNIVERSE_KEY)
                cleared.append("kv")
            except Exception as e:
                errors.append(f"kv: {e}")
        try:
            ref._mem_kv.pop(ref.SCAN_UNIVERSE_KEY, None)
            ref._mem_kv_exp.pop(ref.SCAN_UNIVERSE_KEY, None)
            cleared.append("memory")
        except Exception as e:
            errors.append(f"memory: {e}")
        if ref._redis:
            try:
                ref._redis.delete(ref.SCAN_UNIVERSE_KEY)
                cleared.append("redis")
            except Exception as e:
                errors.append(f"redis: {e}")
        if not cleared:
            return {
                "ok": False,
                "cleared": [],
                "errors": errors,
                "message": "Scan universe cache could not be cleared",
            }
        out = {"message": "Scan universe cache cleared — will rebuild on next scan", "cleared": cleared}
        if errors:
            out["errors"] = errors
        return out

    @catalyst_alert_status_router.get("/catalysts/alert/status")
    def catalyst_alert_status():
        """Status of last catalyst job. Auto-heal if stuck after free-tier sleep."""
        job_key = "stockky:catalyst_job"
        st = ref._redis_get(job_key) or {}
        if not isinstance(st, dict):
            st = {"status": "idle"}
        try:
            if st.get("status") == "running":
                updated = st.get("updated_at") or st.get("started_at")
                age = None
                if updated:
                    try:
                        ts = ref.datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=ref.IST)
                        age = int((ref.datetime.now(ref.IST) - ts).total_seconds())
                    except Exception:
                        age = None
                # No heartbeat for 5 minutes → worker likely dead
                if age is not None and age > 300:
                    st = {
                        **st,
                        "status": "error",
                        "error": f"stale_running age={age}s (worker likely slept)",
                        "message": f"Auto-failed: no progress for {age}s — re-run will continue batches",
                        "updated_at": ref.datetime.now(ref.IST).isoformat(),
                    }
                    try:
                        ref._redis_set(job_key, st, ttl=86400)
                    except Exception as _we:
                        # Reported as error, but the stored job still says
                        # "running" — say so instead of implying it was healed.
                        ref.logger.warning("catalyst status heal write failed: %s", _we)
                        st = {**st, "heal_persisted": False}
        except Exception as e:
            ref.logger.debug("catalyst status heal: %s", e)
        return {"ok": True, **st}

    @ops_keepalive_router.get("/ops/keepalive")
    @ops_keepalive_router.post("/ops/keepalive")
    async def ops_keepalive(deep: bool = False):
        """
    Lightweight keep-alive for free-tier.
    - Always: gateway health (cheap)
    - deep=true: sequential soft-ping of required services (used sparingly by UI while active)
    Avoids parallel storms; safe during scans.
    """
        out = {"ok": True, "gateway": True, "services": {}, "ts": ref.datetime.now(ref.IST).isoformat()}
        if not deep:
            return out
        if not ref._wake_pings_enabled():
            # Always-on host: nothing sleeps, so the browser's keep-alive ping has nothing to wake.
            out["skipped"] = True
            return out
        # Soft sequential pings — never block more than ~12s total
        client = ref._get_http_client()
        for name, cfg in list(ref.SYSTEM_SERVICES.items())[:6]:
            url = (cfg.get("url") or "").rstrip("/")
            if not url:
                continue
            try:
                r = await client.get(f"{url}/health", params={"warm": "true"}, timeout=4.0)
                out["services"][name] = r.status_code == 200
            except Exception:
                out["services"][name] = False
                await ref.asyncio.sleep(0.15)
        out["ok"] = True
        return out

    @refresh_prepare_to_buy_router.post("/data-feed/refresh-prepare-to-buy")
    @refresh_prepare_to_buy_router.post("/api/data-feed/refresh-prepare-to-buy")
    @refresh_prepare_to_buy_router.post("/api/feed/refresh-prepare-to-buy")
    async def refresh_prepare_to_buy(
        min_score: float = 58.0,
        max_score: float = 68.0,
    ):
        """
    Surgical live-quote refresh for high-conviction "Prepare to Buy" setups only.
    Completely bypasses the 300-stock API storm — only candidates in the
    [min_score, max_score) band (default 58–68) are quoted, with a 0.3s gap.
    """
        import asyncio
        from data_feed import (
            find_prepare_to_buy_candidates,
            patch_feed_price,
        )

        try:
            candidates = await asyncio.to_thread(find_prepare_to_buy_candidates, min_score=min_score, max_score=max_score)  # group361: off the event loop
        except Exception as e:
            ref.logger.exception("refresh_prepare_to_buy candidates: %s", e)
            raise ref.HTTPException(status_code=500, detail=str(e)[:200])

        if not candidates:
            return {
                "status": "success",
                "refreshed_count": 0,
                "symbols": [],
                "message": "No Prepare-to-Buy candidates in score band",
                "min_score": min_score,
                "max_score": max_score,
            }

        md_base = (ref.MARKET_DATA_URL or "").rstrip("/")
        updated: list = []
        errors: list = []

        # group358 (LOG-ONLY): snapshot each row and its stored price BEFORE the patch (an in-memory store may hand out the same dict the patch then
        # mutates), so the notes can say "structure now blocks entry at the fresh price, it passed at the last stored one". Fail-open: no notes, same refresh.
        feed_rows: ref.Dict[str, ref.Any] = {}
        old_prices: ref.Dict[str, ref.Any] = {}
        live_prices: ref.Dict[str, float] = {}
        _bs = None
        try:
            import buy_sniper as _bs
            if _bs._prep_mode() != "off":
                from data_feed import get_all_stock_feeds
                _got = await asyncio.to_thread(get_all_stock_feeds, list(candidates))
                for _k, _r in (_got.items() if isinstance(_got, dict) else []):
                    if isinstance(_r, dict):
                        feed_rows[_k] = dict(_r)
                        old_prices[_k] = _r.get("price")
        except Exception as e:  # noqa: BLE001
            ref.logger.debug("refresh_prepare_to_buy structure snapshot failed (no notes): %s", e)
            feed_rows, old_prices = {}, {}

        async with ref.httpx.AsyncClient() as client:
            for symbol in candidates:
                try:
                    q_res = await client.get(f"{md_base}/quote/{symbol}", timeout=3.0)
                    if q_res.status_code == 200:
                        body = q_res.json() if q_res.content else {}
                        live_price = None
                        if isinstance(body, dict):
                            for k in ("cmp", "price", "ltp", "close", "last_price", "regularMarketPrice"):
                                try:
                                    v = float(body.get(k) or 0)
                                    if v > 0:
                                        live_price = v
                                        break
                                except (TypeError, ValueError):
                                    pass
                        if live_price:
                            live_prices[symbol] = live_price
                        if live_price and patch_feed_price(symbol, live_price):
                            updated.append(symbol)
                    else:
                        errors.append({"symbol": symbol, "status": q_res.status_code})
                except Exception as e:
                    errors.append({"symbol": symbol, "error": str(e)[:120]})
                await asyncio.sleep(0.3)

        reply: ref.Dict[str, ref.Any] = {
            "status": "success",
            "refreshed_count": len(updated),
            "symbols": candidates,
            "updated": updated,
            "errors": errors[:20],
            "min_score": min_score,
            "max_score": max_score,
            "message": f"Refreshed {len(updated)}/{len(candidates)} Prepare-to-Buy quotes",
        }
        if feed_rows and _bs is not None:  # group358: structure notes ride along in the reply and the log; the refresh above never depends on them
            try:
                rep = _bs.prepare_refresh_report(feed_rows, live_prices, old_prices)
                if rep.get("mode") != "off":
                    counts = {k: int(rep.get(k) or 0) for k in ("checked", "no_opinion", "turned_bad", "flipped", "with_triggers")}
                    for _sym, _note in (rep.get("notes") or {}).items():
                        if _note.get("headline"):
                            ref.logger.info("prepare refresh structure %s: %s", _sym, _note["headline"])
                    ref.logger.info("prepare refresh structure: checked=%d no_opinion=%d turned_bad=%d flipped=%d with_triggers=%d",
                                counts["checked"], counts["no_opinion"], counts["turned_bad"], counts["flipped"], counts["with_triggers"])
                    for _outcome in ("checked", "no_opinion", "turned_bad", "flipped", "with_triggers"):
                        ref._structure_metric("stockky_prepare_structure_total", counts[_outcome], outcome=_outcome)
                    reply["structure_mode"] = rep.get("mode")
                    reply["structure_notes"] = rep.get("notes") or {}
                    reply["structure_counts"] = counts
            except Exception as e:  # noqa: BLE001
                ref.logger.debug("refresh_prepare_to_buy structure notes failed (ignored): %s", e)
        return reply

    @data_feed_status_router.get("/data-feed/status")
    @data_feed_status_router.get("/api/data-feed/status")
    def data_feed_status():
        """Return job+meta. Auto-heal stale 'running' jobs (worker died after free-tier sleep)."""
        store = ref._feed_store()
        job = store.job()
        meta = store.meta()
        try:
            if job.get("status") == "running":
                stale_sec = int(((ref.os.getenv("DATA_FEED_STALE_SEC") or "").strip() or "900"))  # 15 min — free-tier sleep is not a restart signal
                updated = None
                # Prefer checkpoint/elapsed; fall back to started_at
                for key in ("updated_at", "resumed_at", "started_at"):
                    raw = job.get(key)
                    if not raw:
                        continue
                    try:
                        ts = ref.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=ref.IST)
                        updated = ts
                        break
                    except Exception:
                        pass
                # Also treat long elapsed without stop as ok only if stop_requested
                elapsed = int(job.get("elapsed_sec") or 0)
                stop_req = bool(job.get("stop_requested"))
                age = None
                if updated is not None:
                    age = int((ref.datetime.now(ref.IST) - updated).total_seconds())
                # If stop was requested OR last activity is old, force-commit stopped
                if stop_req or (age is not None and age > stale_sec) or (elapsed > 0 and age is None and stop_req):
                    # Force commit checkpoint as stopped so Resume can work
                    cp = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
                    cursor = int(cp.get("cursor") or job.get("processed") or 0)
                    total = int(job.get("total") or 0)
                    ok_n = int(job.get("ok_count") or meta.get("last_count") or 0)
                    err_n = int(job.get("error_count") or job.get("errors") or 0)
                    ts = ref.datetime.now(ref.IST).isoformat()
                    msg = (
                        f"Auto-stopped (stale/sleep) at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
                        if not stop_req
                        else f"Stopped at {cursor}/{total} — committed {ok_n} fed stocks at {ts}"
                    )
                    store.set_meta(
                        last_success_at=ts,
                        last_count=ok_n,
                        last_errors=err_n,
                        last_message=msg,
                        source="stop_or_stale",
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
                            "done": list(cp.get("done") or []),
                            "universe": cp.get("universe") or [],
                        },
                    )
                    meta = store.meta()
        except Exception as e:
            ref.logger.warning("data-feed status heal: %s", e)
        # Prefer durable index count so UI survives cold start after a successful feed
        try:
            fed_count = int(store.count_symbols())
        except Exception:
            fed_count = 0
        if fed_count <= 0:
            fed_count = int((meta or {}).get("last_count") or (job or {}).get("ok_count") or 0)
        last_ok = (meta or {}).get("last_success_at") or (job or {}).get("finished_at")
        return ref._normalize_data_feed_status(store, job, meta, fed_count, last_ok)

    @purge_over_cap_feed_symbols_router.post("/api/feed/purge-over-cap")
    @purge_over_cap_feed_symbols_router.post("/data-feed/purge-over-cap")
    async def purge_over_cap_feed_symbols():
        """
    Delete every feed row whose stored price is above MAX_UNIVERSE_PRICE.
    These are stale writes from before the price-cap was enforced at every
    write path (see _row_price_over_cap) — Repair cannot legitimately "fix"
    them since a real price fetch would also be over cap, so the only
    correct action is to remove them from the feed store.
    """
        store = ref._feed_store()
        try:
            symbols = list(store.list_symbols() or [])
        except Exception:
            symbols = []
        purged = []
        skipped = []
        for sym in symbols:
            try:
                row = await ref.asyncio.to_thread(store.get_symbol, sym) or {}  # group303
            except Exception as e:
                # A failed read is not evidence the row is over cap (it used to be judged as an empty row,
                # so the by-name denylist could delete a row we never managed to read). Leave it alone.
                ref.logger.warning("purge_over_cap: could not read %s, skipping: %s", sym, e)
                skipped.append(sym)
                continue
            if isinstance(row, dict) and ref._row_price_over_cap(row, symbol=sym):
                try:
                    await ref.asyncio.to_thread(store.delete_symbol, sym)  # group361: off the event loop
                    purged.append(sym)
                except Exception as e:
                    ref.logger.warning("purge_over_cap: failed to delete %s: %s", sym, e)
        return {
            "ok": True,
            "purged_count": len(purged),
            "purged_symbols": purged,
            "skipped_count": len(skipped),
            "skipped_symbols": skipped,
            "message": (
                f"Removed {len(purged)} symbol(s) above ₹{ref.MAX_UNIVERSE_PRICE:.0f} from the feed."
                + (f" Skipped {len(skipped)} symbol(s) that could not be read; run Purge again." if skipped else "")
            ),
        }

    return SimpleNamespace(
        ops_idle_tick=ops_idle_tick,
        ops_idle_tick_router=ops_idle_tick_router,
        health=health,
        health_router=health_router,
        wake_all_services_probe=wake_all_services_probe,
        wake_all_services_probe_router=wake_all_services_probe_router,
        run_scan=run_scan,
        run_scan_router=run_scan_router,
        get_market_indices=get_market_indices,
        get_market_indices_router=get_market_indices_router,
        clear_universe_cache=clear_universe_cache,
        clear_universe_cache_router=clear_universe_cache_router,
        catalyst_alert_status=catalyst_alert_status,
        catalyst_alert_status_router=catalyst_alert_status_router,
        ops_keepalive=ops_keepalive,
        ops_keepalive_router=ops_keepalive_router,
        refresh_prepare_to_buy=refresh_prepare_to_buy,
        refresh_prepare_to_buy_router=refresh_prepare_to_buy_router,
        data_feed_status=data_feed_status,
        data_feed_status_router=data_feed_status_router,
        purge_over_cap_feed_symbols=purge_over_cap_feed_symbols,
        purge_over_cap_feed_symbols_router=purge_over_cap_feed_symbols_router,
    )
