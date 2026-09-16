# Session 55 — api-gateway event-loop-blocking-I/O audit (continued)

Continuing the audit begun in prior sessions (ws_client.py, angelone_session.py,
circuit_breaker.py, rate_limit_monitor.py, batch_worker.py) for the same bug
class: blocking I/O called synchronously, inline, directly on the asyncio
event loop from `async def` route handlers or background loops — which stalls
every other request this process is serving for the duration of the call.

Swept `main.py`'s 106 `async def` functions for calls into the known-blocking
modules (kv_cache.py, data_feed.py, ipo_scanner.py, hotpicks_store.py,
surprise_premarket.py, surprise_scanner.py — all pure-sync — plus direct
`httpx.get/post` calls) that were not already wrapped in `asyncio.to_thread`.
The large majority of hits were single blocking calls inside one-shot,
manually-triggered admin/notify endpoints (acceptable — one request pays the
latency, nothing else queues behind it for long). Three hits were genuinely
in the same severity class as the prior sessions' fixes and were fixed here:

1. **`POST /api/feed/batch` (`data_feed_update_batch`)** — looped over an
   *uncapped* `feeds` dict calling `save_stock_feed()` (a blocking
   Neon/durable-KV write) once per symbol, inline, on the event loop. Unlike
   the sibling `/api/feed/update-batch` route, this one has no
   `DATA_FEED_UPDATE_BATCH_MAX` cap, so a large POST body could serialize
   dozens+ blocking DB writes ahead of every other request. Fixed: validate/
   filter inline (cheap), then run all writes concurrently via
   `asyncio.gather(asyncio.to_thread(save_stock_feed, ...))`.

2. **`_quote_broadcast_loop()`** — the permanent WS quote-push background
   loop called `evaluate_price_alerts()` (blocking `kv_get`, and on any
   trigger a blocking `kv_set` for the cooldown) and, per triggered alert,
   `_wake_notification_service()` (blocking `httpx.get`, up to 5s) and
   `httpx.post(...)` (up to 8s) — all inline, unconditionally, every
   8–20s for the life of the process. Fixed: all three wrapped in
   `asyncio.to_thread`.

3. **`run_scan_parallel()`** — called `_send_scan_notification()` inline at
   the end of every full market scan. That helper itself does a blocking
   `_wake_notification_service()` (~5s) plus one or two blocking
   `httpx.post` calls (15s + 20s) — up to ~40s of the event loop being
   stalled right after each scan completes, affecting every concurrent
   request. Fixed: wrapped the whole call in `asyncio.to_thread`.

Verified: `py_compile` clean; `pyflakes` shows only pre-existing, unrelated
warnings (none on the touched lines); diffed the full extracted zip against
the session54 upload to confirm only `main.py` changed.

Confirmed clean (pure sync helper modules, no `async def`, so the risk is
entirely at call sites, which is what was swept above): `kv_cache.py`
(remaining settings/watchlist/notification-config helpers), `hotpicks_store.py`,
`surprise_premarket.py`.

Lower-priority, left as-is (single blocking `httpx.post`/`.get` call inside a
manually-triggered, one-shot endpoint — not a loop, not a recurring
background task): `api_surprise_notify_top_picks`, `api_ipo_notify_top_picks`,
`api_evaluate_price_alerts`, `api_hotpicks_notify_top_picks`, and
`hard_reset_database`'s ~9-key `kv_delete` loop (rare manual admin action,
small fixed key count). Flagging here rather than guessing whether the user
wants event-loop purity on rarely-hit admin endpoints too — say the word and
I'll sweep those next.

Still ahead / unaudited in api-gateway: `ipo_scanner.py`, `data_feed.py`, and
`surprise_scanner.py`'s internals beyond this call-site sweep (their own
logic, not just the blocking-I/O pattern); `main.py`'s non-blocking-I/O logic
generally (this pass was scoped specifically to the blocking-I/O bug class);
decision-prediction-service's `evaluate.py`/`trades.py`/`models.py`/`app.py`;
the ~20,400-line frontend.
