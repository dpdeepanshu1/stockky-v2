"""
feed/ws_client.py — Angel One smartWebSocketV2 TRUE persistent WebSocket client.

This is the key new infrastructure piece (Step 2 per tracking doc §5).
Replaces the fake "ws" polling in market-data-service/angelone_ws_feed.py
(which is confirmed via code inspection to be a plain REST poll every 3s
dressed up with a websocket filename — no actual WS frames).

SmartWebSocketV2 binary frame format (from AngelOne SmartAPI docs):
  Subscription mode 1 (LTP)        —  51-byte frames
  Subscription mode 2 (Quote)      — 123-byte frames (OHLC + cumulative
                                      day volume — still NO bid/ask)
  Subscription mode 3 (SnapQuote)  — 379-byte frames (adds 52w hi/lo,
                                      circuit limits, and 5-level best
                                      bid/ask depth)

  BUG FIX (this session — root-caused the "connected forever, zero ticks
  ever parsed, last_tick_at always null" symptom): the common-header
  field offsets below were previously off by one byte (token sliced as
  26 bytes, `data[2:28]`, instead of the correct 25, `data[2:27]`),
  which cascaded into every field after it — LTP was read from offset
  44 instead of 43. Fixed; every frame mode below shares this same
  0-50 header layout, so the fix applies to all of them.

  ── Mode upgrade (2026-09-18 — user audit finding) ──────────────────
  Previously subscribed at mode 1 (LTP-only). Two consequences, both
  now fixed by moving to mode 3 (SnapQuote):
   1. config.MAX_SPREAD_PCT was defined and documented (STATUS.md,
      config.py) as one of the hard risk gates, but was NEVER actually
      enforced anywhere in the code — mode 1 carries no bid/ask, so
      there was no spread to check. Every entry was a MARKET order
      into a stock with zero liquidity/spread screening.
   2. screening/engine.py's MIN_AVG_VOLUME floor was measured via
      _volume_accum, which is literally `len(tick_timestamps_in_window)`
      — a tick-COUNT proxy, not real traded volume. A thinly-traded
      stock with an unstable, jumpy price generates lots of ticks and
      would pass this "liquidity" floor despite being the opposite of
      liquid.
  Mode 3 gives real cumulative day volume AND best-5 bid/ask in every
  tick, at the cost of a bigger frame (379 vs 51 bytes) — still trivial
  bandwidth for ~2,000 NSE-EQ symbols. Mode 2 (Quote) was considered
  first since it's cheaper, but per AngelOne's own official reference
  parser (angel-one/smartapi-python, SmartApi/smartWebSocketV2.py,
  `_parse_binary_data`), Quote mode does NOT carry depth — only
  SnapQuote does — so mode 3 is the minimum mode that can feed a real
  MAX_SPREAD_PCT gate.

Binary frame layout (all little-endian). Common header, same for every
mode (bytes 0-50, 51 bytes):
  Byte 0:      subscription_mode (1=LTP, 2=Quote, 3=SnapQuote)
  Byte 1:      exchange_type     (1=NSE_CM, 2=NSE_FO, ...)
  Bytes 2-26:  token             (char[25], null-padded)
  Bytes 27-34: sequence_number   (int64)
  Bytes 35-42: exchange_feed_time (int64, unix seconds)
  Bytes 43-50: LTP               (int64, price * 100)

Mode 2 (Quote) / Mode 3 (SnapQuote) add, bytes 51-122 (all int64 unless
noted; prices are paise, i.e. price * 100):
  51-58:   last_traded_quantity
  59-66:   average_traded_price
  67-74:   volume_trade_for_the_day   <- the REAL cumulative volume
  75-82:   total_buy_quantity   (float64/"d", NOT int64 — per reference)
  83-90:   total_sell_quantity  (float64/"d", NOT int64 — per reference)
  91-98:   open_price_of_the_day
  99-106:  high_price_of_the_day
  107-114: low_price_of_the_day
  115-122: closed_price

Mode 3 (SnapQuote) additionally adds, bytes 123-378:
  123-130: last_traded_timestamp
  131-138: open_interest
  139-146: open_interest_change_percentage
  147-346: best-5 buy/sell depth — 10 packets of 20 bytes each:
             bytes 0-1:   flag (H, uint16) — 0 or 1, see swap note below
             bytes 2-9:   quantity (q, int64)
             bytes 10-17: price    (q, int64, paise)
             bytes 18-19: num_of_orders (H, uint16)
  347-354: upper_circuit_limit
  355-362: lower_circuit_limit
  363-370: 52_week_high_price
  371-378: 52_week_low_price

  ⚠ DEPTH FLAG/LABEL SWAP: AngelOne's own official reference parser
  (angel-one/smartapi-python's `_parse_binary_data`) collects flag==0
  packets into a local `best_5_buy_data` bucket, flag!=0 into
  `best_5_sell_data` — then, when building the final returned dict,
  swaps the two: `parsed_data["best_5_buy_data"] = <the flag!=0
  bucket>` and `parsed_data["best_5_sell_data"] = <the flag==0
  bucket>`. This looks backwards on first read but it's AngelOne's own
  documented behavior, not a transcription error here — replicated
  exactly in `_parse_best5` below (flag==0 -> ASK side, flag!=0 -> BID
  side, matching their final exposed labels) so this client's notion
  of "best bid"/"best ask" matches what every other AngelOne SmartAPI
  integration actually receives. If this is ever confirmed wrong
  against a live captured frame, flip `_ASK_FLAG`/`_BID_FLAG` below —
  don't reorder the parsing itself.

Subscribe message (JSON):
  {
    "correlationID": "ps1",
    "action": 1,       # 1=subscribe, 0=unsubscribe
    "params": {
      "mode": 3,       # 3=SnapQuote (real volume + best bid/ask depth)
      "tokenList": [{"exchangeType": 1, "tokens": ["3045", "1594", ...]}]
    }
  }

Heartbeat: send ping frame every ANGELONE_WS_HEARTBEAT_INTERVAL_S (25s default).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import config
from feed.angelone_session import get_session
from feed.scrip_master import get_all_nse_eq

logger = logging.getLogger("position-stocks-ws-client")

# ── Feed-secret-in-URL hygiene (session99) ────────────────────────────────────
# ws_url below carries clientCode / feedToken / apiKey as query params (AngelOne
# requires them in the URL — no header-auth alternative for this feed). Nothing
# in this module logs ws_url directly, but the `websockets` library itself logs
# the full request line (path + query string) via its "websockets.client"
# logger at DEBUG — see websockets/client.py's `self.logger.debug("> GET %s
# HTTP/1.1", request.path)`. This service's LOG_LEVEL defaults to INFO (so the
# line is normally suppressed), but LOG_LEVEL=DEBUG is a supported, real
# config knob (config.py), and turning it on for any other reason would leak
# the feed token and API key to the log. Same class of leak as session98's
# Telegram-bot-token fix and session99's provider-API-key fixes; closing it the
# same way rather than leaving it conditional on nobody ever setting LOG_LEVEL.
_SECRET_SUBS = (
    (re.compile(r"(?i)([?&]feedToken=)[^&\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)([?&]apiKey=)[^&\s'\"]+"), r"\1***"),
)


def _redact_secrets(text) -> str:
    """str(text) with the AngelOne feed token / API key above replaced by
    ``***``. Never raises."""
    try:
        out = str(text)
        for pattern, repl in _SECRET_SUBS:
            out = pattern.sub(repl, out)
        return out
    except Exception:
        return "<text withheld: redaction failed>"


class _SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _redact_secrets(msg)
            if redacted != msg:
                record.msg, record.args = redacted, ()
        except Exception:
            pass   # a logging filter must never break logging
        return True


def _install_ws_secret_filter() -> None:
    """Idempotent — safe on module reload. Covers "websockets.client" (the
    library's own request-line DEBUG log) and this module's own logger."""
    for name in ("websockets.client", "position-stocks-ws-client"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _SecretRedactingFilter) for f in target.filters):
            target.addFilter(_SecretRedactingFilter())


_install_ws_secret_filter()

# ── Tick storage ────────────────────────────────────────────────────────────
# AUDIT FIX (this session — "check for any other remaining issue,
# consider very high/frequent stock price change"): this buffer used to
# be a hard COUNT cap, `deque(maxlen=3_600)`, on the explicit (and, per
# this module's own then-docstring, deliberately stated) assumption of
# "1 tick/sec × 1h". screening/engine.py's `_rolling_pct_change()` scans
# this buffer backward looking for a tick at least `window_minutes` old
# to use as the reference price for its 1m/5m/15m/60m %-change windows —
# it silently returns None (that window simply produces no candidate)
# whenever it can't find one old enough. A real, liquid, actively-moving
# NSE stock — precisely the kind this scalp strategy is built to catch —
# can push WS ticks far faster than 1/sec during a volatile burst (every
# trade generates a tick in mode-1 LTP), which drains the buffer's actual
# TIME depth below 60 (or even 15) minutes well before it fills on tick
# COUNT. Net effect: on exactly the busiest, most volatile stretches for
# exactly the stocks this strategy targets, the 60m window (and, in a
# severe burst, 15m too) could silently stop producing candidates —
# nothing errors, nothing logs, the window just quietly goes dark.
#
# Fixed by making the buffer genuinely TIME-bounded instead of tick-rate-
# assumption-bounded: every append now also prunes anything older than
# _MAX_BUFFER_AGE_S (65 min — a small safety margin over the longest
# screening window, 60m, so `_rolling_pct_change`'s backward scan always
# has room to find a boundary tick right up to that window's edge). This
# is the same time-window-pruning pattern screening/engine.py's own
# `_update_volume()` already uses for its tick-timestamp list, just
# applied here too. `_MAX_TICKS` is kept, raised generously, purely as a
# memory-safety backstop against unbounded growth in a pathological case
# (e.g. corrupted/non-monotonic timestamps defeating the age prune) — in
# normal operation the time prune keeps the buffer far below it.
_MAX_TICKS = 50_000
_MAX_BUFFER_AGE_S = 65 * 60  # slightly over the longest screening window (60m)
_tick_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_TICKS))
_last_volume:  Dict[str, int]   = {}   # latest volume per symbol from feed

# BUG FIX (2026-09-17): _tick_buffers' deques are written by _ws_loop below,
# which runs as an asyncio task on the event-loop thread, and read by
# screening/engine.py's scan() and orders/adaptive.py's helpers, which run
# in FastAPI's worker thread pool (run_in_threadpool) — a genuinely
# different OS thread. Iterating a deque on one thread while another thread
# appends/popleft's it raises "RuntimeError: deque mutated during
# iteration" (seen live: position-stocks-service's /candidates endpoint,
# _momentum_consistency's list comprehension over buf). A single
# deque.append() is safe on its own, but a multi-element read (iterate,
# reversed(), or even `list(buf)`) is not atomic against a concurrent
# append — the copy itself can be interrupted mid-iteration by the writer.
# Fix: a per-symbol lock, held briefly (no `await` inside it, on either
# side) by both the writer (_ws_loop, just below) and the reader
# (get_tick_buffer) so a snapshot is always taken between ticks, never
# during one.
_buffer_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

# ── Best bid/ask storage (2026-09-18 mode-3 upgrade) ────────────────────────
# Plain dict item assignment, same pattern _last_volume already used below —
# a single dict[key]=value write is atomic under the GIL, so no lock needed
# for this (unlike _tick_buffers, where the risk was a multi-step *read*
# racing a writer mid-iteration — see the BUG FIX comment above
# _buffer_locks). best_bid/best_ask are None until at least one mode-3 frame
# with a non-empty depth book has been parsed for that symbol.
_last_quote: Dict[str, tuple] = {}   # symbol -> (best_bid, best_ask, ts)

# Registered on-tick callbacks — screening engine registers here
_on_tick_callbacks: list[Callable] = []


def register_on_tick(cb: Callable) -> None:
    """Register a callback(symbol, ltp, volume, ts) called on every tick.
    `volume` is now the real cumulative day volume from the mode-3 feed
    (see the 2026-09-18 mode-upgrade note in this module's docstring) —
    previously always 0, since _last_volume was declared but never
    actually written anywhere before that fix."""
    _on_tick_callbacks.append(cb)


def get_best_bid_ask(symbol: str) -> Optional[tuple]:
    """Returns (best_bid, best_ask) for symbol from the most recent mode-3
    tick, or None if no depth has been seen yet for it (e.g. right after
    (re)subscribe, or a symbol with a genuinely empty order book). Callers
    (the MAX_SPREAD_PCT gate in screening/engine.py) must treat None as
    "unknown", not "zero spread" — see that gate's fail-open comment."""
    q = _last_quote.get(symbol)
    if not q:
        return None
    bid, ask, _ts = q
    return (bid, ask)


def get_tick_buffer(symbol: str) -> list:
    """Returns an immutable snapshot list of (ts, ltp) tuples for the
    symbol. Previously returned the live deque directly (see BUG FIX above
    for why that raced with the WS ingestion loop) — now returns a plain
    list copied under the symbol's lock, safe to iterate/index/reverse
    from any thread with no risk of a concurrent-mutation crash."""
    with _buffer_locks[symbol]:
        return list(_tick_buffers[symbol])


def get_last_ltp(symbol: str) -> Optional[float]:
    buf = _tick_buffers.get(symbol)
    if not buf:
        return None
    return buf[-1][1]


def get_last_volume(symbol: str) -> int:
    """Real cumulative day volume (shares) from the mode-3 feed, 0 if no
    tick has arrived for this symbol yet. See the mode-upgrade docstring
    note at the top of this module — this used to always be 0."""
    return _last_volume.get(symbol, 0)


# ── Binary frame parser (mode 3 SnapQuote) ──────────────────────────────────
# Per-depth-packet flag values — see the ⚠ DEPTH FLAG/LABEL SWAP note in this
# module's docstring for why ASK is flag==0, not BID.
_ASK_FLAG = 0
_BID_FLAG = 1


def _parse_best5(data: bytes) -> tuple:
    """data is the 200-byte depth block (frame bytes 147:347) — 10 packets
    of 20 bytes each. Returns (best_bid, best_ask) as floats, or (None,
    None) if either side's top-of-book entry is missing/malformed. Only
    reads the FIRST packet found on each side (index 0) — AngelOne returns
    the 5 levels best-to-worst, so index 0 is top-of-book; we don't need
    the other 4 levels for a spread check."""
    best_bid = best_ask = None
    for i in range(0, len(data) - 19, 20):
        packet = data[i:i + 20]
        try:
            flag  = struct.unpack_from("<H", packet, 0)[0]
            price = struct.unpack_from("<q", packet, 10)[0] / 100.0
        except Exception:
            continue
        if price <= 0:
            continue
        if flag == _ASK_FLAG and best_ask is None:
            best_ask = price
        elif flag == _BID_FLAG and best_bid is None:
            best_bid = price
    return best_bid, best_ask


def _parse_frame(data: bytes) -> Optional[tuple]:
    """Returns (token_str, ltp, ts, volume, best_bid, best_ask) or None on
    parse error / undersized frame. `volume` is real cumulative day
    volume (0 if the frame is too short to carry it — e.g. a stray mode-1
    frame). `best_bid`/`best_ask` are None unless this is a full mode-3
    SnapQuote frame (>= 347 bytes, enough to include the depth block)."""
    if len(data) < 51:
        return None
    try:
        # sub_type = data[0]    # feed mode
        # exch_type = data[1]   # not needed for routing by symbol
        token_raw = data[2:27].rstrip(b"\x00").decode("ascii", errors="ignore").strip()
        # sequence  = struct.unpack_from("<q", data, 27)[0]  # not needed
        # feed_time = struct.unpack_from("<q", data, 35)[0]  # not needed
        ltp_raw = struct.unpack_from("<q", data, 43)[0]   # price * 100
        ltp = ltp_raw / 100.0
        ts = time.time()
        if ltp <= 0:
            return None

        volume = 0
        if len(data) >= 75:
            # bytes 67:75 — volume_trade_for_the_day (see docstring layout)
            volume = struct.unpack_from("<q", data, 67)[0]
            if volume < 0:
                volume = 0

        best_bid = best_ask = None
        if len(data) >= 347:
            best_bid, best_ask = _parse_best5(data[147:347])

        return token_raw, ltp, ts, volume, best_bid, best_ask
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
    """action=1 subscribe, action=0 unsubscribe. NSE_CM exchange_type=1.
    mode=3 (SnapQuote) — see the mode-upgrade note in this module's
    docstring for why LTP/Quote aren't enough (no real volume / no
    bid-ask depth respectively)."""
    return json.dumps({
        "correlationID": "ps1",
        "action": action,
        "params": {
            "mode": 3,  # SnapQuote: LTP + real day volume + best-5 depth
            "tokenList": [{"exchangeType": 1, "tokens": tokens}],
        },
    })


# ── Main WS loop ─────────────────────────────────────────────────────────────
_ws_task: Optional[asyncio.Task] = None
_running = False
_subscribed_tokens: list[str] = []

# BUG FIX (session13 audit): ws_status() previously only reported
# running/subscribed_symbols/task_done — none of which distinguish "the
# background task is alive" from "we actually have a live WS connection
# receiving ticks", which is what the frontend (PositionStocksTab.tsx /
# positionStocksApi.ts's WSStatus type) has always expected: connected,
# last_tick_at, reconnect_attempts. _running just means start() was called
# once; it stays True through every disconnect/backoff/reconnect cycle, so
# the dashboard's "WS Feed: LIVE/DOWN" badge and reconnect-attempt counter
# could never have reflected reality. Tracking these three explicitly.
_connected = False
_reconnect_attempts = 0
_last_tick_at: Optional[float] = None  # unix seconds of the most recent parsed tick, any symbol


async def _ws_loop() -> None:
    """Persistent WS loop with exponential-backoff reconnect."""
    global _subscribed_tokens, _connected, _reconnect_attempts, _last_tick_at
    session = get_session()
    backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_S
    max_backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_MAX_S
    heartbeat_interval = config.ANGELONE_WS_HEARTBEAT_INTERVAL_S

    while _running:
        try:
            await session.ensure_session()
            if not session.token or not session.feed_token or not session.client_id:
                logger.error("position-stocks WS: AngelOne session not ready — retrying in %ss", backoff)
                _connected = False
                _reconnect_attempts += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            # Build the subscription list
            # AUDIT FIX (continued session): get_all_nse_eq() can trigger
            # scrip_master._load_sync()'s blocking httpx.get() (on first
            # call, or once every REFRESH_INTERVAL_S/24h thereafter) — a
            # synchronous network call made directly inside this async
            # loop would stall the ENTIRE event loop (including every
            # FastAPI request this service is handling — health checks,
            # /status, arm/disarm) for however long that HTTP call takes.
            # Same failure mode real-trade-service's auto_pilot.py already
            # documents fixing for its own background loop ("EVENT-LOOP
            # ISOLATION"). Offloading to a worker thread instead.
            symbol_token_map = await asyncio.to_thread(get_all_nse_eq)
            if not symbol_token_map:
                # 2026-09-21: a failed scrip-master fetch used to fall straight
                # through here, connect the WS and "subscribe" to ZERO tokens —
                # connected: true, subscribed_symbols: 0, no ticks, and nothing
                # retried until AngelOne's own idle-timeout dropped the socket.
                # Treat it like any other not-ready precondition: back off and
                # retry (scrip_master applies its own failure backoff, so this
                # doesn't hammer the download).
                logger.error(
                    "position-stocks WS: scrip master map is empty (download failed?) — retrying in %ss",
                    backoff,
                )
                _connected = False
                _reconnect_attempts += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
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
                _connected = True
                _reconnect_attempts = 0

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

                    # BUG FIX (session13, live-tested): this used to be
                    # `await ws.ping()` — a raw WebSocket protocol control
                    # frame. AngelOne's feed gateway (like their own
                    # reference smartapi-python client) listens for an
                    # APPLICATION-level text "ping" message on the data
                    # channel, not a protocol-layer ping — a protocol ping
                    # never reaches whatever idle-connection logic runs on
                    # their end. Symptom confirmed live: the connection was
                    # silently dropped and cleanly reconnected roughly
                    # every ~123s (no error/warning logged, because the
                    # server closes gracefully rather than erroring —
                    # see the "else" branch added below for why that used
                    # to be invisible in the logs too).
                    now = time.time()
                    if now - last_heartbeat >= heartbeat_interval:
                        try:
                            await ws.send("ping")
                            last_heartbeat = now
                        except Exception:
                            pass

                    # Binary tick frame
                    if isinstance(message, bytes):
                        parsed = _parse_frame(message)
                        if parsed:
                            token_str, ltp, ts, volume, best_bid, best_ask = parsed
                            _last_tick_at = ts
                            symbol = _token_to_symbol.get(token_str)
                            if symbol:
                                # BUG FIX (2026-09-17): hold the same lock
                                # get_tick_buffer() reads under (see that
                                # function's docstring) — append+prune here
                                # must not overlap with a reader's
                                # list(deque) snapshot on another thread.
                                # Held only across these three lines, no
                                # `await` inside, so this can't stall the
                                # WS message loop or deadlock.
                                with _buffer_locks[symbol]:
                                    buf = _tick_buffers[symbol]
                                    buf.append((ts, ltp))
                                    # AUDIT FIX (prior session): time-bounded
                                    # prune — see the module-level comment by
                                    # _MAX_BUFFER_AGE_S for the full reasoning.
                                    # O(k) where k is the number of stale
                                    # entries evicted this call, not the whole
                                    # buffer, since popleft() only removes from
                                    # the front and every prior append already
                                    # enforced this same cutoff.
                                    cutoff = ts - _MAX_BUFFER_AGE_S
                                    while buf and buf[0][0] < cutoff:
                                        buf.popleft()
                                # 2026-09-18: real cumulative day volume, not
                                # the always-0 placeholder this used to be —
                                # see the mode-upgrade docstring note.
                                if volume:
                                    _last_volume[symbol] = volume
                                if best_bid is not None and best_ask is not None:
                                    _last_quote[symbol] = (best_bid, best_ask, ts)
                                for cb in _on_tick_callbacks:
                                    try:
                                        cb(symbol, ltp, _last_volume.get(symbol, 0), ts)
                                    except Exception as e:
                                        logger.debug("on_tick callback error: %s", e)
                    # Text frames (status/error messages from server, incl. "pong")
                    elif isinstance(message, str):
                        if message != "pong":
                            logger.debug("position-stocks WS text frame: %s", message[:200])
                else:
                    # BUG FIX (session13, live-tested): `async for` over a
                    # websockets connection exits this loop WITHOUT raising
                    # when the server closes cleanly — so the `except
                    # ConnectionClosed` below never fired for that case,
                    # and the reconnect happened completely silently. This
                    # `else` (executes whenever the for-loop completes
                    # without `break`/exception) makes that visible.
                    #
                    # session14 (RESOLVED, live-confirmed): close_code=1001
                    # / close_reason='Connection Idle Timeout' — AngelOne's
                    # server deliberately closes feed connections carrying
                    # no live tick data (i.e. market closed) roughly every
                    # 2 minutes. This is documented, server-initiated
                    # behavior, not a client bug — no heartbeat mechanism
                    # prevents it, since it's not a heartbeat check on
                    # their end. Reconnect handling here is already
                    # correct (clean detection, fast backoff, full
                    # resubscription every time). Logged at INFO rather
                    # than WARNING specifically for this known, expected
                    # reason so it doesn't look like a recurring alarm
                    # during normal off-hours operation; anything else
                    # (a different close_code/reason, or this recurring
                    # during live market hours with ticks flowing) would
                    # still be worth investigating and should log loud.
                    if ws.close_code == 1001 and (ws.close_reason or "").strip().lower() == "connection idle timeout":
                        logger.info(
                            "position-stocks WS: AngelOne idle-timeout close (expected off-hours "
                            "behavior, close_code=1001) — reconnecting in %ss",
                            backoff,
                        )
                    else:
                        logger.warning(
                            "position-stocks WS: server closed the connection cleanly "
                            "(close_code=%s, close_reason=%r) — reconnecting in %ss",
                            ws.close_code, ws.close_reason, backoff,
                        )

        except ConnectionClosed as e:
            logger.warning(
                "position-stocks WS: connection closed (%s, code=%s, reason=%r) — reconnecting in %ss",
                e, getattr(e, "code", None), getattr(e, "reason", None), backoff,
            )
        except Exception as e:
            logger.error("position-stocks WS: error (%s) — reconnecting in %ss", e, backoff)

        _connected = False
        if _running:
            _reconnect_attempts += 1
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
    global _running, _connected
    _running = False
    _connected = False
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    logger.info("position-stocks WS: stopped")


def ws_status() -> dict:
    # BUG FIX (session13 audit): the frontend (positionStocksApi.ts's
    # WSStatus type / PositionStocksTab.tsx's "WS Feed" card) has always
    # read `connected`, `last_tick_at`, and `reconnect_attempts` — none of
    # which this function returned. `running` only reflects that start()
    # was called once (it stays True through every disconnect/backoff
    # cycle), so the dashboard's LIVE/DOWN badge was actually reading
    # `status?.ws?.connected` as always-undefined -> always "DOWN", and the
    # reconnect counter and last-tick timestamp never appeared at all.
    return {
        "running": _running,
        "connected": _connected,
        "subscribed_symbols": len(_token_to_symbol),
        "task_done": _ws_task.done() if _ws_task else True,
        "reconnect_attempts": _reconnect_attempts,
        "last_tick_at": (
            datetime.fromtimestamp(_last_tick_at, tz=timezone.utc).isoformat()
            if _last_tick_at else None
        ),
    }
