"""dhan_data/ws_feed.py - Dhan live market-feed WebSocket (group 270, phase 4). OPT-IN: DHAN_WS_ENABLED=1.

REST polling (one call per second, up to 1000 symbols) already refreshes the whole universe every second, so this
is only worth turning on for sub-second ticks or more than ~1000 live symbols. It is OFF by default.

IMPORTANT - written from Dhan's v2 market-feed documentation without a live capture. The binary layout below
(little-endian; 8-byte header: code u8, length u16, segment u8, security id u32; ticker 16 bytes, quote 50 bytes,
prev-close 16 bytes) must be checked against a real session before you rely on it. Unknown packet codes are
counted, never guessed at, and /internal/dhan-status shows the counters. The token travels in the connection URL
(Dhan's design), so this module never logs the URL.

Ticks are merged into rows, handed to the quote batcher cache (quotes.inject_rows) so REST callers see them with no
request, and written to live_quotes through live_store (subject to its write-mode rule).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import client, config, creds, live_store, quotes, scrip_master
from .errors import DhanAuthError, DhanNotConfigured

logger = logging.getLogger("dhan-data.ws")

SEG_CODES = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
             7: "BSE_CURRENCY", 8: "BSE_FNO"}
CODE_TICKER, CODE_QUOTE, CODE_PREV_CLOSE, CODE_DISCONNECT = 2, 4, 6, 50
REQ_SUBSCRIBE_TICKER, REQ_SUBSCRIBE_QUOTE = 15, 17
_HEADER = struct.Struct("<BHBI")
_TICKER = struct.Struct("<fi")
_QUOTE = struct.Struct("<fhifiiiffff")
_PREV = struct.Struct("<fi")
_SUB_CHUNK = 100                  # Dhan accepts at most 100 instruments per subscribe message

_state_lock = threading.Lock()
_ticks: Dict[str, dict] = {}      # symbol key -> merged tick state
_dirty: set = set()
_stats = {"connected": False, "connects": 0, "packets": 0, "ticker": 0, "quote": 0, "prev_close": 0,
          "unknown_codes": {}, "bad_packets": 0, "subscribed": 0, "last_packet_at": None, "last_error": None}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ── packet parsing (pure, tested with struct.pack) ──────────────────────────────────────────────────────────────
def parse_packet(data: bytes) -> Optional[dict]:
    """One feed message -> {"kind","segment","security_id",...fields} or None for anything unknown/short."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < _HEADER.size:
        return None
    code, _length, seg_code, sid = _HEADER.unpack_from(data, 0)
    seg = SEG_CODES.get(seg_code)
    body = memoryview(data)[_HEADER.size:]
    if code == CODE_TICKER and len(body) >= _TICKER.size:
        ltp, ltt = _TICKER.unpack_from(body)
        return {"kind": "ticker", "segment": seg, "security_id": sid, "price": ltp, "ltt": ltt}
    if code == CODE_QUOTE and len(body) >= _QUOTE.size:
        ltp, _lq, ltt, atp, vol, _sell, _buy, o, c, h, l = _QUOTE.unpack_from(body)
        return {"kind": "quote", "segment": seg, "security_id": sid, "price": ltp, "ltt": ltt, "avg_price": atp,
                "volume": vol, "open": o, "day_close": c, "high": h, "low": l}
    if code == CODE_PREV_CLOSE and len(body) >= _PREV.size:
        prev, _oi = _PREV.unpack_from(body)
        return {"kind": "prev_close", "segment": seg, "security_id": sid, "prev_close": prev}
    if code == CODE_DISCONNECT:
        return {"kind": "disconnect", "segment": seg, "security_id": sid,
                "reason": struct.unpack_from("<h", body)[0] if len(body) >= 2 else None}
    return {"kind": "unknown", "code": code, "segment": seg, "security_id": sid}


def build_subscribe_messages(instruments: List[Tuple[str, int]], request_code: int = REQ_SUBSCRIBE_QUOTE) -> List[str]:
    """JSON subscribe messages, at most 100 instruments each."""
    msgs = []
    for i in range(0, len(instruments), _SUB_CHUNK):
        part = instruments[i:i + _SUB_CHUNK]
        msgs.append(json.dumps({
            "RequestCode": request_code, "InstrumentCount": len(part),
            "InstrumentList": [{"ExchangeSegment": seg, "SecurityId": str(sid)} for seg, sid in part]}))
    return msgs


def _finite_pos(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and 0 < f < 1e9 else None


def apply_packet(pkt: dict, rev: Dict[Tuple[str, int], str]) -> Optional[str]:
    """Merge a parsed packet into the tick state. Returns the symbol key it updated, or None."""
    key = rev.get((pkt.get("segment"), pkt.get("security_id")))
    if not key:
        return None
    kind = pkt.get("kind")
    with _state_lock:
        t = _ticks.setdefault(key, {})
        if kind in ("ticker", "quote"):
            px = _finite_pos(pkt.get("price"))
            if px is None:
                return None
            t["price"] = px
            if kind == "quote":
                t["volume"] = int(pkt.get("volume") or 0)
                t["open"] = _finite_pos(pkt.get("open"))
                t["day_high"] = _finite_pos(pkt.get("high"))
                t["day_low"] = _finite_pos(pkt.get("low"))
                t["avg_price"] = _finite_pos(pkt.get("avg_price"))
        elif kind == "prev_close":
            pc = _finite_pos(pkt.get("prev_close"))
            if pc is None:
                return None
            t["previous_close"] = pc
        else:
            return None
        _dirty.add(key)
    return key


def snapshot_rows(keys: set) -> List[dict]:
    """Rows (batcher-cache shape) for the given dirty keys that have a price."""
    from datetime import datetime, timezone
    rows = []
    now_m = time.monotonic()
    with _state_lock:
        for k in keys:
            t = _ticks.get(k) or {}
            if not t.get("price"):
                continue
            pc = t.get("previous_close")
            rows.append({
                "symbol": k, "price": t["price"], "previous_close": pc,
                "day_change_pct": round((t["price"] - pc) / pc * 100.0, 2) if pc else None,
                "open": t.get("open"), "day_high": t.get("day_high"), "day_low": t.get("day_low"),
                "volume": t.get("volume"), "avg_price": t.get("avg_price"),
                "upper_circuit": None, "lower_circuit": None, "last_trade_at": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(), "source": "dhan_ws", "_mono": now_m})
    return rows


# ── connection loop ─────────────────────────────────────────────────────────────────────────────────────────────
def _universe_instruments(get_universe: Callable[[], List[str]]) -> Tuple[List[Tuple[str, int]], Dict[Tuple[str, int], str]]:
    inst: List[Tuple[str, int]] = []
    rev: Dict[Tuple[str, int], str] = {}
    for s in (get_universe() or [])[: config.ws_max_instruments()]:
        ident = scrip_master.security_id(s)
        if ident and ident not in rev:
            inst.append(ident)
            rev[ident] = quotes.key_for(s)
    return inst, rev


async def _session(get_universe: Callable[[], List[str]]) -> None:
    import websockets
    client_id, token = creds.get_credentials()
    url = f"{config.ws_url()}?version=2&token={token}&clientId={client_id}&authType=2"   # never logged
    inst, rev = _universe_instruments(get_universe)
    if not inst:
        raise RuntimeError("no instruments to subscribe (scrip master not loaded or empty universe)")
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 20) as ws:
        with _state_lock:
            _stats.update(connected=True, subscribed=len(inst))
            _stats["connects"] += 1
        for m in build_subscribe_messages(inst):
            await ws.send(m)
        logger.info("dhan websocket connected, subscribed to %d instruments", len(inst))
        last_flush = time.monotonic()
        last_universe_check = time.monotonic()
        subscribed = set(inst)
        while not _stop.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                msg = None
            if isinstance(msg, (bytes, bytearray)):
                pkt = parse_packet(msg)
                with _state_lock:
                    _stats["packets"] += 1
                    _stats["last_packet_at"] = time.time()
                if pkt is None:
                    with _state_lock:
                        _stats["bad_packets"] += 1
                elif pkt["kind"] == "disconnect":
                    raise DhanAuthError(f"feed sent disconnect code {pkt.get('reason')}")
                elif pkt["kind"] == "unknown":
                    with _state_lock:
                        uc = _stats["unknown_codes"]
                        uc[str(pkt.get("code"))] = uc.get(str(pkt.get("code")), 0) + 1
                else:
                    with _state_lock:
                        _stats[pkt["kind"]] = _stats.get(pkt["kind"], 0) + 1
                    apply_packet(pkt, rev)
            now = time.monotonic()
            if now - last_flush >= 1.0:
                last_flush = now
                with _state_lock:
                    keys = set(_dirty)
                    _dirty.clear()
                if keys:
                    rows = snapshot_rows(keys)
                    quotes.inject_rows(rows)
                    live_store.submit([{**r, "source": "dhan"} for r in rows])
            if now - last_universe_check >= 300.0:
                last_universe_check = now
                new_inst, new_rev = _universe_instruments(get_universe)
                added = [i for i in new_inst if i not in subscribed]
                if added:
                    rev.update(new_rev)
                    for m in build_subscribe_messages(added):
                        await ws.send(m)
                    subscribed.update(added)
                    with _state_lock:
                        _stats["subscribed"] = len(subscribed)
    with _state_lock:
        _stats["connected"] = False


def _run(get_universe: Callable[[], List[str]]) -> None:
    backoff = 2.0
    while not _stop.is_set():
        try:
            from market_hours import is_feed_window_ist
            if not is_feed_window_ist():
                with _state_lock:
                    _stats["connected"] = False
                _stop.wait(30.0)
                continue
        except Exception:  # noqa: BLE001
            pass
        if not (config.enabled() and config.ws_enabled() and client.available()):
            _stop.wait(10.0)
            continue
        try:
            asyncio.run(_session(get_universe))
            backoff = 2.0
        except (DhanAuthError, DhanNotConfigured) as e:
            client.note_failure(e)
            with _state_lock:
                _stats.update(connected=False, last_error=f"{type(e).__name__}")
            _stop.wait(60.0)
        except Exception as e:  # noqa: BLE001 - never include the URL (it holds the token)
            with _state_lock:
                _stats.update(connected=False, last_error=f"{type(e).__name__}: {str(e)[:80]}")
            logger.warning("dhan websocket dropped (%s), reconnecting in %.0fs", type(e).__name__, backoff)
            _stop.wait(backoff)
            backoff = min(60.0, backoff * 2)


def start(get_universe: Callable[[], List[str]]) -> None:
    global _thread
    if not (config.enabled() and config.ws_enabled()):
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(get_universe,), name="dhan-ws", daemon=True)
    _thread.start()
    logger.info("dhan websocket feed started (opt-in, DHAN_WS_ENABLED=1)")


def stop() -> None:
    _stop.set()


def status() -> dict:
    with _state_lock:
        out = dict(_stats)
        out["unknown_codes"] = dict(_stats["unknown_codes"])
    out["enabled"] = config.ws_enabled()
    return out


def _reset_for_tests() -> None:
    with _state_lock:
        _ticks.clear(); _dirty.clear()
        for k, v in list(_stats.items()):
            if isinstance(v, dict):
                _stats[k] = {}
            elif isinstance(v, bool):
                _stats[k] = False
            elif v is None or isinstance(v, str) or k == "last_packet_at":
                _stats[k] = None
            else:
                _stats[k] = 0
