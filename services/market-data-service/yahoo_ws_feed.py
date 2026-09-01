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

_LIVE: Dict[str, dict] = {}
_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "connected": False,
    "started": False,
    "subscribed": [],
    "last_message_at": 0.0,
    "error": None,
    "reconnects": 0,
}
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


async def _async_feed_main(universe: List[str]) -> None:
    global _WS_CLIENT
    import yfinance as yf

    was_idle = False
    while True:
        # 2026-09-01 fix: this streaming connection used to stay open and
        # subscribed 24/7 with no market-hours awareness — the
        # trading-decision loop (auto_pilot.py in real-trade-service) was
        # already gated to market hours, but this background tick feed
        # was not. Idle (no open connection) outside the window instead of
        # holding a live socket to Yahoo's streamer all night.
        if not is_feed_window_ist():
            if _WS_CLIENT is not None:
                try:
                    close_fn = getattr(_WS_CLIENT, "close", None) or getattr(_WS_CLIENT, "disconnect", None)
                    if close_fn:
                        maybe_coro = close_fn()
                        if asyncio.iscoroutine(maybe_coro):
                            await maybe_coro
                except Exception as e:
                    logger.debug("yahoo_ws_feed: close on market-close failed (non-fatal): %s", e)
                _WS_CLIENT = None
                with _LOCK:
                    _STATE["connected"] = False
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
        try:
            ws = yf.AsyncWebSocket(verbose=False)
            _WS_CLIENT = ws
            ws_symbols = sorted({_to_ws_symbol(s) for s in universe if s})
            await ws.subscribe(ws_symbols)
            with _LOCK:
                _STATE["subscribed"] = ws_symbols
                _STATE["started"] = True
                _STATE["connected"] = True
                _STATE["error"] = None
            logger.info("yahoo_ws_feed: subscribed to %s symbols", len(ws_symbols))
            # listen() runs forever; internally reconnects on transient errors,
            # but a hard failure (auth/network down) still raises out of it —
            # that's what the outer try/except + backoff below is for.
            await ws.listen(_on_message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            with _LOCK:
                _STATE["connected"] = False
                _STATE["error"] = str(e)[:200]
                _STATE["reconnects"] += 1
            logger.warning("yahoo_ws_feed crashed, restarting in 5s: %s", e)
            await asyncio.sleep(5)


def _run_feed_thread(universe: List[str]) -> None:
    global _LOOP
    loop = asyncio.new_event_loop()
    _LOOP = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_feed_main(universe))
    except Exception as e:
        logger.error("yahoo_ws_feed thread died: %s", e)


def start_feed_background(universe: List[str]) -> None:
    """Call once at service startup. Safe to call again — no-ops if already running."""
    global _THREAD
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
    e.g. a newly-listed IPO or a symbol the scan universe picked up mid-day."""
    if _WS_CLIENT is None or _LOOP is None:
        return
    with _LOCK:
        already = set(_STATE.get("subscribed", []))
    new = sorted({_to_ws_symbol(s) for s in symbols if _to_ws_symbol(s) and _to_ws_symbol(s) not in already})
    if not new:
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_WS_CLIENT.subscribe(new), _LOOP)
        fut.result(timeout=10)
        with _LOCK:
            _STATE["subscribed"] = sorted(already | set(new))
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
            "error": _STATE.get("error"),
            "in_market_window": is_feed_window_ist(),
        }
