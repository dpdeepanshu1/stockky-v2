"""Route handlers moved out of main.py in E88 (MainRef batch M, after E55 and E76-E86). Bodies are unchanged except that every main.py name is read as `ref.<name>`
at call time (see gateway_support/__init__.py); names that are locals of a handler or of its nested functions stay as they were. `build_bound_routes_m(ref)` returns a namespace: one router per handler
(include each where the handler used to be) and the handler functions by name. No `from __future__ import annotations` here: signatures and decorators use main.py names, which must be evaluated
when the factory runs (as the original decorators did), not resolved later from this module's globals."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_m(ref):
    websocket_endpoint_router = APIRouter()

    async def add_cors_header(request, call_next):
        response = await call_next(request)
        # wildcard mode only: stamp the "*" headers (they also cover responses the CORS middleware does not see). With an allowlist the
        # CORSMiddleware above sets the headers for the allowed origins and this must NOT widen them.
        if ref._cors_policy.is_wildcard():
            response.headers.update(ref._cors_policy.WILDCARD_HEADERS)
        return response

    async def universal_exception_handler(request, exc):
        ref.logger.error(f"Unhandled exception: {exc}")
        return ref.JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error: {str(exc)}"},
            headers=ref._cors_policy.response_headers(request),
        )

    @websocket_endpoint_router.websocket("/ws")
    async def websocket_endpoint(websocket: ref.WebSocket):
        """Realtime: scan progress, live quotes, market, training.

    Client messages (JSON):
      {"action":"subscribe","channel":"scan:<id>"|"quote:TCS"|"market"|"jobs"|"all"}
      {"action":"subscribe_quotes","symbols":["TCS","INFY"]}
      {"action":"unsubscribe_quotes","symbols":["TCS"]}  # or omit symbols to clear
      {"action":"unsubscribe","channel":"..."}
      {"action":"ping"}
    Server:
      {"channel":"quote:TCS","type":"quote","price":...,"as_of":...}
      {"channel":"scan:...","type":"scan_status",...}
      {"channel":"jobs","type":"jobs_snapshot","data_feed":{...},"refill_additional":{...},
       "surprise_premarket":{...},"rate_limits":{"yfinance":{...},"analysis":{...}}}
    """
        # group 454: CORS does not cover websockets, so an allowlist is enforced here (a browser always sends Origin; non-browser clients may omit it)
        _ws_hdrs = getattr(websocket, "headers", None)
        _ws_origin = _ws_hdrs.get("origin") if _ws_hdrs is not None else None
        if _ws_origin and not ref._cors_policy.origin_allowed(_ws_origin):
            await websocket.close(code=1008)
            return
        await ref.ws_manager.connect(websocket)
        ref._ensure_quote_loop()
        ref._ensure_jobs_loop()
        try:
            await websocket.send_text(ref.json.dumps({
                "channel": "system",
                "type": "connected",
                "ts": ref.datetime.now(ref.IST).isoformat(),
                "features": ["scan", "quotes", "jobs", "ping"],
            }))
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = ref.json.loads(raw) if raw else {}
                except Exception:
                    msg = {}
                # A frame that isn't a JSON object, or has a non-string action /
                # channel, is ignored — it used to raise and drop the connection.
                if not isinstance(msg, dict):
                    msg = {}
                _act = msg.get("action")
                action = _act.lower() if isinstance(_act, str) else ""
                _chan = msg.get("channel")
                channel = _chan.strip() if isinstance(_chan, str) else ""
                # Quote channels are canonicalised ("quote:tcs.ns" -> "quote:TCS")
                # so the subscription, the watch set and the snapshot all agree.
                if channel.lower().startswith("quote:"):
                    _qsym = ref._hot_base(channel.split(":", 1)[1])
                    channel = f"quote:{_qsym}" if _qsym else ""

                if action == "ping":
                    await websocket.send_text(ref.json.dumps({"channel": "system", "type": "pong"}))

                elif action == "subscribe_quotes":
                    syms = ref._ws_symbol_list(msg.get("symbols"))
                    ref.ws_manager.watch_quotes(websocket, syms)
                    await websocket.send_text(ref.json.dumps({
                        "channel": "system",
                        "type": "quotes_subscribed",
                        "symbols": sorted(ref.ws_manager.quote_syms.get(id(websocket), set())),
                    }, default=str))
                    # Immediate snapshot
                    for sym in list(ref.ws_manager.quote_syms.get(id(websocket), set()))[:10]:
                        q = await ref.asyncio.to_thread(ref._resolve_quote_price, sym)
                        if q:
                            await websocket.send_text(ref.json.dumps({
                                "channel": f"quote:{sym}",
                                "type": "quote",
                                **q,
                            }, default=str))

                elif action == "unsubscribe_quotes":
                    syms = msg.get("symbols")
                    if syms is None:
                        ref.ws_manager.unwatch_quotes(websocket, None)
                    else:
                        ref.ws_manager.unwatch_quotes(websocket, ref._ws_symbol_list(syms))
                    await websocket.send_text(ref.json.dumps({
                        "channel": "system",
                        "type": "quotes_unsubscribed",
                    }))

                elif action == "subscribe" and channel:
                    ref.ws_manager.subscribe(websocket, channel)
                    if channel.startswith("quote:"):
                        sym = channel.split(":", 1)[1]
                        ref.ws_manager.watch_quotes(websocket, [sym])
                    await websocket.send_text(ref.json.dumps({
                        "channel": channel, "type": "subscribed"
                    }))
                    if channel.startswith("scan:"):
                        task_id = channel.split(":", 1)[1]
                        data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id) or {}
                        await websocket.send_text(ref.json.dumps({
                            "channel": channel,
                            "type": "scan_status",
                            "task_id": task_id,
                            "status": data.get("status"),
                            "processed": data.get("processed", 0),
                            "total": data.get("total", 0),
                            "elapsed": data.get("elapsed"),
                            "result": data.get("result") if data.get("status") == "done" else None,
                        }, default=str))
                    elif channel.startswith("quote:"):
                        sym = channel.split(":", 1)[1]
                        q = await ref.asyncio.to_thread(ref._resolve_quote_price, sym)
                        if q:
                            await websocket.send_text(ref.json.dumps({
                                "channel": channel, "type": "quote", **q
                            }, default=str))

                elif action == "unsubscribe" and channel:
                    ref.ws_manager.unsubscribe(websocket, channel)
                    if channel.startswith("quote:"):
                        ref.ws_manager.unwatch_quotes(websocket, [channel.split(":", 1)[1]])

                elif action == "poll_scan" and isinstance(msg.get("task_id"), str) and msg.get("task_id"):
                    task_id = msg["task_id"]
                    data = ref._redis_get(ref.SCAN_TASK_PREFIX + task_id) or {}
                    await websocket.send_text(ref.json.dumps({
                        "channel": f"scan:{task_id}",
                        "type": "scan_status",
                        "task_id": task_id,
                        "status": data.get("status"),
                        "processed": data.get("processed", 0),
                        "total": data.get("total", 0),
                        "elapsed": data.get("elapsed"),
                        "result": data.get("result") if data.get("status") == "done" else None,
                    }, default=str))
        except ref.WebSocketDisconnect:
            ref.ws_manager.disconnect(websocket)
        except Exception as e:
            ref.logger.warning("websocket closed: %s", e)
            ref.ws_manager.disconnect(websocket)

    return SimpleNamespace(
        add_cors_header=add_cors_header,
        universal_exception_handler=universal_exception_handler,
        websocket_endpoint_router=websocket_endpoint_router,
        websocket_endpoint=websocket_endpoint,
    )
