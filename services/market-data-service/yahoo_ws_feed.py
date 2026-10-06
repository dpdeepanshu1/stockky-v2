"""
Yahoo Finance live WebSocket feed — ONE persistent connection streaming
price ticks for the whole watched universe, instead of one REST request
per symbol (which is what was hitting yfinance/twelvedata/alphavantage/
polygon rate limits over and over in the logs).

yfinance (already a dependency here, >=0.2.40) ships an official client
for Yahoo's real-time streaming endpoint:
    wss://streamer.finance.yahoo.com/?version=2
This is a *different* Yahoo backend from the crumb-protected REST/download
endpoints (query1.finance.yahoo.com) that were rate-limiting — it's the
same push feed Yahoo's own website uses for its live ticker widget, so it
doesn't share that rate limit at all. Verified present in the exact pinned
version (yfinance==1.5.2): yfinance.live.AsyncWebSocket /
yfinance.live.WebSocket, backed by pricing_pb2 (protobuf) + a subscribe/
listen model — subscribe once to hundreds of symbols, then just receive
ticks with zero further requests.

Usage (called once at FastAPI startup):
    from yahoo_ws_feed import start_feed_background, get_live_quote
    start_feed_background(universe_symbols)

Then in the quote endpoint, check get_live_quote(symbol) BEFORE falling
back to the REST provider cascade — a hit means zero HTTP calls, zero
rate-limiter involvement, for that request.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from market_hours import is_feed_window_ist

logger = logging.getLogger("yahoo-ws-feed")

# How often an idling (outside market hours) connection rechecks whether
# the window has opened, and how long to sleep between disconnect checks
# while parked.
IDLE_RECHECK_S = 60.0

# Group 204: minimum pause before reconnecting after listen() RETURNS (as opposed to
# raising). Before this, a clean return fell straight back to the top of the loop, which
# built a second AsyncWebSocket and re-subscribed the whole universe with no delay and
# no log line explaining why, and the first socket was never closed.
MIN_RECONNECT_GAP_S = 5.0

_LIVE: Dict[str, dict] = {}
_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "connected": False,
    "started": False,
    "subscribed": [],
    "last_message_at": 0.0,
    "error": None,
    "reconnects": 0,
    "connects": 0,   # group 204: sockets opened since boot (1 = the single normal connection)
}
# Group 204: the symbols the feed SHOULD be subscribed to, as Yahoo ids (RELIANCE.NS). The
# connection loop used to subscribe the list start_feed_background() was first called with
# and nothing else, so a later universe (the 20 s refresh) was lost whenever the feed was
# idle or reconnected, and every reconnect shrank back to the boot list. Every connect now
# subscribes this set, and start_feed_background()/ensure_subscribed() only ever add to it.
_DESIRED: set = set()
_START_LOCK = threading.Lock()   # group 204: makes "is a feed thread alive? if not start one" atomic
_THREAD: Optional[threading.Thread] = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WS_CLIENT = None  # yfinance.live.AsyncWebSocket instance, set once the feed thread starts


def _to_ws_symbol(sym: str) -> str:
    s = (sym or "").upper().strip()
    if not s:
        return ""
    if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
        return s
    return f"{s}.NS"


def _from_ws_symbol(ws_id: str) -> str:
    return (ws_id or "").upper().replace(".NS", "").replace(".BO", "")


def _on_message(msg: dict) -> None:
    try:
        wsid = msg.get("id")
        if not wsid:
            return
        sym = _from_ws_symbol(wsid)
        price = msg.get("price")
        if price is None:
            return  # heartbeat/partial message with no tradable price yet
        quote = {
            "symbol": sym,
            "price": float(price),
            "cmp": float(price),
            "previous_close": msg.get("previous_close"),
            "day_change": msg.get("change"),
            "day_change_pct": msg.get("change_percent"),
            "day_high": msg.get("day_high"),
            "day_low": msg.get("day_low"),
            "open_price": msg.get("open_price"),
            "volume": msg.get("day_volume"),
            "market_hours": msg.get("market_hours"),
            "source": "yahoo_ws",
            "ts": time.time(),
        }
        with _LOCK:
            _LIVE[sym] = quote
            _STATE["last_message_at"] = quote["ts"]
            _STATE["connected"] = True
    except Exception as e:
        logger.debug("yahoo_ws on_message error: %s", e)


async def _close_client() -> None:
    """Close and forget the current AsyncWebSocket, if any. Never raises."""
    global _WS_CLIENT
    ws, _WS_CLIENT = _WS_CLIENT, None
    if ws is None:
        return
    try:
        close_fn = getattr(ws, "close", None) or getattr(ws, "disconnect", None)
        if close_fn:
            maybe_coro = close_fn()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
    except Exception as e:
        logger.debug("yahoo_ws_feed: close failed (non-fatal): %s", e)


async def _async_feed_main(universe: List[str]) -> None:
    global _WS_CLIENT
    import yfinance as yf

    with _LOCK:
        _DESIRED.update(w for w in (_to_ws_symbol(s) for s in universe if s) if w)
    was_idle = False
    why = "initial connect"
    while True:
        # 2026-09-01 fix: this streaming connection used to stay open and
        # subscribed 24/7 with no market-hours awareness — the
        # trading-decision loop (auto_pilot.py in real-trade-service) was
        # already gated to market hours, but this background tick feed
        # was not. Idle (no open connection) outside the window instead of
        # holding a live socket to Yahoo's streamer all night.
        if not is_feed_window_ist():
            if _WS_CLIENT is not None:
                await _close_client()
                with _LOCK:
                    _STATE["connected"] = False
                    _STATE["subscribed"] = []
            if not was_idle:
                logger.info(
                    "yahoo_ws_feed: outside market hours (IST) — idling, "
                    "rechecking every %.0fs", IDLE_RECHECK_S,
                )
                was_idle = True
            await asyncio.sleep(IDLE_RECHECK_S)
            continue
        if was_idle:
            logger.info("yahoo_ws_feed: market window open — resuming connection")
            was_idle = False
            why = "market window opened"
        ws = None
        try:
            # Group 204: never hold two sockets. A previous client that was not closed (listen()
            # returned, or a failure part-way through connecting) is closed before a new one opens.
            await _close_client()
            with _LOCK:
                ws_symbols = sorted(_DESIRED)
            if not ws_symbols:
                await asyncio.sleep(IDLE_RECHECK_S)
                continue
            ws = yf.AsyncWebSocket(verbose=False)
            await ws.subscribe(ws_symbols)
            # Publish the client only once the initial subscribe has finished, so ensure_subscribed()
            # cannot send the whole universe a second time onto a socket that is still being set up
            # (its "already subscribed" list is empty until this point).
            _WS_CLIENT = ws
            with _LOCK:
                _STATE["subscribed"] = ws_symbols
                _STATE["started"] = True
                _STATE["connected"] = True
                _STATE["error"] = None
                _STATE["connects"] += 1
                n_connect = _STATE["connects"]
                extra = sorted(_DESIRED - set(ws_symbols))
            logger.info("yahoo_ws_feed: subscribed to %s symbols (connection #%d, %s)",
                        len(ws_symbols), n_connect, why)
            if extra:
                # symbols added (refresh / ensure_subscribed) while the initial subscribe was in flight
                await ws.subscribe(extra)
                with _LOCK:
                    _STATE["subscribed"] = sorted(set(ws_symbols) | set(extra))
            # listen() runs forever; internally reconnects on transient errors,
            # but a hard failure (auth/network down) still raises out of it —
            # that's what the outer try/except + backoff below is for.
            await ws.listen(_on_message)
            # listen() returned without raising: the connection ended. Say so (the next
            # "subscribed to" line is then explained) and pace the reconnect.
            with _LOCK:
                _STATE["connected"] = False
            logger.warning("yahoo_ws_feed: listen() returned (connection closed) — reconnecting in %.0fs",
                           MIN_RECONNECT_GAP_S)
            why = "after listen() returned"
            await asyncio.sleep(MIN_RECONNECT_GAP_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if ws is not None and _WS_CLIENT is None:
                _WS_CLIENT = ws          # so _close_client() below closes a half-set-up socket too
            await _close_client()
            with _LOCK:
                _STATE["connected"] = False
                _STATE["error"] = str(e)[:200]
                _STATE["reconnects"] += 1
            logger.warning("yahoo_ws_feed crashed, restarting in 5s: %s", e)
            why = "after a crash"
            await asyncio.sleep(5)


def _run_feed_thread(universe: List[str]) -> None:
    global _LOOP, _WS_CLIENT
    loop = asyncio.new_event_loop()
    _LOOP = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_feed_main(universe))
    except Exception as e:
        logger.error("yahoo_ws_feed thread died: %s", e)
    finally:
        # a dead thread must not leave a stale client for ensure_subscribed() to send on
        _WS_CLIENT = None
        with _LOCK:
            _STATE["connected"] = False


def start_feed_background(universe: List[str]) -> None:
    """Call once at service startup. Safe to call again — no-ops if already running.

    Group 204: a repeat call still ADDS its symbols to the set the feed subscribes (a running
    but idle or reconnecting feed would otherwise keep only the first call's list), and the
    "is it already running" check and the thread start are now one atomic step, so two callers
    (the boot fallback in a worker thread and the universe refresh on the event loop) can no
    longer each start a thread and open two Yahoo sockets."""
    global _THREAD
    ws_syms = {w for w in (_to_ws_symbol(s) for s in (universe or []) if s) if w}
    with _START_LOCK:
        if ws_syms:
            with _LOCK:
                _DESIRED.update(ws_syms)
        if _THREAD and _THREAD.is_alive():
            return
        if not universe:
            logger.warning("yahoo_ws_feed: empty universe, not starting")
            return
        _THREAD = threading.Thread(
            target=_run_feed_thread, args=(universe,), daemon=True, name="yahoo-ws-feed"
        )
        _THREAD.start()
    logger.info("yahoo_ws_feed: background thread started for %s symbols", len(universe))


def ensure_subscribed(symbols: List[str]) -> None:
    """Add symbols to the live subscription without restarting the connection —
    e.g. a newly-listed IPO or a symbol the scan universe picked up mid-day.

    Group 204: the symbols are always recorded in the desired set, so when no connection is
    up (idle outside market hours, mid-reconnect) the next connection subscribes them."""
    wanted = {w for w in (_to_ws_symbol(s) for s in (symbols or []) if s) if w}
    if wanted:
        with _LOCK:
            _DESIRED.update(wanted)
    ws, loop = _WS_CLIENT, _LOOP
    if ws is None or loop is None:
        return
    with _LOCK:
        already = set(_STATE.get("subscribed", []))
    new = sorted(wanted - already)
    if not new:
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(ws.subscribe(new), loop)
        fut.result(timeout=10)
        with _LOCK:
            _STATE["subscribed"] = sorted(set(_STATE.get("subscribed", [])) | set(new))
    except Exception as e:
        logger.debug("yahoo_ws ensure_subscribed failed: %s", e)


def get_live_quote(symbol: str, max_age_sec: float = 20.0) -> Optional[dict]:
    """Instant, zero-HTTP quote lookup. Returns None (not a stale value) if
    we've never gotten a tick for this symbol, or the last tick is older
    than max_age_sec — the caller should fall back to REST in that case
    (market closed, illiquid/thinly-traded symbol, or just-subscribed and
    no tick has arrived yet)."""
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
    with _LOCK:
        q = _LIVE.get(sym)
    if not q:
        return None
    if time.time() - q["ts"] > max_age_sec:
        return None
    return dict(q)


def get_live_quotes_bulk(symbols: List[str], max_age_sec: float = 20.0) -> Dict[str, dict]:
    out = {}
    for s in symbols:
        q = get_live_quote(s, max_age_sec=max_age_sec)
        if q:
            out[q["symbol"]] = q
    return out


def feed_status() -> dict:
    with _LOCK:
        last = _STATE["last_message_at"]
        return {
            "connected": _STATE["connected"],
            "started": _STATE["started"],
            "subscribed_count": len(_STATE.get("subscribed", [])),
            "live_symbols_count": len(_LIVE),
            "last_message_age_sec": round(time.time() - last, 1) if last else None,
            "reconnects": _STATE["reconnects"],
            "connects": _STATE["connects"],
            "desired_count": len(_DESIRED),
            "error": _STATE.get("error"),
            "in_market_window": is_feed_window_ist(),
        }
