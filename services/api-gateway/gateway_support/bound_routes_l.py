"""Route handlers moved out of main.py in E87 (MainRef batch L, after E55 and E76-E86). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py); names that are locals of a handler or of its nested functions stay as they were. `build_bound_routes_l(ref)` returns a namespace: one router per handler
(include each where the handler used to be) and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names, which must be evaluated
when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_l(ref):
    stock_chart_context_router = APIRouter()
    get_stock_decision_router = APIRouter()
    data_feed_structure_refresh_status_router = APIRouter()
    data_feed_structure_refresh_router = APIRouter()
    data_feed_symbol_router = APIRouter()

    @stock_chart_context_router.get("/stock/{symbol}/chart-context")
    async def stock_chart_context(symbol: str, force: bool = False, tail: ref.Optional[int] = None, tf: str = "1d"):
        """E25 / E26 / E27: candlestick bars (1m, 5m, 15m, 30m, 1h or 1d) + pattern markers / shapes + plain-English badges + regime chips (proxy to the
    technical service). Context only. Always 200: when the engine / upstream is down it returns {ok: False, ...empty} so the card falls back to the plain chart.
    Intraday uses its own circuit-breaker name, so a slow 1-minute history can never open the breaker the analysis features share."""
        base = (symbol or "").strip().upper().replace(".NS", "").replace(".BO", "")
        tf_key = (tf or "1d").strip().lower()
        tf_key = {"60m": "1h", "d": "1d", "daily": "1d"}.get(tf_key, tf_key)
        empty = {"symbol": base, "ok": False, "tf": tf_key if tf_key in ref._CHART_TFS else "1d", "bars": [], "markers": [], "shapes": [], "levels": [], "badges": [],
                 "regime": {"trend": None, "volatility": None, "momentum": None, "structure": None}}
        if not base or tf_key not in ref._CHART_TFS:
            return empty
        try:
            intraday = tf_key != "1d"
            cap = 1500 if intraday else 500
            qs = f"tf={tf_key}" + (f"&tail={max(30, min(int(tail), cap))}" if tail is not None else "") + ("&force=true" if force else "")
            async with ref.httpx.AsyncClient() as client:
                resp = await ref._cb_get(client, "technical_chart" if intraday else "technical", f"{ref.TECHNICAL_URL}/chart-context/{base}?{qs}", timeout=30 if intraday else 25)
            if resp.status_code != 200:
                return empty
            data = resp.json()
            if not isinstance(data, dict) or data.get("schema") != 1 or not data.get("ok"):
                if isinstance(data, dict) and data.get("reason"):
                    empty["reason"] = str(data["reason"])[:40]
                return empty
            return data
        except Exception as e:  # noqa: BLE001
            ref.logger.debug("chart-context proxy %s %s: %s", base, tf_key, e)
            return empty

    @get_stock_decision_router.get("/stock/{symbol}")
    async def get_stock_decision(symbol: str, already_owned: bool = False):
        """
    Single-stock Analyse.

    1) One live decide/{symbol}?force=true (decision service already parallel-gathers
       technical/fundamental/news/events/prediction).
    2) Optional enrichment only when decide clearly lacked a pillar — all conditional
       re-fetches run concurrently via asyncio.gather (no serial waterfall).
    """
        original = symbol.strip()
        typed = ref._squash_symbol(original)
        resolved = (await ref.asyncio.to_thread(ref._resolve_symbol, original))
        if resolved is None:
            symbol_to_use = typed
            corrected_from = None
        elif resolved != typed:
            # An alias (HEROMOTORS -> HEROMOTOCO) is a curated, deliberate mapping. A fuzzy match is
            # only a guess: below the confidence bar, ask instead of analysing a different company.
            if typed not in ref.SYMBOL_ALIASES:
                _known = await ref.asyncio.to_thread(ref._get_all_known_symbols)
                if typed not in _known and not ref._fuzzy_correction_is_confident(typed, resolved, _known):
                    _hints = ref._symbol_suggestions(typed, _known, first=resolved)
                    raise ref.HTTPException(
                        status_code=404,
                        detail=f"Symbol '{typed}' not found. Did you mean: {', '.join(_hints)}?",
                    )
            symbol_to_use = resolved
            corrected_from = typed
        else:
            symbol_to_use = typed
            corrected_from = None

        async def _remember_searched() -> None:
            """Record the symbol only after a real analysis came back. It used to be recorded up front,
        so every typo (and every unknown name) became a 'known' symbol and then won the next fuzzy
        match: that is how a mistyped Hero Motors kept resolving to the non-symbol HEROMOTORS."""
            try:
                await ref.asyncio.to_thread(ref._add_searched, symbol_to_use)
                await ref.asyncio.to_thread(ref._drop_cache_keys, ref.SCAN_UNIVERSE_KEY)
            except Exception as e:
                ref.logger.debug("stock: recording searched symbol %s failed: %s", symbol_to_use, e)

        def _reason_blob(reasons: dict, key: str) -> str:
            return " ".join(str(x) for x in (reasons.get(key) or [])).lower()

        def _pillar_failed(blob: str) -> bool:
            """True only when decide explicitly reported failure — not merely a neutral score."""
            needles = (
                "temporarily unavailable",
                "error processing",
                "recovering",
                "unavailable",
                "timed out",
                "timeout",
                "failed to",
                "not available",
                "data insufficient",
                "could not",
            )
            return any(n in blob for n in needles)

        _t0 = ref.time.monotonic()

        def _remaining() -> float:
            return ref.STOCK_OVERALL_DEADLINE_SEC - (ref.time.monotonic() - _t0)

        try:
            async with ref.httpx.AsyncClient(timeout=ref.httpx.Timeout(ref.STOCK_DECIDE_TIMEOUT_SEC, connect=5.0)) as client:
                # ── 1) Live decide (already fans out in parallel downstream) ──
                try:
                    resp = await client.get(
                        f"{ref.DECISION_URL}/decide/{symbol_to_use}",
                        params={"already_owned": str(already_owned).lower(), "force": "true"},
                        timeout=ref.httpx.Timeout(max(1.0, min(ref.STOCK_DECIDE_TIMEOUT_SEC, _remaining())), connect=5.0),
                    )
                except ref.httpx.ReadTimeout as _slow:
                    # The decision service was reachable but still working (cold caches). A second forced
                    # run would redo everything, so ask WITHOUT force: the downstream analysis caches the
                    # first attempt warmed are now usable, and a finished decision may already be cached.
                    _left = _remaining()
                    if _left < ref.STOCK_DECIDE_RETRY_MIN_SEC:
                        raise
                    ref.logger.warning(
                        "decide/%s read-timeout after %.0fs; retrying once without force (%.0fs of budget left)",
                        symbol_to_use, ref.STOCK_DECIDE_TIMEOUT_SEC, _left,
                    )
                    resp = await client.get(
                        f"{ref.DECISION_URL}/decide/{symbol_to_use}",
                        params={"already_owned": str(already_owned).lower(), "force": "false"},
                        timeout=ref.httpx.Timeout(max(1.0, min(ref.STOCK_DECIDE_TIMEOUT_SEC, _left)), connect=5.0),
                    )
                resp.raise_for_status()
                try:
                    raw = resp.json()
                except ValueError as e:
                    # A 200 whose body is not JSON used to escape as a bare 500.
                    ref.logger.warning("decision engine returned non-JSON for %s: %s", symbol_to_use, e)
                    return ref._degraded_hold(
                        symbol_to_use,
                        "decision engine returned a non-JSON response",
                        f"{symbol_to_use}: decision engine returned an unreadable response — "
                        f"showing a neutral HOLD until it recovers.",
                        "Decision engine error",
                        "Upstream decision-prediction-service returned a non-JSON body — scores unavailable.",
                    )
                result = ref._normalize_decision_response(raw, symbol_to_use)

                reasons = result.get("reasons") if isinstance(result.get("reasons"), dict) else {}
                if not isinstance(reasons, dict):
                    reasons = {}
                    result["reasons"] = reasons

                # Price fill if decide returned no close
                if result.get("close") is None:
                    try:
                        price = await ref.asyncio.to_thread(ref._fetch_price_from_quote, symbol_to_use)
                        if price is not None:
                            result["close"] = price
                    except Exception:
                        pass
                if result.get("close") is not None:
                    result["data_insufficient"] = False
                    try:
                        c = float(result["close"])
                        if result.get("support") is None:
                            result["support"] = round(c * 0.97, 2)
                        if result.get("resistance") is None:
                            result["resistance"] = round(c * 1.03, 2)
                    except (TypeError, ValueError):
                        pass

                tech_blob = _reason_blob(reasons, "technical")
                fund_blob = _reason_blob(reasons, "fundamental")
                news_blob = _reason_blob(reasons, "news")

                # Tighten: neutral score 50 alone is NOT a re-fetch trigger
                need_tech = _pillar_failed(tech_blob) or (
                    result.get("technical_score") is None and result.get("data_insufficient")
                )
                need_fund = _pillar_failed(fund_blob) or (
                    result.get("fundamental_score") is None
                    and not result.get("fundamental_metrics")
                )
                need_news = result.get("news_score") is None and (
                    _pillar_failed(news_blob) or not (reasons.get("news") or [])
                )
                need_events = (
                    not result.get("event_data")
                    and not reasons.get("event")
                    and not result.get("event_risk")  # None or False both mean "no event risk known yet"
                )
                need_pred = result.get("prediction_score") is None and result.get("prediction_note") is None

                async def _get(url: str, timeout: float = 45.0):
                    try:
                        r = await client.get(url, timeout=max(1.0, min(timeout, _remaining())))
                        if r.status_code == 200 and r.content:
                            data = r.json()
                            return data if isinstance(data, dict) else None
                    except Exception as e:
                        ref.logger.debug("enrich %s: %s", url, e)
                    return None

                tasks = {}
                if need_tech:
                    tasks["tech"] = ref.asyncio.create_task(
                        _get(f"{ref.TECHNICAL_URL}/analyze/{symbol_to_use}?force=true", 45.0)
                    )
                if need_fund:
                    tasks["fund"] = ref.asyncio.create_task(
                        _get(f"{ref.FUNDAMENTAL_URL}/analyze/{symbol_to_use}?force=true", 60.0)
                    )
                if need_news:
                    tasks["news"] = ref.asyncio.create_task(
                        _get(f"{ref.NEWS_URL}/analyze/{symbol_to_use}", 30.0)
                    )
                if need_events:
                    tasks["events"] = ref.asyncio.create_task(
                        _get(f"{ref.EVENT_URL}/events/{symbol_to_use}?force=true", 45.0)
                    )
                if need_pred:
                    tasks["pred"] = ref.asyncio.create_task(
                        _get(f"{ref.PREDICTION_URL}/predict/{symbol_to_use}", 30.0)
                    )

                fetched = {}
                if tasks:
                    # Overall deadline: keep whatever finished in time, cancel the rest. (A plain
                    # gather() could outlive the budget by the slowest per-call timeout.)
                    keys = list(tasks.keys())
                    _done, _pending = await ref.asyncio.wait(
                        [tasks[k] for k in keys], timeout=max(0.0, _remaining())
                    )
                    for _t in _pending:
                        _t.cancel()
                    if _pending:
                        await ref.asyncio.gather(*_pending, return_exceptions=True)
                        ref.logger.warning(
                            "stock %s: overall %.0fs deadline hit; dropped enrichment %s",
                            symbol_to_use, ref.STOCK_OVERALL_DEADLINE_SEC,
                            [k for k in keys if tasks[k] in _pending],
                        )
                    for k in keys:
                        t = tasks[k]
                        if t in _pending or t.cancelled():
                            fetched[k] = None
                        elif t.exception() is not None:
                            ref.logger.debug("enrich task %s failed: %s", k, t.exception())
                            fetched[k] = None
                        else:
                            fetched[k] = t.result()

                # Apply enrichment
                td = fetched.get("tech")
                if isinstance(td, dict):
                    if td.get("technical_score") is not None:
                        result["technical_score"] = td["technical_score"]
                    for k in ("support", "resistance", "trend_strength", "volume_surge", "rsi", "close"):
                        if td.get(k) is not None and result.get(k) is None:
                            result[k] = td[k]
                    if td.get("reasons"):
                        reasons["technical"] = td["reasons"] if isinstance(td["reasons"], list) else [str(td["reasons"])]
                    ref.ms_passthrough.merge_into(result, td)  # Group 349: additive market_structure + structure_summary

                fd = fetched.get("fund")
                if isinstance(fd, dict):
                    if fd.get("fundamental_score") is not None:
                        result["fundamental_score"] = fd["fundamental_score"]
                    metrics = fd.get("metrics")
                    if metrics:
                        result["fundamental_metrics"] = metrics
                    result["fundamental_fallback"] = bool(fd.get("fallback_used"))
                    if fd.get("reasons"):
                        reasons["fundamental"] = fd["reasons"] if isinstance(fd["reasons"], list) else [str(fd["reasons"])]
                    # Write real fundamentals back to Neon data-feed (merge, never wipe)
                    try:
                        from data_feed import save_stock_feed, extract_feed_payload
                        row = extract_feed_payload(symbol_to_use, fundamental=fd, extra={
                            "fundamental_score": fd.get("fundamental_score"),
                            "source": "stock_enrich_fundamental",
                        })
                        if row:
                            save_stock_feed(symbol_to_use, row)
                    except Exception as _fe:
                        ref.logger.debug("fund writeback %s: %s", symbol_to_use, _fe)

                nd = fetched.get("news")
                if isinstance(nd, dict) and nd.get("news_score") is not None:
                    result["news_score"] = nd.get("news_score")
                    if nd.get("reasons"):
                        reasons["news"] = nd["reasons"] if isinstance(nd["reasons"], list) else [str(nd["reasons"])]

                ed = fetched.get("events")
                if isinstance(ed, dict):
                    result["event_data"] = ed
                    if ed.get("next_earnings_date"):
                        result["event_risk"] = True
                        reasons.setdefault("event", [])
                        if isinstance(reasons["event"], list):
                            reasons["event"] = list(reasons["event"]) + [f"Earnings due: {ed['next_earnings_date']}"]

                pd = fetched.get("pred")
                if isinstance(pd, dict) and pd.get("model_loaded"):
                    if pd.get("prediction_score") is not None:
                        result["prediction_score"] = pd.get("prediction_score")
                    if pd.get("note"):
                        result["prediction_note"] = pd.get("note")

                result["reasons"] = reasons
                result["enrichment"] = {
                    "need_tech": need_tech,
                    "need_fund": need_fund,
                    "need_news": need_news,
                    "need_events": need_events,
                    "need_pred": need_pred,
                    "fetched": [k for k, v in fetched.items() if v],
                }

            if corrected_from:
                result["corrected_from"] = corrected_from
                result["symbol"] = symbol_to_use

            # Single-stock Analyse is the ONLY place Gemini is allowed to run (per
            # product decision: Gemini is too slow/rate-limited to fire once per
            # symbol across a 300-500 name Run Market Scan, but firing it once for
            # a single stock the user is actively looking at is fine). Falls back
            # to the Hinglish template automatically if GEMINI_API_KEY is unset or
            # the call fails/times out — see _generate_ai_summary.
            try:
                _left = _remaining()
                if _left < ref._STOCK_SUMMARY_RESERVE_SEC:
                    result["natural_language_summary"] = ref._generate_summary(result)
                else:
                    result["natural_language_summary"] = await ref.asyncio.wait_for(
                        ref._generate_ai_summary(result, client), timeout=_left
                    )
            except Exception:      # includes asyncio.TimeoutError from the budget
                result["natural_language_summary"] = ref._generate_summary(result)

            flags = []
            level = "high"
            if result.get("fundamental_fallback") or result.get("data_insufficient"):
                flags.append("Fundamentals partial/fallback")
                level = "medium"
            if result.get("news_score") is None:
                flags.append("News unavailable")
                level = "low" if level != "high" else "medium"
            if result.get("prediction_score") is None:
                flags.append("Model score missing")
            tech_reasons = (result.get("reasons") or {}).get("technical") or []
            if any("delivery" in str(r).lower() and "unavailable" in str(r).lower() for r in tech_reasons):
                flags.append("Delivery % not official")
                level = "medium"
            if result.get("training_score") in (None, 0, 50):
                flags.append("Training signal thin")
            if ref._thin_technical_history(result, tech_reasons):
                # First in the list: the UI shows only the first three flags, and this one changes how far
                # the technical score can be trusted.
                flags.insert(0, ref.THIN_TECHNICAL_FLAG)
                level = "medium" if level == "high" else level
            result["data_quality"] = {
                "level": level if flags else "high",
                "flags": flags,
                "note": (
                    "Scores may be soft — limited free data"
                    if flags
                    else "Core inputs present"
                ),
            }
            if result.get("close") is not None:
                await _remember_searched()
            return result

        except ref.httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                suggestions = ref.difflib.get_close_matches(symbol_to_use, (await ref.asyncio.to_thread(ref._get_all_known_symbols)), n=3, cutoff=0.5)
                suggestion_text = (
                    f"Symbol '{symbol_to_use}' not found. Did you mean: {', '.join(suggestions)}?"
                    if suggestions
                    else f"Symbol '{symbol_to_use}' not found."
                )
                raise ref.HTTPException(status_code=404, detail=suggestion_text)
            else:
                # Upstream (decision-prediction-service) returned a real error
                # response (commonly 500 when it OOMs / crashes on Render's free
                # tier). Previously this re-raised the same status code, which
                # the platform edge often surfaces to the user as a raw
                # "502 Bad Gateway" with no explanation. Degrade gracefully
                # instead — same pattern as /api/scan/find-buys: a decision
                # result the UI can render, flagged as low-confidence, beats an
                # error page.
                ref.logger.warning(
                    "decision engine HTTP %s for %s: %s",
                    e.response.status_code, symbol_to_use, str(e.response.text)[:200],
                )
                return ref._degraded_hold(
                    symbol_to_use,
                    f"decision engine returned HTTP {e.response.status_code}",
                    f"{symbol_to_use}: decision engine is temporarily unavailable "
                    f"(HTTP {e.response.status_code}) — showing a neutral HOLD until it recovers.",
                    "Decision engine error",
                    "Upstream decision-prediction-service returned an error — scores unavailable.",
                )
        except ref.httpx.TimeoutException as e:
            # Reachable but too slow even after the retry (cold caches / heavy load). Say so, instead of
            # claiming an outage: a second try a minute later normally succeeds.
            ref.logger.warning(
                "decision engine timed out for %s after %.0fs: %s: %s",
                symbol_to_use, ref.time.monotonic() - _t0, type(e).__name__, e,
            )
            return ref._degraded_hold(
                symbol_to_use,
                f"decision engine timed out after {ref.time.monotonic() - _t0:.0f}s ({type(e).__name__})",
                f"{symbol_to_use}: the decision engine is taking longer than usual to respond "
                f"(caches warming up) — showing a neutral HOLD. Please try again in a minute.",
                "Decision engine slow",
                "decision-prediction-service did not answer in time — scores unavailable.",
            )
        except ref.httpx.HTTPError as e:
            # Connection refused / DNS failure — service is down or
            # asleep (Render free-tier cold start), not a client error. Same
            # graceful-degrade as above instead of bubbling a raw 502.
            ref.logger.warning("decision engine unreachable for %s: %s", symbol_to_use, e)
            return ref._degraded_hold(
                symbol_to_use,
                f"decision engine unreachable: {e}",
                f"{symbol_to_use}: decision engine is unreachable right now "
                f"(cold start or outage) — showing a neutral HOLD until it recovers.",
                "Decision engine unreachable",
                "Could not reach decision-prediction-service — scores unavailable.",
            )

    @data_feed_structure_refresh_status_router.get("/data-feed/structure-refresh/status")
    @data_feed_structure_refresh_status_router.get("/api/data-feed/structure-refresh/status")
    def data_feed_structure_refresh_status():
        """Status of the post-close structure refresh job (+ lifetime counters of this process)."""
        try:
            import structure_refresh as _sr
            _t = ref._STRUCTURE_CATCHUP_TASK
            _catchup = {"enabled": _sr.catchup_enabled(), "interval_s": _sr.catchup_interval_s(),
                        "loop_running": bool(_t is not None and not _t.done()), "market_phase": ref._market_session_phase_ist()}   # group378
            return {"ok": True, **_sr.get_job(), "catchup": _catchup}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200], "status": "idle"}

    @data_feed_structure_refresh_router.post("/data-feed/structure-refresh")
    @data_feed_structure_refresh_router.post("/api/data-feed/structure-refresh")
    async def data_feed_structure_refresh(force: bool = False, dry_run: bool = False, limit: ref.Optional[int] = None):
        """Re-read ONLY the daily market-structure block (technical /structure/{symbol}) for feed rows that have none or an old one, and replace
    their ms_* keys. Run it after the daily bar closes (QStash tick action "structure-refresh"). Returns at once; poll .../status.
    force=true also re-checks rows that look current; dry_run=true only reports the selection; limit caps the symbols."""
        return await ref.asyncio.to_thread(ref._start_structure_refresh, force, dry_run, limit)

    @data_feed_symbol_router.get("/data-feed/{symbol}")
    @data_feed_symbol_router.get("/api/data-feed/{symbol}")
    @data_feed_symbol_router.get("/api/feed/{symbol}")
    def data_feed_symbol(symbol: str):
        if symbol in ref._FEED_POST_ONLY_ACTIONS:
            raise ref.HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "POST"})
        fed = ref._feed_store().get_symbol(symbol)
        if not fed:
            return {"ok": False, "symbol": symbol.upper(), "detail": "No data feed entry"}
        return {"ok": True, "data": fed}

    return SimpleNamespace(
        stock_chart_context_router=stock_chart_context_router,
        stock_chart_context=stock_chart_context,
        get_stock_decision_router=get_stock_decision_router,
        get_stock_decision=get_stock_decision,
        data_feed_structure_refresh_status_router=data_feed_structure_refresh_status_router,
        data_feed_structure_refresh_status=data_feed_structure_refresh_status,
        data_feed_structure_refresh_router=data_feed_structure_refresh_router,
        data_feed_structure_refresh=data_feed_structure_refresh,
        data_feed_symbol_router=data_feed_symbol_router,
        data_feed_symbol=data_feed_symbol,
    )
