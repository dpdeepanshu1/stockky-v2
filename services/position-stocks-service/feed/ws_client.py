"""
feed/ws_client.py — Angel One smartWebSocketV2 TRUE persistent WebSocket client.

This is the key new infrastructure piece (Step 2 per tracking doc §5).
Replaces the fake "ws" polling in market-data-service/angelone_ws_feed.py
(which is confirmed via code inspection to be a plain REST poll every 3s
dressed up with a websocket filename — no actual WS frames).

SmartWebSocketV2 binary frame format (from AngelOne SmartAPI docs):
  Subscription mode 1 (LTP only) — 51-byte frames
  Subscription mode 2 (quote)    — 195-byte frames
  Subscription mode 3 (snap quote) — 501-byte frames

We use mode 1 (LTP) — we only need last traded price + volume for the
rolling-window screener. That gives us the fastest parse path and lowest
bandwidth.

Binary frame layout (mode 1, all little-endian):
  Byte 0:      subscription_type (1=LTP, 2=Quote, 3=SnapQuote)
  Byte 1:      exchange_type     (1=NSE_CM, 2=NSE_FO, ...)
  Bytes 2-27:  token             (char[26], null-padded)
  Bytes 28-35: sequence_number   (int64)
  Bytes 36-43: exchange_feed_time (int64, unix seconds)
  Bytes 44-51: LTP               (int64, price * 100)
  Bytes 52-59: LTT               (int64, unix seconds) — mode 1 only has these
  (additional fields in mode 2/3 not used here)

Subscribe message (JSON):
  {
    "correlationID": "ps1",
    "action": 1,       # 1=subscribe, 0=unsubscribe
    "params": {
      "mode": 1,       # 1=LTP
      "tokenList": [{"exchangeType": 1, "tokens": ["3045", "1594", ...]}]
    }
  }

Heartbeat: send ping frame every ANGELONE_WS_HEARTBEAT_INTERVAL_S (25s default).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import config
from feed.angelone_session import get_session
from feed.scrip_master import get_all_nse_eq

logger = logging.getLogger("position-stocks-ws-client")

# ── Tick storage ────────────────────────────────────────────────────────────
# Ring buffer of (timestamp_float, ltp_float) per symbol, max 3600 entries
# (1 tick/sec × 1h). O(1) append, O(window) scan for pct-change.
_MAX_TICKS = 3_600
_tick_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_TICKS))
_last_volume:  Dict[str, int]   = {}   # latest volume per symbol from feed

# Registered on-tick callbacks — screening engine registers here
_on_tick_callbacks: list[Callable] = []


def register_on_tick(cb: Callable) -> None:
    """Register a callback(symbol, ltp, volume, ts) called on every tick."""
    _on_tick_callbacks.append(cb)


def get_tick_buffer(symbol: str) -> deque:
    """Returns the deque of (ts, ltp) tuples for the symbol (read-only view)."""
    return _tick_buffers[symbol]


def get_last_ltp(symbol: str) -> Optional[float]:
    buf = _tick_buffers.get(symbol)
    if not buf:
        return None
    return buf[-1][1]


# ── Binary frame parser (mode 1 LTP) ────────────────────────────────────────
def _parse_ltp_frame(data: bytes) -> Optional[tuple]:
    """Returns (token_str, ltp_float, ts_float) or None on parse error."""
    if len(data) < 52:
        return None
    try:
        # sub_type = data[0]    # feed mode — always 1 (LTP) since that's all we subscribe to
        # exch_type = data[1]   # not needed for routing by symbol
        token_raw  = data[2:28].rstrip(b"\x00").decode("ascii", errors="ignore").strip()
        # sequence  = struct.unpack_from("<q", data, 28)[0]  # not needed
        # feed_time = struct.unpack_from("<q", data, 36)[0]  # not needed
        ltp_raw    = struct.unpack_from("<q", data, 44)[0]   # price * 100
        ltp        = ltp_raw / 100.0
        ts         = time.time()
        if ltp <= 0:
            return None
        return token_raw, ltp, ts
    except Exception as e:
        logger.debug("frame parse error: %s (len=%d)", e, len(data))
        return None


# ── Token→symbol reverse map (built once at subscribe time) ─────────────────
_token_to_symbol: Dict[str, str] = {}


def _build_reverse_map(symbol_token_map: Dict[str, str]) -> None:
    global _token_to_symbol
    _token_to_symbol = {v: k for k, v in symbol_token_map.items()}


# ── Subscribe message builder ────────────────────────────────────────────────
def _build_subscribe_msg(tokens: list[str], action: int = 1) -> str:
    """action=1 subscribe, action=0 unsubscribe. NSE_CM exchange_type=1."""
    return json.dumps({
        "correlationID": "ps1",
        "action": action,
        "params": {
            "mode": 1,  # LTP only
            "tokenList": [{"exchangeType": 1, "tokens": tokens}],
        },
    })


# ── Main WS loop ─────────────────────────────────────────────────────────────
_ws_task: Optional[asyncio.Task] = None
_running = False
_subscribed_tokens: list[str] = []


async def _ws_loop() -> None:
    """Persistent WS loop with exponential-backoff reconnect."""
    global _subscribed_tokens
    session = get_session()
    backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_S
    max_backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_MAX_S
    heartbeat_interval = config.ANGELONE_WS_HEARTBEAT_INTERVAL_S

    while _running:
        try:
            await session.ensure_session()
            if not session.token or not session.feed_token or not session.client_id:
                logger.error("position-stocks WS: AngelOne session not ready — retrying in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            # Build the subscription list
            symbol_token_map = get_all_nse_eq()
            _build_reverse_map(symbol_token_map)
            _subscribed_tokens = list(symbol_token_map.values())
            logger.info("position-stocks WS: subscribing to %d NSE-EQ tokens", len(_subscribed_tokens))

            ws_url = (
                f"{config.ANGELONE_WS_URL}"
                f"?clientCode={session.client_id}"
                f"&feedToken={session.feed_token}"
                f"&apiKey={session.api_key}"
            )

            async with websockets.connect(
                ws_url,
                ping_interval=None,   # we handle heartbeats ourselves
                ping_timeout=None,
                close_timeout=5,
                max_size=2**20,
            ) as ws:
                logger.info("position-stocks WS: connected")
                backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_S  # reset on success

                # Subscribe in chunks of 1000 (AngelOne cap per message)
                chunk_size = config.ANGELONE_WS_MAX_SYMBOLS_PER_CONNECTION
                for i in range(0, len(_subscribed_tokens), chunk_size):
                    chunk = _subscribed_tokens[i:i + chunk_size]
                    await ws.send(_build_subscribe_msg(chunk))
                    await asyncio.sleep(0.1)

                last_heartbeat = time.time()

                async for message in ws:
                    if not _running:
                        break

                    # Heartbeat (ping frame)
                    now = time.time()
                    if now - last_heartbeat >= heartbeat_interval:
                        try:
                            await ws.ping()
                            last_heartbeat = now
                        except Exception:
                            pass

                    # Binary tick frame
                    if isinstance(message, bytes):
                        parsed = _parse_ltp_frame(message)
                        if parsed:
                            token_str, ltp, ts = parsed
                            symbol = _token_to_symbol.get(token_str)
                            if symbol:
                                _tick_buffers[symbol].append((ts, ltp))
                                for cb in _on_tick_callbacks:
                                    try:
                                        cb(symbol, ltp, _last_volume.get(symbol, 0), ts)
                                    except Exception as e:
                                        logger.debug("on_tick callback error: %s", e)
                    # Text frames (status/error messages from server)
                    elif isinstance(message, str):
                        logger.debug("position-stocks WS text frame: %s", message[:200])

        except ConnectionClosed as e:
            logger.warning("position-stocks WS: connection closed (%s) — reconnecting in %ss", e, backoff)
        except Exception as e:
            logger.error("position-stocks WS: error (%s) — reconnecting in %ss", e, backoff)

        if _running:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    logger.info("position-stocks WS: loop stopped.")


async def start() -> None:
    global _ws_task, _running
    if _running:
        return
    _running = True
    _ws_task = asyncio.create_task(_ws_loop(), name="position-stocks-ws")
    logger.info("position-stocks WS: task started")


async def stop() -> None:
    global _running
    _running = False
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    logger.info("position-stocks WS: stopped")


def ws_status() -> dict:
    return {
        "running": _running,
        "subscribed_symbols": len(_token_to_symbol),
        "task_done": _ws_task.done() if _ws_task else True,
    }
